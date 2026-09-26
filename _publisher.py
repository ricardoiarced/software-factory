"""Live newest-stable publisher verification for Run Admission.

Every Run performs one live unauthenticated lookup per Harness before its
Compatibility Set match: GitHub ``releases/latest`` cross-checked against the
npm ``latest`` dist-tag for Codex, and the npm ``latest`` dist-tag
cross-checked against GitHub ``releases/latest`` and the
``downloads.claude.ai`` ``latest`` pointer for Claude Code. Transport faults
get exactly one bounded retry; surfaces that disagree refuse immediately; a
failed lookup never stale-allows a Run.
"""

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Protocol

from _harness import RunAdmissionError

_CONNECT_TIMEOUT_SECONDS = 3.0
_TOTAL_TIMEOUT_SECONDS = 10.0
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_READ_CHUNK_BYTES = 65536

_GITHUB_API = "https://api.github.com"
_NPM_REGISTRY = "https://registry.npmjs.org"
_CLAUDE_DOWNLOADS = "https://downloads.claude.ai/claude-code-releases"

_STABLE_SEMVER = re.compile(
    r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z",
)


def parse_stable_version(value: str) -> tuple[int, int, int] | None:
    """Return the release's (major, minor, patch) or None unless stable semver."""

    match = _STABLE_SEMVER.fullmatch(value)
    if match is None:
        return None
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch)


class _TransportError(Exception):
    """A connect error, timeout, or server-side 5xx from one GET."""


class _ReadableResponse(Protocol):
    """The one response surface the bounded reader consumes."""

    def read(self, size: int) -> bytes:
        """Return up to ``size`` bytes; an empty result ends the body."""
        ...


def _publisher_request(url: str) -> urllib.request.Request:
    """Build the one unauthenticated GET every publisher surface uses."""

    return urllib.request.Request(
        url,
        headers={"Accept": "*/*", "User-Agent": "factory-newest-stable-verification"},
    )


def _read_bounded(response: _ReadableResponse, deadline: float) -> bytes:
    """Read the body in chunks under one shared absolute deadline.

    The per-recv socket timeout alone cannot bound a slow-drip response, so
    each chunk is checked against the lookup's total deadline; an endless or
    oversized body stops at the response bound and lets the caller classify
    the oversize.
    """

    body = bytearray()
    while True:
        if time.monotonic() >= deadline:
            raise _TransportError("the lookup deadline expired mid-response")
        chunk = response.read(_READ_CHUNK_BYTES)
        if not chunk:
            return bytes(body)
        body += chunk
        if len(body) > _MAX_RESPONSE_BYTES:
            return bytes(body)


def _http_get(
    url: str, *, timeout_seconds: float, deadline: float,
) -> tuple[int, bytes]:
    """Perform one unauthenticated GET and return its status and bounded body.

    Factory never reads or sends credentials: the request carries no
    Authorization header even when ``GH_TOKEN`` or ``GITHUB_TOKEN`` is set,
    and a redirect that leaves HTTPS is refused as a transport fault.
    """

    request = _publisher_request(url)
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            if not response.geturl().startswith("https://"):
                raise _TransportError("redirect left HTTPS")
            return response.status, _read_bounded(response, deadline)
    except urllib.error.HTTPError as error:
        return error.code, error.read(_MAX_RESPONSE_BYTES + 1)
    except _TransportError:
        raise
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise _TransportError(
            str(getattr(error, "reason", None) or error),
        ) from error


class _Lookup:
    """One per-Harness lookup: one retry per GET under one shared deadline."""

    def __init__(self, harness: str) -> None:
        self.harness = harness
        self._deadline = time.monotonic() + _TOTAL_TIMEOUT_SECONDS

    def _refusal(self, cause: str) -> RunAdmissionError:
        return RunAdmissionError(
            f"Factory could not reach the {self.harness} publisher to verify "
            f"the newest stable release ({cause}); check connectivity and retry",
        )

    def get(self, url: str) -> tuple[int, bytes]:
        last_failure = "the lookup deadline expired"
        for _ in range(2):
            remaining = self._deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                status, body = _http_get(
                    url,
                    timeout_seconds=min(_CONNECT_TIMEOUT_SECONDS, remaining),
                    deadline=self._deadline,
                )
            except _TransportError as error:
                last_failure = str(error)
                continue
            if 500 <= status <= 599:
                last_failure = f"HTTP {status}"
                continue
            if status in (403, 429):
                raise RunAdmissionError(
                    f"The {self.harness} publisher rate-limited the newest-stable "
                    f"lookup of {url} (HTTP {status}); wait for the rate limit "
                    "to reset and retry",
                )
            return status, body
        raise self._refusal(last_failure)


def _metadata_failure(harness: str, surface: str, detail: str) -> RunAdmissionError:
    return RunAdmissionError(
        f"Factory did not recognize the {harness} {surface} metadata: {detail}; "
        "update Factory to a release that reads the publisher's current metadata",
    )


def _json_document(
    harness: str, surface: str, body: bytes,
) -> object:
    if len(body) > _MAX_RESPONSE_BYTES:
        raise _metadata_failure(harness, surface, "response exceeds the size bound")
    try:
        return json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise _metadata_failure(harness, surface, "response is not JSON") from None


def _github_latest_stable(
    lookup: _Lookup, repository: str, tag_prefix: str,
) -> str:
    url = f"{_GITHUB_API}/repos/{repository}/releases/latest"
    status, body = lookup.get(url)
    if status == 404:
        raise lookup._refusal(f"{url} published no stable release (HTTP 404)")
    if status != 200:
        raise lookup._refusal(f"{url} answered HTTP {status}")
    document = _json_document(lookup.harness, "GitHub Releases", body)
    if not isinstance(document, dict):
        raise _metadata_failure(
            lookup.harness, "GitHub Releases", "response is not an object",
        )
    tag = document.get("tag_name")
    if not isinstance(tag, str) or not tag.startswith(tag_prefix):
        raise _metadata_failure(
            lookup.harness, "GitHub Releases", f"unrecognized tag_name {tag!r}",
        )
    for flag in ("draft", "prerelease"):
        if document.get(flag) is not False:
            raise _metadata_failure(
                lookup.harness, "GitHub Releases",
                f"{tag} is published with {flag} set",
            )
    if not isinstance(document.get("assets"), list):
        raise _metadata_failure(
            lookup.harness, "GitHub Releases", "assets is not a list",
        )
    version = tag[len(tag_prefix):]
    if parse_stable_version(version) is None:
        raise _metadata_failure(
            lookup.harness, "GitHub Releases",
            f"tag {tag} does not name a stable release",
        )
    return version


def _npm_latest(lookup: _Lookup, package: str) -> str:
    quoted = package.replace("/", urllib.parse.quote("/", safe=""))
    url = f"{_NPM_REGISTRY}/-/package/{quoted}/dist-tags"
    status, body = lookup.get(url)
    if status == 404:
        raise lookup._refusal(f"{url} published no dist-tags (HTTP 404)")
    if status != 200:
        raise lookup._refusal(f"{url} answered HTTP {status}")
    document = _json_document(lookup.harness, "npm dist-tags", body)
    if not isinstance(document, dict):
        raise _metadata_failure(
            lookup.harness, "npm dist-tags", "response is not an object",
        )
    latest = document.get("latest")
    if not isinstance(latest, str) or parse_stable_version(latest) is None:
        raise _metadata_failure(
            lookup.harness, "npm dist-tags",
            f"latest dist-tag {latest!r} does not name a stable release",
        )
    return latest


def _disagreement_failure(
    harness: str, observations: tuple[tuple[str, str], ...],
) -> RunAdmissionError:
    listed = "; ".join(
        f"{surface} reports {version}" for surface, version in observations
    )
    return RunAdmissionError(
        f"The {harness} publisher surfaces disagree about the newest stable "
        f"release ({listed}); refusing until the publisher surfaces agree",
    )


def newest_stable_codex() -> str:
    """Return the Codex publisher's newest stable release version."""

    lookup = _Lookup("Codex")
    github = _github_latest_stable(lookup, "openai/codex", "rust-v")
    npm = _npm_latest(lookup, "@openai/codex")
    if github != npm:
        raise _disagreement_failure(
            "Codex",
            (("GitHub Releases", github), ("npm latest dist-tag", npm)),
        )
    return github


def _pointer_latest(lookup: _Lookup, url: str) -> str:
    """Read one downloads.claude.ai channel pointer naming a stable version."""

    status, body = lookup.get(url)
    if status != 200:
        raise lookup._refusal(f"{url} answered HTTP {status}")
    if len(body) > _MAX_RESPONSE_BYTES:
        raise _metadata_failure(
            lookup.harness, "downloads.claude.ai latest pointer",
            "response exceeds the size bound",
        )
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        raise _metadata_failure(
            lookup.harness, "downloads.claude.ai latest pointer",
            "response is not UTF-8 text",
        ) from None
    text = text.removesuffix("\n")
    if parse_stable_version(text) is None:
        raise _metadata_failure(
            lookup.harness, "downloads.claude.ai latest pointer",
            f"pointer {text!r} does not name a stable release",
        )
    return text


def newest_stable_claude_code() -> str:
    """Return the Claude Code publisher's newest stable release version.

    The publisher's ``latest`` dist-tag defines newest stable; the delayed
    ``stable`` channel is a regression-skipping lag, not a prerelease filter,
    so it never influences this result.
    """

    lookup = _Lookup("Claude Code")
    npm = _npm_latest(lookup, "@anthropic-ai/claude-code")
    github = _github_latest_stable(lookup, "anthropics/claude-code", "v")
    if github != npm:
        raise _disagreement_failure(
            "Claude Code",
            (("npm latest dist-tag", npm), ("GitHub Releases", github)),
        )
    pointer = _pointer_latest(lookup, f"{_CLAUDE_DOWNLOADS}/latest")
    if pointer != npm:
        raise _disagreement_failure(
            "Claude Code",
            (
                ("npm latest dist-tag", npm),
                ("downloads.claude.ai latest pointer", pointer),
            ),
        )
    return npm


_CODEX_STANDALONE_INSTALLER = "curl -fsSL https://chatgpt.com/codex/install.sh | sh"
_CODEX_NPM_UPDATE = "npm install -g @openai/codex"
_CLAUDE_NPM_UPDATE = "npm install -g @anthropic-ai/claude-code@latest"


def codex_update_command(executable: Path) -> str:
    """Name the exact update command for this Codex install channel."""

    path = os.fspath(executable)
    if "/.codex/packages/standalone/" in path:
        return _CODEX_STANDALONE_INSTALLER
    if "/node_modules/@openai/codex" in path:
        return _CODEX_NPM_UPDATE
    if "/Caskroom/codex/" in path or "/Cellar/codex/" in path:
        return "brew upgrade --cask codex"
    return f"{_CODEX_NPM_UPDATE}, or {_CODEX_STANDALONE_INSTALLER}"


def claude_code_update_command(executable: Path) -> str:
    """Name the exact update command for this Claude Code install channel."""

    path = os.fspath(executable)
    if "/.local/share/claude/versions/" in path or "/.claude/local/" in path:
        return "claude update"
    if "/node_modules/@anthropic-ai/claude-code" in path:
        return _CLAUDE_NPM_UPDATE
    if "/Caskroom/claude-code/" in path or "/Cellar/claude-code/" in path:
        return "brew upgrade --cask claude-code"
    return f"claude update, or {_CLAUDE_NPM_UPDATE}"


def require_newest_stable(
    harness: str, installed: str, newest: str, update_command: str,
) -> None:
    """Classify the installed release against the verified newest stable.

    Installed below newest stable refuses with the channel's update command;
    a prerelease-suffixed install is never a stable channel; at or above
    newest stable passes to the Compatibility Set match.
    """

    installed_tuple = parse_stable_version(installed)
    if installed_tuple is None:
        raise RunAdmissionError(
            f"{harness} release {json.dumps(installed)} is not a stable release; "
            f"install the {harness} stable release with {update_command}",
        )
    newest_tuple = parse_stable_version(newest)
    assert newest_tuple is not None, "the lookup only returns stable versions"
    if installed_tuple < newest_tuple:
        raise RunAdmissionError(
            f"{harness} release {json.dumps(installed)} is older than the newest "
            f"stable release {json.dumps(newest)}; update {harness} with "
            f"{update_command} and retry",
        )


def verify_codex_newest_stable(executable: Path, installed: str) -> str:
    """Run the live Codex lookup and refuse anything older than newest stable."""

    newest = newest_stable_codex()
    require_newest_stable(
        "Codex", installed, newest, codex_update_command(executable),
    )
    return newest


def verify_claude_code_newest_stable(executable: Path, installed: str) -> str:
    """Run the live Claude Code lookup and refuse anything older."""

    newest = newest_stable_claude_code()
    require_newest_stable(
        "Claude Code", installed, newest, claude_code_update_command(executable),
    )
    return newest
