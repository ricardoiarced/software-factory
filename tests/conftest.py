"""Shared fixtures for deterministic Run Admission tests."""

import json
import os
from pathlib import Path

import pytest

import _publisher


@pytest.fixture(autouse=True)
def run_state_stays_out_of_the_user_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep every test's Project Registry and Run Records in the test's own
    configuration directory; tests that need specific store state set their
    own ``XDG_CONFIG_HOME`` over this default."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))


@pytest.fixture(autouse=True)
def codex_executable_on_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Make tests that reach later Run Admission stages independent of the host."""
    executable_directory = tmp_path / "codex-bin"
    executable_directory.mkdir()
    executable = executable_directory / "codex"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    monkeypatch.setenv(
        "PATH",
        os.pathsep.join((str(executable_directory), os.environ["PATH"])),
    )


@pytest.fixture(autouse=True)
def claude_executable_on_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Make selected Claude Runs independent of a host Claude Code install."""
    executable_directory = tmp_path / "claude-bin"
    executable_directory.mkdir()
    executable = executable_directory / "claude"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    monkeypatch.setenv(
        "PATH",
        os.pathsep.join((str(executable_directory), os.environ["PATH"])),
    )


@pytest.fixture(autouse=True)
def newest_stable_verification_passes_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Treat the installed release as the newest stable one unless a test
    proves otherwise; newest-stable behavior is covered by tests that install
    their own verification."""
    monkeypatch.setattr(
        "_codex.verify_codex_newest_stable",
        lambda executable, installed: installed,
    )
    monkeypatch.setattr(
        "_claude.verify_claude_code_newest_stable",
        lambda executable, installed: installed,
    )


@pytest.fixture(autouse=True)
def publisher_lookups_stay_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Refuse every in-process publisher GET and point subprocess GETs at a
    dead local proxy so no deterministic test reaches the live publishers."""

    def refused(
        url: str, *, timeout_seconds: float, deadline: float
    ) -> tuple[int, bytes]:
        raise AssertionError(f"unexpected publisher GET in tests: {url}")

    monkeypatch.setattr("_publisher._http_get", refused)
    proxy = "http://127.0.0.1:9"
    monkeypatch.setenv("HTTPS_PROXY", proxy)
    monkeypatch.setenv("https_proxy", proxy)


class PublisherResponses:
    """Fixture publisher documents served through the transport seam."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[str] = []
        self._monkeypatch = monkeypatch

    def install(self, responses: dict[str, object]) -> None:
        calls = self.calls

        def serve(
            url: str, *, timeout_seconds: float, deadline: float
        ) -> tuple[int, bytes]:
            calls.append(url)
            entry = responses[url]
            if isinstance(entry, Exception):
                raise entry
            if isinstance(entry, tuple):
                status, body = entry
                return status, body
            return 200, entry

        self._monkeypatch.setattr("_publisher._http_get", serve)


@pytest.fixture
def publisher_responses(monkeypatch: pytest.MonkeyPatch) -> PublisherResponses:
    return PublisherResponses(monkeypatch)


@pytest.fixture
def live_newest_stable_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Restore the real per-Run publisher verification over the passing
    default so a test proves the production wiring, not the stub."""
    monkeypatch.setattr(
        "_codex.verify_codex_newest_stable",
        _publisher.verify_codex_newest_stable,
    )
    monkeypatch.setattr(
        "_claude.verify_claude_code_newest_stable",
        _publisher.verify_claude_code_newest_stable,
    )


CODEX_GITHUB_RELEASES = "https://api.github.com/repos/openai/codex/releases/latest"
CODEX_NPM_DIST_TAGS = "https://registry.npmjs.org/-/package/@openai%2Fcodex/dist-tags"
CLAUDE_NPM_DIST_TAGS = (
    "https://registry.npmjs.org/-/package/@anthropic-ai%2Fclaude-code/dist-tags"
)
CLAUDE_GITHUB_RELEASES = (
    "https://api.github.com/repos/anthropics/claude-code/releases/latest"
)
CLAUDE_LATEST_POINTER = "https://downloads.claude.ai/claude-code-releases/latest"


def github_release(tag: str, *, prerelease: bool = False) -> bytes:
    """One recorded GitHub releases/latest document."""

    return json.dumps(
        {
            "tag_name": tag,
            "draft": False,
            "prerelease": prerelease,
            "assets": [{"name": "artifact.tar.gz"}],
        }
    ).encode("utf-8")


def npm_dist_tags(**tags: str) -> bytes:
    """One recorded npm dist-tags document."""

    return json.dumps(tags).encode("utf-8")
