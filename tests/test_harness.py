"""Harness selection and the fail-closed Runner interface."""

import io
from pathlib import Path
from types import SimpleNamespace

import pytest

from _claude import ClaudeCodeRunner
from _codex import CodexRunner
from _harness import (
    DEFAULT_HARNESS,
    HARNESS_NAMES,
    PreparedRun,
    RunAdmissionError,
    UnknownHarnessError,
    select_harness,
)
from _project import Project


def test_harness_registry_selects_each_registered_runner() -> None:
    assert DEFAULT_HARNESS == "codex"
    assert HARNESS_NAMES == ("codex", "claude")
    assert isinstance(select_harness("codex"), CodexRunner)
    assert isinstance(select_harness("claude"), ClaudeCodeRunner)


def test_select_harness_refuses_an_unregistered_name() -> None:
    with pytest.raises(UnknownHarnessError, match='unknown harness "vim"'):
        select_harness("vim")


def test_codex_runner_admit_freezes_the_matched_launch_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "codex-artifact"
    invocation = [str(executable), "exec", "-"]
    policy = SimpleNamespace(canonical_path=executable)
    monkeypatch.setattr("_codex._admit_run", lambda project: (policy, invocation))

    prepared = CodexRunner().admit(Project(tmp_path, tmp_path))

    assert prepared == PreparedRun(
        harness="Codex", executable=executable, invocation=tuple(invocation),
    )


def test_claude_runner_refuses_admission_and_never_launches(tmp_path: Path) -> None:
    runner = ClaudeCodeRunner()
    project = Project(tmp_path, tmp_path)
    spool = io.BytesIO(b"# Run Request\n\n## Instruction\n\nFix")

    with pytest.raises(
        RunAdmissionError,
        match=(
            "Claude Compatibility Set is empty; "
            "update Factory to a release that certifies Claude Code"
        ),
    ):
        runner.admit(project)
    with pytest.raises(
        RunAdmissionError, match="Claude Compatibility Set is empty",
    ):
        runner.run(project, spool)

    assert spool.tell() == 0
