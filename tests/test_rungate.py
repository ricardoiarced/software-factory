"""Shared Run Gate behavior at the identity, Git, runtime, and policy stages."""

import errno
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import FrozenInstanceError, replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

import _claude
import _codex
import _harness
import _rungate
import factory
from _harness import factory_version
from _identity import RunSelection
from _rungate import RunGateError, run_gate
from _runrecord import run_record

CLIENT_ID = "cli_" + "a" * 32
PROJECT_ID = "prj_" + "b" * 32
REPOSITORY_ID = "repo_" + "c" * 32
CHECKOUT_ID = "chk_" + "d" * 32
OTHER_CLIENT_ID = "cli_" + "e" * 32
OTHER_PROJECT_ID = "prj_" + "f" * 32
OTHER_CHECKOUT_ID = "chk_" + "1" * 32
SECOND_REPOSITORY_ID = "repo_" + "2" * 32
PROJECT_TWO_ID = "prj_" + "2" * 32
CHECKOUT_TWO_ID = "chk_" + "3" * 32

_PIN_NAMES = ("worktree_root", "git_dir", "common_dir")


def git(
    *args: str, cwd: Path, check: bool = True
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["git", *args], cwd=cwd, check=check, capture_output=True)


def git_init_commit(root: Path) -> None:
    """Create one non-bare Git repository with a readable commit HEAD."""
    git("init", "-q", cwd=root)
    commit_worktree(root)


def commit_worktree(root: Path) -> None:
    """Stage and commit every fixture file so the assessment sees CLEAN."""
    git("add", "-A", cwd=root)
    git(
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "--allow-empty",
        "-m",
        "fixture",
        "-q",
        cwd=root,
    )


def pin(path: Path) -> dict[str, object]:
    info = path.stat()
    return {"path": str(path), "st_dev": info.st_dev, "st_ino": info.st_ino}


def git_proof_pins(root: Path) -> dict[str, dict[str, object]]:
    """Pin the worktree root, Git directory, and common dir Git resolves."""
    result = git(
        "-C",
        str(root),
        "rev-parse",
        "--show-toplevel",
        "--absolute-git-dir",
        "--git-common-dir",
        cwd=root,
    )
    paths = []
    for line in result.stdout.splitlines():
        text = os.fsdecode(line)
        resolved = Path(text) if os.path.isabs(text) else root / text
        paths.append(resolved.resolve(strict=True))
    return {name: pin(path) for name, path in zip(_PIN_NAMES, paths)}


def register_checkout(root: Path, config: Path) -> None:
    """Register one real Git worktree as the acme/web Checkout of a v2 store."""
    proof = git_proof_pins(root)
    document = {
        "version": 2,
        "clients": [{"id": CLIENT_ID, "key": "acme", "name": None}],
        "projects": [
            {
                "id": PROJECT_ID,
                "client_id": CLIENT_ID,
                "key": "web",
                "name": None,
                "default_checkout_id": CHECKOUT_ID,
            }
        ],
        "repositories": [
            {"id": REPOSITORY_ID, "name": None, "common_dir": proof["common_dir"]}
        ],
        "checkouts": [
            {
                "id": CHECKOUT_ID,
                "project_id": PROJECT_ID,
                "repository_id": REPOSITORY_ID,
                "boundary": proof["worktree_root"],
                "worktree_root": proof["worktree_root"],
                "git_dir": proof["git_dir"],
                "common_dir": proof["common_dir"],
            }
        ],
        "legacy": {},
    }
    registry = config / "factory" / "projects.json"
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text(json.dumps(document))


def registered_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path]:
    """One committed worktree registered as acme/web with its store outside."""
    root = tmp_path / "worktree"
    root.mkdir()
    git_init_commit(root)
    config = tmp_path / "config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    register_checkout(root, config)
    return root, config


def read_store(config: Path) -> dict[str, object]:
    return json.loads(
        (config / "factory" / "projects.json").read_text(encoding="utf-8")
    )


def write_store(config: Path, document: dict[str, object]) -> None:
    (config / "factory" / "projects.json").write_text(
        json.dumps(document), encoding="utf-8"
    )


def record_documents(config: Path) -> list[dict[str, object]]:
    runs = config / "factory" / "runs"
    return [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(runs.glob("run_*.json"))
    ]


def run_to_harness(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
    status: int,
    *arguments: str,
) -> tuple[list[Path], list[bytes]]:
    """Wire Factory to a recording Runner stand-in and aim argv at root."""
    requests: list[bytes] = []
    calls: list[Path] = []

    def handoff(
        project: factory.Project,
        spool: object,
        gate: object,
        *,
        re_probe: bool = False,
    ) -> int:
        calls.append(project.boundary)
        requests.append(spool.read())
        return status

    monkeypatch.setattr(
        factory, "select_harness", lambda _name: SimpleNamespace(run=handoff)
    )
    monkeypatch.setattr(sys, "argv", ["factory", *arguments])
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"")))
    monkeypatch.chdir(root)
    return calls, requests


def direct_selection(root: Path) -> RunSelection:
    """The recorded selection of the registered acme/web Checkout."""
    return RunSelection(
        client_id=CLIENT_ID,
        project_id=PROJECT_ID,
        repository_id=REPOSITORY_ID,
        checkout_id=CHECKOUT_ID,
        boundary=root,
    )


def gate_verdict(
    config: Path, selection: RunSelection, harness: str = "codex"
) -> tuple[dict[str, object], RunGateError | None]:
    """Run the gate directly against one store and return its record outcome."""

    with run_record(["factory", "Fix"], harness) as record:
        error: RunGateError | None = None
        try:
            run_gate(record, selection, harness)
        except RunGateError as refusal:
            error = refusal
    document = next(
        entry for entry in record_documents(config) if entry["id"] == record.identifier
    )
    return document, error


def policy_evidence(
    config: Path, selection: RunSelection, harness: str = "codex"
) -> dict[str, object]:
    """Run the gate for one selection and return its committed policy evidence."""

    document, error = gate_verdict(config, selection, harness)
    assert error is None
    gate = document["gate"]
    assert isinstance(gate, dict)
    policy = gate["policy"]
    assert isinstance(policy, dict)
    return policy


def revision(root: Path) -> str:
    result = git("rev-parse", "HEAD", cwd=root)
    return os.fsdecode(result.stdout.strip())


def without_override(items: list[str], key: str) -> list[str]:
    """Drop one certified ``-c`` override pair from the certified item list."""

    overridden = [
        index for index, item in enumerate(items) if item.startswith(f"{key}=")
    ]
    drop = {index - 1 for index in overridden} | set(overridden)
    return [item for index, item in enumerate(items) if index not in drop]


def test_the_run_gate_runs_between_the_context_gate_and_run_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, config = registered_checkout(tmp_path, monkeypatch)
    (root / "context").write_bytes(b"ab")
    commit_worktree(root)
    monkeypatch.setenv("FACTORY_CONTEXT_WARN_BYTES", "1")
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"")))
    monkeypatch.chdir(root)

    monkeypatch.setattr(
        sys,
        "argv",
        ["factory", "--checkout", str(root), "--file", "context", "Fix"],
    )
    assert factory.main() == 1
    first = record_documents(config)[-1]
    assert first["refusal"]["stage"] == "context-gate"
    assert "gate" not in first

    monkeypatch.setattr(
        sys,
        "argv",
        ["factory", "--checkout", str(root), "--file", "context", "--force", "Fix"],
    )
    assert factory.main() == 1
    second = next(
        entry for entry in record_documents(config) if entry["id"] != first["id"]
    )
    assert "gate" in second
    assert second["refusal"]["stage"] == "admission"
    assert "context-gate" not in second["refusal"]["detail"]


def test_a_gate_refusal_refuses_the_run_without_reaching_the_harness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, config = registered_checkout(tmp_path, monkeypatch)
    (root / "tracked.txt").write_bytes(b"moved\n")
    git("add", "tracked.txt", cwd=root)
    git("update-index", "--skip-worktree", "tracked.txt", cwd=root)

    calls, _ = run_to_harness(monkeypatch, root, 0, "--checkout", str(root), "Fix")
    assert factory.main() == 1
    assert calls == []
    assert capsys.readouterr().err.startswith("factory: error: ")
    document = record_documents(config)[-1]
    assert document["status"] == "refused"
    assert document["refusal"]["stage"] == "git"


def test_duplicate_repository_common_directory_pins_refuse_as_store_corruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, config = registered_checkout(tmp_path, monkeypatch)
    document = read_store(config)
    document["repositories"].append(
        {
            "id": SECOND_REPOSITORY_ID,
            "name": None,
            "common_dir": document["repositories"][0]["common_dir"],
        }
    )
    write_store(config, document)

    calls, _ = run_to_harness(monkeypatch, root, 0, "--checkout", str(root), "Fix")
    assert factory.main() == 1
    assert calls == []
    stderr = capsys.readouterr().err
    assert SECOND_REPOSITORY_ID in stderr
    assert "pin one Git common directory" in stderr
    assert "more than one Client" not in stderr
    refusal = record_documents(config)[-1]["refusal"]
    assert refusal["stage"] == "store-graph"


@pytest.mark.parametrize("pinned_component", ["boundary", "git_dir"])
def test_duplicate_checkout_pins_refuse_as_store_corruption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    pinned_component: str,
) -> None:
    root, config = registered_checkout(tmp_path, monkeypatch)
    document = read_store(config)
    original = dict(document["checkouts"][0])
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    git_init_commit(unrelated)
    other_pins = git_proof_pins(unrelated)
    twin = {
        "id": OTHER_CHECKOUT_ID,
        "project_id": PROJECT_ID,
        "repository_id": REPOSITORY_ID,
        "boundary": original["boundary"],
        "worktree_root": original["worktree_root"],
        "git_dir": original["git_dir"],
        "common_dir": original["common_dir"],
    }
    if pinned_component == "git_dir":
        twin["boundary"] = other_pins["worktree_root"]
        twin["worktree_root"] = other_pins["worktree_root"]
    document["checkouts"].append(twin)
    write_store(config, document)

    selection = direct_selection(root)
    document_after, error = gate_verdict(config, selection)
    assert error is not None
    refusal = document_after["refusal"]
    assert refusal["stage"] == "store-graph"
    if pinned_component == "boundary":
        assert "pin the same worktree boundary" in refusal["detail"]
    else:
        assert "pin the same worktree Git directory" in refusal["detail"]


@pytest.mark.parametrize("dangling", ["project", "client", "repository"])
def test_dangling_identity_references_refuse_as_store_corruption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    dangling: str,
) -> None:
    root, config = registered_checkout(tmp_path, monkeypatch)
    document = read_store(config)
    if dangling == "project":
        document["projects"] = []
    elif dangling == "client":
        document["clients"] = []
    else:
        document["checkouts"][0]["repository_id"] = "repo_" + "9" * 32
    write_store(config, document)

    document_after, error = gate_verdict(config, direct_selection(root))
    assert error is not None
    refusal = document_after["refusal"]
    assert refusal["stage"] == "store-graph"
    assert "the identity store is corrupt" in refusal["detail"]


def test_cross_client_corruption_names_the_cross_client_condition_prominently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, config = registered_checkout(tmp_path, monkeypatch)
    document = read_store(config)
    document["repositories"].append(
        {
            "id": SECOND_REPOSITORY_ID,
            "name": None,
            "common_dir": document["repositories"][0]["common_dir"],
        }
    )
    document["clients"].append({"id": OTHER_CLIENT_ID, "key": "beta", "name": None})
    document["projects"].append(
        {
            "id": OTHER_PROJECT_ID,
            "client_id": OTHER_CLIENT_ID,
            "key": "other",
            "name": None,
            "default_checkout_id": None,
        }
    )
    document["checkouts"].append(
        {
            "id": OTHER_CHECKOUT_ID,
            "project_id": OTHER_PROJECT_ID,
            "repository_id": SECOND_REPOSITORY_ID,
            "boundary": document["checkouts"][0]["boundary"],
            "worktree_root": document["checkouts"][0]["worktree_root"],
            "git_dir": document["checkouts"][0]["git_dir"],
            "common_dir": document["checkouts"][0]["common_dir"],
        }
    )
    write_store(config, document)

    document_after, error = gate_verdict(config, direct_selection(root))
    assert error is not None
    refusal = document_after["refusal"]
    assert refusal["stage"] == "store-graph"
    assert (
        "cross-Client Project Data Isolation cannot be established" in refusal["detail"]
    )
    assert "(acme, beta)" in refusal["detail"]


def test_an_absent_selected_checkout_refuses_at_the_scope_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, config = registered_checkout(tmp_path, monkeypatch)
    selection = RunSelection(
        client_id=CLIENT_ID,
        project_id=PROJECT_ID,
        repository_id=REPOSITORY_ID,
        checkout_id="chk_" + "9" * 32,
        boundary=root,
    )

    document_after, error = gate_verdict(config, selection)
    assert error is not None
    refusal = document_after["refusal"]
    assert refusal["stage"] == "scope"
    assert "absent from the identity store" in refusal["detail"]


def test_a_recorded_selection_no_longer_matching_the_store_refuses_at_the_scope_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, config = registered_checkout(tmp_path, monkeypatch)
    selection = RunSelection(
        client_id=CLIENT_ID,
        project_id=PROJECT_TWO_ID,
        repository_id=REPOSITORY_ID,
        checkout_id=CHECKOUT_ID,
        boundary=root,
    )

    document_after, error = gate_verdict(config, selection)
    assert error is not None
    refusal = document_after["refusal"]
    assert refusal["stage"] == "scope"
    assert "no longer matches the identity store" in refusal["detail"]


def test_a_repository_bound_to_two_clients_refuses_with_the_store_detected_diagnostic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, config = registered_checkout(tmp_path, monkeypatch)
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    git_init_commit(unrelated)
    other_pins = git_proof_pins(unrelated)
    document = read_store(config)
    document["clients"].append({"id": OTHER_CLIENT_ID, "key": "beta", "name": None})
    document["projects"].append(
        {
            "id": OTHER_PROJECT_ID,
            "client_id": OTHER_CLIENT_ID,
            "key": "other",
            "name": None,
            "default_checkout_id": None,
        }
    )
    document["checkouts"].append(
        {
            "id": OTHER_CHECKOUT_ID,
            "project_id": OTHER_PROJECT_ID,
            "repository_id": REPOSITORY_ID,
            "boundary": other_pins["worktree_root"],
            "worktree_root": other_pins["worktree_root"],
            "git_dir": other_pins["git_dir"],
            "common_dir": other_pins["common_dir"],
        }
    )
    write_store(config, document)

    calls, _ = run_to_harness(monkeypatch, root, 0, "--checkout", str(root), "Fix")
    assert factory.main() == 1
    assert calls == []
    assert capsys.readouterr().err == (
        f"factory: error: Repository {REPOSITORY_ID} is bound to Checkouts of "
        "more than one Client (acme, beta); a Git common directory is not "
        "partitioned by Client, so Project Data Isolation cannot be proven and "
        "cross-Client shared Repositories never admit Runs.\n"
    )
    refusal = record_documents(config)[-1]["refusal"]
    assert refusal["stage"] == "association"
    assert refusal["facts"] == {
        "repository_id": REPOSITORY_ID,
        "clients": "acme, beta",
    }


def test_a_retained_unreferenced_repository_is_not_foreign_to_the_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, config = registered_checkout(tmp_path, monkeypatch)
    linked = tmp_path / "linked"
    git("worktree", "add", "-q", "-b", "linked", str(linked), cwd=root)
    retained = tmp_path / "retained"
    retained.mkdir()
    git_init_commit(retained)
    document = read_store(config)
    shared_pin = document["repositories"][0]["common_dir"]
    document["projects"].append(
        {
            "id": PROJECT_TWO_ID,
            "client_id": CLIENT_ID,
            "key": "docs",
            "name": None,
            "default_checkout_id": None,
        }
    )
    document["repositories"].append(
        {
            "id": SECOND_REPOSITORY_ID,
            "name": None,
            "common_dir": git_proof_pins(retained)["common_dir"],
        }
    )
    document["checkouts"].append(
        {
            "id": CHECKOUT_TWO_ID,
            "project_id": PROJECT_TWO_ID,
            "repository_id": REPOSITORY_ID,
            "boundary": git_proof_pins(linked)["worktree_root"],
            "worktree_root": git_proof_pins(linked)["worktree_root"],
            "git_dir": git_proof_pins(linked)["git_dir"],
            "common_dir": shared_pin,
        }
    )
    write_store(config, document)

    calls, _ = run_to_harness(monkeypatch, linked, 0, "--checkout", str(linked), "Fix")
    assert factory.main() == 0
    assert calls == [linked]
    gate = record_documents(config)[-1]["gate"]
    assert gate["head"] == revision(linked)


def test_the_gate_records_the_fresh_proof_pins_head_and_object_format(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, config = registered_checkout(tmp_path, monkeypatch)

    run_to_harness(monkeypatch, root, 0, "--checkout", str(root), "Fix")
    assert factory.main() == 0
    gate = record_documents(config)[-1]["gate"]
    proof = git_proof_pins(root)
    assert gate["head"] == revision(root)
    assert gate["object_format"] == "sha1"
    assert gate["proof"] == proof
    assert gate["git"]["outcome"] == "clean"


def test_an_unreadable_head_refuses_at_the_proof_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "worktree"
    root.mkdir()
    git("init", "-q", cwd=root)
    config = tmp_path / "config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    register_checkout(root, config)

    document_after, error = gate_verdict(config, direct_selection(root))
    assert error is not None
    refusal = document_after["refusal"]
    assert refusal["stage"] == "proof"
    assert "no readable commit HEAD" in refusal["detail"]


def test_gate_git_inspection_ignores_repository_location_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, config = registered_checkout(tmp_path, monkeypatch)
    proof = git_proof_pins(root)
    head = revision(root)
    monkeypatch.setenv("GIT_DIR", str(root / ".git" / "refs"))
    monkeypatch.setenv("GIT_WORK_TREE", str(tmp_path))
    monkeypatch.setenv("GIT_COMMON_DIR", str(tmp_path / "elsewhere"))
    monkeypatch.setenv("GIT_OBJECT_DIRECTORY", str(tmp_path / "objects"))
    monkeypatch.setenv("GIT_INDEX_FILE", str(tmp_path / "index"))

    run_to_harness(monkeypatch, root, 0, "--checkout", str(root), "Fix")
    assert factory.main() == 0
    gate = record_documents(config)[-1]["gate"]
    assert gate["proof"] == proof
    assert gate["head"] == head


def test_drifted_checkout_pins_refuse_with_re_registration_guidance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, config = registered_checkout(tmp_path, monkeypatch)
    replaced = tmp_path / "replaced"
    root.rename(replaced)
    root.mkdir()
    git_init_commit(root)

    calls, _ = run_to_harness(monkeypatch, root, 0, "--checkout", str(root), "Fix")
    assert factory.main() == 1
    assert calls == []
    stderr = capsys.readouterr().err
    assert "is stale" in stderr
    assert "requires explicit re-registration" in stderr
    refusal = record_documents(config)[-1]["refusal"]
    assert refusal["stage"] == "drift"
    assert refusal["facts"] == {"checkout_id": CHECKOUT_ID}


def test_drift_into_a_foreign_repository_refuses_with_the_drift_detected_diagnostic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, config = registered_checkout(tmp_path, monkeypatch)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    git_init_commit(elsewhere)
    drifted_pin = git_proof_pins(elsewhere)["common_dir"]
    real_common = read_store(config)["repositories"][0]["common_dir"]
    document = read_store(config)
    document["repositories"][0]["common_dir"] = drifted_pin
    document["repositories"].append(
        {"id": SECOND_REPOSITORY_ID, "name": None, "common_dir": real_common}
    )
    document["clients"].append({"id": OTHER_CLIENT_ID, "key": "beta", "name": None})
    document["projects"].append(
        {
            "id": OTHER_PROJECT_ID,
            "client_id": OTHER_CLIENT_ID,
            "key": "other",
            "name": None,
            "default_checkout_id": None,
        }
    )
    document["checkouts"].append(
        {
            "id": OTHER_CHECKOUT_ID,
            "project_id": OTHER_PROJECT_ID,
            "repository_id": SECOND_REPOSITORY_ID,
            "boundary": drifted_pin,
            "worktree_root": drifted_pin,
            "git_dir": drifted_pin,
            "common_dir": drifted_pin,
        }
    )
    write_store(config, document)

    calls, _ = run_to_harness(monkeypatch, root, 0, "--checkout", str(root), "Fix")
    assert factory.main() == 1
    assert calls == []
    assert capsys.readouterr().err == (
        f"factory: error: Checkout {CHECKOUT_ID} now resolves to the Git common "
        f"directory of Repository {SECOND_REPOSITORY_ID} bound to another "
        "Client; a shared common directory never admits Runs; restore the "
        "Checkout to its expected Repository or explicitly repair its "
        "registration.\n"
    )
    refusal = record_documents(config)[-1]["refusal"]
    assert refusal["stage"] == "drift"
    assert refusal["facts"] == {
        "checkout_id": CHECKOUT_ID,
        "repository_id": SECOND_REPOSITORY_ID,
    }


def test_repository_pin_drift_to_an_unregistered_directory_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, config = registered_checkout(tmp_path, monkeypatch)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    git_init_commit(elsewhere)
    document = read_store(config)
    document["repositories"][0]["common_dir"] = git_proof_pins(elsewhere)["common_dir"]
    write_store(config, document)

    calls, _ = run_to_harness(monkeypatch, root, 0, "--checkout", str(root), "Fix")
    assert factory.main() == 1
    assert calls == []
    stderr = capsys.readouterr().err
    assert "repository re-register" in stderr
    assert REPOSITORY_ID in stderr
    refusal = record_documents(config)[-1]["refusal"]
    assert refusal["stage"] == "drift"
    assert refusal["facts"] == {
        "checkout_id": CHECKOUT_ID,
        "repository_id": REPOSITORY_ID,
    }


def test_a_changed_head_passes_the_gate_and_is_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, config = registered_checkout(tmp_path, monkeypatch)
    before = revision(root)
    (root / "next.txt").write_bytes(b"next\n")
    commit_worktree(root)
    after = revision(root)
    assert before != after

    run_to_harness(monkeypatch, root, 0, "--checkout", str(root), "Fix")
    assert factory.main() == 0
    gate = record_documents(config)[-1]["gate"]
    assert gate["head"] == after


def test_the_assessment_records_categories_and_excludes_ignored_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, config = registered_checkout(tmp_path, monkeypatch)
    (root / ".gitignore").write_text("ignored/\n")
    commit_worktree(root)
    original = os.fsdecode(
        git("symbolic-ref", "--short", "HEAD", cwd=root).stdout.strip()
    )
    git("checkout", "-q", "-b", "feature", cwd=root)
    (root / "f.txt").write_text("b\n")
    commit_worktree(root)
    git("checkout", "-q", original, cwd=root)
    (root / "f.txt").write_text("a\n")
    commit_worktree(root)
    git(
        "-c",
        "user.name=Test",
        "-c",
        "user.email=t@e",
        "merge",
        "feature",
        cwd=root,
        check=False,
    )
    (root / "added.txt").write_bytes(b"staged\n")
    git("add", "added.txt", cwd=root)
    (root / ".gitignore").write_text("ignored/\nextra\n")
    (root / "rogue.txt").write_bytes(b"untracked\n")
    (root / "ignored").mkdir()
    (root / "ignored" / "log.txt").write_bytes(b"ignored\n")

    calls, _ = run_to_harness(monkeypatch, root, 0, "--checkout", str(root), "Fix")
    assert factory.main() == 0
    assert len(calls) == 1
    assessment = record_documents(config)[-1]["gate"]["git"]
    assert assessment["outcome"] == "dirty"
    assert assessment["staged"] == ["added.txt"]
    assert assessment["unstaged"] == [".gitignore"]
    assert assessment["untracked"] == ["rogue.txt"]
    assert assessment["conflicts"] == ["f.txt"]
    assert "ignored/log.txt" not in json.dumps(assessment)
    warning = capsys.readouterr().err
    assert "ignored/log.txt" not in warning
    assert '"rogue.txt"' in warning
    assert "is dirty at HEAD" in warning


def test_required_submodule_evidence_covers_the_three_commit_identities(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    leaf = tmp_path / "leaf"
    leaf.mkdir()
    git_init_commit(leaf)
    (leaf / "leaf.txt").write_bytes(b"leaf\n")
    commit_worktree(leaf)
    inner = tmp_path / "inner"
    inner.mkdir()
    git_init_commit(inner)
    git(
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        "-q",
        str(leaf),
        "inner-leaf",
        cwd=inner,
    )
    commit_worktree(inner)
    root, config = registered_checkout(tmp_path, monkeypatch)
    git(
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        "-q",
        str(inner),
        "inner-sub",
        cwd=root,
    )
    git(
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "update",
        "--init",
        "inner-leaf",
        cwd=root / "inner-sub",
    )
    commit_worktree(root)
    (root / "inner-sub" / "inner-leaf" / "leaf.txt").write_bytes(b"changed\n")
    (root / "inner-sub" / "inner.txt").write_bytes(b"next\n")
    commit_worktree(root / "inner-sub")
    git("add", "inner-sub", cwd=root)

    run_to_harness(monkeypatch, root, 0, "--checkout", str(root), "Fix")
    assert factory.main() == 0
    submodules = record_documents(config)[-1]["gate"]["git"]["submodules"]
    assert len(submodules) == 1
    sub = submodules[0]
    assert sub["path"] == "inner-sub"
    assert sub["staged_gitlink_change"] is True
    assert sub["checkout_commit_mismatch"] is False
    assert sub["checkout_head"] == sub["index_gitlink"]
    assert sub["head_gitlink"] != sub["index_gitlink"]
    assert sub["changes"] == ["inner-leaf"]
    nested = sub["submodules"][0]
    assert nested["path"] == "inner-leaf"
    assert nested["staged_gitlink_change"] is False
    assert nested["checkout_commit_mismatch"] is False
    assert nested["head_gitlink"] == nested["index_gitlink"]
    assert nested["checkout_head"] == nested["index_gitlink"]
    assert nested["changes"] == ["leaf.txt"]


@pytest.mark.parametrize("missing", ["deleted", "uninitialized"])
def test_a_tracked_submodule_deletion_is_dirty_and_an_uninitialized_one_refuses_as_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    leaf = tmp_path / "leaf"
    leaf.mkdir()
    git_init_commit(leaf)
    root, config = registered_checkout(tmp_path, monkeypatch)
    git(
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        "-q",
        str(leaf),
        "sub",
        cwd=root,
    )
    commit_worktree(root)
    pointer = root / "sub" / ".git"
    if missing == "deleted":
        shutil.rmtree(root / "sub")
    elif pointer.is_dir():
        shutil.rmtree(pointer)
    else:
        pointer.unlink()

    if missing == "deleted":
        calls, _ = run_to_harness(monkeypatch, root, 0, "--checkout", str(root), "Fix")
        assert factory.main() == 0
        submodules = record_documents(config)[-1]["gate"]["git"]["submodules"]
        assert submodules[0]["path"] == "sub"
        assert submodules[0]["deleted"] is True
        assert "checkout_head" not in submodules[0]
    else:
        calls, _ = run_to_harness(monkeypatch, root, 0, "--checkout", str(root), "Fix")
        assert factory.main() == 1
        assert calls == []
        refusal = record_documents(config)[-1]["refusal"]
        assert refusal["stage"] == "git"
        assert "sub" in refusal["detail"]
        assert "not an initialized Git repository" in refusal["detail"]
        assert "dirty" not in refusal["detail"]
        assert "clean" not in refusal["detail"]


@pytest.mark.parametrize("flag", ["skip-worktree", "assume-unchanged"])
def test_skip_worktree_and_assume_unchanged_entries_refuse_as_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flag: str
) -> None:
    root, config = registered_checkout(tmp_path, monkeypatch)
    (root / "tracked.txt").write_bytes(b"original\n")
    commit_worktree(root)
    (root / "tracked.txt").write_bytes(b"moved\n")
    git("update-index", f"--{flag}", "tracked.txt", cwd=root)

    calls, _ = run_to_harness(monkeypatch, root, 0, "--checkout", str(root), "Fix")
    assert factory.main() == 1
    assert calls == []
    refusal = record_documents(config)[-1]["refusal"]
    assert refusal["stage"] == "git"
    assert "tracked.txt" in refusal["detail"]
    assert "skip-worktree or assume-unchanged" in refusal["detail"]
    assert "clean" not in refusal["detail"]
    assert "dirty" not in refusal["detail"]


@pytest.mark.parametrize("failure", ["timeout", "failed"])
def test_a_timed_out_or_failed_inspection_refuses_as_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    root, config = registered_checkout(tmp_path, monkeypatch)
    if failure == "timeout":
        real_git = shutil.which("git")
        assert real_git is not None
        slow_bin = tmp_path / "slow-bin"
        slow_bin.mkdir()
        fake_git = slow_bin / "git"
        fake_git.write_text(
            f'#!/bin/sh\nif [ "$4" = "ls-files" ]; then sleep 2; fi\n'
            f'exec {real_git} "$@"\n'
        )
        fake_git.chmod(0o755)
        monkeypatch.setenv("PATH", f"{slow_bin}:{os.environ['PATH']}")
        monkeypatch.setattr(_rungate, "_GIT_TIMEOUT_SECONDS", 0.5)
    else:
        (root / ".git" / "index").write_bytes(b"garbage")

    calls, _ = run_to_harness(monkeypatch, root, 0, "--checkout", str(root), "Fix")
    assert factory.main() == 1
    assert calls == []
    refusal = record_documents(config)[-1]["refusal"]
    assert refusal["stage"] == "git"
    if failure == "timeout":
        assert "timed out" in refusal["detail"]
    else:
        assert "git ls-files failed with exit status 128" in refusal["detail"]
    assert "clean" not in refusal["detail"]
    assert "dirty" not in refusal["detail"]


def test_proof_or_head_changes_during_assessment_refuse_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, config = registered_checkout(tmp_path, monkeypatch)
    assessment = _rungate._assess_tree

    def assess_then_move(worktree: Path) -> _rungate._TreeStatus:
        status = assessment(worktree)
        git(
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "--allow-empty",
            "-m",
            "moved during assessment",
            "-q",
            cwd=root,
        )
        return status

    monkeypatch.setattr(_rungate, "_assess_tree", assess_then_move)

    calls, _ = run_to_harness(monkeypatch, root, 0, "--checkout", str(root), "Fix")
    assert factory.main() == 1
    assert calls == []
    refusal = record_documents(config)[-1]["refusal"]
    assert refusal["stage"] == "git"
    assert "changed during its Git Assessment" in refusal["detail"]


def test_the_gate_evidence_records_the_observation_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, config = registered_checkout(tmp_path, monkeypatch)

    run_to_harness(monkeypatch, root, 0, "--checkout", str(root), "Fix")
    assert factory.main() == 0
    gate = record_documents(config)[-1]["gate"]
    observed = datetime.fromisoformat(gate["at"])
    assert observed.tzinfo is not None
    assert observed.year >= 2026


def test_a_dirty_worktree_warns_the_identities_and_proceeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, config = registered_checkout(tmp_path, monkeypatch)
    (root / "added.txt").write_bytes(b"staged\n")
    git("add", "added.txt", cwd=root)
    (root / "rogue.txt").write_bytes(b"untracked\n")
    head = revision(root)

    calls, _ = run_to_harness(monkeypatch, root, 5, "--checkout", str(root), "Fix")
    assert factory.main() == 5
    assert calls == [root]
    assert capsys.readouterr().err == (
        f"factory: warning: Checkout {CHECKOUT_ID} (client {CLIENT_ID}, project "
        f"{PROJECT_ID}, repository {REPOSITORY_ID}) at {_rungate._quoted(str(root))} "
        f'is dirty at HEAD {head}: staged: "added.txt"; untracked: "rogue.txt". '
        "Ignored paths are excluded, and the observed working tree is not fully "
        "represented by HEAD.\n"
    )
    assessment = record_documents(config)[-1]["gate"]["git"]
    assert assessment["outcome"] == "dirty"


def test_the_gate_extends_the_fixed_stage_order_with_the_policy_evidence_stages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, config = registered_checkout(tmp_path, monkeypatch)

    policy = policy_evidence(config, direct_selection(root))
    assert list(policy) == [
        "at",
        "runtime",
        "instructions",
        "sandbox",
        "network",
        "environment",
    ]
    for stage in ("runtime", "instructions", "sandbox", "network"):
        document = policy[stage]
        assert isinstance(document, dict)
        assert document["harness"] == "codex"
        assert document["provenance"] == "configured"
    environment = policy["environment"]
    assert isinstance(environment, dict)
    assert environment["harness"] == "codex"
    assert environment["provenance"] == "observed"


def test_the_runtime_stage_records_the_declared_certified_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, config = registered_checkout(tmp_path, monkeypatch)

    runtime = policy_evidence(config, direct_selection(root))["runtime"]
    assert isinstance(runtime, dict)
    assert runtime["harness"] == "codex"
    assert runtime["factory_version"] == factory_version()
    assert runtime["certified"] == [
        {
            "version": policy.version,
            "channel": policy.install_channel,
            "os": policy.operating_system,
            "arch": policy.architecture,
            "sha256": policy.sha256,
        }
        for policy in _codex._COMPATIBILITY_SET
    ]


def test_a_harness_with_no_certified_runtime_refuses_at_the_runtime_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, config = registered_checkout(tmp_path, monkeypatch)
    monkeypatch.setattr("_codex._COMPATIBILITY_SET", ())

    document, error = gate_verdict(config, direct_selection(root))
    assert error is not None
    refusal = document["refusal"]
    assert refusal["stage"] == "runtime"
    assert "declares no certified runtime" in refusal["detail"]
    assert "UNKNOWN" in refusal["detail"]


def test_an_unregistered_harness_refuses_at_the_runtime_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, config = registered_checkout(tmp_path, monkeypatch)

    document, error = gate_verdict(config, direct_selection(root), harness="vim")
    assert error is not None
    refusal = document["refusal"]
    assert refusal["stage"] == "runtime"
    assert "not a registered Runner" in refusal["detail"]
    assert "UNKNOWN" in refusal["detail"]


@pytest.mark.parametrize("harness", ["codex", "claude"])
def test_the_instructions_stage_records_the_certified_instruction_controls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, harness: str
) -> None:
    root, config = registered_checkout(tmp_path, monkeypatch)

    instructions = policy_evidence(config, direct_selection(root), harness)[
        "instructions"
    ]
    assert isinstance(instructions, dict)
    controls = instructions["controls"]
    assert isinstance(controls, dict)
    assert controls["project_spec"] == {
        "path": ".agent/spec.md",
        "scope": "boundary-only",
    }
    if harness == "codex":
        assert controls["discovered_guidance"] == {
            "project_doc_max_bytes": "0",
            "project_root_markers": "[]",
            "skills.include_instructions": "false",
        }
        assert controls["flags"] == [
            "--ephemeral",
            "--strict-config",
            "--ignore-user-config",
            "--ignore-rules",
        ]
    else:
        assert controls["flags"] == [
            "-p",
            "--restricted",
            "--safe-mode",
            "--strict-mcp-config",
        ]
        assert controls["settings_source"] == "inline"


@pytest.mark.parametrize("harness", ["codex", "claude"])
def test_a_missing_instruction_control_refuses_as_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, harness: str
) -> None:
    root, config = registered_checkout(tmp_path, monkeypatch)
    if harness == "codex":
        configuration = _codex._Codex01600Policy._configuration

        def without_project_doc(self: object, enclosing_git_root: Path) -> list[str]:
            return without_override(
                configuration(self, enclosing_git_root), "project_doc_max_bytes"
            )

        monkeypatch.setattr(
            _codex._Codex01600Policy, "_configuration", without_project_doc
        )
    else:
        invocation = _claude._ClaudePolicy._invocation

        def without_restricted(self: object, boundary: Path) -> list[str]:
            return [
                item for item in invocation(self, boundary) if item != "--restricted"
            ]

        monkeypatch.setattr(_claude._ClaudePolicy, "_invocation", without_restricted)

    document, error = gate_verdict(config, direct_selection(root), harness)
    assert error is not None
    refusal = document["refusal"]
    assert refusal["stage"] == "instructions"
    assert "cannot be established as version-bound" in refusal["detail"]
    assert "UNKNOWN" in refusal["detail"]


@pytest.mark.parametrize("harness", ["codex", "claude"])
def test_the_sandbox_stage_records_the_certified_permission_controls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, harness: str
) -> None:
    root, config = registered_checkout(tmp_path, monkeypatch)

    sandbox = policy_evidence(config, direct_selection(root), harness)["sandbox"]
    assert isinstance(sandbox, dict)
    controls = sandbox["controls"]
    assert isinstance(controls, dict)
    if harness == "codex":
        assert controls["approval_policy"] == '"never"'
        assert controls["allow_login_shell"] == "false"
        assert controls["default_permissions"] == '"factory-isolated"'
        profile = controls["permission_profile"]
        assert isinstance(profile, str)
        assert "network = {enabled = false}" in profile
        assert ":workspace_roots" in profile
        assert controls["workspace_trust_level"] == '"untrusted"'
    else:
        assert controls["tools"] == _claude._ClaudePolicy._admitted_tools
        assert controls["permission_mode"] == "dontAsk"
        assert controls["sandbox_enabled"] is True
        assert controls["fail_if_unavailable"] is True
        assert controls["allow_unsandboxed_commands"] is False
        assert controls["excluded_commands"] == []
        read_fence = controls["read_fence"]
        assert isinstance(read_fence, dict)
        assert str(root) in read_fence["allow_read"]
        assert str(root.parent) in read_fence["deny_read"]
        credentials = controls["credential_denials"]
        assert isinstance(credentials, list)
        assert str(Path.home() / ".ssh") in credentials
        assert str(Path.home() / ".aws") in credentials


@pytest.mark.parametrize("harness", ["codex", "claude"])
def test_a_missing_or_contradicted_sandbox_control_refuses_as_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, harness: str
) -> None:
    root, config = registered_checkout(tmp_path, monkeypatch)
    if harness == "codex":
        configuration = _codex._Codex01600Policy._configuration

        def without_permissions(self: object, enclosing_git_root: Path) -> list[str]:
            return without_override(
                configuration(self, enclosing_git_root), "default_permissions"
            )

        monkeypatch.setattr(
            _codex._Codex01600Policy, "_configuration", without_permissions
        )
    else:
        document_builder = _claude._ClaudePolicy._settings_document

        def disabled_sandbox(self: object, boundary: Path) -> str:
            settings = json.loads(document_builder(self, boundary))
            assert isinstance(settings, dict)
            sandbox = settings["sandbox"]
            assert isinstance(sandbox, dict)
            sandbox["enabled"] = False
            return json.dumps(settings, ensure_ascii=True, sort_keys=True)

        monkeypatch.setattr(
            _claude._ClaudePolicy, "_settings_document", disabled_sandbox
        )

    document, error = gate_verdict(config, direct_selection(root), harness)
    assert error is not None
    refusal = document["refusal"]
    assert refusal["stage"] == "sandbox"
    assert "cannot be established as version-bound" in refusal["detail"]
    assert "UNKNOWN" in refusal["detail"]


@pytest.mark.parametrize("harness", ["codex", "claude"])
def test_the_network_stage_records_the_certified_network_controls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, harness: str
) -> None:
    root, config = registered_checkout(tmp_path, monkeypatch)

    network = policy_evidence(config, direct_selection(root), harness)["network"]
    assert isinstance(network, dict)
    controls = network["controls"]
    assert isinstance(controls, dict)
    if harness == "codex":
        assert controls == {"sandbox_command_network": "disabled"}
    else:
        assert controls["sandbox_allowlist"] == {"strict": True, "allow": []}
        assert controls["nonessential_traffic_pins"] == [
            name for name, _ in _claude._ENVIRONMENT_PINS
        ]


@pytest.mark.parametrize("harness", ["codex", "claude"])
def test_a_missing_network_control_refuses_as_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, harness: str
) -> None:
    root, config = registered_checkout(tmp_path, monkeypatch)
    if harness == "codex":
        profile_builder = _codex._Codex01600Policy._permission_profile

        def enabled_network(self: object) -> str:
            return profile_builder(self).replace(
                "network = {enabled = false}", "network = {enabled = true}"
            )

        monkeypatch.setattr(
            _codex._Codex01600Policy, "_permission_profile", enabled_network
        )
    else:
        document_builder = _claude._ClaudePolicy._settings_document

        def allowed_network(self: object, boundary: Path) -> str:
            settings = json.loads(document_builder(self, boundary))
            assert isinstance(settings, dict)
            sandbox = settings["sandbox"]
            assert isinstance(sandbox, dict)
            network = sandbox["network"]
            assert isinstance(network, dict)
            network["allow"] = ["https://example.com"]
            return json.dumps(settings, ensure_ascii=True, sort_keys=True)

        monkeypatch.setattr(
            _claude._ClaudePolicy, "_settings_document", allowed_network
        )

    document, error = gate_verdict(config, direct_selection(root), harness)
    assert error is not None
    refusal = document["refusal"]
    assert refusal["stage"] == "network"
    assert "cannot be established as version-bound" in refusal["detail"]
    assert "UNKNOWN" in refusal["detail"]


def test_excluded_ambient_codex_configuration_carries_no_admission_obligation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, config = registered_checkout(tmp_path, monkeypatch)
    baseline_document, error = gate_verdict(config, direct_selection(root), "codex")
    assert error is None
    baseline_gate = baseline_document["gate"]
    assert isinstance(baseline_gate, dict)
    baseline = baseline_gate["policy"]
    assert isinstance(baseline, dict)

    ambient_home = tmp_path / "ambient-codex-home"
    ambient_home.mkdir()
    (ambient_home / "config.toml").write_text(
        "factory_sentinel_unknown_key = true\n[features]\napps = true\nbroken = = =\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_HOME", str(ambient_home))
    ambient_document, error = gate_verdict(config, direct_selection(root), "codex")

    assert error is None
    ambient_gate = ambient_document["gate"]
    assert isinstance(ambient_gate, dict)
    ambient = ambient_gate["policy"]
    assert isinstance(ambient, dict)
    for stage in ("instructions", "sandbox", "network"):
        assert ambient[stage] == baseline[stage]
    environment = ambient["environment"]
    assert isinstance(environment, dict)
    assert environment["provenance"] == "observed"
    assert environment["config_location"] == {
        "variable": "CODEX_HOME",
        "path": str(ambient_home),
    }


def test_excluded_ambient_claude_settings_carries_no_admission_obligation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, config = registered_checkout(tmp_path, monkeypatch)
    home = tmp_path / "ambient-home"
    (home / ".claude").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    baseline_document, error = gate_verdict(config, direct_selection(root), "claude")
    assert error is None
    baseline_gate = baseline_document["gate"]
    assert isinstance(baseline_gate, dict)
    baseline = baseline_gate["policy"]
    assert isinstance(baseline, dict)

    (home / ".claude" / "settings.json").write_text(
        '{"factorySentinelUserKey": true, "hooks": {"SessionStart": []}}\n',
        encoding="utf-8",
    )
    (root / ".claude").mkdir()
    (root / ".claude" / "settings.json").write_text(
        '{"model": "claude-opus", "factorySentinelBoundaryKey": "ignored"}\n',
        encoding="utf-8",
    )
    (root / ".claude" / "settings.local.json").write_text(
        "{not even json\n",
        encoding="utf-8",
    )
    commit_worktree(root)
    ambient_document, error = gate_verdict(config, direct_selection(root), "claude")

    assert error is None
    ambient_gate = ambient_document["gate"]
    assert isinstance(ambient_gate, dict)
    ambient = ambient_gate["policy"]
    assert isinstance(ambient, dict)
    for stage in ("instructions", "sandbox", "network", "environment"):
        assert ambient[stage] == baseline[stage]


def test_the_environment_stage_records_the_effective_environment_derivation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, config = registered_checkout(tmp_path, monkeypatch)
    monkeypatch.setenv("FACTORY_PROVENANCE_VARIABLE", "present")
    monkeypatch.setenv("CODEX_MANAGED_BY_NPM", "stray")

    run_to_harness(monkeypatch, root, 0, "--checkout", str(root), "Fix")
    assert factory.main() == 0
    environment = record_documents(config)[-1]["gate"]["policy"]["environment"]
    assert environment["digest_scheme"] == "factory-env-digest-v1"
    assert environment["removals"] == list(_codex._CODEX_MANAGED_MARKERS)
    assert environment["pins"] == []
    assert "FACTORY_PROVENANCE_VARIABLE" in environment["inherited"]
    assert "CODEX_MANAGED_BY_NPM" not in environment["inherited"]
    effective = _rungate.effective_environment(
        (), _codex._CODEX_MANAGED_MARKERS, tuple(os.environ.items())
    )
    assert environment["variable_count"] == len(effective)
    assert environment["digest"] == _rungate.environment_digest(effective)
    assert environment["shell_environment_policy"] == {
        "inherit": '"core"',
        "ignore_default_excludes": "false",
    }
    assert environment["config_location"] == {
        "variable": "CODEX_HOME",
        "path": os.environ.get("CODEX_HOME"),
    }


def test_the_claude_environment_stage_records_the_certified_pins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, config = registered_checkout(tmp_path, monkeypatch)

    environment = policy_evidence(config, direct_selection(root), "claude")[
        "environment"
    ]
    assert environment["pins"] == [name for name, _ in _claude._ENVIRONMENT_PINS]
    assert environment["removals"] == []
    assert environment["config_location"] == {
        "variable": "CLAUDE_CONFIG_DIR",
        "path": os.environ.get("CLAUDE_CONFIG_DIR"),
    }
    assert "shell_environment_policy" not in environment


def test_the_environment_digest_canonical_serialization_is_pinned() -> None:
    expected = hashlib.sha256(
        b'{"schema":"factory-env-digest-v1","variables":[["A","1"],["B","2"]]}'
    ).hexdigest()

    assert _rungate.environment_digest((("A", "1"), ("B", "2"))) == expected
    assert _rungate.environment_digest(
        (("B", "2"), ("A", "1"))
    ) == _rungate.environment_digest((("A", "1"), ("B", "2")))
    assert _rungate.environment_digest((("A", "2"), ("B", "2"))) != expected
    assert (
        _rungate.environment_digest((("K", "\udcff"),))
        == hashlib.sha256(
            b'{"schema":"factory-env-digest-v1","variables":[["K","\\udcff"]]}'
        ).hexdigest()
    )


@pytest.mark.parametrize("breakage", ["missing", "ambiguous"])
def test_a_missing_or_ambiguous_required_pin_or_removal_refuses_as_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, breakage: str
) -> None:
    root, config = registered_checkout(tmp_path, monkeypatch)
    if breakage == "missing":
        monkeypatch.setattr("_claude._ENVIRONMENT_PINS", (("DISABLE_TELEMETRY", "1"),))
    else:
        declared = _rungate._claude_policy

        def ambiguous(boundary: Path) -> _rungate.HarnessPolicy:
            return replace(declared(boundary), removals=("DISABLE_TELEMETRY",))

        monkeypatch.setattr(_rungate, "_claude_policy", ambiguous)

    document, error = gate_verdict(config, direct_selection(root), "claude")
    assert error is not None
    refusal = document["refusal"]
    assert refusal["stage"] == "environment"
    assert "cannot be established as version-bound" in refusal["detail"]
    assert "UNKNOWN" in refusal["detail"]
    if breakage == "missing":
        assert "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC" in refusal["detail"]
    else:
        assert "DISABLE_TELEMETRY is both pinned and removed" in refusal["detail"]


def test_environment_values_never_enter_the_run_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, config = registered_checkout(tmp_path, monkeypatch)
    monkeypatch.setenv("FACTORY_TEST_TOKEN", "s3cret-value")

    run_to_harness(monkeypatch, root, 0, "--checkout", str(root), "Fix")
    assert factory.main() == 0
    text = json.dumps(record_documents(config)[-1])
    assert "FACTORY_TEST_TOKEN" in text
    assert "s3cret-value" not in text


def passed_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, harness: str = "codex"
) -> tuple[Path, Path, _rungate.ValidatedGate]:
    """Run the real gate over the registered acme/web Checkout and return it."""

    root, config = registered_checkout(tmp_path, monkeypatch)
    with run_record(["factory", "Fix"], harness) as record:
        gate = run_gate(record, direct_selection(root), harness)
    return root, config, gate


def stub_harness(tmp_path: Path) -> Path:
    """One executable stub whose bytes bind into sealed plans."""

    executable = tmp_path / "harness-bin" / "harness"
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    return executable


def admitted_run(
    harness: str,
    executable: Path,
    *,
    probe: dict[str, object] | None = None,
) -> _harness.PreparedRun:
    """The admitted inputs one Runner freezes for the stub executable."""

    info = executable.stat()
    return _harness.PreparedRun(
        harness="Claude Code" if harness == "claude" else "Codex",
        executable=executable,
        invocation=(str(executable),),
        executable_identity=(info.st_dev, info.st_ino),
        executable_sha256=hashlib.sha256(executable.read_bytes()).hexdigest(),
        environment=_claude._ENVIRONMENT_PINS if harness == "claude" else (),
        unset_environment=() if harness == "claude" else _codex._CODEX_MANAGED_MARKERS,
        probe=probe,
    )


def sealed_plan(
    harness: str,
    gate: _rungate.ValidatedGate,
    executable: Path,
    *,
    probe: dict[str, object] | None = None,
    instruction: bytes = b"# Run Request\n\n## Instruction\n\nFix",
) -> _rungate.ValidatedRunPlan:
    """Seal the passed gate and the admitted stub into one Validated Run Plan."""

    return _rungate.seal_validated_run_plan(
        gate, admitted_run(harness, executable, probe=probe), instruction
    )


def quoted_absolute(path: Path) -> str:
    return json.dumps(str(path.absolute()), ensure_ascii=False)


def record_document(config: Path, identifier: str) -> dict[str, object]:
    """Read one Run Record document by its identifier."""

    return json.loads(
        (config / "factory" / "runs" / f"{identifier}.json").read_text(encoding="utf-8")
    )


def launch_sealed(config: Path, plan: _rungate.ValidatedRunPlan) -> dict[str, object]:
    """Launch one sealed plan under an active Run Record; return its document."""

    with (
        tempfile.TemporaryFile(mode="w+b") as spool,
        run_record(["factory", "Fix"], "codex") as record,
    ):
        assert _harness.launch(plan, spool) == 0
        document = record_document(config, record.identifier)
    assert document["status"] == "launched"
    return document


def claude_probe_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[_claude._ClaudePolicy, Path, list[str]]:
    """A Claude policy whose Host Admission Probe records each run."""

    executable = tmp_path / "probe-claude"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    policy = _claude._ClaudePolicy(
        version="9.9.9",
        sha256="a" * 64,
        operating_system="Linux",
        architecture="x86_64",
        install_channel="native-installer",
        canonical_path=executable,
        filesystem_identity=(2096, 1),
    )
    probes: list[str] = []
    monkeypatch.setattr(
        _claude._ClaudePolicy,
        "_run_host_admission_probe",
        lambda self: probes.append("ran"),
    )
    cache = tmp_path / "cache"
    monkeypatch.setattr(_claude, "_cache_directory", lambda: cache)
    return policy, cache, probes


def test_a_passed_gate_seals_the_validated_run_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _config, gate = passed_gate(tmp_path, monkeypatch, "claude")
    executable = stub_harness(tmp_path)
    instruction = b"# Run Request\n\n## Instruction\n\nFix the fence\n"
    probe = {"cache": "fresh", "key": "k" * 64, "age_seconds": None}
    prepared = admitted_run("claude", executable, probe=probe)
    plan = _rungate.seal_validated_run_plan(gate, prepared, instruction)
    selection = direct_selection(root)
    pinned = {name for name, _ in _claude._ENVIRONMENT_PINS}

    assert (plan.client_id, plan.project_id, plan.repository_id, plan.checkout_id) == (
        selection.client_id,
        selection.project_id,
        selection.repository_id,
        selection.checkout_id,
    )
    assert plan.boundary == selection.boundary
    assert plan.boundary_canonical == os.path.realpath(root)
    assert plan.boundary_identity == (root.stat().st_dev, root.stat().st_ino)
    assert plan.head == gate.observation.head == revision(root)
    assert plan.object_format == gate.observation.object_format
    assert plan.harness == "Claude Code"
    assert plan.executable == executable
    assert plan.executable_identity == prepared.executable_identity
    assert plan.executable_sha256 == prepared.executable_sha256
    assert plan.invocation == prepared.invocation
    assert plan.environment == gate.environment
    assert plan.pins == _claude._ENVIRONMENT_PINS
    assert plan.removals == ()
    assert plan.instruction == instruction
    assert plan.environment_provenance == {
        name: ("pinned" if name in pinned else "inherited")
        for name, _ in plan.environment
    }
    assert set(plan.environment_provenance.values()) == {"pinned", "inherited"}
    assert plan.evidence["gate"]["head"] == plan.head
    assert plan.evidence["gate"]["object_format"] == plan.object_format
    assert plan.evidence["gate"]["proof"] == {
        name: dict(gate.observation.pins[name])
        for name in sorted(gate.observation.pins)
    }
    assert plan.evidence["gate"]["assessment"] == gate.observation.assessment
    assert plan.evidence["gate"]["policy"] == gate.observation.policy
    assert plan.evidence["admission"] == {
        "harness": "Claude Code",
        "executable": str(executable),
        "executable_sha256": prepared.executable_sha256,
        "probe": probe,
    }
    assert plan.fingerprint == _rungate.plan_fingerprint(plan)
    assert plan == _rungate.seal_validated_run_plan(gate, prepared, instruction)
    with pytest.raises(FrozenInstanceError):
        plan.head = "tampered"


def test_the_launcher_consumes_the_plan_without_rederiving_gated_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FACTORY_PLANNED_VARIABLE", "gate-time-value")
    root, config, gate = passed_gate(tmp_path, monkeypatch)
    executable = stub_harness(tmp_path)
    plan = sealed_plan("codex", gate, executable)
    (config / "factory" / "projects.json").write_text("corrupt after the seal")
    commit_worktree(root)
    monkeypatch.setenv("FACTORY_PLANNED_VARIABLE", "ambient-value")
    monkeypatch.setenv("CODEX_MANAGED_BY_NPM", "stray-return")
    monkeypatch.setenv("FACTORY_POST_SEAL_VARIABLE", "ambient-only")
    launched: dict[str, object] = {}

    class RecordingProcess:
        def __init__(self, invocation: object, **keywords: object) -> None:
            launched["invocation"] = tuple(invocation)
            launched["cwd"] = keywords["cwd"]
            launched["env"] = keywords["env"]

        def wait(self) -> int:
            return 0

    monkeypatch.setattr(subprocess, "Popen", RecordingProcess)
    document = launch_sealed(config, plan)
    environment = launched["env"]
    assert isinstance(environment, dict)

    assert launched["invocation"] == plan.invocation
    assert launched["cwd"] == plan.boundary
    assert environment == dict(plan.environment)
    assert environment["FACTORY_PLANNED_VARIABLE"] == "gate-time-value"
    assert "CODEX_MANAGED_BY_NPM" not in environment
    assert "FACTORY_POST_SEAL_VARIABLE" not in environment
    assert document["launch"]["plan_fingerprint"] == plan.fingerprint


def test_the_plan_fingerprint_canonical_serialization_is_pinned() -> None:
    plan = _rungate.ValidatedRunPlan(
        client_id="cli_" + "a" * 32,
        project_id="prj_" + "b" * 32,
        repository_id="repo_" + "c" * 32,
        checkout_id="chk_" + "d" * 32,
        boundary=Path("/selected"),
        boundary_canonical="/selected",
        boundary_identity=(2096, 42),
        head="a" * 40,
        object_format="sha1",
        harness="Claude Code",
        executable=Path("/bin/stub"),
        executable_identity=(2096, 7),
        executable_sha256="b" * 64,
        invocation=("claude", "-p", "Fix"),
        environment=(("A", "1"), ("B", "2")),
        environment_provenance={"A": "inherited", "B": "inherited"},
        pins=(),
        removals=(),
        instruction=b"Fix",
        evidence={"probe": {"cache": "fresh"}},
        fingerprint="",
    )
    canonical = (
        '{"boundary":{"canonical":"/selected","st_dev":2096,"st_ino":42},'
        '"environment_digest":'
        '"22bbdfd991c6ee7f2ba43ce580d059fe4d02a2fc285aeec91d6613e8126554dd",'
        '"evidence":{"sha256":'
        '"a372fdd54a95d360e983ae985567305578ea62cef75aa1c4f49ad004080a0109"},'
        '"git":{"head":"' + "a" * 40 + '","object_format":"sha1"},'
        '"harness":{"executable":"/bin/stub","name":"Claude Code","sha256":"'
        + "b"
        * 64
        + '","st_dev":2096,"st_ino":7},'
        '"identity":["cli_'
        + "a" * 32
        + '","prj_'
        + "b" * 32
        + '","repo_'
        + "c" * 32
        + '","chk_'
        + "d" * 32
        + '"],'
        '"instruction":{"bytes":3,"sha256":'
        '"21f1595b614ffc31c2693545a5ccb27559edf20c53cdd21099bc83b743313fd8"},'
        '"invocation":["claude","-p","Fix"],'
        '"schema":"factory-plan-fingerprint-v1"}'
    )
    fingerprint = hashlib.sha256(canonical.encode("ascii")).hexdigest()

    assert _rungate.plan_fingerprint(plan) == fingerprint
    assert (
        _rungate.plan_fingerprint(replace(plan, environment=(("B", "2"), ("A", "1"))))
        == fingerprint
    )
    mutations = (
        replace(plan, client_id="cli_" + "e" * 32),
        replace(plan, project_id="prj_" + "e" * 32),
        replace(plan, repository_id="repo_" + "e" * 32),
        replace(plan, checkout_id="chk_" + "e" * 32),
        replace(plan, boundary_canonical="/elsewhere"),
        replace(plan, boundary_identity=(2096, 43)),
        replace(plan, head="b" * 40),
        replace(plan, object_format="sha256"),
        replace(plan, harness="Codex"),
        replace(plan, executable=Path("/bin/other")),
        replace(plan, executable_identity=(2096, 8)),
        replace(plan, executable_sha256="c" * 64),
        replace(plan, invocation=("claude", "-p", "Fix it")),
        replace(plan, environment=(("A", "1"), ("B", "3"))),
        replace(plan, instruction=b"Fix it"),
        replace(plan, evidence={"probe": {"cache": "reused"}}),
    )
    assert all(
        _rungate.plan_fingerprint(mutated) != fingerprint for mutated in mutations
    )


def test_the_boundary_recheck_precedes_the_durable_launch_commit_and_process_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _root, config, gate = passed_gate(tmp_path, monkeypatch)
    executable = stub_harness(tmp_path)
    plan = sealed_plan("codex", gate, executable)
    events: list[str] = []
    state: dict[str, object] = {}
    real_recheck = _harness._recheck_boundary
    real_commit = _harness.commit_pending_launch
    real_popen = subprocess.Popen

    def recheck(observed: object) -> None:
        events.append("recheck")
        real_recheck(observed)  # type: ignore[arg-type]

    def commit(authorization: object) -> None:
        events.append("commit")
        real_commit(authorization)  # type: ignore[arg-type]

    def popen(invocation: object, **keywords: object) -> subprocess.Popen[bytes]:
        events.append("popen")
        state["record_at_popen"] = record_document(config, str(state["identifier"]))[
            "status"
        ]
        return real_popen(invocation, **keywords)  # type: ignore[arg-type]

    monkeypatch.setattr(_harness, "_recheck_boundary", recheck)
    monkeypatch.setattr(_harness, "commit_pending_launch", commit)
    monkeypatch.setattr(subprocess, "Popen", popen)

    with (
        tempfile.TemporaryFile(mode="w+b") as spool,
        run_record(["factory", "Fix"], "codex") as record,
    ):
        state["identifier"] = record.identifier
        assert _harness.launch(plan, spool) == 0
    document = record_document(config, str(state["identifier"]))

    assert events == ["recheck", "commit", "popen"]
    assert state["record_at_popen"] == "launched"
    assert document["launch"]["plan_fingerprint"] == plan.fingerprint

    drifted = replace(
        plan,
        boundary_identity=(plan.boundary_identity[0], plan.boundary_identity[1] + 1),
    )
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *arguments, **keywords: pytest.fail(
            "process creation must not be attempted"
        ),
    )
    with (
        pytest.raises(_harness.RunAdmissionError),
        tempfile.TemporaryFile(mode="w+b") as spool,
        run_record(["factory", "Fix"], "codex") as record,
    ):
        _harness.launch(drifted, spool)
    refused = record_document(config, record.identifier)

    assert events == ["recheck", "commit", "popen", "recheck"]
    assert refused["status"] == "open"
    assert "launch" not in refused


def test_launch_supplies_the_planned_environment_and_working_directory_verbatim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FACTORY_PLANNED_VARIABLE", "planned-value")
    root, _config, gate = passed_gate(tmp_path, monkeypatch)
    executable = tmp_path / "harness-bin" / "harness"
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_text(
        "#!/bin/sh\n"
        "{\n"
        "printf '%s' \"$FACTORY_PLANNED_VARIABLE\"\n"
        "printf '\\n'\n"
        "printf '%s' \"$PWD\"\n"
        'if [ -n "${FACTORY_POST_SEAL_VARIABLE+x}" ]; then\n'
        "printf '\\nambient'\n"
        "fi\n"
        "} > observation.txt\n"
    )
    executable.chmod(0o755)
    plan = sealed_plan("codex", gate, executable)
    monkeypatch.setenv("FACTORY_PLANNED_VARIABLE", "ambient-after-seal")
    monkeypatch.setenv("FACTORY_POST_SEAL_VARIABLE", "ambient-only")

    with tempfile.TemporaryFile(mode="w+b") as spool:
        assert _harness.launch(plan, spool) == 0
    observed = (root / "observation.txt").read_text(encoding="utf-8").splitlines()

    assert observed[0] == "planned-value"
    assert Path(observed[1]).resolve() == root.resolve()
    assert len(observed) == 2


def test_a_boundary_vanishing_at_launch_refuses_with_the_frozen_diagnostic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _config, gate = passed_gate(tmp_path, monkeypatch)
    plan = sealed_plan("codex", gate, stub_harness(tmp_path))
    shutil.rmtree(root)

    with pytest.raises(_harness.RunAdmissionError) as raised:
        _harness.launch(plan, io.BytesIO(b"# Run Request\n"))

    assert str(raised.value) == (
        f"The Project Boundary {quoted_absolute(root)} does not exist at launch; "
        "the Run was admitted against a directory that has since disappeared."
    )


def test_a_boundary_replaced_by_a_file_refuses_with_the_frozen_diagnostic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _config, gate = passed_gate(tmp_path, monkeypatch)
    plan = sealed_plan("codex", gate, stub_harness(tmp_path))
    shutil.rmtree(root)
    root.write_text("now a plain file", encoding="utf-8")

    with pytest.raises(_harness.RunAdmissionError) as raised:
        _harness.launch(plan, io.BytesIO(b"# Run Request\n"))

    assert str(raised.value) == (
        f"The Project Boundary {quoted_absolute(root)} is no longer a directory "
        "at launch."
    )


def test_a_boundary_identity_change_at_launch_refuses_with_the_frozen_diagnostic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _config, gate = passed_gate(tmp_path, monkeypatch)
    plan = sealed_plan("codex", gate, stub_harness(tmp_path))
    drifted = replace(
        plan,
        boundary_identity=(plan.boundary_identity[0] + 1, plan.boundary_identity[1]),
    )
    observed = root.stat()

    with pytest.raises(_harness.RunAdmissionError) as raised:
        _harness.launch(drifted, io.BytesIO(b"# Run Request\n"))

    assert str(raised.value) == (
        f"The Project Boundary {quoted_absolute(root)} changed during admission: "
        f"the gate admitted dev/ino {drifted.boundary_identity[0]}:"
        f"{drifted.boundary_identity[1]}, launch found {observed.st_dev}:"
        f"{observed.st_ino}; refusing to launch against a directory other "
        "than the one validated."
    )


def test_a_boundary_resolving_elsewhere_at_launch_refuses_with_the_frozen_diagnostic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _config, gate = passed_gate(tmp_path, monkeypatch)
    plan = sealed_plan("codex", gate, stub_harness(tmp_path))
    drifted = replace(plan, boundary_canonical=os.fspath(tmp_path / "elsewhere"))

    with pytest.raises(_harness.RunAdmissionError) as raised:
        _harness.launch(drifted, io.BytesIO(b"# Run Request\n"))

    assert str(raised.value) == (
        f"The Project Boundary {quoted_absolute(root)} resolved to "
        f"{quoted_absolute(root)} at launch, not the admitted canonical path "
        f"{quoted_absolute(Path(drifted.boundary_canonical))}; refusing."
    )


def test_an_impossible_boundary_reobservation_refuses_rather_than_trusting_the_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _config, gate = passed_gate(tmp_path, monkeypatch)
    plan = sealed_plan("codex", gate, stub_harness(tmp_path))
    real_stat = Path.stat

    def refuse_observation(
        path: Path, *arguments: object, **keywords: object
    ) -> os.stat_result:
        if path == root:
            raise PermissionError(errno.EACCES, "Permission denied")
        return real_stat(path, *arguments, **keywords)  # type: ignore[arg-type,return-value]

    monkeypatch.setattr(Path, "stat", refuse_observation)

    with pytest.raises(_harness.RunAdmissionError) as raised:
        _harness.launch(plan, io.BytesIO(b"# Run Request\n"))

    assert str(raised.value) == (
        f"The Project Boundary {quoted_absolute(root)} could not be re-observed "
        "at launch (Permission denied); refusing rather than trusting the "
        "admitted observation."
    )


def test_an_executable_changed_at_launch_refuses_with_the_unchanged_diagnostic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _root, _config, gate = passed_gate(tmp_path, monkeypatch)
    executable = stub_harness(tmp_path)
    plan = sealed_plan("codex", gate, executable)
    executable.write_text("#!/bin/sh\nexit 7\n")
    executable.chmod(0o755)

    with pytest.raises(_harness.RunAdmissionError) as raised:
        _harness.launch(plan, io.BytesIO(b"# Run Request\n"))

    assert str(raised.value) == (
        f"{plan.harness} executable {quoted_absolute(executable)} changed "
        "during Run Admission; refusing to evaluate its replacement"
    )


def test_an_uninspectable_executable_at_launch_refuses_before_the_launch_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _root, config, gate = passed_gate(tmp_path, monkeypatch)
    executable = stub_harness(tmp_path)
    plan = sealed_plan("codex", gate, executable)
    executable.chmod(0o000)
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *arguments, **keywords: pytest.fail(
            "process creation must not be attempted"
        ),
    )

    with (
        pytest.raises(_harness.RunAdmissionError) as raised,
        tempfile.TemporaryFile(mode="w+b") as spool,
        run_record(["factory", "Fix"], "codex") as record,
    ):
        _harness.launch(plan, spool)

    assert str(raised.value) == (
        f"{plan.harness} executable {quoted_absolute(executable)} could not be "
        "inspected: Permission denied; check its permissions"
    )
    document = record_document(config, record.identifier)
    assert document["status"] == "open"
    assert "launch" not in document


def test_the_claude_probe_cache_key_binds_the_resolved_sandbox_tooling_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy, cache, probes = claude_probe_policy(tmp_path, monkeypatch)
    tooling = tmp_path / "tooling"
    tooling.mkdir()
    for name, contents in (("bwrap", b"bubblewrap_v1"), ("socat", b"relay_v1")):
        tool = tooling / name
        tool.write_bytes(contents)
        tool.chmod(0o755)
    monkeypatch.setattr(_claude.shutil, "which", lambda name: str(tooling / name))
    payload = policy._host_probe_cache_payload()
    key = hashlib.sha256(payload).hexdigest()

    assert json.loads(payload)["sandbox_tooling"] == {
        "bwrap": {
            "path": str(tooling / "bwrap"),
            "sha256": hashlib.sha256(b"bubblewrap_v1").hexdigest(),
        },
        "socat": {
            "path": str(tooling / "socat"),
            "sha256": hashlib.sha256(b"relay_v1").hexdigest(),
        },
    }
    first = policy.prepare_run(tmp_path)
    second = policy.prepare_run(tmp_path)

    assert probes == ["ran"]
    assert first[1] == {"cache": "fresh", "key": key, "age_seconds": None}
    assert second[1]["cache"] == "reused"
    assert second[1]["key"] == key
    assert (cache / "host-admission-probe" / f"{key}.json").is_file()

    (tooling / "bwrap").write_bytes(b"bubblewrap_v2")
    rekeyed = policy.prepare_run(tmp_path)
    rekey = rekeyed[1]["key"]

    assert probes == ["ran", "ran"]
    assert rekeyed[1]["cache"] == "fresh"
    assert rekey == hashlib.sha256(policy._host_probe_cache_payload()).hexdigest()
    assert rekey != key
    assert (cache / "host-admission-probe" / f"{rekey}.json").is_file()

    (tooling / "bwrap").chmod(0o000)
    unreadable = policy.prepare_run(tmp_path)
    identity = json.loads(policy._host_probe_cache_payload())["sandbox_tooling"]

    assert identity["bwrap"]["sha256"] is None
    assert unreadable[1]["cache"] == "fresh"
    assert unreadable[1]["key"] not in {key, rekey}


def test_probe_evidence_stays_valid_until_a_cache_key_input_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy, cache, probes = claude_probe_policy(tmp_path, monkeypatch)
    first = policy.prepare_run(tmp_path)
    entry = cache / "host-admission-probe" / f"{first[1]['key']}.json"
    aged = entry.stat().st_mtime - 1_000_000_000
    os.utime(entry, (aged, aged))

    second = policy.prepare_run(tmp_path)

    assert probes == ["ran"]
    assert second[1]["cache"] == "reused"
    assert second[1]["key"] == first[1]["key"]
    assert second[1]["age_seconds"] > 999_999_999

    monkeypatch.setattr("_claude._factory_version", lambda: "next-factory")
    third = policy.prepare_run(tmp_path)
    monkeypatch.setattr(
        "_claude._host_identity",
        lambda: {"release": "next-kernel", "version": "next"},
    )
    fourth = policy.prepare_run(tmp_path)
    fifth = replace(policy, version="9.9.10", sha256="b" * 64).prepare_run(tmp_path)
    sixth = replace(policy, operating_system="Other", architecture="riscv").prepare_run(
        tmp_path
    )

    assert [evidence[1]["cache"] for evidence in (third, fourth, fifth, sixth)] == [
        "fresh",
        "fresh",
        "fresh",
        "fresh",
    ]
    keys = {
        first[1]["key"],
        third[1]["key"],
        fourth[1]["key"],
        fifth[1]["key"],
        sixth[1]["key"],
    }
    assert len(keys) == 5


def test_re_probe_bypasses_the_cache_and_replaces_the_positive_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy, cache, probes = claude_probe_policy(tmp_path, monkeypatch)
    first = policy.prepare_run(tmp_path)
    reused = policy.prepare_run(tmp_path)
    key = first[1]["key"]
    entry = cache / "host-admission-probe" / f"{key}.json"
    generation = entry.stat().st_ino

    reprobed = policy.prepare_run(tmp_path, re_probe=True)

    assert probes == ["ran", "ran"]
    assert first[1]["cache"] == "fresh"
    assert reused[1]["cache"] == "reused"
    assert reprobed[1]["cache"] == "fresh"
    assert reprobed[1]["key"] == key
    assert tuple((cache / "host-admission-probe").iterdir()) == (entry,)
    assert entry.stat().st_ino != generation
    served = policy.prepare_run(tmp_path)

    assert probes == ["ran", "ran"]
    assert served[1]["cache"] == "reused"


def test_a_failed_re_probe_refuses_and_invalidates_the_prior_positive_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy, cache, probes = claude_probe_policy(tmp_path, monkeypatch)
    cached = policy.prepare_run(tmp_path)
    entry = cache / "host-admission-probe" / f"{cached[1]['key']}.json"

    def failing(self: _claude._ClaudePolicy) -> None:
        raise self._host_probe_failure("exit status 7")

    monkeypatch.setattr(_claude._ClaudePolicy, "_run_host_admission_probe", failing)
    with pytest.raises(_harness.RunAdmissionError, match="exit status 7"):
        policy.prepare_run(tmp_path, re_probe=True)

    assert not entry.exists()
    monkeypatch.setattr(
        _claude._ClaudePolicy,
        "_run_host_admission_probe",
        lambda self: probes.append("ran"),
    )
    recovered = policy.prepare_run(tmp_path)

    assert probes == ["ran", "ran"]
    assert recovered[1]["cache"] == "fresh"
    assert recovered[1]["key"] == cached[1]["key"]
    assert entry.exists()


def test_probe_provenance_binds_into_the_plan_and_the_launch_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _root, config, gate = passed_gate(tmp_path, monkeypatch)
    executable = stub_harness(tmp_path)
    reused = {"cache": "reused", "key": "k" * 64, "age_seconds": 12.5}
    plan = sealed_plan("codex", gate, executable, probe=reused)
    reprobed = sealed_plan(
        "codex",
        gate,
        executable,
        probe={"cache": "fresh", "key": "k" * 64, "age_seconds": None},
    )

    assert plan.evidence["admission"]["probe"] == reused
    assert reprobed.fingerprint != plan.fingerprint
    document = launch_sealed(config, plan)

    assert document["launch"]["plan_fingerprint"] == plan.fingerprint


def test_security_relevant_claude_keys_bind_to_version_bound_behavioral_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _config, gate = passed_gate(tmp_path, monkeypatch, "claude")
    policy, _cache, probes = claude_probe_policy(tmp_path, monkeypatch)
    invocation, evidence = policy.prepare_run(root)
    executable = stub_harness(tmp_path)
    plan = sealed_plan("claude", gate, executable, probe=evidence)
    policy_document = gate.observation.policy
    assert isinstance(policy_document, dict)
    sandbox = policy_document["sandbox"]
    assert isinstance(sandbox, dict)
    controls = sandbox["controls"]
    assert isinstance(controls, dict)
    key = hashlib.sha256(policy._host_probe_cache_payload()).hexdigest()

    assert sandbox["provenance"] == "configured"
    assert controls["permission_mode"] == "dontAsk"
    assert controls["sandbox_enabled"] is True
    read_fence = controls["read_fence"]
    assert isinstance(read_fence, dict)
    assert str(root) in read_fence["allow_read"]
    assert evidence["cache"] == "fresh"
    assert evidence["key"] == key
    assert plan.evidence["admission"]["probe"] == evidence
    assert invocation.index("--permission-mode") + 1 == invocation.index("dontAsk")

    superseded = replace(policy, version="9.9.10")
    _superseded_invocation, superseded_evidence = superseded.prepare_run(root)

    assert probes == ["ran", "ran"]
    assert superseded_evidence["cache"] == "fresh"
    assert (
        superseded_evidence["key"]
        == hashlib.sha256(superseded._host_probe_cache_payload()).hexdigest()
    )
    assert superseded_evidence["key"] != key


def test_a_failing_host_probe_refuses_despite_recorded_sandbox_controls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, config = registered_checkout(tmp_path, monkeypatch)
    sandbox = policy_evidence(config, direct_selection(root), "claude")["sandbox"]
    assert isinstance(sandbox, dict)
    controls = sandbox["controls"]
    assert isinstance(controls, dict)
    assert controls["permission_mode"] == "dontAsk"

    policy, cache, probes = claude_probe_policy(tmp_path, monkeypatch)

    def failing(self: _claude._ClaudePolicy) -> None:
        raise self._host_probe_failure("exit status 7")

    monkeypatch.setattr(_claude._ClaudePolicy, "_run_host_admission_probe", failing)
    with pytest.raises(_harness.RunAdmissionError, match="exit status 7"):
        policy.prepare_run(root)

    assert probes == []
    assert list(cache.glob("host-admission-probe/*")) == []
