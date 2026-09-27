"""Deterministic selection rules for real-host certification."""

import json
import subprocess
from pathlib import Path

import pytest

import _codex
from certification._selection import SelectionError, load_committed_selection

PROJECT = Path(__file__).resolve().parents[1]


def make_candidate_repository(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    candidates = tmp_path / "certification" / "candidates"
    candidates.mkdir(parents=True)
    (tmp_path / "tracked").write_text("tracked\n")
    subprocess.run(["git", "add", "tracked"], cwd=tmp_path, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Factory Test",
            "-c",
            "user.email=factory@example.invalid",
            "commit",
            "-qm",
            "initial",
        ],
        cwd=tmp_path,
        check=True,
    )
    return candidates


def candidate_document(digest: str = "a" * 64) -> dict[str, object]:
    return {
        "schema": 2,
        "harness": "codex",
        "harness_version": "0.155.1",
        "install_channel": "github-asset",
        "official_sha256": digest,
        "official_source": "https://github.com/openai/codex/releases/tag/rust-v0.155.1",
        "operating_system": "Linux",
        "architecture": "x86_64",
    }


def commit_candidate(
    root: Path,
    candidate: Path,
    document: dict[str, object] | None = None,
) -> None:
    candidate.write_text(json.dumps(document or candidate_document()) + "\n")
    subprocess.run(
        ["git", "add", candidate.relative_to(root)],
        cwd=root,
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Factory Test",
            "-c",
            "user.email=factory@example.invalid",
            "commit",
            "-qm",
            "candidate",
        ],
        cwd=root,
        check=True,
    )


def test_certification_loads_one_exact_committed_candidate(tmp_path: Path) -> None:
    candidates = make_candidate_repository(tmp_path)
    candidate = candidates / "linux-x86_64.json"
    commit_candidate(tmp_path, candidate)

    selection = load_committed_selection(tmp_path, candidate)

    assert selection.harness == "codex"
    assert selection.version == "0.155.1"
    assert selection.install_channel == "github-asset"
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
    tmp_path: Path,
    state: str,
) -> None:
    candidates = make_candidate_repository(tmp_path)
    candidate = candidates / "linux-x86_64.json"
    if state == "modified":
        commit_candidate(tmp_path, candidate)
    candidate.write_text(json.dumps(candidate_document("b" * 64)) + "\n")

    with pytest.raises(
        SelectionError,
        match="must exactly match a file committed at HEAD",
    ):
        load_committed_selection(tmp_path, candidate)


def test_certification_refuses_the_retired_schema_one_manifest(
    tmp_path: Path,
) -> None:
    """Exactly one manifest schema exists; schema value 1 cannot select."""

    candidates = make_candidate_repository(tmp_path)
    candidate = candidates / "linux-x86_64.json"
    document = candidate_document()
    document["schema"] = 1
    commit_candidate(tmp_path, candidate, document)

    with pytest.raises(SelectionError, match="schema is not supported"):
        load_committed_selection(tmp_path, candidate)


def test_certification_refuses_a_manifest_with_unrecognized_fields(
    tmp_path: Path,
) -> None:
    candidates = make_candidate_repository(tmp_path)
    candidate = candidates / "linux-x86_64.json"
    document = candidate_document()
    document["schema"] = 1
    document["codex_version"] = document.pop("harness_version")
    commit_candidate(tmp_path, candidate, document)

    with pytest.raises(
        SelectionError,
        match="fields are incomplete or unrecognized",
    ):
        load_committed_selection(tmp_path, candidate)


@pytest.mark.parametrize(
    ("harness", "install_channel"),
    [
        ("cursor", "npm"),
        ("claude", "github-asset"),
        ("codex", "native-installer"),
    ],
)
def test_certification_requires_a_certified_harness_and_channel(
    tmp_path: Path,
    harness: str,
    install_channel: str,
) -> None:
    candidates = make_candidate_repository(tmp_path)
    candidate = candidates / "linux-x86_64.json"
    document = candidate_document()
    document["harness"] = harness
    document["install_channel"] = install_channel
    commit_candidate(tmp_path, candidate, document)

    with pytest.raises(
        SelectionError,
        match="not a certified Harness|not certified for",
    ):
        load_committed_selection(tmp_path, candidate)


@pytest.mark.parametrize(
    ("harness", "install_channel", "official_source"),
    [
        (
            "codex",
            "npm",
            "https://www.npmjs.com/package/@openai/codex-linux-x64",
        ),
        (
            "claude",
            "npm",
            "https://www.npmjs.com/package/@anthropic-ai/claude-code",
        ),
        (
            "claude",
            "native-installer",
            "https://downloads.claude.ai/claude-code-releases/stable/manifest.json",
        ),
    ],
)
def test_certification_selects_every_first_scope_artifact_family(
    tmp_path: Path,
    harness: str,
    install_channel: str,
    official_source: str,
) -> None:
    candidates = make_candidate_repository(tmp_path)
    candidate = candidates / "linux-x86_64.json"
    document = candidate_document()
    document["harness"] = harness
    document["install_channel"] = install_channel
    document["official_source"] = official_source
    commit_candidate(tmp_path, candidate, document)

    selection = load_committed_selection(tmp_path, candidate)

    assert selection.harness == harness
    assert selection.install_channel == install_channel
    assert selection.official_source == official_source


def test_certification_pins_official_source_to_the_artifact_family(
    tmp_path: Path,
) -> None:
    candidates = make_candidate_repository(tmp_path)
    candidate = candidates / "linux-x86_64.json"
    document = candidate_document()
    document["install_channel"] = "npm"
    document["official_source"] = (
        "https://github.com/openai/codex/releases/tag/rust-v0.155.1"
    )
    commit_candidate(tmp_path, candidate, document)

    with pytest.raises(
        SelectionError,
        match="official_source does not name the artifact family",
    ):
        load_committed_selection(tmp_path, candidate)


def test_only_the_certified_linux_tuple_enters_the_production_compatibility_set() -> (
    None
):
    assert len(_codex._COMPATIBILITY_SET) == 1
    policy = _codex._COMPATIBILITY_SET[0]
    assert policy.version == "0.157.1"
    assert policy.sha256 == (
        "3e2584f3f3829a43a0495011a1cecb2facbe64a2403e2b682351fd9c2983f970"
    )
    assert (policy.operating_system, policy.architecture) == ("Linux", "x86_64")
    assert policy.canonical_path == Path(
        "/home/ricardoiarced/.codex/packages/standalone/releases/"
        "0.157.1-x86_64-unknown-linux-musl/bin/codex"
    )
    assert policy.filesystem_identity == (2096, 140479)


def test_every_named_candidate_has_a_recorded_support_decision() -> None:
    results = PROJECT / "certification" / "results"
    records = [path.read_text(encoding="utf-8") for path in results.glob("*.md")]
    for manifest in sorted(
        (PROJECT / "certification" / "candidates").glob("*.json"),
    ):
        path = f"certification/candidates/{manifest.name}"
        decisions = [
            record for record in records if path in record and '"result":"' in record
        ]
        assert decisions, path
