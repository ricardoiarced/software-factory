"""Shared fixtures for deterministic Run Admission tests."""

import os
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def codex_executable_on_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Make tests that reach later Run Admission stages independent of the host."""
    executable_directory = tmp_path / "codex-bin"
    executable_directory.mkdir()
    executable = executable_directory / "codex"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    monkeypatch.setenv(
        "PATH", os.pathsep.join((str(executable_directory), os.environ["PATH"])),
    )
