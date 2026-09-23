"""Live newest-stable publisher lookup and refusal classification."""

from pathlib import Path

import pytest

from _harness import RunAdmissionError
import _publisher
from _publisher import (
    _http_get as real_http_get,
    claude_code_update_command,
    codex_update_command,
    newest_stable_claude_code,
    newest_stable_codex,
    parse_stable_version,
    require_newest_stable,
)
from conftest import (
    CLAUDE_GITHUB_RELEASES,
    CLAUDE_LATEST_POINTER,
    CLAUDE_NPM_DIST_TAGS,
    CODEX_GITHUB_RELEASES,
    CODEX_NPM_DIST_TAGS,
    github_release,
    npm_dist_tags,
)
from conftest import PublisherResponses


def test_stable_release_semver_parsing_rejects_prerelease_build_and_malformed_versions() -> None:
    assert parse_stable_version("0.156.1") == (0, 156, 1)
    assert parse_stable_version("2.1.281") == (2, 1, 281)

    assert parse_stable_version("0.157.0-alpha.9") is None
    assert parse_stable_version("0.156.1-linux-x64") is None
    assert parse_stable_version("1.2.3+build.7") is None
    assert parse_stable_version("1.2") is None
    assert parse_stable_version("1.2.3.4") is None
    assert parse_stable_version("01.2.3") is None
    assert parse_stable_version("v1.2.3") is None
    assert parse_stable_version("") is None


def test_codex_newest_stable_requires_github_and_npm_agreement(
    publisher_responses: PublisherResponses,
) -> None:
    publisher_responses.install({
        CODEX_GITHUB_RELEASES: github_release("rust-v0.156.1"),
        CODEX_NPM_DIST_TAGS: npm_dist_tags(**{"latest": "0.156.1", "alpha": "0.157.0-alpha.1"}),
    })

    assert newest_stable_codex() == "0.156.1"


def test_codex_newest_stable_refuses_disagreeing_surfaces_immediately(
    publisher_responses: PublisherResponses,
) -> None:
    publisher_responses.install({
        CODEX_GITHUB_RELEASES: github_release("rust-v0.156.2"),
        CODEX_NPM_DIST_TAGS: npm_dist_tags(**{"latest": "0.156.1"}),
    })

    with pytest.raises(RunAdmissionError) as refused:
        newest_stable_codex()

    message = str(refused.value)
    assert "0.156.2" in message and "0.156.1" in message
    assert "GitHub" in message and "npm" in message
    assert publisher_responses.calls == [CODEX_GITHUB_RELEASES, CODEX_NPM_DIST_TAGS]


def test_codex_newest_stable_rejects_unstable_github_releases(
    publisher_responses: PublisherResponses,
) -> None:
    for release in (
        github_release("rust-v0.156.1", prerelease=True),
        b'{"tag_name":"rust-v0.156.1","draft":true,"prerelease":false,"assets":[]}',
    ):
        publisher_responses.install({
            CODEX_GITHUB_RELEASES: release,
            CODEX_NPM_DIST_TAGS: npm_dist_tags(**{"latest": "0.156.1"}),
        })
        with pytest.raises(RunAdmissionError, match="GitHub Releases"):
            newest_stable_codex()


def test_codex_newest_stable_rejects_unrecognized_publisher_metadata(
    publisher_responses: PublisherResponses,
) -> None:
    def refusal_for(responses: dict[str, object]) -> RunAdmissionError:
        publisher_responses.install(responses)
        with pytest.raises(RunAdmissionError) as refused:
            newest_stable_codex()
        return refused.value

    malformed = [
        b"not json at all",
        b'["an", "array"]',
        b'{"draft": false, "prerelease": false, "assets": []}',
        b'{"tag_name": "release-0.156.1", "draft": false, "prerelease": false, "assets": []}',
        b'{"tag_name": "rust-v0.156.1-rc.1", "draft": false, "prerelease": false, "assets": []}',
        b'{"tag_name": "rust-v0.156.1", "draft": false, "prerelease": false}',
    ]
    for body in malformed:
        failure = refusal_for({
            CODEX_GITHUB_RELEASES: body,
            CODEX_NPM_DIST_TAGS: npm_dist_tags(**{"latest": "0.156.1"}),
        })
        assert "GitHub Releases metadata" in str(failure)

    npm_failures = [
        b"",
        b'{"next": "0.156.1"}',
        b'{"latest": "0.156.1-linux-x64"}',
    ]
    for body in npm_failures:
        failure = refusal_for({
            CODEX_GITHUB_RELEASES: github_release("rust-v0.156.1"),
            CODEX_NPM_DIST_TAGS: body,
        })
        assert "npm dist-tags metadata" in str(failure)


def test_codex_newest_stable_refuses_a_publisher_without_a_stable_release(
    publisher_responses: PublisherResponses,
) -> None:
    publisher_responses.install({
        CODEX_GITHUB_RELEASES: (404, b"not found"),
        CODEX_NPM_DIST_TAGS: npm_dist_tags(**{"latest": "0.156.1"}),
    })

    with pytest.raises(RunAdmissionError, match="published no stable release"):
        newest_stable_codex()


def test_publisher_http_error_statuses_are_lookup_failures(
    publisher_responses: PublisherResponses,
) -> None:
    publisher_responses.install({
        CODEX_GITHUB_RELEASES: github_release("rust-v0.156.1"),
        CODEX_NPM_DIST_TAGS: (404, b"not found"),
    })
    with pytest.raises(RunAdmissionError) as refused:
        newest_stable_codex()
    message = str(refused.value)
    assert "could not reach the Codex publisher" in message
    assert "HTTP 404" in message

    publisher_responses.calls.clear()
    publisher_responses.install({
        CODEX_GITHUB_RELEASES: (410, b"gone"),
        CODEX_NPM_DIST_TAGS: npm_dist_tags(**{"latest": "0.156.1"}),
    })
    with pytest.raises(RunAdmissionError) as refused:
        newest_stable_codex()
    message = str(refused.value)
    assert "could not reach the Codex publisher" in message
    assert "answered HTTP 410" in message


def test_a_claude_pointer_error_status_is_a_lookup_failure(
    publisher_responses: PublisherResponses,
) -> None:
    publisher_responses.install({
        CLAUDE_NPM_DIST_TAGS: npm_dist_tags(**{"latest": "2.1.281"}),
        CLAUDE_GITHUB_RELEASES: github_release("v2.1.281"),
        CLAUDE_LATEST_POINTER: (404, b"not found"),
    })

    with pytest.raises(RunAdmissionError) as refused:
        newest_stable_claude_code()

    message = str(refused.value)
    assert "could not reach the Claude Code publisher" in message
    assert "HTTP 404" in message


def test_claude_newest_stable_requires_npm_github_and_pointer_agreement(
    publisher_responses: PublisherResponses,
) -> None:
    publisher_responses.install({
        CLAUDE_NPM_DIST_TAGS: npm_dist_tags(**{"latest": "2.1.281", "stable": "2.1.273"}),
        CLAUDE_GITHUB_RELEASES: github_release("v2.1.281"),
        CLAUDE_LATEST_POINTER: b"2.1.281\n",
    })

    assert newest_stable_claude_code() == "2.1.281"


def test_claude_newest_stable_refuses_disagreeing_surfaces_immediately(
    publisher_responses: PublisherResponses,
) -> None:
    publisher_responses.install({
        CLAUDE_NPM_DIST_TAGS: npm_dist_tags(**{"latest": "2.1.281"}),
        CLAUDE_GITHUB_RELEASES: github_release("v2.1.280"),
        CLAUDE_LATEST_POINTER: b"2.1.281",
    })
    with pytest.raises(RunAdmissionError) as refused:
        newest_stable_claude_code()
    message = str(refused.value)
    assert "2.1.281" in message and "2.1.280" in message
    assert "npm" in message and "GitHub" in message
    assert publisher_responses.calls == [CLAUDE_NPM_DIST_TAGS, CLAUDE_GITHUB_RELEASES]

    publisher_responses.install({
        CLAUDE_NPM_DIST_TAGS: npm_dist_tags(**{"latest": "2.1.281"}),
        CLAUDE_GITHUB_RELEASES: github_release("v2.1.281"),
        CLAUDE_LATEST_POINTER: b"2.1.279",
    })
    publisher_responses.calls.clear()
    with pytest.raises(RunAdmissionError) as refused:
        newest_stable_claude_code()
    message = str(refused.value)
    assert "2.1.281" in message and "2.1.279" in message
    assert "downloads.claude.ai" in message
    assert publisher_responses.calls == [
        CLAUDE_NPM_DIST_TAGS, CLAUDE_GITHUB_RELEASES, CLAUDE_LATEST_POINTER,
    ]


def test_transport_faults_retry_once_then_refuse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: list[str] = []

    def flaky_once_then_success(
        url: str, *, timeout_seconds: float, deadline: float,
    ) -> tuple[int, bytes]:
        attempts.append(url)
        if url == CODEX_GITHUB_RELEASES and attempts.count(url) == 1:
            raise _publisher._TransportError("connection refused")
        return 200, github_release("rust-v0.156.1") if url == CODEX_GITHUB_RELEASES \
            else npm_dist_tags(**{"latest": "0.156.1"})

    monkeypatch.setattr("_publisher._http_get", flaky_once_then_success)
    assert newest_stable_codex() == "0.156.1"
    assert attempts.count(CODEX_GITHUB_RELEASES) == 2

    attempts.clear()

    def always_unreachable(url: str, *, timeout_seconds: float, deadline: float) -> tuple[int, bytes]:
        attempts.append(url)
        raise _publisher._TransportError("name resolution failed")

    monkeypatch.setattr("_publisher._http_get", always_unreachable)
    with pytest.raises(RunAdmissionError, match="check connectivity and retry"):
        newest_stable_codex()
    assert attempts.count(CODEX_GITHUB_RELEASES) == 2


def test_server_faults_retry_once_then_refuse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: list[str] = []

    def always_overloaded(url: str, *, timeout_seconds: float, deadline: float) -> tuple[int, bytes]:
        attempts.append(url)
        return 503, b"overloaded"

    monkeypatch.setattr("_publisher._http_get", always_overloaded)
    with pytest.raises(RunAdmissionError, match="HTTP 503"):
        newest_stable_codex()
    assert attempts == [CODEX_GITHUB_RELEASES] * 2


def test_rate_limited_lookups_refuse_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: list[str] = []

    def rate_limited(url: str, *, timeout_seconds: float, deadline: float) -> tuple[int, bytes]:
        attempts.append(url)
        return 429, b"rate limited"

    monkeypatch.setattr("_publisher._http_get", rate_limited)
    with pytest.raises(RunAdmissionError, match="rate-limited.*HTTP 429"):
        newest_stable_codex()
    assert attempts == [CODEX_GITHUB_RELEASES]


def test_an_expired_lookup_deadline_stops_all_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: list[str] = []
    deadlines = iter([0.0, 10.5, 10.5])

    def record(url: str, *, timeout_seconds: float, deadline: float) -> tuple[int, bytes]:
        attempts.append(url)
        return 200, github_release("rust-v0.156.1")

    monkeypatch.setattr("_publisher._http_get", record)
    monkeypatch.setattr("_publisher.time.monotonic", lambda: next(deadlines))

    with pytest.raises(RunAdmissionError, match="deadline expired"):
        newest_stable_codex()
    assert attempts == []


def test_every_get_is_bounded_by_three_seconds_of_the_remaining_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timeouts: list[float] = []

    def record(
        url: str, *, timeout_seconds: float, deadline: float,
    ) -> tuple[int, bytes]:
        timeouts.append(timeout_seconds)
        if url == CODEX_GITHUB_RELEASES:
            return 200, github_release("rust-v0.156.1")
        return 200, npm_dist_tags(**{"latest": "0.156.1"})

    monkeypatch.setattr("_publisher._http_get", record)
    newest_stable_codex()
    assert timeouts == [3.0, 3.0]


def test_a_slow_drip_response_cannot_outlive_the_shared_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SlowDrip:
        status = 200

        def __init__(self) -> None:
            self.reads = 0

        def geturl(self) -> str:
            return CODEX_GITHUB_RELEASES

        def read(self, amount: int) -> bytes:
            self.reads += 1
            return b"x"  # never EOF: the body never completes

        def __enter__(self) -> "SlowDrip":
            return self

        def __exit__(self, *args: object) -> None:
            return None

    drip = SlowDrip()
    # This test exercises the real transport function itself, so restore it
    # over the offline guard before pinning its urlopen dependency.
    monkeypatch.setattr("_publisher._http_get", real_http_get)
    monkeypatch.setattr(
        "_publisher.urllib.request.urlopen", lambda request, timeout: drip,
    )
    # Every clock check burns four seconds; the ten-second deadline must cut
    # the endless body off after a bounded number of chunked reads.
    ticks = iter([0.0, 4.0, 8.0, 12.0, 16.0, 20.0, 24.0, 28.0])
    monkeypatch.setattr("_publisher.time.monotonic", lambda: next(ticks))

    with pytest.raises(
        RunAdmissionError, match="could not reach the Codex publisher",
    ):
        newest_stable_codex()

    assert drip.reads <= 4


def test_publisher_requests_carry_no_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GH_TOKEN", "secret-token")
    monkeypatch.setenv("GITHUB_TOKEN", "secret-token")

    request = _publisher._publisher_request(CODEX_GITHUB_RELEASES)

    assert request.get_full_url() == CODEX_GITHUB_RELEASES
    assert "Authorization" not in request.headers


def test_a_redirect_off_https_is_a_transport_fault(monkeypatch: pytest.MonkeyPatch) -> None:
    class Redirected:
        def geturl(self) -> str:
            return "http://mirror.example.invalid/releases/latest"

        def read(self, amount: int) -> bytes:
            return b"{}"

        def __enter__(self) -> "Redirected":
            return self

        def __exit__(self, *args: object) -> None:
            return None

    # This test exercises the real transport function itself, so restore it
    # over the offline guard before pinning its urlopen dependency.
    monkeypatch.setattr("_publisher._http_get", real_http_get)
    monkeypatch.setattr(
        "_publisher.urllib.request.urlopen", lambda request, timeout: Redirected(),
    )

    with pytest.raises(_publisher._TransportError, match="HTTPS"):
        _publisher._http_get(CODEX_GITHUB_RELEASES, timeout_seconds=3.0, deadline=10.0)


def test_installed_below_newest_stable_refuses_and_names_the_update_command() -> None:
    with pytest.raises(RunAdmissionError) as refused:
        require_newest_stable(
            "Codex", "0.156.0", "0.156.1", "npm install -g @openai/codex",
        )

    message = str(refused.value)
    assert "Codex" in message
    assert "0.156.0" in message and "0.156.1" in message
    assert "older than the newest stable release" in message
    assert "npm install -g @openai/codex" in message


def test_installed_at_or_above_newest_stable_passes_verification() -> None:
    require_newest_stable("Codex", "0.156.1", "0.156.1", "ignored")
    require_newest_stable("Codex", "0.156.2", "0.156.1", "ignored")
    require_newest_stable("Claude Code", "2.2.0", "2.1.281", "ignored")


def test_a_prerelease_installed_release_refuses_as_an_unstable_channel() -> None:
    with pytest.raises(RunAdmissionError) as refused:
        require_newest_stable(
            "Codex", "0.158.0-alpha.2", "0.156.1",
            "curl -fsSL https://chatgpt.com/codex/install.sh | sh",
        )

    message = str(refused.value)
    assert "not a stable release" in message
    assert "0.158.0-alpha.2" in message
    assert "curl -fsSL https://chatgpt.com/codex/install.sh | sh" in message


def test_codex_update_commands_follow_the_install_channel() -> None:
    standalone = Path(
        "/home/user/.codex/packages/standalone/releases/"
        "0.156.1-x86_64-unknown-linux-musl/bin/codex",
    )
    npm = Path("/usr/lib/node_modules/@openai/codex/bin/codex.js")
    platform_alias = Path(
        "/usr/lib/node_modules/@openai/codex-linux-x64/vendor/codex",
    )
    homebrew = Path("/opt/homebrew/Caskroom/codex/0.156.1/codex")

    assert codex_update_command(standalone) == (
        "curl -fsSL https://chatgpt.com/codex/install.sh | sh"
    )
    assert codex_update_command(npm) == "npm install -g @openai/codex"
    assert codex_update_command(platform_alias) == "npm install -g @openai/codex"
    assert codex_update_command(homebrew) == "brew upgrade --cask codex"
    fallback = codex_update_command(Path("/usr/bin/codex"))
    assert "npm install -g @openai/codex" in fallback
    assert "install.sh" in fallback


def test_claude_update_commands_follow_the_install_channel() -> None:
    native = Path("/home/user/.local/share/claude/versions/2.1.281")
    npm = Path("/usr/lib/node_modules/@anthropic-ai/claude-code/cli.js")
    homebrew = Path("/opt/homebrew/Caskroom/claude-code/2.1.281/claude")

    assert claude_code_update_command(native) == "claude update"
    assert claude_code_update_command(npm) == (
        "npm install -g @anthropic-ai/claude-code@latest"
    )
    assert claude_code_update_command(homebrew) == "brew upgrade --cask claude-code"
    fallback = claude_code_update_command(Path("/usr/bin/claude"))
    assert "claude update" in fallback
    assert "npm install -g @anthropic-ai/claude-code@latest" in fallback
