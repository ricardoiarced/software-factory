"""Codex Run Admission and lifecycle boundary."""

from pathlib import Path
from typing import BinaryIO


class RunAdmissionError(Exception):
    """Codex cannot be launched under the certified compatibility contract."""


def run(project_boundary: Path, prompt_spool: BinaryIO) -> int:
    """Refuse every Run until a Codex artifact and host tuple is certified."""
    raise RunAdmissionError("Codex Compatibility Set is empty; no Run is supported")
