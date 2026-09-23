"""Claude Code Run Admission and lifecycle boundary."""

from pathlib import Path
import re
from typing import BinaryIO, NoReturn

from _harness import (
    PreparedRun,
    RunAdmissionError,
    _quoted_absolute_path,
    launch,
    reported_version,
    resolve_executable,
)
from _project import Project
from _publisher import verify_claude_code_newest_stable


_VERSION_OUTPUT = re.compile(
    rb"([0-9]+(?:\.[0-9]+)*(?:[-+][0-9A-Za-z.-]+)?) \(Claude Code\)\n\Z",
)

# No Claude Code release has completed real-host certification; the
# certification suite and reviewed candidate manifests extend this set.
_CLAUDE_COMPATIBILITY_SET: tuple[()] = ()


def _resolve_executable() -> Path:
    return resolve_executable("claude", "Claude Code")


def _reported_version(executable: Path) -> str:
    return reported_version(
        executable,
        pattern=_VERSION_OUTPUT,
        label="Claude Code",
        remedy="install a stable Claude Code release",
    )


def _require_certified_tuple(executable: Path, version: str) -> NoReturn:
    """Match the exact Claude Compatibility Set tuple or refuse.

    Every Claude Code release fails closed today: the Compatibility Set is
    empty until the certification suite and a reviewed candidate manifest
    land, so no Run Request is ever submitted.
    """

    if not _CLAUDE_COMPATIBILITY_SET:
        raise RunAdmissionError(
            "Claude Compatibility Set is empty; update Factory to a release "
            "that certifies Claude Code",
        )
    # A non-empty set requires the certified launch pipeline before any Run;
    # refuse rather than launch an unprobed configuration.
    raise RunAdmissionError(
        "Claude Code executable "
        f"{_quoted_absolute_path(executable)} reports {version}; no certified "
        "launch pipeline is implemented; update Factory to a release that "
        "certifies Claude Code",
    )


class ClaudeCodeRunner:
    """The Claude Code Runner: live newest-stable admission, fail-closed launch."""

    def admit(self, project: Project) -> PreparedRun:
        """Run Admission, refusing while no Claude Code tuple is certified."""

        executable = _resolve_executable()
        version = _reported_version(executable)
        verify_claude_code_newest_stable(executable, version)
        _require_certified_tuple(executable, version)

    def run(self, project: Project, run_request_spool: BinaryIO) -> int:
        """Admit, then launch; admission refuses every Run today."""

        return launch(project, self.admit(project), run_request_spool)
