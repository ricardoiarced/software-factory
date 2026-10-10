"""The durable Run Record envelope and launch atomicity (S14)."""

import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
from collections.abc import Callable, Generator, Sequence
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, BinaryIO

import pytest

import _codex
import _harness
import _rungate
import _runrecord
import factory
from _identity import RunSelection

FACTORY = Path(__file__).resolve().parents[1] / "factory.py"

CLIENT_ID = "cli_" + "a" * 32
PROJECT_ID = "prj_" + "b" * 32
REPOSITORY_ID = "repo_" + "c" * 32
CHECKOUT_ID = "chk_" + "d" * 32


def invoke(
    config: Path,
    *args: str,
    input: bytes | None = None,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [sys.executable, str(FACTORY), *args],
        check=False,
        capture_output=True,
        input=input,
        cwd=cwd,
        env={**os.environ, "XDG_CONFIG_HOME": str(config), **(env or {})},
    )


def runs_directory(config: Path) -> Path:
    return config / "factory" / "runs"


def read_record(config: Path, name: str) -> dict[str, Any]:
    return json.loads((runs_directory(config) / name).read_text(encoding="utf-8"))


def only_record_file(config: Path) -> Path:
    files = sorted(runs_directory(config).glob("run_*.json"))
    assert len(files) == 1, f"expected one Run Record, found {[f.name for f in files]}"
    return files[0]


def only_record(config: Path) -> dict[str, Any]:
    return json.loads(only_record_file(config).read_text(encoding="utf-8"))


def latest_record(config: Path) -> dict[str, Any]:
    files = sorted(
        runs_directory(config).glob("run_*.json"),
        key=lambda path: path.stat().st_mtime_ns,
    )
    assert files, "expected at least one Run Record"
    return json.loads(files[-1].read_text(encoding="utf-8"))


def stub_executable(root: Path, body: str = "exit 0\n") -> Path:
    executable = root / "stub-bin" / "stub"
    executable.parent.mkdir(parents=True)
    executable.write_text(f"#!/bin/sh\n{body}")
    executable.chmod(0o755)
    return executable


def sealed_plan(
    executable: Path,
    boundary: Path,
    *,
    environment: tuple[tuple[str, str], ...] = (),
    unset_environment: tuple[str, ...] = (),
) -> _rungate.ValidatedRunPlan:
    """One sealed Validated Run Plan launching the stub executable."""

    effective = _rungate.effective_environment(
        environment,
        unset_environment,
        tuple(os.environ.items()),
    )
    executable_info = executable.stat()
    boundary_info = boundary.stat()
    plan = _rungate.ValidatedRunPlan(
        client_id=CLIENT_ID,
        project_id=PROJECT_ID,
        repository_id=REPOSITORY_ID,
        checkout_id=CHECKOUT_ID,
        boundary=boundary,
        boundary_canonical=str(boundary),
        boundary_identity=(boundary_info.st_dev, boundary_info.st_ino),
        head="0" * 40,
        object_format="sha1",
        harness="Stub",
        executable=executable,
        executable_identity=(executable_info.st_dev, executable_info.st_ino),
        executable_sha256=hashlib.sha256(executable.read_bytes()).hexdigest(),
        invocation=(str(executable),),
        environment=effective,
        environment_provenance={
            name: ("pinned" if name in {n for n, _ in environment} else "inherited")
            for name, _ in effective
        },
        pins=environment,
        removals=unset_environment,
        instruction=b"# Run Request\n\n## Instruction\n\nFix",
        evidence={},
        fingerprint="",
    )
    return replace(plan, fingerprint=_rungate.plan_fingerprint(plan))


def launch_with_record(
    config: Path,
    executable: Path,
    monkeypatch: pytest.MonkeyPatch,
    environment: tuple[tuple[str, str], ...] = (),
    unset_environment: tuple[str, ...] = (),
) -> int:
    config.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    with _runrecord.run_record(["Fix"], "codex"), run_spool() as spool:
        return _harness.launch(
            sealed_plan(
                executable,
                config,
                environment=environment,
                unset_environment=unset_environment,
            ),
            spool,
        )


@contextmanager
def run_spool() -> Generator[BinaryIO]:
    handle = tempfile.TemporaryFile(mode="w+b")  # noqa: SIM115
    with handle:
        yield handle


def registered_store(config: Path, worktree: Path) -> None:
    """Register one real worktree as acme/web's sole Checkout in a v2 store."""

    worktree.mkdir(parents=True)
    subprocess.run(
        ["git", "init", "-q", "-b", "main"],
        cwd=worktree,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=factory@example.com",
            "-c",
            "user.name=Factory",
            "commit",
            "--allow-empty",
            "-m",
            "initial",
        ],
        cwd=worktree,
        check=True,
        capture_output=True,
    )
    git_dir = worktree / ".git"

    def pin(path: Path) -> dict[str, object]:
        info = path.stat()
        return {"path": str(path), "st_dev": info.st_dev, "st_ino": info.st_ino}

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
            {"id": REPOSITORY_ID, "name": None, "common_dir": pin(git_dir)}
        ],
        "checkouts": [
            {
                "id": CHECKOUT_ID,
                "project_id": PROJECT_ID,
                "repository_id": REPOSITORY_ID,
                "boundary": pin(worktree),
                "worktree_root": pin(worktree),
                "git_dir": pin(git_dir),
                "common_dir": pin(git_dir),
            }
        ],
        "legacy": {},
    }
    registry = config / "factory" / "projects.json"
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text(json.dumps(document), encoding="utf-8")


def test_usage_errors_and_help_create_no_run_record(tmp_path: Path) -> None:
    config = tmp_path / "config"
    cases = [
        invoke(config),
        invoke(config, "--bogus", "Fix"),
        invoke(config, "-h"),
        invoke(config, "--help"),
        invoke(config, "--version"),
        invoke(config, "project", "list"),
    ]
    expected = [2, 2, 0, 0, 0, 0]
    assert [case.returncode for case in cases] == expected
    assert not (config / "factory").exists()


def test_the_run_record_opens_after_parse_and_before_selection(tmp_path: Path) -> None:
    config = tmp_path / "config"

    result = invoke(config, "Fix", "it")

    assert result.returncode == 1
    assert b"No identity store exists" in result.stderr
    document = only_record(config)
    assert document["status"] == "refused"
    assert document["argv"] == {"schema": "factory-argv-v1", "argv": ["Fix", "it"]}
    assert document["refusal"]["stage"] == "selection"
    assert "selection" not in document


def test_run_records_are_separate_files_with_prefixed_random_ids(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config"
    assert invoke(config, "Fix").returncode == 1
    assert invoke(config, "Fix").returncode == 1

    files = sorted(runs_directory(config).glob("run_*.json"))
    assert len(files) == 2
    identifiers = []
    for path in files:
        document = json.loads(path.read_text(encoding="utf-8"))
        assert re.fullmatch(r"run_[0-9a-f]{32}", document["id"])
        assert path.name == f"{document['id']}.json"
        identifiers.append(document["id"])
    assert identifiers[0] != identifiers[1]


def test_an_unwritable_run_store_refuses_the_run_before_selection(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config"
    store = config / "factory"
    store.mkdir(parents=True)
    os.chmod(store, 0o500)
    try:
        result = invoke(config, "Fix")
    finally:
        os.chmod(store, 0o700)

    assert result.returncode == 1
    assert result.stderr.startswith(b"factory: error: ")
    assert b"Run Record store cannot be opened" in result.stderr
    assert not (store / "runs").exists()


def test_the_open_record_holds_only_pregate_facts_and_omits_unset_evidence(
    tmp_path: Path,
) -> None:
    with _runrecord.run_record(["Fix"], "codex") as record:
        pass

    document = only_record(tmp_path / "config")
    assert list(document) == ["schema", "id", "argv", "harness", "opened_at", "status"]
    assert document["schema"] == "factory-run-v1"
    assert document["id"] == record.identifier
    assert re.fullmatch(r"run_[0-9a-f]{32}", document["id"])
    assert document["argv"] == {"schema": "factory-argv-v1", "argv": ["Fix"]}
    assert document["harness"] == "codex"
    datetime.fromisoformat(document["opened_at"])
    assert document["status"] == "open"
    assert (runs_directory(tmp_path / "config") / f"{record.identifier}.json").is_file()


def test_context_payloads_never_enter_the_run_record(tmp_path: Path) -> None:
    config = tmp_path / "config"
    source = tmp_path / "context.txt"
    source.write_text("FILE-MARKER-3Z", encoding="utf-8")

    result = invoke(
        config,
        "--file",
        str(source),
        "ARGV-MARKER-9K",
        input=b"STDIN-MARKER-7Q",
    )

    assert result.returncode == 1
    raw = only_record_file(config).read_bytes()
    assert b"FILE-MARKER-3Z" not in raw
    assert b"STDIN-MARKER-7Q" not in raw
    assert b"ARGV-MARKER-9K" in raw


def test_factory_argv_v1_serialization_is_pinned() -> None:
    document = _runrecord.argv_document(["--project", "café", "Fix it"])

    assert document == {
        "schema": "factory-argv-v1",
        "argv": ["--project", "café", "Fix it"],
    }
    assert _runrecord._serialize(document) == (
        b"{\n"
        b'  "schema": "factory-argv-v1",\n'
        b'  "argv": [\n'
        b'    "--project",\n'
        b'    "caf\\u00e9",\n'
        b'    "Fix it"\n'
        b"  ]\n"
        b"}\n"
    )
    lossless = _runrecord.argv_document(["surrogate \udc80 byte"])
    assert b'"surrogate \\udc80 byte"' in _runrecord._serialize(lossless)


def test_pre_admission_refusals_commit_their_stage_and_diagnostic(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config"

    result = invoke(config, "--checkout", str(tmp_path / "absent"), "Fix")

    assert result.returncode == 1
    document = latest_record(config)
    assert document["status"] == "refused"
    assert document["refusal"]["stage"] == "selection"
    assert "No identity store exists" in document["refusal"]["detail"]
    assert "selection" not in document

    worktree = tmp_path / "worktree"
    registered_store(config, worktree)

    result = invoke(config, "--file", str(tmp_path / "absent.txt"), "Fix", cwd=worktree)

    assert result.returncode == 1
    document = latest_record(config)
    assert document["status"] == "refused"
    assert document["refusal"]["stage"] == "composition"
    assert "cannot be opened" in document["refusal"]["detail"]
    assert document["selection"]["checkout_id"] == CHECKOUT_ID

    result = invoke(
        config,
        "Fix",
        input=b"payload bytes",
        env={"FACTORY_CONTEXT_WARN_BYTES": "1"},
        cwd=worktree,
    )

    assert result.returncode == 1
    assert result.stdout == b""
    document = latest_record(config)
    assert document["status"] == "refused"
    assert document["refusal"]["stage"] == "context-gate"
    assert document["refusal"]["detail"].startswith("Context is ")
    assert document["selection"]["boundary"] == str(worktree)


def test_admission_refusal_commits_the_structured_admission_result(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config"
    worktree = tmp_path / "worktree"
    registered_store(config, worktree)

    result = invoke(config, "Fix", cwd=worktree)

    assert result.returncode == 1
    assert b"reported an unrecognized version" in result.stderr
    document = only_record(config)
    assert document["status"] == "refused"
    assert document["selection"] == {
        "client_id": CLIENT_ID,
        "project_id": PROJECT_ID,
        "repository_id": REPOSITORY_ID,
        "checkout_id": CHECKOUT_ID,
        "boundary": str(worktree),
    }
    assert document["refusal"]["stage"] == "admission"
    assert "unrecognized version" in document["refusal"]["detail"]
    assert document["refusal"]["facts"] == {
        "executable": str(tmp_path / "codex-bin" / "codex")
    }
    assert "launch" not in document
    assert "outcome" not in document


def test_a_failed_refusal_recording_never_masks_or_alters_the_refusal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    real_store = _runrecord.RunRecord._store

    def selective(self: _runrecord.RunRecord, document: dict[str, object]) -> None:
        if "refusal" in document:
            raise _runrecord.RunRecordError("injected store failure")
        real_store(self, document)

    monkeypatch.setattr(_runrecord.RunRecord, "_store", selective)
    monkeypatch.setattr(sys, "argv", ["factory", "Fix"])

    assert factory.main() == 1

    assert "No identity store exists" in capsys.readouterr().err
    document = only_record(tmp_path / "config")
    assert document["status"] == "open"
    assert "refusal" not in document


def test_process_creation_happens_only_after_the_durable_launch_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub = stub_executable(tmp_path)
    observed: dict[str, object] = {}
    real_popen = subprocess.Popen

    def spy(  # noqa: PLR0913
        invocation: Sequence[str],
        cwd: Path,
        stdin: BinaryIO | None,
        stdout: int | None,
        stderr: int | None,
        env: dict[str, str] | None,
        shell: bool,
        start_new_session: bool,
    ) -> subprocess.Popen[bytes]:
        observed["status"] = only_record(tmp_path / "config")["status"]
        return real_popen(
            invocation,
            cwd=cwd,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            env=env,
            shell=shell,
            start_new_session=start_new_session,
        )

    monkeypatch.setattr(subprocess, "Popen", spy)

    status = launch_with_record(tmp_path / "config", stub, monkeypatch)

    assert status == 0
    assert observed == {"status": "launched"}


def test_the_launch_commit_records_the_admitted_launch_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "config"
    config.mkdir(parents=True, exist_ok=True)
    stub = stub_executable(tmp_path, "exit 3\n")
    plan = sealed_plan(
        stub,
        config,
        environment=(("FACTORY_PIN", "1"),),
        unset_environment=("FACTORY_UNSET",),
    )

    assert (
        launch_with_record(
            config,
            stub,
            monkeypatch,
            environment=(("FACTORY_PIN", "1"),),
            unset_environment=("FACTORY_UNSET",),
        )
        == 3
    )

    document = only_record(config)
    assert document["status"] == "launched"
    launch = document["launch"]
    assert list(launch) == [
        "committed_at",
        "harness",
        "executable",
        "invocation",
        "plan_fingerprint",
        "environment",
        "unset_environment",
    ]
    assert launch["harness"] == "Stub"
    assert launch["executable"] == str(stub)
    assert launch["invocation"] == [str(stub)]
    assert launch["plan_fingerprint"] == plan.fingerprint
    assert launch["environment"] == {"FACTORY_PIN": "1"}
    assert launch["unset_environment"] == ["FACTORY_UNSET"]
    datetime.fromisoformat(launch["committed_at"])

    bare = tmp_path / "bare-config"
    bare_stub = stub_executable(tmp_path / "bare-stub")
    assert launch_with_record(bare, bare_stub, monkeypatch) == 0
    document = only_record(bare)
    assert list(document["launch"]) == [
        "committed_at",
        "harness",
        "executable",
        "invocation",
        "plan_fingerprint",
    ]


def test_a_failed_launch_commit_refuses_before_process_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub = stub_executable(tmp_path)

    def broken(self: _runrecord.RunRecord, document: dict[str, object]) -> None:
        raise _runrecord.RunRecordError("injected store failure")

    monkeypatch.setattr(_runrecord.RunRecord, "_store", broken)
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *args: pytest.fail("process creation must not be attempted"),
    )

    with (
        pytest.raises(_runrecord.RunRecordError),
        _runrecord.run_record(["Fix"], "codex"),
        run_spool() as spool,
    ):
        _harness.launch(sealed_plan(stub, tmp_path), spool)

    document = only_record(tmp_path / "config")
    assert document["status"] == "open"
    assert "launch" not in document


def test_launch_without_an_active_run_record_writes_nothing(tmp_path: Path) -> None:
    stub = stub_executable(tmp_path)

    with run_spool() as spool:
        status = _harness.launch(sealed_plan(stub, tmp_path), spool)

    assert status == 0
    assert not (tmp_path / "config" / "factory").exists()


def test_post_launch_outcomes_are_recorded_best_effort(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exited = tmp_path / "exited-config"
    stub = stub_executable(tmp_path, "exit 3\n")
    assert launch_with_record(exited, stub, monkeypatch) == 3
    document = only_record(exited)
    assert document["status"] == "launched"
    assert document["outcome"]["kind"] == "exit"
    assert document["outcome"]["status"] == 3
    datetime.fromisoformat(document["outcome"]["at"])

    spawned = tmp_path / "spawned-config"
    blocked = tmp_path / "blocked-bin" / "stub"
    blocked.parent.mkdir()
    blocked.write_text("#!/bin/sh\nexit 0\n")
    with pytest.raises(_harness.RunAdmissionError, match="could not start"):
        launch_with_record(spawned, blocked, monkeypatch)
    document = only_record(spawned)
    assert document["status"] == "launched"
    assert document["outcome"]["kind"] == "spawn-failure"
    assert "Permission denied" in document["outcome"]["detail"]

    signaled_config = tmp_path / "signaled-config"
    signaled_config.mkdir(parents=True)
    killer = stub_executable(tmp_path / "signaled", "kill -USR1 $$\n")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(signaled_config))
    factory_pid = os.fork()
    if factory_pid == 0:
        os.setpgid(0, 0)
        try:
            with _runrecord.run_record(["Fix"], "codex"), run_spool() as spool:
                _harness.launch(sealed_plan(killer, signaled_config), spool)
        except BaseException:  # noqa: BLE001
            os._exit(125)
        os._exit(0)
    _, status = os.waitpid(factory_pid, 0)

    assert os.WIFSIGNALED(status)
    assert os.WTERMSIG(status) == signal.SIGUSR1
    document = only_record(signaled_config)
    assert document["status"] == "launched"
    assert document["outcome"]["kind"] == "signal"
    assert document["outcome"]["signal"] == int(signal.SIGUSR1)


def test_only_the_first_post_launch_outcome_is_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stored: list[dict[str, Any]] = []

    def capture(self: _runrecord.RunRecord, document: dict[str, object]) -> None:
        stored.append(document)

    monkeypatch.setattr(_runrecord.RunRecord, "_store", capture)
    record = _runrecord.RunRecord("run_" + "e" * 32, {"status": "launched"})
    record.record_outcome({"kind": "exit", "status": 0})
    record.record_outcome({"kind": "exit", "status": 7})
    record.record_outcome({"kind": "signal", "signal": int(signal.SIGUSR1)})

    assert len(stored) == 1
    outcome = stored[0]["outcome"]
    assert outcome["kind"] == "exit"
    assert outcome["status"] == 0
    datetime.fromisoformat(outcome["at"])


def test_a_failed_outcome_recording_never_alters_the_run_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_store = _runrecord.RunRecord._store

    def selective(self: _runrecord.RunRecord, document: dict[str, object]) -> None:
        if "outcome" in document:
            raise _runrecord.RunRecordError("injected store failure")
        real_store(self, document)

    monkeypatch.setattr(_runrecord.RunRecord, "_store", selective)
    stub = stub_executable(tmp_path, "exit 3\n")

    assert launch_with_record(tmp_path / "config", stub, monkeypatch) == 3

    document = only_record(tmp_path / "config")
    assert document["status"] == "launched"
    assert "outcome" not in document


def test_the_run_record_serialization_is_deterministic_and_versioned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        _runrecord, "_timestamp", lambda: "2026-01-01T00:00:00.000000+00:00"
    )
    monkeypatch.setattr(_runrecord.secrets, "token_hex", lambda size: "ab" * size)

    with _runrecord.run_record(["Fix", "it"], "codex") as record:
        pass

    identifier = "run_" + "ab" * 16
    assert record.identifier == identifier
    expected = (
        "{\n"
        '  "schema": "factory-run-v1",\n'
        f'  "id": "{identifier}",\n'
        '  "argv": {\n'
        '    "schema": "factory-argv-v1",\n'
        '    "argv": [\n'
        '      "Fix",\n'
        '      "it"\n'
        "    ]\n"
        "  },\n"
        '  "harness": "codex",\n'
        '  "opened_at": "2026-01-01T00:00:00.000000+00:00",\n'
        '  "status": "open"\n'
        "}\n"
    ).encode("ascii")
    assert (
        runs_directory(tmp_path / "config") / f"{identifier}.json"
    ).read_bytes() == (expected)
    assert read_record(tmp_path / "config", f"{identifier}.json") == json.loads(
        expected
    )

    record.refuse("selection", "refused cause")
    refused = (
        "{\n"
        '  "schema": "factory-run-v1",\n'
        f'  "id": "{identifier}",\n'
        '  "argv": {\n'
        '    "schema": "factory-argv-v1",\n'
        '    "argv": [\n'
        '      "Fix",\n'
        '      "it"\n'
        "    ]\n"
        "  },\n"
        '  "harness": "codex",\n'
        '  "opened_at": "2026-01-01T00:00:00.000000+00:00",\n'
        '  "status": "refused",\n'
        '  "refusal": {\n'
        '    "stage": "selection",\n'
        '    "detail": "refused cause",\n'
        '    "at": "2026-01-01T00:00:00.000000+00:00"\n'
        "  }\n"
        "}\n"
    ).encode("ascii")
    assert (
        runs_directory(tmp_path / "config") / f"{identifier}.json"
    ).read_bytes() == (refused)


def admitted_run(executable: Path) -> _harness.PreparedRun:
    """The admitted inputs one Runner freezes for the stub executable."""

    info = executable.stat()
    return _harness.PreparedRun(
        harness="Codex",
        executable=executable,
        invocation=(str(executable),),
        executable_identity=(info.st_dev, info.st_ino),
        executable_sha256=hashlib.sha256(executable.read_bytes()).hexdigest(),
        unset_environment=_codex._CODEX_MANAGED_MARKERS,
    )


def test_an_admitted_run_reads_back_the_frozen_final_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "config"
    worktree = tmp_path / "worktree"
    registered_store(config, worktree)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    selection = RunSelection(
        client_id=CLIENT_ID,
        project_id=PROJECT_ID,
        repository_id=REPOSITORY_ID,
        checkout_id=CHECKOUT_ID,
        boundary=worktree,
    )
    stub = stub_executable(tmp_path)

    with _runrecord.run_record(["Fix"], "codex") as record:
        record.selected(selection)
        gate = _rungate.run_gate(record, selection, "codex")
        plan = _rungate.seal_validated_run_plan(
            gate,
            admitted_run(stub),
            b"# Run Request\n\n## Instruction\n\nFix",
        )
        with run_spool() as spool:
            assert _harness.launch(plan, spool) == 0

    document = _runrecord.read_run_record(
        runs_directory(config) / f"{record.identifier}.json"
    )
    assert list(document) == [
        "schema",
        "id",
        "argv",
        "harness",
        "opened_at",
        "status",
        "selection",
        "gate",
        "launch",
        "outcome",
    ]
    assert document["status"] == "launched"
    assert document["selection"] == {
        "client_id": CLIENT_ID,
        "project_id": PROJECT_ID,
        "repository_id": REPOSITORY_ID,
        "checkout_id": CHECKOUT_ID,
        "boundary": str(worktree),
    }
    assert set(document["gate"]) == {
        "at",
        "head",
        "object_format",
        "proof",
        "git",
        "policy",
    }
    assert document["launch"]["plan_fingerprint"] == plan.fingerprint
    assert document["outcome"] == {
        "kind": "exit",
        "status": 0,
        "at": document["outcome"]["at"],
    }


def test_a_terminal_record_is_never_rewritten(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "config"
    assert invoke(config, "Fix").returncode == 1
    path = only_record_file(config)
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["status"] == "refused"
    terminal = _runrecord.RunRecord(document["id"], document)

    terminal.refuse("admission", "late cause")
    terminal.record_outcome({"kind": "exit", "status": 0})
    with pytest.raises(_runrecord.RunRecordError):
        terminal.selected(
            RunSelection(
                client_id=CLIENT_ID,
                project_id=PROJECT_ID,
                repository_id=REPOSITORY_ID,
                checkout_id=CHECKOUT_ID,
                boundary=config,
            )
        )
    with pytest.raises(_runrecord.RunRecordError):
        terminal.gated(
            SimpleNamespace(
                head="0" * 40,
                object_format="sha1",
                pins={},
                assessment={},
                policy={},
            )
        )
    with pytest.raises(_runrecord.RunRecordError):
        terminal.commit_launch(sealed_plan(stub_executable(tmp_path), config))

    assert path.read_bytes() == _runrecord._serialize(document)
    assert _runrecord.read_run_record(path) == document

    stored: list[dict[str, Any]] = []
    monkeypatch.setattr(
        _runrecord.RunRecord, "_store", lambda self, update: stored.append(update)
    )
    finalized = _runrecord.RunRecord(
        "run_" + "f" * 32,
        {"status": "launched", "launch": {"committed_at": "committed"}},
    )
    finalized.record_outcome({"kind": "exit", "status": 0})
    finalized.refuse("admission", "late cause")
    assert len(stored) == 1
    assert stored[0]["outcome"]["kind"] == "exit"


def test_a_crash_leaves_the_honest_unknown_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "config"
    config.mkdir(parents=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    stub = stub_executable(tmp_path)

    crashed_pid = os.fork()
    if crashed_pid == 0:
        os.setpgid(0, 0)
        try:
            with _runrecord.run_record(["Fix"], "codex"):
                os._exit(9)
        except BaseException:  # noqa: BLE001
            os._exit(125)
    _, crashed_status = os.waitpid(crashed_pid, 0)
    assert os.WIFEXITED(crashed_status)
    assert os.WEXITSTATUS(crashed_status) == 9

    opened = only_record(config)
    assert opened["status"] == "open"
    assert "selection" not in opened
    read_back = _runrecord.read_run_record(
        runs_directory(config) / f"{opened['id']}.json"
    )
    assert read_back["status"] == "open"
    assert "outcome" not in read_back

    launched_pid = os.fork()
    if launched_pid == 0:
        os.setpgid(0, 0)
        try:
            with (
                _runrecord.run_record(["Fix"], "codex"),
                run_spool(),
            ):
                _runrecord.commit_pending_launch(sealed_plan(stub, config))
                os._exit(9)
        except BaseException:  # noqa: BLE001
            os._exit(125)
    _, launched_status = os.waitpid(launched_pid, 0)
    assert os.WIFEXITED(launched_status)
    assert os.WEXITSTATUS(launched_status) == 9

    crashed = latest_record(config)
    assert crashed["status"] == "launched"
    assert "outcome" not in crashed
    read_back = _runrecord.read_run_record(
        runs_directory(config) / f"{crashed['id']}.json"
    )
    assert read_back["status"] == "launched"
    assert "outcome" not in read_back


def test_readback_refuses_unrecognized_records(tmp_path: Path) -> None:
    runs = runs_directory(tmp_path)
    runs.mkdir(parents=True)
    timestamp = "2026-01-01T00:00:00.000000+00:00"
    document: dict[str, Any] = {
        "schema": "factory-run-v1",
        "id": "run_" + "9" * 32,
        "argv": {"schema": "factory-argv-v1", "argv": ["Fix"]},
        "harness": "codex",
        "opened_at": timestamp,
        "status": "open",
    }
    launch = {
        "committed_at": timestamp,
        "harness": "Codex",
        "executable": "/bin/stub",
        "invocation": ["/bin/stub"],
        "plan_fingerprint": "f" * 64,
    }

    def written(name: str, payload: bytes) -> Path:
        path = runs / name
        path.write_bytes(payload)
        return path

    def refusing(name: str, change: Callable[[dict[str, Any]], None]) -> Path:
        case = json.loads(json.dumps(document))
        change(case)
        return written(name, _runrecord._serialize(case))

    complete = written(f"run_{'9' * 32}.json", _runrecord._serialize(document))
    assert _runrecord.read_run_record(complete) == document

    refusal = {"stage": "selection", "detail": "cause", "at": timestamp}
    unrecognized = [
        refusing(
            "foreign.json",
            lambda case: case.update(schema="factory-run-v2", id="run_" + "a" * 32),
        ),
        refusing("unknown.json", lambda case: case.update(extra=True)),
        refusing("missing.json", lambda case: case.pop("harness")),
        refusing("status.json", lambda case: case.update(status="finished")),
        refusing(
            "argv.json", lambda case: case.update(argv={"schema": "other", "argv": []})
        ),
        refusing("opened.json", lambda case: case.update(opened_at="not-a-timestamp")),
        refusing("carrying.json", lambda case: case.update(refusal=refusal)),
        refusing("norefusal.json", lambda case: case.update(status="refused")),
        refusing("nolaunch.json", lambda case: case.update(status="launched")),
        refusing(
            "refused-launch.json",
            lambda case: case.update(status="refused", refusal=refusal, launch=launch),
        ),
        refusing(
            "launched-refusal.json",
            lambda case: case.update(status="launched", launch=launch, refusal=refusal),
        ),
        refusing(
            "outcome.json",
            lambda case: case.update(
                status="launched",
                launch=launch,
                outcome={"kind": "paused", "at": timestamp},
            ),
        ),
        refusing(
            "commit.json",
            lambda case: case.update(
                status="launched", launch={**launch, "invocation": None}
            ),
        ),
        refusing(
            "mismatched.json",
            lambda case: case.update(id="run_" + "b" * 32),
        ),
    ]
    for path in unrecognized:
        with pytest.raises(_runrecord.RunRecordError):
            _runrecord.read_run_record(path)

    with pytest.raises(_runrecord.RunRecordError):
        _runrecord.read_run_record(written("object.json", b"[]\n"))
    with pytest.raises(_runrecord.RunRecordError):
        _runrecord.read_run_record(written("broken.json", b"{\n"))
    linked = written(
        "real.json", _runrecord._serialize({**document, "id": "run_" + "c" * 32})
    )
    alias = runs / f"run_{'c' * 32}.json"
    alias.symlink_to(linked)
    with pytest.raises(_runrecord.RunRecordError):
        _runrecord.read_run_record(alias)


def test_the_run_path_never_reads_the_run_record_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = tmp_path / "config"
    runs = runs_directory(config)
    runs.mkdir(parents=True)
    planted = runs / f"run_{'8' * 32}.json"
    planted.write_text("{ not json", encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))

    def forbidden(path: Path) -> dict[str, object]:
        pytest.fail("the Run path read the Run Record store")

    monkeypatch.setattr(_runrecord, "read_run_record", forbidden)
    monkeypatch.setattr(sys, "argv", ["factory", "Fix"])

    assert factory.main() == 1
    assert "No identity store exists" in capsys.readouterr().err
    assert planted.read_text(encoding="utf-8") == "{ not json"
    assert len(list(runs.glob("run_*.json"))) == 2

    stub = stub_executable(tmp_path)
    assert launch_with_record(tmp_path / "launch-config", stub, monkeypatch) == 0
    document = only_record(tmp_path / "launch-config")
    assert document["outcome"]["status"] == 0
