"""Deterministic selection rules for real-host certification."""

import json
from pathlib import Path
import subprocess

import pytest

from certification._selection import load_committed_selection, SelectionError


def make_candidate_repository(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    candidates = tmp_path / "certification" / "candidates"
    candidates.mkdir(parents=True)
    (tmp_path / "tracked").write_text("tracked\n")
    subprocess.run(["git", "add", "tracked"], cwd=tmp_path, check=True)
    subprocess.run([
        "git", "-c", "user.name=Factory Test", "-c",
        "user.email=factory@example.invalid", "commit", "-qm", "initial",
    ], cwd=tmp_path, check=True)
    return candidates


def candidate_document(digest: str = "a" * 64) -> dict[str, object]:
    return {
        "schema": 1,
        "codex_version": "0.155.1",
        "official_sha256": digest,
        "official_source": "https://github.com/openai/codex/releases/tag/rust-v0.155.1",
        "operating_system": "Linux",
        "architecture": "x86_64",
    }


def commit_candidate(root: Path, candidate: Path) -> None:
    candidate.write_text(json.dumps(candidate_document()) + "\n")
    subprocess.run(
        ["git", "add", candidate.relative_to(root)], cwd=root, check=True,
    )
    subprocess.run([
        "git", "-c", "user.name=Factory Test", "-c",
        "user.email=factory@example.invalid", "commit", "-qm", "candidate",
    ], cwd=root, check=True)


def test_certification_loads_one_exact_committed_candidate(tmp_path: Path) -> None:
    candidates = make_candidate_repository(tmp_path)
    candidate = candidates / "linux-x86_64.json"
    commit_candidate(tmp_path, candidate)

    selection = load_committed_selection(tmp_path, candidate)

    assert selection.version == "0.155.1"
    assert selection.official_sha256 == "a" * 64
    assert selection.operating_system == "Linux"
    assert selection.architecture == "x86_64"
    assert selection.official_source == (
        "https://github.com/openai/codex/releases/tag/rust-v0.155.1"
    )
    assert selection.manifest == Path("certification/candidates/linux-x86_64.json")
    assert len(selection.selection_commit) == 40


@pytest.mark.parametrize("state", ["untracked", "modified"])
def test_uncommitted_digest_cannot_select_an_executable_for_certification(
    tmp_path: Path, state: str,
) -> None:
    candidates = make_candidate_repository(tmp_path)
    candidate = candidates / "linux-x86_64.json"
    if state == "modified":
        commit_candidate(tmp_path, candidate)
    candidate.write_text(json.dumps(candidate_document("b" * 64)) + "\n")

    with pytest.raises(
        SelectionError, match="must exactly match a file committed at HEAD",
    ):
        load_committed_selection(tmp_path, candidate)
