"""The Project Doctor: read-only observation of the Shared Run Gate."""

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import _runrecord
import factory

FACTORY = Path(__file__).resolve().parents[1] / "factory.py"

CLIENT_ID = "cli_" + "a" * 32
PROJECT_ID = "prj_" + "b" * 32
REPOSITORY_ID = "repo_" + "c" * 32
CHECKOUT_ID = "chk_" + "d" * 32
OTHER_CLIENT_ID = "cli_" + "1" * 32
OTHER_PROJECT_ID = "prj_" + "2" * 32
OTHER_REPOSITORY_ID = "repo_" + "3" * 32
OTHER_CHECKOUT_ID = "chk_" + "4" * 32

NO_STORE = (
    "No identity store exists; register a Project first with "
    "'factory project register CLIENT/KEY PATH'."
)
MIGRATE_FIRST = "projects registry schema v1; run 'factory project migrate' first."
AMBIGUOUS_TARGET = (
    "Project acme/web has several Checkouts and no valid default Checkout; "
    "select one explicitly with --checkout PATH or set a default with "
    "'factory project set-default-checkout acme/web PATH'."
)
NO_CHECKOUTS = (
    "No Run is admissible: Project acme/web has no Checkouts; register one "
    "with 'factory checkout register acme/web PATH'."
)
STANDING_LABEL = (
    "Doctor observations are not Run admission; a Run re-validates every "
    "check before launch."
)
GATE_STAGES = ("store-graph", "scope", "association", "proof", "drift", "git")
SECTION_ORDER = (
    "Resolution",
    "Inventory",
    "Shared Repository topology",
    "Shared Run Gate observations",
    "Summary",
    STANDING_LABEL,
)
SECTION_HEADERS = (
    "Resolution",
    "Inventory",
    "Shared Repository topology",
    "Shared Run Gate observations",
    "Summary",
)


def invoke(
    *args: str, env: dict[str, str], cwd: Path | None = None
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [sys.executable, str(FACTORY), *args],
        check=False,
        capture_output=True,
        cwd=cwd,
        env={**os.environ, **env},
    )


def git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def git_repo(path: Path) -> None:
    """Create one non-bare Git repository with a readable commit HEAD."""
    path.mkdir(parents=True)
    git("init", "-q", "-b", "main", cwd=path)
    git(
        "-c",
        "user.email=factory@example.com",
        "-c",
        "user.name=Factory",
        "commit",
        "--allow-empty",
        "-m",
        "initial",
        cwd=path,
    )


def pin(path: Path) -> dict[str, object]:
    info = path.stat()
    return {"path": str(path), "st_dev": info.st_dev, "st_ino": info.st_ino}


def repository_record(repository_id: str, repo: Path) -> dict[str, object]:
    return {"id": repository_id, "name": None, "common_dir": pin(repo / ".git")}


def checkout_record(
    checkout_id: str,
    project_id: str,
    repository_id: str,
    repo: Path,
) -> dict[str, object]:
    git_dir = repo / ".git"
    return {
        "id": checkout_id,
        "project_id": project_id,
        "repository_id": repository_id,
        "boundary": pin(repo),
        "worktree_root": pin(repo),
        "git_dir": pin(git_dir),
        "common_dir": pin(git_dir),
    }


def project_record(
    project_id: str,
    client_id: str,
    key: str,
    default_checkout_id: str | None,
) -> dict[str, object]:
    return {
        "id": project_id,
        "client_id": client_id,
        "key": key,
        "name": None,
        "default_checkout_id": default_checkout_id,
    }


def client_record(client_id: str, key: str) -> dict[str, object]:
    return {"id": client_id, "key": key, "name": None}


def store_document(**overrides: object) -> dict[str, object]:
    """One v2 store holding acme/web; pass whole sections to override."""
    document: dict[str, object] = {
        "version": 2,
        "clients": [client_record(CLIENT_ID, "acme")],
        "projects": [project_record(PROJECT_ID, CLIENT_ID, "web", CHECKOUT_ID)],
        "repositories": [
            {"id": REPOSITORY_ID, "name": None, "common_dir": pin_of_unused()}
        ],
        "checkouts": [],
        "legacy": {},
    }
    document.update(overrides)
    return document


def pin_of_unused() -> dict[str, object]:
    return {"path": "/unused/repo/.git", "st_dev": 1, "st_ino": 2}


def write_store(config: Path, document: dict[str, object]) -> Path:
    registry = config / "factory" / "projects.json"
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text(json.dumps(document), encoding="utf-8")
    return registry


def registered_checkout(
    tmp_path: Path, *, directory: str = "repo", key: str = "web"
) -> tuple[dict[str, str], Path, Path]:
    """Register one real Checkout; return the env, the repo, and the config."""
    repo = tmp_path / directory
    git_repo(repo)
    config = tmp_path / "config"
    write_store(
        config,
        store_document(
            repositories=[repository_record(REPOSITORY_ID, repo)],
            checkouts=[checkout_record(CHECKOUT_ID, PROJECT_ID, REPOSITORY_ID, repo)],
            projects=[project_record(PROJECT_ID, CLIENT_ID, key, CHECKOUT_ID)],
        ),
    )
    return {"XDG_CONFIG_HOME": str(config)}, repo, config


def run_doctor(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], *args: str
) -> tuple[int, str, str]:
    """Invoke the CLI in-process so module seams stay monkeypatchable."""
    monkeypatch.setattr(sys, "argv", ["factory", *args])
    status = factory.main()
    captured = capsys.readouterr()
    return status, captured.out, captured.err


def matched_policy(monkeypatch: pytest.MonkeyPatch, cache: Path) -> SimpleNamespace:
    """One matched-policy double whose probe cache lives at ``cache``."""
    payload = (
        json.dumps(
            {
                "schema": 1,
                "success": True,
                "factory_version": "0.1.2",
                "codex_version": "0.160.0",
                "executable_sha256": "0" * 64,
                "operating_system": "Linux",
                "architecture": "x86_64",
                "host_identity": {"release": "test", "version": "test"},
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        + b"\n"
    )
    policy = SimpleNamespace(
        version="0.160.0",
        install_channel="github-asset",
        _host_probe_cache_payload=lambda: payload,
        _host_probe_cache_path=lambda _payload: cache,
        _inspect_strict_configuration=lambda boundary, enclosing: None,
    )
    monkeypatch.setattr(
        "_codex._match_compatibility", lambda executable, version: policy
    )
    return policy


def test_doctor_help_is_stable_stdout() -> None:
    wide = invoke("doctor", "--help", env={"COLUMNS": "200"})
    narrow = invoke("doctor", "-h", env={"COLUMNS": "20"})

    assert wide.returncode == narrow.returncode == 0
    assert wide.stdout == narrow.stdout
    assert wide.stderr == narrow.stderr == b""
    assert wide.stdout.startswith(
        b"Usage:\n"
        b"  factory doctor project [--] SELECTOR [--checkout PATH] "
        b"[--harness NAME]\n"
        b"  factory doctor (-h | --help)\n"
    )
    for detail in (
        b"Doctor observations are not Run admission",
        b"a Run re-validates every check",
        b"--checkout PATH",
        b"--harness NAME",
        b"never runs the Host Admission Probe",
        b"never submits a Run Request",
    ):
        assert detail in wide.stdout


@pytest.mark.parametrize(
    "arguments",
    (
        ("doctor",),
        ("doctor", "inventory"),
        ("doctor", "project"),
        ("doctor", "project", "acme/web", "extra"),
        ("doctor", "project", "acme/web", "--harness", "vim"),
        ("doctor", "project", "acme/web", "--checkout"),
        ("doctor", "project", "acme/web", "--checkout", "/a", "--checkout", "/b"),
        ("doctor", "project", "acme/web", "--file", "notes.txt"),
        ("doctor", "project", "acme/web", "--path", "/a"),
        ("doctor", "project", "acme/web", "--unknown"),
    ),
)
def test_doctor_usage_errors_exit_two_with_the_doctor_synopsis(
    arguments: tuple[str, ...],
) -> None:
    result = invoke(*arguments, env={})

    assert result.returncode == 2
    assert result.stdout == b""
    assert result.stderr.startswith(b"factory: error: ")
    assert b"Usage:\n  factory doctor project " in result.stderr
    assert b"Try 'factory doctor --help' for more information.\n" in result.stderr
    assert b"Traceback" not in result.stderr


def test_only_exact_lowercase_doctor_enters_the_doctor_namespace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _env, _repo, _config = registered_checkout(tmp_path)
    monkeypatch.chdir(tmp_path)

    escaped, out, err = run_doctor(monkeypatch, capsys, "--", "doctor", "project")
    assert escaped == 1
    assert out == ""
    assert "No Git worktree encloses the invocation directory" in err

    cased, out, err = run_doctor(monkeypatch, capsys, "DOCTOR", "project", "acme/web")
    assert cased == 1
    assert out == ""
    assert "No Git worktree encloses the invocation directory" in err


def test_doctor_store_state_refusals_match_the_run_precedence(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config"
    env = {"XDG_CONFIG_HOME": str(config)}

    missing = invoke("doctor", "project", "acme/web", env=env)
    assert missing.returncode == 1
    assert missing.stdout == b""
    assert missing.stderr == f"factory: error: {NO_STORE}\n".encode()

    config.joinpath("factory").mkdir(parents=True)
    config.joinpath("factory", "projects.json").write_text(
        json.dumps({"version": 1, "projects": {"web": "/unused"}}),
        encoding="utf-8",
    )
    legacy = invoke("doctor", "project", "acme/web", env=env)
    assert legacy.returncode == 1
    assert legacy.stdout == b""
    assert legacy.stderr == f"factory: error: {MIGRATE_FIRST}\n".encode()

    config.joinpath("factory", "projects.json").write_text(
        "{ not json", encoding="utf-8"
    )
    malformed = invoke("doctor", "project", "acme/web", env=env)
    assert malformed.returncode == 1
    assert malformed.stdout == b""
    assert malformed.stderr.startswith(b"factory: error: malformed Project Registry:")


def test_doctor_selector_refusals_are_verbatim(tmp_path: Path) -> None:
    env, _repo, config = registered_checkout(tmp_path)
    write_store(
        config,
        store_document(
            clients=[
                client_record(CLIENT_ID, "acme"),
                client_record(OTHER_CLIENT_ID, "globex"),
            ],
            projects=[
                project_record(PROJECT_ID, CLIENT_ID, "web", None),
                project_record(OTHER_PROJECT_ID, OTHER_CLIENT_ID, "web", None),
            ],
        ),
    )

    unknown_client = invoke("doctor", "project", "nope/x", env=env)
    assert unknown_client.returncode == 1
    assert unknown_client.stdout == b""
    assert unknown_client.stderr == (
        b'factory: error: Client "nope" is not registered.\n'
    )

    unknown = invoke("doctor", "project", "acme/nope", env=env)
    assert unknown.returncode == 1
    assert unknown.stdout == b""
    assert unknown.stderr == (
        b'factory: error: Project "acme/nope" is not registered.\n'
    )

    ambiguous = invoke("doctor", "project", "web", env=env)
    assert ambiguous.returncode == 1
    assert ambiguous.stdout == b""
    assert ambiguous.stderr == (
        b'factory: error: Project key "web" is ambiguous: "acme/web", "globex/web".\n'
    )


def test_doctor_target_selection_shares_the_run_refusal_table(
    tmp_path: Path,
) -> None:
    env, repo, config = registered_checkout(tmp_path)
    other = tmp_path / "other"
    git_repo(other)
    write_store(
        config,
        store_document(
            clients=[client_record(CLIENT_ID, "acme")],
            projects=[
                project_record(PROJECT_ID, CLIENT_ID, "web", CHECKOUT_ID),
                project_record(OTHER_PROJECT_ID, CLIENT_ID, "other", OTHER_CHECKOUT_ID),
            ],
            repositories=[
                repository_record(REPOSITORY_ID, repo),
                repository_record(OTHER_REPOSITORY_ID, other),
            ],
            checkouts=[
                checkout_record(CHECKOUT_ID, PROJECT_ID, REPOSITORY_ID, repo),
                checkout_record(
                    OTHER_CHECKOUT_ID, OTHER_PROJECT_ID, OTHER_REPOSITORY_ID, other
                ),
            ],
        ),
    )

    doctor = invoke("doctor", "project", "acme/web", "--checkout", str(other), env=env)
    run = invoke("--project", "acme/web", "--checkout", str(other), "Fix", env=env)
    assert doctor.returncode == run.returncode == 1
    assert doctor.stdout == b""
    assert doctor.stderr == run.stderr
    assert b"belongs to Project" in doctor.stderr

    interior_directory = repo / "deep"
    interior_directory.mkdir()
    interior = invoke(
        "doctor", "project", "acme/web", "--checkout", str(interior_directory), env=env
    )
    assert interior.returncode == 1
    assert interior.stdout == b""
    assert b"must exactly" in interior.stderr


def test_the_ambiguous_target_diagnostic_is_shared_with_the_run(
    tmp_path: Path,
) -> None:
    env, repo, config = registered_checkout(tmp_path)
    second = tmp_path / "second"
    git_repo(second)
    write_store(
        config,
        store_document(
            projects=[project_record(PROJECT_ID, CLIENT_ID, "web", None)],
            repositories=[
                repository_record(REPOSITORY_ID, repo),
                repository_record(OTHER_REPOSITORY_ID, second),
            ],
            checkouts=[
                checkout_record(CHECKOUT_ID, PROJECT_ID, REPOSITORY_ID, repo),
                checkout_record(
                    OTHER_CHECKOUT_ID, PROJECT_ID, OTHER_REPOSITORY_ID, second
                ),
            ],
        ),
    )

    doctor = invoke("doctor", "project", "acme/web", env=env)
    run = invoke("--project", "acme/web", "Fix", env=env)

    assert doctor.returncode == run.returncode == 1
    assert doctor.stdout == b""
    assert doctor.stderr == run.stderr
    assert doctor.stderr == f"factory: error: {AMBIGUOUS_TARGET}\n".encode()


def test_doctor_reports_a_project_without_checkouts_as_a_finding(
    tmp_path: Path,
) -> None:
    env, _repo, config = registered_checkout(tmp_path)
    write_store(
        config,
        store_document(
            checkouts=[],
            projects=[project_record(PROJECT_ID, CLIENT_ID, "web", None)],
        ),
    )

    result = invoke("doctor", "project", "acme/web", env=env)

    assert result.returncode == 0
    assert result.stderr == b""
    text = result.stdout.decode("utf-8")
    assert NO_CHECKOUTS in text
    assert "A Run would refuse before the Shared Run Gate" in text
    assert STANDING_LABEL in text


def test_doctor_reports_resolution_inventory_and_a_fresh_gate(
    tmp_path: Path,
) -> None:
    env, _repo, _config = registered_checkout(tmp_path)

    result = invoke("doctor", "project", "acme/web", env=env)

    assert result.returncode == 0
    assert result.stderr == b""
    text = result.stdout.decode("utf-8")
    positions = [text.index(section) for section in SECTION_ORDER]
    assert positions == sorted(positions)
    assert "Project Doctor report for acme/web" in text
    assert f"Checkout: {CHECKOUT_ID} at" in text
    for stage in GATE_STAGES:
        assert f"  {stage}: ok (observed)" in text
    assert "A Run would pass the Shared Run Gate." in text
    assert text.rstrip("\n").endswith(STANDING_LABEL)


def test_every_reported_fact_carries_a_provenance_label(tmp_path: Path) -> None:
    env, _repo, _config = registered_checkout(tmp_path)

    result = invoke("doctor", "project", "acme/web", env=env)

    assert result.returncode == 0
    lines = result.stdout.decode("utf-8").splitlines()
    provenances = (
        "(stored)",
        "(observed)",
        "(cached)",
        "(not reached)",
        "(configured)",
    )
    section = None
    for line in lines:
        if line.startswith("Project Doctor report") or line == STANDING_LABEL:
            continue
        if line in SECTION_HEADERS:
            section = line
            continue
        if section == "Summary" or line.startswith("    "):
            continue
        assert line.endswith(provenances), line


def test_doctor_not_reached_after_the_first_failing_gate_stage(
    tmp_path: Path,
) -> None:
    env, repo, _config = registered_checkout(tmp_path)
    moved = repo.parent / "moved"
    repo.rename(moved)

    result = invoke("doctor", "project", "acme/web", env=env)

    assert result.returncode == 0
    text = result.stdout.decode("utf-8")
    assert "  proof: fail (observed)" in text
    assert "  drift: not reached (not reached)" in text
    assert "  git: not reached (not reached)" in text
    assert "the gate stops at the proof stage" in text
    assert "A Run would stop at the proof stage." in text
    assert f"Checkout: {CHECKOUT_ID} at" in text
    assert f"{CHECKOUT_ID} at" in text
    assert "stale (observed)" in text


def test_a_doctor_dirty_assessment_warns_and_an_unknown_one_refuses(
    tmp_path: Path,
) -> None:
    env, repo, _config = registered_checkout(tmp_path)
    (repo / "untracked.txt").write_bytes(b"new\n")

    dirty = invoke("doctor", "project", "acme/web", env=env)
    assert dirty.returncode == 0
    dirty_text = dirty.stdout.decode("utf-8")
    assert "  git: warn (observed)" in dirty_text
    assert "is dirty at HEAD" in dirty_text
    assert "Ignored paths are excluded" in dirty_text
    assert "A Run would pass the Shared Run Gate with the dirty warning." in dirty_text

    unknown_repo = tmp_path / "unknown-repo"
    git_repo(unknown_repo)
    unknown_env = {"XDG_CONFIG_HOME": str(tmp_path / "unknown-config")}
    write_store(
        tmp_path / "unknown-config",
        store_document(
            repositories=[repository_record(REPOSITORY_ID, unknown_repo)],
            checkouts=[
                checkout_record(CHECKOUT_ID, PROJECT_ID, REPOSITORY_ID, unknown_repo)
            ],
        ),
    )
    (unknown_repo / "tracked.txt").write_bytes(b"moved\n")
    git("add", "tracked.txt", cwd=unknown_repo)
    git("update-index", "--skip-worktree", "tracked.txt", cwd=unknown_repo)

    unknown = invoke("doctor", "project", "acme/web", env=unknown_env)
    assert unknown.returncode == 0
    unknown_text = unknown.stdout.decode("utf-8")
    assert "  git: fail (observed)" in unknown_text
    assert "the Checkout Git Assessment cannot establish the state" in unknown_text
    assert "A Run would stop at the git stage." in unknown_text


def test_doctor_names_the_stale_component_and_remedy_while_continuing(
    tmp_path: Path,
) -> None:
    env, repo, config = registered_checkout(tmp_path)
    sibling = tmp_path / "sibling"
    git_repo(sibling)
    write_store(
        config,
        store_document(
            repositories=[repository_record(REPOSITORY_ID, repo)],
            checkouts=[
                checkout_record(CHECKOUT_ID, PROJECT_ID, REPOSITORY_ID, repo),
                checkout_record(OTHER_CHECKOUT_ID, PROJECT_ID, REPOSITORY_ID, sibling),
            ],
        ),
    )
    moved = tmp_path / "moved-sibling"
    sibling.rename(moved)

    result = invoke("doctor", "project", "acme/web", env=env)

    assert result.returncode == 0
    text = result.stdout.decode("utf-8")
    assert f"Checkout: {OTHER_CHECKOUT_ID} at" in text
    assert "stale (observed)" in text
    assert "factory project checkout re-register" in text
    assert "Shared Repository topology" in text
    assert "Shared Run Gate observations" in text


def test_doctor_surfaces_shared_repository_topology_prominently(
    tmp_path: Path,
) -> None:
    env, repo, config = registered_checkout(tmp_path)
    shared_repo = tmp_path / "shared"
    git_repo(shared_repo)
    write_store(
        config,
        store_document(
            clients=[
                client_record(CLIENT_ID, "acme"),
                client_record(OTHER_CLIENT_ID, "globex"),
            ],
            projects=[
                project_record(PROJECT_ID, CLIENT_ID, "web", CHECKOUT_ID),
                project_record(
                    OTHER_PROJECT_ID, OTHER_CLIENT_ID, "other", OTHER_CHECKOUT_ID
                ),
            ],
            repositories=[
                repository_record(REPOSITORY_ID, repo),
                repository_record(OTHER_REPOSITORY_ID, shared_repo),
            ],
            checkouts=[
                checkout_record(CHECKOUT_ID, PROJECT_ID, OTHER_REPOSITORY_ID, repo),
                checkout_record(
                    OTHER_CHECKOUT_ID,
                    OTHER_PROJECT_ID,
                    OTHER_REPOSITORY_ID,
                    shared_repo,
                ),
            ],
        ),
    )

    result = invoke("doctor", "project", "acme/web", env=env)

    assert result.returncode == 0
    text = result.stdout.decode("utf-8")
    assert (
        f"Repository {OTHER_REPOSITORY_ID} is bound to Projects acme/web, "
        "globex/other. (stored)"
    ) in text
    assert (
        "is bound to Checkouts of more than one Client (acme, globex); "
        "a Git common directory is not partitioned by Client" in text
    )
    assert "A Run would stop at the association stage." in text


def test_doctor_harness_observations_follow_the_run_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _env, _repo, _config = registered_checkout(tmp_path)
    cache = tmp_path / "cache" / "entry.json"
    matched_policy(monkeypatch, cache)
    monkeypatch.setattr("_codex._reported_version", lambda executable: "0.160.0")

    def forbidden() -> None:
        pytest.fail("the doctor ran the Host Admission Probe")

    monkeypatch.setattr("_codex._Codex01600Policy._run_host_admission_probe", forbidden)
    monkeypatch.setattr("_claude._ClaudePolicy._run_host_admission_probe", forbidden)

    status, out, err = run_doctor(
        monkeypatch, capsys, "doctor", "project", "acme/web", "--harness", "codex"
    )

    assert status == 0
    assert err == ""
    assert "Harness observations (codex)" in out
    order = [
        "executable resolution: ok",
        "local version: ok",
        "newest stable: ok",
        "compatibility: ok",
        "strict configuration: ok",
        "host admission probe cache: ok (cached)",
    ]
    positions = [out.index(step) for step in order]
    assert positions == sorted(positions)
    assert "no Host Admission Probe cache entry exists" in out
    assert "A Run would pass the Shared Run Gate." in out


def test_doctor_harness_observations_stop_at_the_first_failure(
    tmp_path: Path,
) -> None:
    env, _repo, _config = registered_checkout(tmp_path)

    result = invoke("doctor", "project", "acme/web", "--harness", "codex", env=env)

    assert result.returncode == 0
    text = result.stdout.decode("utf-8")
    assert "  executable resolution: ok (observed)" in text
    assert "  local version: fail (observed)" in text
    assert "reported an unrecognized version" in text
    for step in (
        "newest stable",
        "compatibility",
        "strict configuration",
        "host admission probe cache",
    ):
        assert f"  {step}: not reached (not reached)" in text
    assert "the admission stops at the local version step" in text
    assert "A codex Run would refuse admission at the local version step." in text


def test_doctor_without_harness_performs_no_network_lookup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _env, _repo, _config = registered_checkout(tmp_path)

    def forbidden(
        url: str, *, timeout_seconds: float, deadline: float
    ) -> tuple[int, bytes]:
        pytest.fail(f"the doctor reached the publisher: {url}")

    monkeypatch.setattr("_publisher._http_get", forbidden)

    status, out, err = run_doctor(monkeypatch, capsys, "doctor", "project", "acme/web")

    assert status == 0
    assert err == ""
    assert "Harness observations" not in out


def test_doctor_probe_cache_state_is_cached_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _env, _repo, _config = registered_checkout(tmp_path)
    cache = tmp_path / "cache" / "entry.json"
    policy = matched_policy(monkeypatch, cache)
    monkeypatch.setattr("_codex._reported_version", lambda executable: "0.160.0")

    status, out, _err = run_doctor(
        monkeypatch, capsys, "doctor", "project", "acme/web", "--harness", "codex"
    )
    assert status == 0
    assert "host admission probe cache: ok (cached)" in out
    assert "no Host Admission Probe cache entry exists for this Factory version" in out

    cache.parent.mkdir(parents=True)
    cache.write_bytes(policy._host_probe_cache_payload())
    _status, matched, _err = run_doctor(
        monkeypatch, capsys, "doctor", "project", "acme/web", "--harness", "codex"
    )
    assert "a Host Admission Probe success is cached" in matched
    assert "codex 0.160.0, factory 0.1.2" in matched
    assert "recorded" in matched

    cache.write_bytes(policy._host_probe_cache_payload() + b"tail")
    _status, mismatched, _err = run_doctor(
        monkeypatch, capsys, "doctor", "project", "acme/web", "--harness", "codex"
    )
    assert "does not match this Factory version, artifact, and host" in mismatched


def test_doctor_never_mints_a_run_record_or_runs_the_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _env, _repo, config = registered_checkout(tmp_path)
    cache = tmp_path / "cache" / "entry.json"
    matched_policy(monkeypatch, cache)

    def forbidden_record(*_args: object, **_kwargs: object) -> None:
        pytest.fail("the doctor minted a Run Record")

    def forbidden_probe(*_args: object, **_kwargs: object) -> None:
        pytest.fail("the doctor ran the Host Admission Probe")

    monkeypatch.setattr(factory, "run_record", forbidden_record)
    monkeypatch.setattr(_runrecord, "run_record", forbidden_record)
    monkeypatch.setattr(
        "_codex._Codex01600Policy._run_host_admission_probe", forbidden_probe
    )
    monkeypatch.setattr(
        "_claude._ClaudePolicy._run_host_admission_probe", forbidden_probe
    )
    monkeypatch.setattr(
        "_codex._Codex01600Policy._store_host_probe_success", forbidden_probe
    )
    monkeypatch.setattr(
        "_claude._ClaudePolicy._store_host_probe_success", forbidden_probe
    )
    monkeypatch.setattr(
        "_codex._Codex01600Policy._invalidate_host_probe_success",
        forbidden_probe,
    )
    monkeypatch.setattr(
        "_claude._ClaudePolicy._invalidate_host_probe_success",
        forbidden_probe,
    )
    monkeypatch.setattr(factory, "select_harness", forbidden_probe)

    for harness in (None, "codex", "claude"):
        arguments = ["doctor", "project", "acme/web"]
        if harness is not None:
            arguments += ["--harness", harness]
        status, _out, err = run_doctor(monkeypatch, capsys, *arguments)
        assert status == 0, err
    assert not (config / "factory" / "runs").exists()


def test_doctor_never_mutates_identity_state(tmp_path: Path) -> None:
    env, _repo, config = registered_checkout(tmp_path)
    registry = config / "factory" / "projects.json"
    before = registry.read_bytes()

    result = invoke("doctor", "project", "acme/web", env=env)

    assert result.returncode == 0
    assert registry.read_bytes() == before
    assert not (config / "factory" / "projects.lock").exists()


def test_harness_observations_classify_configuration_by_launch_effectiveness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _env, _repo, _config = registered_checkout(tmp_path)
    matched_policy(monkeypatch, tmp_path / "cache" / "entry.json")

    _status, out, _err = run_doctor(
        monkeypatch, capsys, "doctor", "project", "acme/web", "--harness", "codex"
    )
    assert "Configuration by launch effectiveness" in out
    assert "Effective launch inputs:" in out
    assert "validated as a whole by --strict-config" in out
    assert "nothing is a gated managed input" in out
    assert (
        "not a launch input, and their presence, absence, or validity never affects admission"
        in out
    )
    assert "(configured)" in out

    _status, claude_out, _err = run_doctor(
        monkeypatch, capsys, "doctor", "project", "acme/web", "--harness", "claude"
    )
    assert (
        "the managed-policy surfaces observed by the managed-policy inspection are the gated managed inputs"
        in claude_out
    )
    assert "--strict-mcp-config confines" in claude_out
