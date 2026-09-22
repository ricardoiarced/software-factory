"""Load one immutable, repository-reviewed certification candidate."""

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any


class SelectionError(Exception):
    """The real-host certification candidate is not trustworthy."""


@dataclass(frozen=True)
class CertificationSelection:
    """The independently named Codex artifact and platform tuple."""

    version: str
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
        "codex_version",
        "official_sha256",
        "official_source",
        "operating_system",
        "architecture",
    }
    if not isinstance(document, dict) or set(document) != expected_fields:
        raise SelectionError("candidate manifest fields are incomplete or unrecognized")
    if type(document["schema"]) is not int or document["schema"] != 1:
        raise SelectionError("candidate manifest schema is not supported")
    for name in expected_fields - {"schema"}:
        value = document[name]
        if not isinstance(value, str) or not value or value.strip() != value:
            raise SelectionError(f"candidate manifest {name} is not an exact string")
    digest = document["official_sha256"]
    if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise SelectionError(
            "candidate manifest official_sha256 is not a lowercase SHA-256 digest",
        )
    official_source = document["official_source"]
    if not official_source.startswith("https://github.com/openai/codex/releases/"):
        raise SelectionError(
            "candidate manifest official_source is not an official Codex release",
        )

    return CertificationSelection(
        version=document["codex_version"],
        official_sha256=digest,
        official_source=official_source,
        operating_system=document["operating_system"],
        architecture=document["architecture"],
        manifest=relative,
        selection_commit=selection_commit,
    )
