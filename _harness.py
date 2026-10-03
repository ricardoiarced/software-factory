"""Shared Harness selection, the Runner interface, and the Run lifecycle."""

import errno
import json
import os
import platform
import re
import signal
import stat
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from typing import Any, BinaryIO, Protocol

from _project import Project
from _runrecord import commit_pending_launch, record_run_outcome


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
    """A frozen launch plan established by completed Run Admission.

    ``harness`` names the selected Harness for Factory-owned diagnostics,
    ``executable`` is the admitted artifact, ``invocation`` is its exact
    launch command; ``environment`` pins certified variable values and
    ``unset_environment`` removes named variables from the inherited host
    environment (both empty for a pure pass-through).
    """

    harness: str
    executable: Path
    invocation: tuple[str, ...]
    environment: tuple[tuple[str, str], ...] = ()
    unset_environment: tuple[str, ...] = ()


class Runner(Protocol):
    """The only Run boundary the Factory command consumes."""

    def admit(self, project: Project) -> PreparedRun: ...

    def run(self, project: Project, run_request_spool: BinaryIO) -> int: ...


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


def launch(
    project: Project,
    prepared: PreparedRun,
    run_request_spool: BinaryIO,
) -> int:
    """Run the admitted launch plan under the shared lifecycle contract.

    Factory invokes the admitted executable directly without a shell, with
    the Project Boundary as the child working directory and the composed Run
    Request spool as its standard input. Standard output, standard error, and
    the host environment pass through unchanged except for the certified
    environment edits of the admitted launch plan; Factory shares the child's
    foreground process group, returns its ordinary exit status, and
    re-exposes signal termination as the same signal. Process creation is
    never attempted before the active Run Record holds a crash-durable
    Launch Commit of the admitted launch plan.
    """

    commit_pending_launch(prepared)
    environment = None
    if prepared.environment or prepared.unset_environment:
        environment = {**os.environ, **dict(prepared.environment)}
        for name in prepared.unset_environment:
            environment.pop(name, None)
    previous_handlers: dict[
        signal.Signals,
        Callable[[int, FrameType | None], Any] | int | None,
    ] = {}
    try:
        process = subprocess.Popen(
            prepared.invocation,
            cwd=project.boundary,
            stdin=run_request_spool,
            stdout=None,
            stderr=None,
            env=environment,
            shell=False,
            start_new_session=False,
        )
    except OSError as error:
        record_run_outcome(
            {"kind": "spawn-failure", "detail": error.strerror or str(error)}
        )
        raise RunAdmissionError(
            f"{prepared.harness} executable "
            f"{_quoted_absolute_path(prepared.executable)} could not start: "
            f"{error.strerror or error}",
            executable=prepared.executable,
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
