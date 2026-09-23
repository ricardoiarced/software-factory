"""Claude Code Run Admission and lifecycle boundary."""

from typing import BinaryIO

from _harness import PreparedRun, RunAdmissionError, launch
from _project import Project


class ClaudeCodeRunner:
    """Refuse every Run until a Factory release certifies Claude Code.

    The Claude Compatibility Set is empty: no Claude Code release has
    completed real-host certification, so every selected Run fails closed
    before the Run Request is submitted and Factory never falls back to
    another Harness.
    """

    def admit(self, project: Project) -> PreparedRun:
        """Refuse Run Admission while no Claude Code tuple is certified."""

        raise RunAdmissionError(
            "Claude Compatibility Set is empty; update Factory to a release "
            "that certifies Claude Code",
        )

    def run(self, project: Project, run_request_spool: BinaryIO) -> int:
        """Admit, then launch; admission refuses every Run today."""

        return launch(project, self.admit(project), run_request_spool)
