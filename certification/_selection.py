"""Load one immutable, repository-reviewed certification candidate."""

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class SelectionError(Exception):
    """The real-host certification candidate is not trustworthy."""


# Manifest schema 2 names the certified tuple (harness, version, artifact
# family / install channel, OS, architecture). Exactly one schema exists;
# schema-1 Codex manifests migrated when schema 2 landed.
_SCHEMA = 2

_HARNESSES = ("codex", "claude")

# The install channels of the certified first scope. A channel names the
# artifact family whose official distribution surface anchors the digest.
_INSTALL_CHANNELS = {
    "codex": ("github-asset", "npm"),
    "claude": ("native-installer", "npm"),
}

# npm-family digests anchor the unpacked platform binary and are
# cross-checked against the registry's dist-integrity before review;
# their official source is the npm package page.
_SOURCE_PREFIXES = {
    ("codex", "github-asset"): "https://github.com/openai/codex/releases/",
    ("codex", "npm"): "https://www.npmjs.com/package/",
    ("claude", "native-installer"): "https://",
    ("claude", "npm"): "https://www.npmjs.com/package/",
}


@dataclass(frozen=True)
class CertificationSelection:
    """The independently named Harness artifact, channel, and platform tuple."""

    harness: str
    version: str
    install_channel: str
    official_sha256: str
    official_source: str
    operating_system: str
    architecture: str
    manifest: Path
    selection_commit: str


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for name, item in pairs:
        if name in value:
            raise SelectionError(f"candidate manifest repeats {name!r}")
        value[name] = item
    return value


def _git(root: Path, *arguments: str) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            ["git", *arguments],
            cwd=root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError as error:
        raise SelectionError(
            f"candidate manifest commit could not be inspected: {error.strerror or error}",
        ) from error


def _committed_bytes(root: Path, relative: Path) -> tuple[bytes, str]:
    committed = _git(root, "show", "HEAD:" + relative.as_posix())
    revision = _git(root, "rev-parse", "--verify", "HEAD")
    if committed.returncode or revision.returncode:
        raise SelectionError(
            "candidate manifest must exactly match a file committed at HEAD",
        )
    try:
        commit = revision.stdout.decode("ascii", errors="strict").strip()
    except UnicodeDecodeError as error:
        raise SelectionError(
            "candidate manifest HEAD identity is unrecognized",
        ) from error
    if re.fullmatch(r"[0-9a-f]{40,64}", commit) is None:
        raise SelectionError("candidate manifest HEAD identity is unrecognized")
    return committed.stdout, commit


def load_committed_selection(
    project_root: Path, candidate_argument: str | os.PathLike[str],
) -> CertificationSelection:
    """Load an exact candidate only when its manifest is committed at HEAD."""

    root = project_root.resolve(strict=True)
    candidate = Path(candidate_argument)
    if not candidate.is_absolute():
        candidate = root / candidate
    if candidate.is_symlink():
        raise SelectionError("candidate manifest must not be a symlink")
    try:
        candidate = candidate.resolve(strict=True)
    except OSError as error:
        raise SelectionError(
            f"candidate manifest cannot be resolved: {error.strerror or error}",
        ) from error
    candidates = (root / "certification" / "candidates").resolve()
    try:
        candidate.relative_to(candidates)
        relative = candidate.relative_to(root)
    except ValueError as error:
        raise SelectionError(
            "candidate manifest must be inside certification/candidates",
        ) from error
    if not candidate.is_file() or re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._-]*\.json", candidate.name,
    ) is None:
        raise SelectionError("candidate manifest must be a named JSON file")

    try:
        document_bytes = candidate.read_bytes()
    except OSError as error:
        raise SelectionError(
            f"candidate manifest cannot be read: {error.strerror or error}",
        ) from error
    committed_bytes, selection_commit = _committed_bytes(root, relative)
    if document_bytes != committed_bytes:
        raise SelectionError(
            "candidate manifest must exactly match a file committed at HEAD",
        )

    try:
        document = json.loads(document_bytes, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise SelectionError("candidate manifest is not strict JSON") from error
    expected_fields = {
        "schema",
        "harness",
        "harness_version",
        "install_channel",
        "official_sha256",
        "official_source",
        "operating_system",
        "architecture",
    }
    if not isinstance(document, dict) or set(document) != expected_fields:
        raise SelectionError("candidate manifest fields are incomplete or unrecognized")
    if type(document["schema"]) is not int or document["schema"] != _SCHEMA:
        raise SelectionError("candidate manifest schema is not supported")
    for name in expected_fields - {"schema"}:
        value = document[name]
        if not isinstance(value, str) or not value or value.strip() != value:
            raise SelectionError(f"candidate manifest {name} is not an exact string")
    harness = document["harness"]
    if harness not in _HARNESSES:
        raise SelectionError(
            f"candidate manifest harness {harness!r} is not a certified Harness",
        )
    install_channel = document["install_channel"]
    if install_channel not in _INSTALL_CHANNELS[harness]:
        raise SelectionError(
            f"candidate manifest install channel {install_channel!r} is not "
            f"certified for {harness}",
        )
    digest = document["official_sha256"]
    if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise SelectionError(
            "candidate manifest official_sha256 is not a lowercase SHA-256 digest",
        )
    official_source = document["official_source"]
    if not official_source.startswith(_SOURCE_PREFIXES[(harness, install_channel)]):
        raise SelectionError(
            "candidate manifest official_source does not name the artifact "
            "family's official distribution surface",
        )

    return CertificationSelection(
        harness=harness,
        version=document["harness_version"],
        install_channel=install_channel,
        official_sha256=digest,
        official_source=official_source,
        operating_system=document["operating_system"],
        architecture=document["architecture"],
        manifest=relative,
        selection_commit=selection_commit,
    )
