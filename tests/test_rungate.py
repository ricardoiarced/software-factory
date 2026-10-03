"""Shared Run Gate behavior at the identity and Git admission stages."""

import io
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

import _rungate
import factory
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

    def handoff(project: factory.Project, spool: object) -> int:
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
    config: Path, selection: RunSelection
) -> tuple[dict[str, object], RunGateError | None]:
    """Run the gate directly against one store and return its record outcome."""

    with run_record(["factory", "Fix"], "codex") as record:
        error: RunGateError | None = None
        try:
            run_gate(record, selection)
        except RunGateError as refusal:
            error = refusal
    document = next(
        entry for entry in record_documents(config) if entry["id"] == record.identifier
    )
    return document, error


def revision(root: Path) -> str:
    result = git("rev-parse", "HEAD", cwd=root)
    return os.fsdecode(result.stdout.strip())


def test_the_run_gate_runs_between_the_context_gate_and_run_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, config = registered_checkout(tmp_path, monkeypatch)
    (root / "context").write_bytes(b"ab")
    commit_worktree(root)
    monkeypatch.setenv("FACTORY_CONTEXT_WARN_BYTES", "1")
    monkeypatch.setattr("_codex._COMPATIBILITY_SET", ())
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
