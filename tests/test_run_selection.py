"""Run selection onto registered Checkouts: the frozen resolution contract."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import _identity as identity

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
DISCOVERY_WITHOUT_WORKTREE = (
    "No Git worktree encloses the invocation directory; start inside a "
    "registered Checkout or select one with --project or --checkout."
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


def store_document(**overrides: object) -> dict[str, object]:
    """One v2 store holding acme/web; pass whole sections to override."""
    document: dict[str, object] = {
        "version": 2,
        "clients": [{"id": CLIENT_ID, "key": "acme", "name": None}],
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
    return {"path": "/repo", "st_dev": 1, "st_ino": 2}


def write_store(config: Path, document: dict[str, object]) -> Path:
    registry = config / "factory" / "projects.json"
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text(json.dumps(document), encoding="utf-8")
    return registry


def registered_checkout(
    tmp_path: Path, *, directory: str = "repo"
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
        ),
    )
    return {"XDG_CONFIG_HOME": str(config)}, repo, config


def reached_codex_admission(result: subprocess.CompletedProcess[bytes]) -> bool:
    return result.stderr.endswith(
        b" reported an unrecognized version; install a certified Codex release.\n"
    )


def test_explicit_checkout_runs_the_registered_boundary(tmp_path: Path) -> None:
    env, repo, _config = registered_checkout(tmp_path)

    result = invoke("--checkout", str(repo), "Fix", env=env)

    assert result.returncode == 1
    assert result.stdout == b""
    assert reached_codex_admission(result)


def test_explicit_checkout_resolves_relative_paths_and_symlinks(
    tmp_path: Path,
) -> None:
    env, repo, _config = registered_checkout(tmp_path)
    link = tmp_path / "link"
    link.symlink_to(repo, target_is_directory=True)

    relative = invoke("--checkout", "repo", "Fix", env=env, cwd=tmp_path)
    aliased = invoke("--checkout", str(link), "Fix", env=env)

    assert reached_codex_admission(relative)
    assert reached_codex_admission(aliased)


@pytest.mark.parametrize(
    "argv",
    [
        ["--checkout", "packages/foo"],
        ["--project", "acme/web", "--checkout", "packages/foo"],
    ],
    ids=["bare-checkout", "combined-selectors"],
)
def test_containment_refuses_interior_paths_without_widening(
    tmp_path: Path, argv: list[str]
) -> None:
    env, repo, _config = registered_checkout(tmp_path)
    interior = repo / "packages" / "foo"
    interior.mkdir(parents=True)

    result = invoke(*argv, "Fix", env=env, cwd=repo)

    assert result.returncode == 1
    assert result.stdout == b""
    expected = (
        f'factory: error: "{interior}" is inside Checkout {CHECKOUT_ID} whose '
        f'registered boundary is "{repo}"; Run boundaries must exactly match '
        f'registered Checkout boundaries. Run at "{repo}", or make "{interior}" '
        f"a Git worktree and register it as a Checkout.\n"
    )
    assert result.stderr == expected.encode()


def test_legacy_paths_refuse_with_the_from_legacy_remedy(tmp_path: Path) -> None:
    env, repo, config = registered_checkout(tmp_path)
    legacy_repo = tmp_path / "legacy"
    git_repo(legacy_repo)
    write_store(
        config,
        store_document(
            projects=[project_record(PROJECT_ID, CLIENT_ID, "web", None)],
            repositories=[repository_record(REPOSITORY_ID, repo)],
            legacy={"api": str(legacy_repo)},
        ),
    )

    result = invoke("--checkout", str(legacy_repo), "Fix", env=env)

    assert result.returncode == 1
    expected = (
        f'factory: error: "{legacy_repo}" is recorded by legacy project "api"; '
        "it is not Run-selectable until re-registered: 'factory project register "
        f'CLIENT/KEY "{legacy_repo}" --from-legacy api\'.\n'
    )
    assert result.stderr == expected.encode()


def test_unregistered_worktree_roots_refuse_with_registration_guidance(
    tmp_path: Path,
) -> None:
    env, _repo, _config = registered_checkout(tmp_path)
    other = tmp_path / "other"
    git_repo(other)

    result = invoke("--checkout", str(other), "Fix", env=env)

    assert result.returncode == 1
    expected = (
        f'factory: error: "{other}" is a Git worktree root but not a registered '
        "Checkout boundary; register it with 'factory checkout register "
        f'CLIENT/KEY "{other}"\' or select a Project with --project.\n'
    )
    assert result.stderr == expected.encode()


def test_paths_that_are_not_worktree_roots_refuse_without_a_remedy(
    tmp_path: Path,
) -> None:
    env, _repo, _config = registered_checkout(tmp_path)
    plain = tmp_path / "plain"
    plain.mkdir()

    result = invoke("--checkout", str(plain), "Fix", env=env)

    assert result.returncode == 1
    expected = (
        f'factory: error: "{plain}" is not a Git worktree root and not a '
        f"registered Checkout boundary.\n"
    )
    assert result.stderr == expected.encode()


def test_scoped_mismatch_refuses_when_the_checkout_belongs_elsewhere(
    tmp_path: Path,
) -> None:
    env, repo, config = registered_checkout(tmp_path)
    other = tmp_path / "other"
    git_repo(other)
    write_store(
        config,
        store_document(
            clients=[
                {"id": CLIENT_ID, "key": "acme", "name": None},
                {"id": OTHER_CLIENT_ID, "key": "globex", "name": None},
            ],
            projects=[
                project_record(PROJECT_ID, CLIENT_ID, "web", CHECKOUT_ID),
                project_record(OTHER_PROJECT_ID, OTHER_CLIENT_ID, "web", None),
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

    result = invoke("--project", "acme/web", "--checkout", str(other), "Fix", env=env)

    assert result.returncode == 1
    expected = (
        f'factory: error: Checkout {OTHER_CHECKOUT_ID} at "{other}" belongs to '
        "Project globex/web, not the selected Project acme/web; the selected "
        f"Project admits only its own Checkouts.\n"
    )
    assert result.stderr == expected.encode()


def test_scoped_selection_runs_when_the_checkout_belongs_to_the_project(
    tmp_path: Path,
) -> None:
    env, repo, _config = registered_checkout(tmp_path)

    result = invoke("--project", "acme/web", "--checkout", str(repo), "Fix", env=env)

    assert result.returncode == 1
    assert reached_codex_admission(result)


def test_scoped_selection_refuses_an_unknown_project(tmp_path: Path) -> None:
    env, repo, _config = registered_checkout(tmp_path)

    result = invoke(
        "--project", "acme/missing", "--checkout", str(repo), "Fix", env=env
    )

    assert result.returncode == 1
    assert b'Project "acme/missing" is not registered' in result.stderr


def test_run_selection_requires_a_version_two_store(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    git_repo(repo)
    config = tmp_path / "config"
    env = {"XDG_CONFIG_HOME": str(config)}
    modes: list[tuple[list[str], Path]] = [
        ([], repo),
        (["--project", "acme/web"], repo),
        (["--checkout", str(repo)], repo),
    ]

    for argv, cwd in modes:
        absent = invoke(*argv, "Fix", env=env, cwd=cwd)
        assert absent.returncode == 1, argv
        assert absent.stdout == b""
        assert absent.stderr == f"factory: error: {NO_STORE}\n".encode()

    registry = config / "factory" / "projects.json"
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text('{"version": 1, "projects": {"api": "/"}}')
    for argv, cwd in modes:
        version_one = invoke(*argv, "Fix", env=env, cwd=cwd)
        assert version_one.returncode == 1, argv
        assert version_one.stderr == f"factory: error: {MIGRATE_FIRST}\n".encode()


def test_discovery_selects_the_registered_nearest_worktree_root(
    tmp_path: Path,
) -> None:
    outer = tmp_path / "outer"
    inner = outer / "inner"
    git_repo(outer)
    git_repo(inner)
    config = tmp_path / "config"
    env = {"XDG_CONFIG_HOME": str(config)}
    write_store(
        config,
        store_document(
            repositories=[repository_record(REPOSITORY_ID, inner)],
            checkouts=[checkout_record(CHECKOUT_ID, PROJECT_ID, REPOSITORY_ID, inner)],
        ),
    )
    nested = inner / "service" / "src"
    nested.mkdir(parents=True)

    result = invoke("Fix", env=env, cwd=nested)

    assert result.returncode == 1
    assert reached_codex_admission(result)


def test_discovery_stops_at_the_nearest_unregistered_worktree_root(
    tmp_path: Path,
) -> None:
    outer = tmp_path / "outer"
    inner = outer / "inner"
    git_repo(outer)
    git_repo(inner)
    config = tmp_path / "config"
    env = {"XDG_CONFIG_HOME": str(config)}
    write_store(
        config,
        store_document(
            repositories=[repository_record(REPOSITORY_ID, outer)],
            checkouts=[checkout_record(CHECKOUT_ID, PROJECT_ID, REPOSITORY_ID, outer)],
        ),
    )
    nested = inner / "service"
    nested.mkdir()

    result = invoke("Fix", env=env, cwd=nested)

    assert result.returncode == 1
    expected = (
        "factory: error: The nearest Git worktree root "
        f'"{inner}" is not a registered Checkout boundary; register it with '
        f"'factory checkout register CLIENT/KEY \"{inner}\"' or select a Project "
        f"with --project.\n"
    )
    assert result.stderr == expected.encode()


@pytest.mark.parametrize("kind", ["outside", "bare"])
def test_discovery_refuses_without_a_non_bare_worktree(
    tmp_path: Path, kind: str
) -> None:
    env, _repo, _config = registered_checkout(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    if kind == "bare":
        bare = outside / "bare.git"
        git("init", "--bare", "-q", str(bare), cwd=tmp_path)
        cwd = bare
    else:
        cwd = outside

    result = invoke("Fix", env=env, cwd=cwd)

    assert result.returncode == 1
    assert result.stderr == f"factory: error: {DISCOVERY_WITHOUT_WORKTREE}\n".encode()


def test_discovery_ignores_repository_location_overrides(tmp_path: Path) -> None:
    env, repo, _config = registered_checkout(tmp_path)
    poisoned = {**env, "GIT_DIR": str(tmp_path / "missing-git-dir")}

    result = invoke("Fix", env=poisoned, cwd=repo)

    assert result.returncode == 1
    assert reached_codex_admission(result)


def test_project_run_selects_the_default_checkout_over_later_ones(
    tmp_path: Path,
) -> None:
    env, repo, config = registered_checkout(tmp_path)
    second = tmp_path / "second"
    git_repo(second)
    write_store(
        config,
        store_document(
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

    result = invoke("--project", "acme/web", "Fix", env=env)

    assert result.returncode == 1
    assert reached_codex_admission(result)


def test_project_run_selects_the_sole_checkout_without_a_default(
    tmp_path: Path,
) -> None:
    env, repo, config = registered_checkout(tmp_path)
    write_store(
        config,
        store_document(
            projects=[project_record(PROJECT_ID, CLIENT_ID, "web", None)],
            repositories=[repository_record(REPOSITORY_ID, repo)],
            checkouts=[checkout_record(CHECKOUT_ID, PROJECT_ID, REPOSITORY_ID, repo)],
        ),
    )

    result = invoke("--project", "acme/web", "Fix", env=env)

    assert result.returncode == 1
    assert reached_codex_admission(result)


def test_project_run_refuses_multiple_checkouts_without_a_default(
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

    result = invoke("--project", "acme/web", "Fix", env=env)

    assert result.returncode == 1
    expected = (
        "factory: error: Project acme/web has several Checkouts and no valid "
        "default Checkout; select one explicitly with --checkout PATH or set a "
        "default with 'factory project set-default-checkout acme/web PATH'.\n"
    )
    assert result.stderr == expected.encode()


def test_project_run_refuses_a_project_without_checkouts(tmp_path: Path) -> None:
    env, repo, config = registered_checkout(tmp_path)
    write_store(
        config,
        store_document(
            projects=[project_record(PROJECT_ID, CLIENT_ID, "web", None)],
            repositories=[repository_record(REPOSITORY_ID, repo)],
            checkouts=[],
        ),
    )

    result = invoke("--project", "acme/web", "Fix", env=env)

    assert result.returncode == 1
    expected = (
        'factory: error: Project "acme/web" has no registered Checkout; register '
        "one with 'factory project checkout register acme/web PATH'.\n"
    )
    assert result.stderr == expected.encode()


def test_every_selector_resolves_the_same_four_immutable_identities(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env, repo, config = registered_checkout(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    expected = identity.RunSelection(
        client_id=CLIENT_ID,
        project_id=PROJECT_ID,
        repository_id=REPOSITORY_ID,
        checkout_id=CHECKOUT_ID,
        boundary=repo,
    )

    assert identity.resolve_run_checkout(str(repo), None, tmp_path) == expected
    assert identity.resolve_run_checkout(str(repo), "acme/web", tmp_path) == expected
    assert identity.resolve_run_project("acme/web") == expected
    assert identity.resolve_run_project("web") == expected
    assert identity.resolve_run_discovery(repo) == expected


def test_project_run_refuses_a_stale_checkout_without_repair(
    tmp_path: Path,
) -> None:
    env, repo, _config = registered_checkout(tmp_path)
    repo.rename(tmp_path / "moved")

    result = invoke("--project", "acme/web", "Fix", env=env)

    assert result.returncode == 1
    assert (
        result.stderr
        == (
            f'factory: error: Checkout "{CHECKOUT_ID}" is stale: path does not exist.\n'
        ).encode()
    )
    assert not (tmp_path / "config" / "factory" / "projects.lock").exists()
