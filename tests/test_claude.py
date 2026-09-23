"""Claude Code Run Admission: the live newest-stable gate, fail-closed launch."""

import io
import os
from pathlib import Path

import pytest

import _publisher
from _claude import ClaudeCodeRunner
from _harness import RunAdmissionError
from _project import Project
from conftest import (
    CLAUDE_GITHUB_RELEASES,
    CLAUDE_LATEST_POINTER,
    CLAUDE_NPM_DIST_TAGS,
    PublisherResponses,
    github_release,
    npm_dist_tags,
)


def make_claude_executable(path: Path, version: str) -> None:
    path.write_text(f"#!/bin/sh\nprintf '{version} (Claude Code)\\n'\n")
    path.chmod(0o755)


def install_native_claude(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: str,
) -> None:
    executable = tmp_path / ".local" / "share" / "claude" / "versions" / "claude"
    executable.parent.mkdir(parents=True)
    make_claude_executable(executable, version)
    monkeypatch.setenv("PATH", os.fspath(executable.parent))


def restore_live_verification(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "_claude.verify_claude_code_newest_stable",
        _publisher.verify_claude_code_newest_stable,
    )


def run_claude(project: Project) -> io.BytesIO:
    spool = io.BytesIO(b"# Run Request\n\n## Instruction\n\nFix")
    ClaudeCodeRunner().run(project, spool)
    return spool


def agreeing_claude_publisher(responses: PublisherResponses, newest: str) -> None:
    responses.install({
        CLAUDE_NPM_DIST_TAGS: npm_dist_tags(**{"latest": newest, "stable": "9.7.3"}),
        CLAUDE_GITHUB_RELEASES: github_release(f"v{newest}"),
        CLAUDE_LATEST_POINTER: f"{newest}\n".encode(),
    })


def test_claude_run_refuses_an_installed_release_older_than_newest_stable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    publisher_responses: PublisherResponses,
) -> None:
    install_native_claude(tmp_path, monkeypatch, version="9.8.0")
    restore_live_verification(monkeypatch)
    agreeing_claude_publisher(publisher_responses, "9.9.9")

    with pytest.raises(RunAdmissionError) as refused:
        run_claude(Project(tmp_path, tmp_path))

    message = str(refused.value)
    assert "9.8.0" in message and "9.9.9" in message
    assert "older than the newest stable release" in message
    assert "update Claude Code with claude update and retry" in message


def test_claude_run_refuses_a_local_version_failure_before_any_publisher_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    publisher_responses: PublisherResponses,
) -> None:
    # The default harness fake is silent, so the local version read refuses
    # on its own; the offline guard turns any publisher GET into a failure.
    restore_live_verification(monkeypatch)

    with pytest.raises(RunAdmissionError, match="reported an unrecognized version"):
        run_claude(Project(tmp_path, tmp_path))

    assert publisher_responses.calls == []


def test_claude_run_reaches_the_empty_compatibility_set_when_installed_is_newest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    publisher_responses: PublisherResponses,
) -> None:
    install_native_claude(tmp_path, monkeypatch, version="9.9.9")
    restore_live_verification(monkeypatch)
    agreeing_claude_publisher(publisher_responses, "9.9.9")

    with pytest.raises(RunAdmissionError, match=(
        "Claude Compatibility Set is empty; "
        "update Factory to a release that certifies Claude Code"
    )):
        run_claude(Project(tmp_path, tmp_path))


def test_claude_run_refuses_when_the_publisher_lookup_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    publisher_responses: PublisherResponses,
) -> None:
    install_native_claude(tmp_path, monkeypatch, version="9.9.9")
    restore_live_verification(monkeypatch)
    publisher_responses.install({
        CLAUDE_NPM_DIST_TAGS: _publisher._TransportError("connection refused"),
        CLAUDE_GITHUB_RELEASES: _publisher._TransportError("connection refused"),
        CLAUDE_LATEST_POINTER: _publisher._TransportError("connection refused"),
    })

    with pytest.raises(
        RunAdmissionError, match="could not reach the Claude Code publisher",
    ):
        run_claude(Project(tmp_path, tmp_path))
