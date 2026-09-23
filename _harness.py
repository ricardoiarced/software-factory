"""Shared Harness selection, the Runner interface, and the Run lifecycle."""

from collections.abc import Callable
from dataclasses import dataclass
import json
import os
from pathlib import Path
import signal
import subprocess
from types import FrameType
from typing import Any, BinaryIO, Protocol

from _project import Project


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
    ``executable`` is the admitted artifact, and ``invocation`` is its exact
    launch command.
    """

    harness: str
    executable: Path
    invocation: tuple[str, ...]


class Runner(Protocol):
    """The only Run boundary the Factory command consumes."""

    def admit(self, project: Project) -> PreparedRun:
        """Complete this Harness's Run Admission and freeze the launch plan."""

    def run(self, project: Project, run_request_spool: BinaryIO) -> int:
        """Admit, then launch one Run and return the Harness's status."""


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


# Keep this to the foreground-group contract. Synchronous faults and
# process-local timer/resource signals retain Factory's native dispositions.
_CHILD_RUNTIME_SIGNALS = (
    signal.SIGHUP,
    signal.SIGINT,
    signal.SIGQUIT,
    signal.SIGTERM,
    signal.SIGUSR1,
)


def _defer_runtime_signal_to_child(
    _signal_number: int, _frame: FrameType | None,
) -> None:
    """Let the foreground child status decide the Run's signal outcome."""


def launch(
    project: Project, prepared: PreparedRun, run_request_spool: BinaryIO,
) -> int:
    """Run the admitted launch plan under the shared lifecycle contract.

    Factory invokes the admitted executable directly without a shell, with
    the Project Boundary as the child working directory and the composed Run
    Request spool as its standard input. Standard output, standard error, and
    the host environment pass through unchanged; Factory shares the child's
    foreground process group, returns its ordinary exit status, and
    re-exposes signal termination as the same signal.
    """

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
            env=None,
            shell=False,
            start_new_session=False,
        )
    except OSError as error:
        raise RunAdmissionError(
            f"{prepared.harness} executable "
            f"{_quoted_absolute_path(prepared.executable)} could not start: "
            f"{error.strerror or error}",
            executable=prepared.executable,
        ) from error
    try:
        for runtime_signal in _CHILD_RUNTIME_SIGNALS:
            previous_handler = signal.getsignal(runtime_signal)
            previous_handlers[runtime_signal] = previous_handler
            if previous_handler != signal.SIG_IGN:
                signal.signal(runtime_signal, _defer_runtime_signal_to_child)
        status = process.wait()
    finally:
        for handled_signal, handler in previous_handlers.items():
            signal.signal(handled_signal, handler)
    if status < 0:
        termination_signal = -status
        if termination_signal != signal.SIGKILL:
            signal.signal(termination_signal, signal.SIG_DFL)
            signal.pthread_sigmask(signal.SIG_UNBLOCK, {termination_signal})
        os.kill(os.getpid(), termination_signal)
    return status
