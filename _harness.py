"""Shared Harness selection, the Runner interface, and the Run lifecycle."""

import errno
import hashlib
import json
import os
import platform
import re
import signal
import stat
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from typing import TYPE_CHECKING, Any, BinaryIO, Protocol

from _project import Project
from _runrecord import commit_pending_launch, record_run_outcome

if TYPE_CHECKING:
    from _rungate import ValidatedGate


def factory_version() -> str:
    """Return the installed Factory distribution version."""

    from importlib.metadata import version

    return version("software-factory")


def host_identity() -> dict[str, str]:
    """Return the OS/kernel facts whose change invalidates probe evidence."""

    return {
        "release": platform.release(),
        "version": platform.version(),
    }


class RunAdmissionError(Exception):
    """The selected Harness cannot be launched under its certified contract."""

    def __init__(self, message: str, *, executable: Path | None = None) -> None:
        super().__init__(message)
        self.executable = executable


class UnknownHarnessError(Exception):
    """A --harness value selects no registered Runner."""


@dataclass(frozen=True)
class PreparedRun:
    """The admitted inputs established by completed Run Admission.

    ``harness`` names the selected Harness for Factory-owned diagnostics,
    ``executable`` is the admitted artifact with the ``executable_identity``
    and ``executable_sha256`` admission bound from it, ``invocation`` is its
    exact launch command; ``environment`` pins certified variable values and
    ``unset_environment`` removes named variables from the inherited host
    environment (both empty for a pure pass-through); ``probe`` carries the
    Host Admission Probe verdict with its provenance when one applied.
    """

    harness: str
    executable: Path
    invocation: tuple[str, ...]
    executable_identity: tuple[int, int]
    executable_sha256: str
    environment: tuple[tuple[str, str], ...] = ()
    unset_environment: tuple[str, ...] = ()
    probe: Mapping[str, object] | None = None


class LaunchPlan(Protocol):
    """The sealed Validated Run Plan the launcher consumes by value.

    The launcher's sole gated-input source: every property is a value the
    gate or admission bound, never an invitation to rediscover state.
    """

    @property
    def harness(self) -> str: ...

    @property
    def executable(self) -> Path: ...

    @property
    def executable_identity(self) -> tuple[int, int]: ...

    @property
    def executable_sha256(self) -> str: ...

    @property
    def invocation(self) -> tuple[str, ...]: ...

    @property
    def environment(self) -> tuple[tuple[str, str], ...]: ...

    @property
    def pins(self) -> tuple[tuple[str, str], ...]: ...

    @property
    def removals(self) -> tuple[str, ...]: ...

    @property
    def boundary(self) -> Path: ...

    @property
    def boundary_canonical(self) -> str: ...

    @property
    def boundary_identity(self) -> tuple[int, int]: ...

    @property
    def fingerprint(self) -> str: ...


class Runner(Protocol):
    """The only Run boundary the Factory command consumes.

    ``run`` receives the passed gate's evidence and seals it — with the
    admitted inputs and the exact Run Request bytes — into the Validated
    Run Plan the launcher consumes by value.
    """

    def admit(self, project: Project, *, re_probe: bool = False) -> PreparedRun: ...

    def run(
        self,
        project: Project,
        run_request_spool: BinaryIO,
        gate: "ValidatedGate",
        *,
        re_probe: bool = False,
    ) -> int: ...


DEFAULT_HARNESS = "codex"


def _codex_runner() -> Runner:
    from _codex import CodexRunner

    return CodexRunner()


def _claude_runner() -> Runner:
    from _claude import ClaudeCodeRunner

    return ClaudeCodeRunner()


_RUNNERS: dict[str, Callable[[], Runner]] = {
    "codex": _codex_runner,
    "claude": _claude_runner,
}
HARNESS_NAMES = tuple(_RUNNERS)


def select_harness(name: str) -> Runner:
    """Build the Runner for one parsed --harness value."""

    build = _RUNNERS.get(name)
    if build is None:
        raise UnknownHarnessError(f"unknown harness {json.dumps(name)}")
    return build()


def _quoted_absolute_path(path: Path) -> str:
    return json.dumps(str(path.absolute()), ensure_ascii=False)


def inspection_failure(label: str, path: Path, error: OSError) -> RunAdmissionError:
    cause = error.strerror or str(error)
    return RunAdmissionError(
        f"{label} executable {_quoted_absolute_path(path)} could not be inspected: "
        f"{cause}; check its permissions"
    )


def _resolve_candidate(candidate: Path, label: str) -> Path:
    """Resolve one existing PATH candidate to a regular executable file."""
    try:
        executable = candidate.resolve(strict=True)
        metadata = executable.stat()
    except FileNotFoundError as error:
        raise RunAdmissionError(
            f"{label} executable {_quoted_absolute_path(candidate)} is a dangling symlink; "
            "repair or remove it from PATH"
        ) from error
    except RuntimeError as error:
        raise RunAdmissionError(
            f"{label} executable {_quoted_absolute_path(candidate)} contains a symlink loop; "
            "repair or remove it from PATH"
        ) from error
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise RunAdmissionError(
                f"{label} executable {_quoted_absolute_path(candidate)} contains a symlink loop; "
                "repair or remove it from PATH"
            ) from error
        raise inspection_failure(label, candidate, error) from error
    if not stat.S_ISREG(metadata.st_mode):
        raise RunAdmissionError(
            f"{label} executable {_quoted_absolute_path(executable)} "
            "is not a regular file; "
            f"install {label} or remove it from PATH"
        )
    if not os.access(executable, os.X_OK):
        raise RunAdmissionError(
            f"{label} executable {_quoted_absolute_path(executable)} is not executable; "
            "grant execute permission or replace it"
        )
    return executable


def resolve_executable(name: str, label: str) -> Path:
    """Resolve one Harness executable from PATH as an executable regular file."""

    label_quoted = json.dumps(name)
    first_failure: RunAdmissionError | None = None
    for directory in os.get_exec_path():
        candidate = Path(directory or os.curdir) / name
        try:
            candidate.lstat()
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError as error:
            if first_failure is None:
                first_failure = inspection_failure(label, candidate, error)
            continue
        try:
            return _resolve_candidate(candidate, label)
        except RunAdmissionError as failure:
            if first_failure is None:
                first_failure = failure

    if first_failure is not None:
        raise first_failure
    raise RunAdmissionError(
        f"{label} executable {label_quoted} was not found on PATH; "
        f"install {label} or add it to PATH"
    )


def reported_version(
    executable: Path,
    *,
    pattern: re.Pattern[bytes],
    label: str,
    remedy: str,
) -> str:
    """Read the installed release's self-reported version string."""

    try:
        result = subprocess.run(
            [os.fspath(executable), "--version"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
        )
    except OSError as error:
        raise RunAdmissionError(
            f"{label} executable {_quoted_absolute_path(executable)} could not report its version: "
            f"{error.strerror or error}; check its permissions",
            executable=executable,
        ) from error
    if result.returncode:
        raise RunAdmissionError(
            f"{label} executable {_quoted_absolute_path(executable)} could not report its version: "
            f"exit status {result.returncode}",
            executable=executable,
        )
    match = pattern.fullmatch(result.stdout)
    if match is None:
        raise RunAdmissionError(
            f"{label} executable {_quoted_absolute_path(executable)} reported an unrecognized version; "
            f"{remedy}",
            executable=executable,
        )
    return match.group(1).decode("ascii")


_CHILD_FOREGROUND_SIGNALS = (
    signal.SIGHUP,
    signal.SIGINT,
    signal.SIGQUIT,
    signal.SIGTERM,
    signal.SIGUSR1,
)


def _defer_runtime_signal_to_child(
    _signal_number: int,
    _frame: FrameType | None,
) -> None:
    """Let the foreground child status decide the Run's signal outcome."""


def _changed_executable_failure(plan: LaunchPlan) -> RunAdmissionError:
    return RunAdmissionError(
        f"{plan.harness} executable {_quoted_absolute_path(plan.executable)} changed "
        "during Run Admission; refusing to evaluate its replacement",
        executable=plan.executable,
    )


def _recheck_executable(plan: LaunchPlan) -> None:
    """Re-observe the admitted executable against the plan's bound identity."""

    try:
        with plan.executable.open("rb") as artifact:
            metadata = os.fstat(artifact.fileno())
            identity = (metadata.st_dev, metadata.st_ino)
            digest = hashlib.file_digest(artifact, "sha256").hexdigest()
        metadata_after_hash = plan.executable.stat()
    except OSError as error:
        raise inspection_failure(plan.harness, plan.executable, error) from error
    if (
        identity != plan.executable_identity
        or digest != plan.executable_sha256
        or (metadata_after_hash.st_dev, metadata_after_hash.st_ino) != identity
    ):
        raise _changed_executable_failure(plan)


def _boundary_reobservation_failure(
    boundary: Path, error: OSError | RuntimeError
) -> str:
    cause = (
        error.strerror if isinstance(error, OSError) and error.strerror else str(error)
    )
    return (
        f"The Project Boundary {_quoted_absolute_path(boundary)} could not be "
        f"re-observed at launch ({cause}); refusing rather than trusting the "
        "admitted observation."
    )


def _recheck_boundary(plan: LaunchPlan) -> None:
    """Re-observe the admitted Project Boundary; any disagreement refuses.

    The frozen checks run in order — existence, directory, filesystem
    identity, canonical path — and an impossible re-observation refuses
    rather than trusting the admitted observation. There is no fallback,
    rediscovery, widening, or generic identity-mismatch diagnostic.
    """

    boundary = plan.boundary
    quoted = _quoted_absolute_path(boundary)
    try:
        observed = boundary.stat()
    except FileNotFoundError as error:
        raise RunAdmissionError(
            f"The Project Boundary {quoted} does not exist at launch; the Run "
            "was admitted against a directory that has since disappeared."
        ) from error
    except OSError as error:
        raise RunAdmissionError(_boundary_reobservation_failure(boundary, error)) from (
            error
        )
    if not stat.S_ISDIR(observed.st_mode):
        raise RunAdmissionError(
            f"The Project Boundary {quoted} is no longer a directory at launch."
        )
    if (observed.st_dev, observed.st_ino) != plan.boundary_identity:
        raise RunAdmissionError(
            f"The Project Boundary {quoted} changed during admission: the gate "
            f"admitted dev/ino {plan.boundary_identity[0]}:"
            f"{plan.boundary_identity[1]}, launch found {observed.st_dev}:"
            f"{observed.st_ino}; refusing to launch against a directory other "
            "than the one validated."
        )
    try:
        canonical = boundary.resolve()
    except (OSError, RuntimeError) as error:
        raise RunAdmissionError(_boundary_reobservation_failure(boundary, error)) from (
            error
        )
    if str(canonical) != plan.boundary_canonical:
        raise RunAdmissionError(
            f"The Project Boundary {quoted} resolved to "
            f"{_quoted_absolute_path(canonical)} at launch, not the admitted "
            "canonical path "
            f"{_quoted_absolute_path(Path(plan.boundary_canonical))}; refusing."
        )


def _recheck_launch_boundary(plan: LaunchPlan) -> None:
    """Re-observe both Two-Observation Facts; process creation comes next.

    The final admission invariant inside the launcher, after all
    Harness-specific preparation and immediately before the Launch Commit:
    no launch-time Git re-proof, identity-store read, effective-configuration
    read, Git Assessment, or newest-stable lookup happens here.
    """

    _recheck_executable(plan)
    _recheck_boundary(plan)


def launch(plan: LaunchPlan, run_request_spool: BinaryIO) -> int:
    """Launch the sealed Validated Run Plan under the shared lifecycle contract.

    The plan is the launcher's sole gated-input source. Factory re-observes
    the two Two-Observation Facts at the Launch Boundary Recheck, writes the
    crash-durable Launch Commit binding the plan fingerprint, and only then
    creates the process: the planned invocation directly without a shell, in
    the planned boundary, with the planned effective environment supplied
    verbatim and the composed Run Request spool as standard input. The
    ambient process environment is never read at launch. Factory shares the
    child's foreground process group, returns its ordinary exit status, and
    re-exposes signal termination as the same signal. Process creation is
    never attempted before the active Run Record holds a crash-durable
    Launch Commit of the admitted launch plan.
    """

    _recheck_launch_boundary(plan)
    commit_pending_launch(plan)
    previous_handlers: dict[
        signal.Signals,
        Callable[[int, FrameType | None], Any] | int | None,
    ] = {}
    try:
        process = subprocess.Popen(
            plan.invocation,
            cwd=plan.boundary,
            stdin=run_request_spool,
            stdout=None,
            stderr=None,
            env=dict(plan.environment),
            shell=False,
            start_new_session=False,
        )
    except OSError as error:
        record_run_outcome(
            {"kind": "spawn-failure", "detail": error.strerror or str(error)}
        )
        raise RunAdmissionError(
            f"{plan.harness} executable "
            f"{_quoted_absolute_path(plan.executable)} could not start: "
            f"{error.strerror or error}",
            executable=plan.executable,
        ) from error
    try:
        for foreground_signal in _CHILD_FOREGROUND_SIGNALS:
            previous_handler = signal.getsignal(foreground_signal)
            previous_handlers[foreground_signal] = previous_handler
            if previous_handler != signal.SIG_IGN:
                signal.signal(foreground_signal, _defer_runtime_signal_to_child)
        status = process.wait()
    finally:
        for handled_signal, handler in previous_handlers.items():
            signal.signal(handled_signal, handler)
    if status < 0:
        termination_signal = -status
        record_run_outcome({"kind": "signal", "signal": termination_signal})
        if termination_signal != signal.SIGKILL:
            signal.signal(termination_signal, signal.SIG_DFL)
            signal.pthread_sigmask(signal.SIG_UNBLOCK, {termination_signal})
        os.kill(os.getpid(), termination_signal)
    record_run_outcome({"kind": "exit", "status": status})
    return status
