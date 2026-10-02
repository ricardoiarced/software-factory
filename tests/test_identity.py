"""The version-two identity store: records, registration, and migration."""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import _identity as identity
import pytest

FACTORY = Path(__file__).resolve().parents[1] / "factory.py"

CLIENT_ID = "cli_" + "a" * 32
PROJECT_ID = "prj_" + "b" * 32
REPOSITORY_ID = "repo_" + "c" * 32
CHECKOUT_ID = "chk_" + "d" * 32

ID_PATTERNS = {
    "clients": r"cli_[0-9a-f]{32}",
    "projects": r"prj_[0-9a-f]{32}",
    "repositories": r"repo_[0-9a-f]{32}",
    "checkouts": r"chk_[0-9a-f]{32}",
}


def invoke(*args: str, env: dict[str, str]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [sys.executable, str(FACTORY), *args],
        check=False,
        capture_output=True,
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


def config_env(config: Path) -> dict[str, str]:
    return {"XDG_CONFIG_HOME": str(config)}


def store_path(config: Path) -> Path:
    return config / "factory" / "projects.json"


def read_store(config: Path) -> dict[str, object]:
    return json.loads(store_path(config).read_text(encoding="utf-8"))


def write_store(config: Path, document: dict[str, object]) -> Path:
    registry = store_path(config)
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text(json.dumps(document), encoding="utf-8")
    return registry


def pin(path: str, dev: int, ino: int) -> dict[str, object]:
    return {"path": path, "st_dev": dev, "st_ino": ino}


def client_record(
    key: str = "acme", client_id: str = CLIENT_ID, name: str | None = None
) -> dict[str, object]:
    return {"id": client_id, "key": key, "name": name}


def project_record(
    key: str = "web",
    project_id: str = PROJECT_ID,
    client_id: str = CLIENT_ID,
    default_checkout_id: str | None = None,
    name: str | None = None,
) -> dict[str, object]:
    return {
        "id": project_id,
        "client_id": client_id,
        "key": key,
        "name": name,
        "default_checkout_id": default_checkout_id,
    }


def repository_record(
    repository_id: str = REPOSITORY_ID,
    path: str = "/repo",
    dev: int = 1,
    ino: int = 2,
    name: str | None = None,
) -> dict[str, object]:
    return {"id": repository_id, "name": name, "common_dir": pin(path, dev, ino)}


def checkout_record(  # noqa: PLR0913
    checkout_id: str = CHECKOUT_ID,
    project_id: str = PROJECT_ID,
    repository_id: str = REPOSITORY_ID,
    boundary: str = "/wt",
    git_dir: str = "/wt/.git",
    common_dir: str = "/repo",
    dev: int = 1,
    root_ino: int = 3,
    git_dir_ino: int = 4,
    common_ino: int = 2,
) -> dict[str, object]:
    return {
        "id": checkout_id,
        "project_id": project_id,
        "repository_id": repository_id,
        "boundary": pin(boundary, dev, root_ino),
        "worktree_root": pin(boundary, dev, root_ino),
        "git_dir": pin(git_dir, dev, git_dir_ino),
        "common_dir": pin(common_dir, dev, common_ino),
    }


def store_document(
    clients: list[dict[str, object]] | None = None,
    projects: list[dict[str, object]] | None = None,
    repositories: list[dict[str, object]] | None = None,
    checkouts: list[dict[str, object]] | None = None,
    legacy: dict[str, str] | None = None,
) -> dict[str, object]:
    return {
        "version": 2,
        "clients": clients or [],
        "projects": projects or [],
        "repositories": repositories or [],
        "checkouts": checkouts or [],
        "legacy": legacy or {},
    }


def registered_project(
    tmp_path: Path, key: str = "web", client_key: str = "acme"
) -> tuple[dict[str, str], Path]:
    """Register one client and one project through the CLI; return env, path."""
    repository = tmp_path / f"repo-{client_key}-{key}"
    git_repo(repository)
    env = config_env(tmp_path / "config")
    assert invoke("project", "client", "register", client_key, env=env).returncode == 0
    registered = invoke(
        "project", "register", f"{client_key}/{key}", str(repository), env=env
    )
    assert registered.returncode == 0, registered.stderr
    return env, repository


def test_identity_registration_assigns_prefixed_random_immutable_ids(
    tmp_path: Path,
) -> None:
    env, repository = registered_project(tmp_path)
    store = read_store(tmp_path / "config")

    for namespace, pattern in ID_PATTERNS.items():
        for record in store[namespace]:  # type: ignore[union-attr]
            assert re.fullmatch(pattern, record["id"])  # type: ignore[index]
    assert store["clients"][0]["id"] != store["projects"][0]["id"]  # type: ignore[index]
    assert store["projects"][0]["client_id"] == store["clients"][0]["id"]  # type: ignore[index]
    assert store["checkouts"][0]["project_id"] == store["projects"][0]["id"]  # type: ignore[index]
    assert store["checkouts"][0]["repository_id"] == store["repositories"][0]["id"]  # type: ignore[index]
    assert store["projects"][0]["default_checkout_id"] == store["checkouts"][0]["id"]  # type: ignore[index]
    checkout = store["checkouts"][0]  # type: ignore[index]
    assert checkout["boundary"]["path"] == str(repository)  # type: ignore[index]
    assert isinstance(checkout["git_dir"]["st_dev"], int)  # type: ignore[index]
    assert isinstance(checkout["git_dir"]["st_ino"], int)  # type: ignore[index]

    before = [record["id"] for record in store["projects"]]  # type: ignore[union-attr]
    assert (
        invoke("project", "set-name", "acme/web", "Web Frontend", env=env).returncode
        == 0
    )
    assert [
        record["id"]
        for record in read_store(tmp_path / "config")["projects"]  # type: ignore[union-attr]
    ] == before


def test_identity_reader_refuses_unrecognized_store_schema(tmp_path: Path) -> None:
    base = store_document(
        clients=[client_record()],
        projects=[project_record()],
        repositories=[repository_record()],
        checkouts=[checkout_record()],
    )

    def with_clients(clients: list[dict[str, object]]) -> dict[str, object]:
        return {**base, "clients": clients}

    single = client_record()
    duplicate_key = (
        '{"version": 2, "clients": [], "clients": [], "projects": [], '
        '"repositories": [], "checkouts": [], "legacy": {}}'
    )
    documents: list[dict[str, object] | str] = [
        {**base, "extra": 1},
        {key: value for key, value in base.items() if key != "legacy"},
        with_clients([{**single, "extra": 1}]),
        with_clients([{key: value for key, value in single.items() if key != "name"}]),
        with_clients([{**single, "name": 3}]),
        with_clients([{**single, "id": "cli_" + "g" * 31}]),
        with_clients([{**single, "id": "prj_" + "a" * 32}]),
        with_clients([{**single, "key": "bad key"}]),
        with_clients([{**single, "key": "a" * 65}]),
        {**base, "version": 3},
        {**base, "version": "2"},
        {**base, "clients": {}},
        {**base, "legacy": {"bad name": "/p"}},
        {**base, "legacy": {"ok": "relative"}},
        duplicate_key,
    ]
    for index, document in enumerate(documents):
        config = tmp_path / f"config-{index}"
        registry = config / "factory" / "projects.json"
        registry.parent.mkdir(parents=True)
        if isinstance(document, str):
            registry.write_text(document, encoding="utf-8")
        else:
            registry.write_text(json.dumps(document), encoding="utf-8")

        result = invoke("project", "client", "list", env=config_env(config))

        assert result.returncode == 1, document
        assert result.stdout == b""
        assert b"malformed Project Registry" in result.stderr, document
        assert b"Traceback" not in result.stderr
        assert registry.is_file()


def test_identity_reader_refuses_dangling_references_and_inconsistent_pins(
    tmp_path: Path,
) -> None:
    documents = [
        store_document(
            clients=[client_record()],
            projects=[project_record(default_checkout_id=CHECKOUT_ID)],
            repositories=[repository_record()],
        ),
        store_document(
            clients=[client_record()],
            projects=[project_record(client_id="cli_" + "e" * 32)],
            repositories=[repository_record()],
            checkouts=[checkout_record()],
        ),
        store_document(
            clients=[client_record()],
            projects=[project_record()],
            repositories=[repository_record()],
            checkouts=[checkout_record(repository_id="repo_" + "f" * 32)],
        ),
        store_document(
            clients=[client_record()],
            projects=[project_record()],
            repositories=[repository_record()],
            checkouts=[
                checkout_record(
                    boundary="/elsewhere",
                    git_dir="/elsewhere/.git",
                )
            ],
        ),
        store_document(
            clients=[client_record()],
            projects=[project_record()],
            repositories=[{**repository_record(), "common_dir": pin("/r", 1, 2)}],
        ),
        store_document(
            clients=[client_record()],
            projects=[project_record()],
            repositories=[repository_record()],
            checkouts=[checkout_record()],
            legacy={CLIENT_ID: "/p"},
        ),
    ]
    for index, document in enumerate(documents):
        config = tmp_path / f"config-{index}"
        write_store(config, document)

        result = invoke("project", "client", "list", env=config_env(config))

        assert result.returncode == 1, document
        assert result.stdout == b""
        assert b"malformed Project Registry" in result.stderr, document


def test_identity_store_serialization_is_deterministic(tmp_path: Path) -> None:
    café = tmp_path / "café"
    git_repo(café)
    env = config_env(tmp_path / "config")
    assert invoke("project", "client", "register", "acme", env=env).returncode == 0
    assert (
        invoke("project", "register", "acme/café", str(café), env=env).returncode == 0
    )
    registry = store_path(tmp_path / "config")
    store = read_store(tmp_path / "config")
    client_id = store["clients"][0]["id"]  # type: ignore[index]
    project_id = store["projects"][0]["id"]  # type: ignore[index]
    repository_id = store["repositories"][0]["id"]  # type: ignore[index]
    checkout_id = store["checkouts"][0]["id"]  # type: ignore[index]
    repository_pin = store["repositories"][0]["common_dir"]  # type: ignore[index]
    boundary = store["checkouts"][0]["boundary"]  # type: ignore[index]
    worktree_root = store["checkouts"][0]["worktree_root"]  # type: ignore[index]
    git_dir = store["checkouts"][0]["git_dir"]  # type: ignore[index]
    common_dir = store["checkouts"][0]["common_dir"]  # type: ignore[index]

    def pin_block(label: str, value: dict[str, object]) -> str:
        return (
            f'      "{label}": {{\n'
            f'        "path": "{value["path"]}",\n'
            f'        "st_dev": {value["st_dev"]},\n'
            f'        "st_ino": {value["st_ino"]}\n'
            "      }"
        )

    expected = (
        "{\n"
        '  "version": 2,\n'
        '  "clients": [\n'
        "    {\n"
        f'      "id": "{client_id}",\n'
        '      "key": "acme",\n'
        '      "name": null\n'
        "    }\n"
        "  ],\n"
        '  "projects": [\n'
        "    {\n"
        f'      "id": "{project_id}",\n'
        f'      "client_id": "{client_id}",\n'
        '      "key": "café",\n'
        '      "name": null,\n'
        f'      "default_checkout_id": "{checkout_id}"\n'
        "    }\n"
        "  ],\n"
        '  "repositories": [\n'
        "    {\n"
        f'      "id": "{repository_id}",\n'
        '      "name": null,\n'
        '      "common_dir": {\n'
        f'        "path": "{repository_pin["path"]}",\n'
        f'        "st_dev": {repository_pin["st_dev"]},\n'
        f'        "st_ino": {repository_pin["st_ino"]}\n'
        "      }\n"
        "    }\n"
        "  ],\n"
        '  "checkouts": [\n'
        "    {\n"
        f'      "id": "{checkout_id}",\n'
        f'      "project_id": "{project_id}",\n'
        f'      "repository_id": "{repository_id}",\n'
        + pin_block("boundary", boundary)
        + ",\n"
        + pin_block("worktree_root", worktree_root)
        + ",\n"
        + pin_block("git_dir", git_dir)
        + ",\n"
        + pin_block("common_dir", common_dir)
        + "\n"
        "    }\n"
        "  ],\n"
        '  "legacy": {}\n'
        "}\n"
    )
    assert registry.read_text(encoding="utf-8") == expected

    assert invoke("project", "client", "register", "zeta", env=env).returncode == 0
    other = tmp_path / "other"
    git_repo(other)
    assert (
        invoke("project", "register", "zeta/site", str(other), env=env).returncode == 0
    )
    store = read_store(tmp_path / "config")
    raw = registry.read_text(encoding="utf-8")
    for namespace in ("clients", "projects", "repositories", "checkouts"):
        identifiers = [
            record["id"]
            for record in store[namespace]  # type: ignore[union-attr]
        ]
        assert identifiers == sorted(identifiers), namespace
    assert "café" in raw
    assert raw.endswith("\n") and not raw.endswith("\n\n")
    assert json.dumps(store, ensure_ascii=True) != raw


def test_identity_registration_verifies_the_complete_git_proof(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    git_repo(repository)
    config = tmp_path / "config"
    env = config_env(config)
    assert invoke("project", "client", "register", "acme", env=env).returncode == 0

    subdirectory = repository / "service"
    subdirectory.mkdir()
    missing = tmp_path / "missing"
    plain = tmp_path / "plain"
    plain.mkdir()
    unborn = tmp_path / "unborn"
    unborn.mkdir()
    git("init", "-q", cwd=unborn)
    file_path = tmp_path / "file"
    file_path.write_text("data")
    cases = [
        (str(subdirectory), b"must equal the containing Git worktree root"),
        (str(missing), b"cannot be resolved"),
        (str(file_path), b"is not a directory"),
        (str(unborn), b"readable commit HEAD"),
    ]
    for supplied, expected in cases:
        result = invoke("project", "register", "acme/web", supplied, env=env)
        assert result.returncode == 1, supplied
        assert result.stdout == b""
        assert expected in result.stderr, supplied
    assert not config.exists()

    registered = invoke("project", "register", "acme/web", str(repository), env=env)
    assert registered.returncode == 0
    store = read_store(config)
    checkout = store["checkouts"][0]  # type: ignore[index]
    git_dir_ino = os.stat(repository / ".git").st_ino
    assert checkout["boundary"]["path"] == str(repository)  # type: ignore[index]
    assert checkout["worktree_root"]["path"] == str(repository)  # type: ignore[index]
    assert checkout["git_dir"]["path"] == str(repository / ".git")  # type: ignore[index]
    assert checkout["git_dir"]["st_ino"] == git_dir_ino  # type: ignore[index]
    assert checkout["common_dir"]["path"] == str(repository / ".git")  # type: ignore[index]


def test_identity_git_inspection_ignores_repository_location_overrides(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    git_repo(repository)
    decoy = tmp_path / "decoy"
    git_repo(decoy)
    env = config_env(tmp_path / "config")
    assert invoke("project", "client", "register", "acme", env=env).returncode == 0

    result = invoke(
        "project",
        "register",
        "acme/web",
        str(repository),
        env={**env, "GIT_DIR": str(decoy / ".git"), "GIT_WORK_TREE": str(decoy)},
    )

    assert result.returncode == 0, result.stderr
    checkout = read_store(tmp_path / "config")["checkouts"][0]  # type: ignore[index]
    assert checkout["boundary"]["path"] == str(repository)  # type: ignore[index]
    assert checkout["common_dir"]["path"] == str(repository / ".git")  # type: ignore[index]


def test_project_register_creates_project_repository_and_checkout_atomically(
    tmp_path: Path,
) -> None:
    _env, repository = registered_project(tmp_path)
    store = read_store(tmp_path / "config")
    client_id = store["clients"][0]["id"]  # type: ignore[index]
    project_id = store["projects"][0]["id"]  # type: ignore[index]
    repository_id = store["repositories"][0]["id"]  # type: ignore[index]
    checkout_id = store["checkouts"][0]["id"]  # type: ignore[index]

    result = invoke(
        "project",
        "register",
        "acme/web",
        str(repository),
        env=config_env(tmp_path / "fresh"),
    )

    assert result.returncode == 0
    assert (
        result.stdout
        == (
            f'Registered project "acme/web" at "{repository}" '
            f'as checkout "{checkout_id}" with repository "{repository_id}".\n'
        ).encode()
    )
    assert store["projects"][0] == {  # type: ignore[index]
        "id": project_id,
        "client_id": client_id,
        "key": "web",
        "name": None,
        "default_checkout_id": checkout_id,
    }
    assert store["clients"][0] == {  # type: ignore[index]
        "id": client_id,
        "key": "acme",
        "name": None,
    }


def test_project_register_refuses_an_existing_project_and_an_unknown_client(
    tmp_path: Path,
) -> None:
    env, repository = registered_project(tmp_path)
    store = read_store(tmp_path / "config")
    project_id = store["projects"][0]["id"]  # type: ignore[index]

    duplicate = invoke(
        "project", "register", "acme/web", str(repository / "again"), env=env
    )
    assert duplicate.returncode == 1
    assert duplicate.stdout == b""
    assert (
        duplicate.stderr
        == (
            f'factory: error: Project "acme/web" already exists as "{project_id}".\n'
        ).encode()
    )

    unknown = invoke("project", "register", "missing/web", str(repository), env=env)
    assert unknown.returncode == 1
    assert unknown.stderr == (b'factory: error: Client "missing" is not registered.\n')
    assert read_store(tmp_path / "config") == store


def test_repository_implicit_creation_reuses_a_full_identity_match(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    git_repo(repository)
    linked = tmp_path / "linked"
    git("worktree", "add", "-q", str(linked), "-b", "linked", cwd=repository)
    env = config_env(tmp_path / "config")
    assert invoke("project", "client", "register", "acme", env=env).returncode == 0
    assert (
        invoke("project", "register", "acme/main", str(repository), env=env).returncode
        == 0
    )
    assert (
        invoke("project", "register", "acme/linked", str(linked), env=env).returncode
        == 0
    )

    store = read_store(tmp_path / "config")
    assert len(store["repositories"]) == 1  # type: ignore[arg-type]
    assert (
        store["checkouts"][0]["repository_id"]  # type: ignore[index]
        == store["checkouts"][1]["repository_id"]  # type: ignore[index]
    )
    assert store["checkouts"][1]["common_dir"]["path"] == str(  # type: ignore[index]
        repository / ".git"
    )


def test_repository_implicit_creation_refuses_a_partial_identity_match(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    git_repo(repository)
    env = config_env(tmp_path / "config")
    assert invoke("project", "client", "register", "acme", env=env).returncode == 0
    assert (
        invoke("project", "register", "acme/main", str(repository), env=env).returncode
        == 0
    )
    store = read_store(tmp_path / "config")
    repository_id = store["repositories"][0]["id"]  # type: ignore[index]

    replacement = tmp_path / "replacement"
    git_repo(replacement)
    created = invoke("project", "register", "acme/other", str(replacement), env=env)
    assert created.returncode == 0, created.stderr
    assert len(read_store(tmp_path / "config")["repositories"]) == 2  # type: ignore[arg-type]

    moved = tmp_path / "moved"
    repository.rename(moved)
    moved_result = invoke("project", "register", "acme/moved", str(moved), env=env)
    assert moved_result.returncode == 1
    assert moved_result.stdout == b""
    assert (
        moved_result.stderr
        == (
            f'factory: error: Repository "{repository_id}" is pinned at '
            f'"{repository / ".git"}" but a component of the verified identity '
            f'matches "{moved / ".git"}"; repair it with '
            "'factory project repository re-register'.\n"
        ).encode()
    )


def test_checkout_register_binds_an_additional_checkout_of_the_project(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    git_repo(repository)
    linked = tmp_path / "linked"
    git("worktree", "add", "-q", str(linked), "-b", "linked", cwd=repository)
    env = config_env(tmp_path / "config")
    assert invoke("project", "client", "register", "acme", env=env).returncode == 0
    assert (
        invoke("project", "register", "acme/web", str(repository), env=env).returncode
        == 0
    )
    store = read_store(tmp_path / "config")
    project_id = store["projects"][0]["id"]  # type: ignore[index]
    default_id = store["projects"][0]["default_checkout_id"]  # type: ignore[index]
    repository_id = store["repositories"][0]["id"]  # type: ignore[index]

    result = invoke("project", "checkout", "register", "acme/web", str(linked), env=env)

    assert result.returncode == 0, result.stderr
    updated = read_store(tmp_path / "config")
    new_checkout = updated["checkouts"][1]  # type: ignore[index]
    assert (
        result.stdout
        == (
            f'Registered checkout "{new_checkout["id"]}" for project "acme/web" '
            f'at "{linked}" with repository "{repository_id}".\n'
        ).encode()
    )
    assert new_checkout["project_id"] == project_id  # type: ignore[index]
    assert new_checkout["repository_id"] == repository_id  # type: ignore[index]
    assert updated["projects"][0]["default_checkout_id"] == default_id  # type: ignore[index]
    assert len(updated["repositories"]) == 1  # type: ignore[arg-type]


def test_checkout_register_refuses_a_new_repository_for_an_existing_project(
    tmp_path: Path,
) -> None:
    env, _repository = registered_project(tmp_path)
    unrelated = tmp_path / "unrelated"
    git_repo(unrelated)

    result = invoke(
        "project", "checkout", "register", "acme/web", str(unrelated), env=env
    )

    assert result.returncode == 1
    assert result.stdout == b""
    assert (
        result.stderr
        == (
            'factory: error: Project "acme/web" already exists and '
            f'"{unrelated}" belongs to a new Repository; '
            "registering additional Repositories for an existing Project "
            "is not supported in this release.\n"
        ).encode()
    )
    assert len(read_store(tmp_path / "config")["checkouts"]) == 1  # type: ignore[arg-type]


def test_checkout_register_refuses_duplicate_checkouts(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    git_repo(repository)
    linked = tmp_path / "linked"
    git("worktree", "add", "-q", str(linked), "-b", "linked", cwd=repository)
    other = tmp_path / "other"
    git_repo(other)
    env = config_env(tmp_path / "config")
    assert invoke("project", "client", "register", "acme", env=env).returncode == 0
    assert invoke("project", "client", "register", "beta", env=env).returncode == 0
    assert (
        invoke("project", "register", "acme/web", str(repository), env=env).returncode
        == 0
    )
    assert (
        invoke("project", "register", "beta/other", str(other), env=env).returncode == 0
    )
    checkout_id = read_store(tmp_path / "config")["checkouts"][0]["id"]  # type: ignore[index]

    same_project = invoke(
        "project", "checkout", "register", "acme/web", str(repository), env=env
    )
    assert same_project.returncode == 1
    assert (
        same_project.stderr
        == (
            f'factory: error: Path "{repository}" is already registered as Checkout '
            f'"{checkout_id}"; use explicit re-registration to repair.\n'
        ).encode()
    )

    other_project = invoke(
        "project", "checkout", "register", "beta/other", str(repository), env=env
    )
    assert other_project.returncode == 1
    assert (
        other_project.stderr
        == (
            f'factory: error: Path "{repository}" is already pinned by checkout '
            f'"{checkout_id}".\n'
        ).encode()
    )


def test_checkout_register_shares_a_repository_across_projects_of_one_client(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    git_repo(repository)
    linked = tmp_path / "linked"
    git("worktree", "add", "-q", str(linked), "-b", "linked", cwd=repository)
    unrelated = tmp_path / "unrelated"
    git_repo(unrelated)
    env = config_env(tmp_path / "config")
    assert invoke("project", "client", "register", "acme", env=env).returncode == 0
    assert (
        invoke("project", "register", "acme/web", str(repository), env=env).returncode
        == 0
    )
    assert (
        invoke("project", "register", "acme/site", str(unrelated), env=env).returncode
        == 0
    )
    store = read_store(tmp_path / "config")
    repository_id = store["checkouts"][0]["repository_id"]  # type: ignore[index]

    result = invoke(
        "project",
        "checkout",
        "register",
        "acme/site",
        str(linked),
        "--repository",
        repository_id,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    updated = read_store(tmp_path / "config")
    shared = updated["checkouts"][-1]  # type: ignore[index]
    assert shared["project_id"] == updated["projects"][1]["id"]  # type: ignore[index]
    assert shared["repository_id"] == repository_id  # type: ignore[index]
    assert len(updated["repositories"]) == 2  # type: ignore[arg-type]


def test_checkout_register_refuses_cross_client_sharing(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    git_repo(repository)
    unrelated = tmp_path / "unrelated"
    git_repo(unrelated)
    env = config_env(tmp_path / "config")
    assert invoke("project", "client", "register", "acme", env=env).returncode == 0
    assert invoke("project", "client", "register", "beta", env=env).returncode == 0
    assert (
        invoke("project", "register", "acme/web", str(repository), env=env).returncode
        == 0
    )
    assert (
        invoke("project", "register", "beta/other", str(unrelated), env=env).returncode
        == 0
    )
    repository_id = read_store(tmp_path / "config")["checkouts"][0]["repository_id"]  # type: ignore[index]

    result = invoke(
        "project",
        "checkout",
        "register",
        "beta/other",
        str(repository),
        "--repository",
        repository_id,
        env=env,
    )

    assert result.returncode == 1
    assert result.stdout == b""
    assert (
        result.stderr
        == (
            f'factory: error: Repository "{repository_id}" is owned by a Checkout '
            "in another Client; cross-client Repository sharing requires the "
            "isolation admission policy (#167).\n"
        ).encode()
    )


def test_checkout_register_refuses_a_mismatched_repository_identity(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    git_repo(repository)
    unrelated = tmp_path / "unrelated"
    git_repo(unrelated)
    env = config_env(tmp_path / "config")
    assert invoke("project", "client", "register", "acme", env=env).returncode == 0
    assert (
        invoke("project", "register", "acme/web", str(repository), env=env).returncode
        == 0
    )
    assert (
        invoke("project", "register", "acme/site", str(unrelated), env=env).returncode
        == 0
    )
    store = read_store(tmp_path / "config")
    repository_id = store["checkouts"][0]["repository_id"]  # type: ignore[index]
    repository_pin = store["repositories"][0]["common_dir"]["path"]  # type: ignore[index]

    result = invoke(
        "project",
        "checkout",
        "register",
        "acme/site",
        str(unrelated),
        "--repository",
        repository_id,
        env=env,
    )

    assert result.returncode == 1
    assert result.stdout == b""
    assert (
        result.stderr
        == (
            f'factory: error: Repository "{repository_id}" is pinned at '
            f'"{repository_pin}" but the verified common directory of '
            f'"{unrelated}" is "{unrelated / ".git"}"; re-register the repository '
            "first.\n"
        ).encode()
    )


def test_checkout_re_register_preserves_identity_and_reports_old_and_new(
    tmp_path: Path,
) -> None:
    env, repository = registered_project(tmp_path)
    moved = tmp_path / "moved"
    repository.rename(moved)
    store = read_store(tmp_path / "config")
    checkout_id = store["checkouts"][0]["id"]  # type: ignore[index]
    project_id = store["projects"][0]["id"]  # type: ignore[index]

    result = invoke(
        "project", "checkout", "re-register", checkout_id, str(moved), env=env
    )

    assert result.returncode == 0, result.stderr
    updated = read_store(tmp_path / "config")
    checkout = updated["checkouts"][0]  # type: ignore[index]
    git_dir_ino = os.stat(moved / ".git").st_ino
    assert (
        result.stdout
        == (
            f'Re-registered checkout "{checkout_id}" in project "acme/web".\n'
            f'Old boundary: "{repository}"\n'
            f'New boundary: "{moved}"\n'
            f'Old proof: worktree root "{repository}", Git dir "{repository / ".git"}", '
            f'common dir "{repository / ".git"}"\n'
            f'New proof: worktree root "{moved}", Git dir "{moved / ".git"}", '
            f'common dir "{moved / ".git"}"\n'
        ).encode()
    )
    assert checkout["id"] == checkout_id  # type: ignore[index]
    assert checkout["project_id"] == project_id  # type: ignore[index]
    assert checkout["boundary"]["path"] == str(moved)  # type: ignore[index]
    assert checkout["git_dir"]["st_ino"] == git_dir_ino  # type: ignore[index]

    moved_back = invoke(
        "project", "checkout", "re-register", checkout_id, str(repository), env=env
    )
    assert moved_back.returncode == 0
    assert (
        read_store(tmp_path / "config")["checkouts"][0]["boundary"]["path"]  # type: ignore[index]
        == str(repository)
    )


def test_checkout_re_register_refuses_collisions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "config"
    write_store(
        config,
        store_document(
            clients=[client_record()],
            projects=[project_record()],
            repositories=[repository_record()],
            checkouts=[
                checkout_record(),
                checkout_record(
                    checkout_id="chk_" + "e" * 32,
                    boundary="/other",
                    git_dir="/other/.git",
                    root_ino=5,
                    git_dir_ino=6,
                ),
            ],
        ),
    )
    env = config_env(config)
    proof = (
        pin("/other", 1, 9),
        identity.CheckoutProof(
            worktree_root=pin("/other", 1, 9),
            git_dir=pin("/other/.git", 1, 9),
            common_dir=pin("/repo", 1, 2),
            head="0" * 40,
        ),
    )
    monkeypatch.setattr(identity, "verify_checkout_proof", lambda *_: proof)

    path_collision = invoke(
        "project", "checkout", "re-register", CHECKOUT_ID, "/other", env=env
    )
    assert path_collision.returncode == 1
    assert (
        path_collision.stderr
        == (
            'factory: error: Path "/other" is already pinned by checkout '
            '"chk_' + "e" * 32 + '".\n'
        ).encode()
    )

    proof = (
        pin("/fresh", 1, 9),
        identity.CheckoutProof(
            worktree_root=pin("/fresh", 1, 9),
            git_dir=pin("/fresh/.git", 1, 6),
            common_dir=pin("/repo", 1, 2),
            head="0" * 40,
        ),
    )
    identity_collision = invoke(
        "project", "checkout", "re-register", CHECKOUT_ID, "/fresh", env=env
    )
    assert identity_collision.returncode == 1
    assert (
        identity_collision.stderr
        == (
            'factory: error: Git directory identity of "/fresh/.git" is already '
            'pinned by checkout "chk_' + "e" * 32 + '".\n'
        ).encode()
    )
    assert read_store(config)["checkouts"][0]["boundary"]["path"] == "/wt"  # type: ignore[index]


def test_checkout_re_register_refuses_an_inconsistent_repository(
    tmp_path: Path,
) -> None:
    env, repository = registered_project(tmp_path)
    store = read_store(tmp_path / "config")
    checkout_id = store["checkouts"][0]["id"]  # type: ignore[index]
    repository_id = store["checkouts"][0]["repository_id"]  # type: ignore[index]
    other = tmp_path / "other"
    git_repo(other)

    result = invoke(
        "project", "checkout", "re-register", checkout_id, str(other), env=env
    )

    assert result.returncode == 1
    assert result.stdout == b""
    assert (
        result.stderr
        == (
            f'factory: error: Checkout "{checkout_id}" pins repository '
            f'"{repository_id}" at "{repository / ".git"}" but the verified common '
            f'directory is "{other / ".git"}"; repair the repository with '
            "'factory project repository re-register' first.\n"
        ).encode()
    )
    assert read_store(tmp_path / "config") == store


def test_repository_re_register_changes_only_the_pin_and_is_idempotent(
    tmp_path: Path,
) -> None:
    env, repository = registered_project(tmp_path)
    moved = tmp_path / "moved"
    repository.rename(moved)
    store = read_store(tmp_path / "config")
    repository_id = store["repositories"][0]["id"]  # type: ignore[index]
    checkout = store["checkouts"][0]  # type: ignore[index]

    result = invoke(
        "project", "repository", "re-register", repository_id, str(moved), env=env
    )

    assert result.returncode == 0, result.stderr
    updated = read_store(tmp_path / "config")
    assert (
        result.stdout
        == (
            f'Re-registered repository "{repository_id}".\n'
            f'Old pin: "{repository / ".git"}"\n'
            f'New pin: "{moved / ".git"}"\n'
        ).encode()
    )
    new_pin = updated["repositories"][0]["common_dir"]  # type: ignore[index]
    assert new_pin["path"] == str(moved / ".git")  # type: ignore[index]
    assert new_pin["st_ino"] == os.stat(moved / ".git").st_ino  # type: ignore[index]
    assert updated["checkouts"][0] == checkout  # type: ignore[index]

    registry = store_path(tmp_path / "config")
    before = registry.stat()
    repeat = invoke(
        "project", "repository", "re-register", repository_id, str(moved), env=env
    )
    assert repeat.returncode == 0
    assert (
        repeat.stdout
        == (
            f'Repository "{repository_id}" is already pinned at "{moved / ".git"}".\n'
        ).encode()
    )
    assert registry.stat().st_ino == before.st_ino
    assert registry.stat().st_mtime_ns == before.st_mtime_ns


def test_repository_re_register_refuses_a_pin_held_by_another_repository(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "config"
    write_store(
        config,
        store_document(
            clients=[client_record()],
            projects=[project_record()],
            repositories=[
                repository_record(),
                repository_record(
                    repository_id="repo_" + "e" * 32,
                    path="/repo2",
                    ino=7,
                ),
            ],
            checkouts=[checkout_record()],
        ),
    )
    env = config_env(config)
    proof = (
        pin("/wt", 1, 3),
        identity.CheckoutProof(
            worktree_root=pin("/wt", 1, 3),
            git_dir=pin("/wt/.git", 1, 4),
            common_dir=pin("/repo2", 1, 7),
            head="0" * 40,
        ),
    )
    monkeypatch.setattr(identity, "verify_checkout_proof", lambda *_: proof)

    result = invoke(
        "project", "repository", "re-register", REPOSITORY_ID, "/wt", env=env
    )

    assert result.returncode == 1
    assert result.stdout == b""
    assert (
        result.stderr
        == (
            'factory: error: Common directory "/repo2" is already pinned by '
            'repository "repo_' + "e" * 32 + '".\n'
        ).encode()
    )


def test_checkout_remove_clears_the_default_without_replacement(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    git_repo(repository)
    linked = tmp_path / "linked"
    git("worktree", "add", "-q", str(linked), "-b", "linked", cwd=repository)
    env = config_env(tmp_path / "config")
    assert invoke("project", "client", "register", "acme", env=env).returncode == 0
    assert (
        invoke("project", "register", "acme/web", str(repository), env=env).returncode
        == 0
    )
    assert (
        invoke(
            "project", "checkout", "register", "acme/web", str(linked), env=env
        ).returncode
        == 0
    )
    store = read_store(tmp_path / "config")
    default_id = store["projects"][0]["default_checkout_id"]  # type: ignore[index]
    project_id = store["projects"][0]["id"]  # type: ignore[index]

    result = invoke("project", "checkout", "remove", default_id, env=env)

    assert result.returncode == 0
    assert (
        result.stdout
        == (
            f'Removed checkout "{default_id}" from project "acme/web".\n'
            f'Cleared the default checkout of project "acme/web".\n'
        ).encode()
    )
    updated = read_store(tmp_path / "config")
    assert updated["projects"][0]["default_checkout_id"] is None  # type: ignore[index]
    assert updated["projects"][0]["id"] == project_id  # type: ignore[index]
    assert len(updated["checkouts"]) == 1  # type: ignore[arg-type]

    replacement = invoke(
        "project", "checkout", "register", "acme/web", str(repository), env=env
    )
    assert replacement.returncode == 0
    final = read_store(tmp_path / "config")
    assert final["projects"][0]["default_checkout_id"] is None  # type: ignore[index]


def test_project_and_client_removal_refuse_while_owned_records_remain(
    tmp_path: Path,
) -> None:
    env, _repository = registered_project(tmp_path)
    store = read_store(tmp_path / "config")
    checkout_id = store["checkouts"][0]["id"]  # type: ignore[index]

    project_refusal = invoke("project", "remove", "acme/web", env=env)
    assert project_refusal.returncode == 1
    assert (
        project_refusal.stderr
        == (
            f'factory: error: Project "acme/web" still owns checkout "{checkout_id}"; '
            "remove its checkouts first.\n"
        ).encode()
    )

    client_refusal = invoke("project", "client", "remove", "acme", env=env)
    assert client_refusal.returncode == 1
    assert client_refusal.stderr == (
        b'factory: error: Client "acme" still owns project "acme/web"; '
        b"remove its projects first.\n"
    )

    assert invoke("project", "checkout", "remove", checkout_id, env=env).returncode == 0
    removed = invoke("project", "remove", "acme/web", env=env)
    assert removed.returncode == 0
    assert removed.stdout == b'Removed project "acme/web".\n'
    assert len(read_store(tmp_path / "config")["repositories"]) == 1  # type: ignore[arg-type]

    removed_client = invoke("project", "client", "remove", "acme", env=env)
    assert removed_client.returncode == 0
    assert removed_client.stdout == b'Removed client "acme".\n'
    store = read_store(tmp_path / "config")
    assert store["clients"] == []  # type: ignore[comparison-overlap]
    assert len(store["repositories"]) == 1  # type: ignore[arg-type]


def test_retained_repositories_stay_inspectable_and_reusable(tmp_path: Path) -> None:
    env, repository = registered_project(tmp_path)
    store = read_store(tmp_path / "config")
    repository_id = store["repositories"][0]["id"]  # type: ignore[index]
    checkout_id = store["checkouts"][0]["id"]  # type: ignore[index]

    assert invoke("project", "checkout", "remove", checkout_id, env=env).returncode == 0
    assert invoke("project", "remove", "acme/web", env=env).returncode == 0
    assert read_store(tmp_path / "config")["repositories"] == store["repositories"]  # type: ignore[comparison-overlap]

    reused = invoke("project", "register", "acme/again", str(repository), env=env)
    assert reused.returncode == 0
    updated = read_store(tmp_path / "config")
    assert updated["checkouts"][0]["repository_id"] == repository_id  # type: ignore[index]
    assert len(updated["repositories"]) == 1  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "key",
    ["", ".a", "a-", "a b", "é", "a/b", "a" * 65, "-leading"],
)
def test_keys_reuse_the_project_name_grammar_and_collisions_refuse(
    tmp_path: Path,
    key: str,
) -> None:
    config = tmp_path / "config"
    env = config_env(config)

    result = invoke("project", "client", "register", key, env=env)

    assert result.returncode == 1
    assert result.stdout == b""
    assert b"invalid Project Name" in result.stderr
    assert not config.exists()

    env, _ = registered_project(tmp_path)
    store = read_store(tmp_path / "config")
    client_id = store["clients"][0]["id"]  # type: ignore[index]
    project_id = store["projects"][0]["id"]  # type: ignore[index]

    duplicate = invoke("project", "client", "register", "acme", env=env)
    assert duplicate.returncode == 1
    assert (
        duplicate.stderr
        == (
            f'factory: error: Client "acme" already exists as "{client_id}".\n'
        ).encode()
    )

    duplicate_project = invoke(
        "project", "register", "acme/web", str(tmp_path), env=env
    )
    assert duplicate_project.returncode == 1
    assert (
        duplicate_project.stderr
        == (
            f'factory: error: Project "acme/web" already exists as "{project_id}".\n'
        ).encode()
    )


def test_set_key_and_set_name_validate_before_committing_and_clear_names(
    tmp_path: Path,
) -> None:
    env, _ = registered_project(tmp_path)
    store = read_store(tmp_path / "config")

    invalid = invoke("project", "client", "set-key", "acme", "bad key", env=env)
    assert invalid.returncode == 1
    assert b"invalid Project Name" in invalid.stderr
    assert read_store(tmp_path / "config") == store

    unknown = invoke("project", "client", "set-key", "missing", "other", env=env)
    assert unknown.returncode == 1
    assert unknown.stderr == b'factory: error: Client "missing" is not registered.\n'

    changed = invoke("project", "client", "set-key", "acme", "acme2", env=env)
    assert changed.returncode == 0
    assert changed.stdout == b'Changed the key of client "acme" to "acme2".\n'
    updated = read_store(tmp_path / "config")
    assert updated["clients"][0]["key"] == "acme2"  # type: ignore[index]
    assert updated["clients"][0]["id"] == store["clients"][0]["id"]  # type: ignore[index]

    named = invoke("project", "client", "set-name", "acme2", "Acme Corp", env=env)
    assert named.returncode == 0
    assert named.stdout == b'Changed the name of client "acme2" to "Acme Corp".\n'
    cleared = invoke("project", "client", "set-name", "acme2", env=env)
    assert cleared.returncode == 0
    assert cleared.stdout == b'Cleared the name of client "acme2".\n'
    assert read_store(tmp_path / "config")["clients"][0]["name"] is None  # type: ignore[index]

    project_named = invoke("project", "set-name", "acme2/web", "Web Frontend", env=env)
    assert project_named.returncode == 0
    assert project_named.stdout == (
        b'Changed the name of project "acme2/web" to "Web Frontend".\n'
    )
    project_key = invoke("project", "set-key", "acme2/web", "site", env=env)
    assert project_key.returncode == 0
    assert project_key.stdout == b'Changed the key of project "acme2/web" to "site".\n'
    final = read_store(tmp_path / "config")
    assert final["projects"][0]["key"] == "site"  # type: ignore[index]
    assert final["projects"][0]["id"] == store["projects"][0]["id"]  # type: ignore[index]

    collision = invoke("project", "set-key", "acme2/site", "site", env=env)
    assert collision.returncode == 1
    assert b"already belongs to" in collision.stderr


def test_project_selectors_resolve_canonically_and_refuse_ambiguity(
    tmp_path: Path,
) -> None:
    env, repository = registered_project(tmp_path)
    assert invoke("project", "client", "register", "globex", env=env).returncode == 0
    assert (
        invoke("project", "register", "globex/web", str(repository), env=env).returncode
        == 0
    )
    assert invoke("project", "set-name", "acme/web", "Site", env=env).returncode == 0

    ambiguous = invoke("project", "show", "web", env=env)
    assert ambiguous.returncode == 1
    assert ambiguous.stderr == (
        b'factory: error: Project key "web" is ambiguous: "acme/web", "globex/web".\n'
    )

    canonical = invoke("project", "show", "acme/web", env=env)
    assert canonical.returncode == 0

    missing_client = invoke("project", "show", "missing/web", env=env)
    assert missing_client.returncode == 1
    assert missing_client.stderr == (
        b'factory: error: Client "missing" is not registered.\n'
    )

    not_found = invoke("project", "show", "acme/missing", env=env)
    assert not_found.returncode == 1
    assert not_found.stderr == (
        b'factory: error: Project "acme/missing" is not registered.\n'
    )

    malformed = invoke("project", "show", "a/b/c", env=env)
    assert malformed.returncode == 1
    assert malformed.stderr == (b'factory: error: Invalid project selector "a/b/c".\n')

    by_name = invoke("project", "show", "Site", env=env)
    assert by_name.returncode == 1
    assert b"is not registered" in by_name.stderr


def test_the_default_checkout_is_set_changed_and_cleared_explicitly(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    git_repo(repository)
    linked = tmp_path / "linked"
    link = tmp_path / "link"
    git("worktree", "add", "-q", str(linked), "-b", "linked", cwd=repository)
    link.symlink_to(linked, target_is_directory=True)
    env = config_env(tmp_path / "config")
    assert invoke("project", "client", "register", "acme", env=env).returncode == 0
    assert (
        invoke("project", "register", "acme/web", str(repository), env=env).returncode
        == 0
    )
    assert (
        invoke(
            "project", "checkout", "register", "acme/web", str(linked), env=env
        ).returncode
        == 0
    )
    store = read_store(tmp_path / "config")
    first = store["projects"][0]["default_checkout_id"]  # type: ignore[index]
    checkouts = sorted(
        record["id"]
        for record in store["checkouts"]  # type: ignore[union-attr]
    )
    second = checkouts[1] if checkouts[0] == first else checkouts[0]

    changed = invoke("project", "set-default-checkout", "acme/web", str(link), env=env)
    assert changed.returncode == 0
    assert (
        changed.stdout
        == (f'Set the default checkout of project "acme/web" to "{second}".\n').encode()
    )
    assert (
        read_store(tmp_path / "config")["projects"][0]["default_checkout_id"]  # type: ignore[index]
        == second
    )

    stranger = tmp_path / "stranger"
    git_repo(stranger)
    unowned = invoke(
        "project", "set-default-checkout", "acme/web", str(stranger), env=env
    )
    assert unowned.returncode == 1
    assert (
        unowned.stderr
        == (
            f'factory: error: Path "{stranger}" is not a checkout of project '
            '"acme/web".\n'
        ).encode()
    )


def test_project_migrate_carries_every_entry_into_the_legacy_section(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config"
    registry = config / "factory" / "projects.json"
    registry.parent.mkdir(parents=True)
    first = tmp_path / "first"
    first.mkdir()
    second = tmp_path / "café"
    second.mkdir()
    registry.write_text(
        json.dumps(
            {
                "version": 1,
                "projects": {"api": str(first), "web": str(second)},
            }
        ),
        encoding="utf-8",
    )

    result = invoke("project", "migrate", env=config_env(config))

    assert result.returncode == 0
    assert result.stdout == b"Migrated 2 legacy entries.\n"
    assert result.stderr == b""
    store = read_store(config)
    assert store == {
        "version": 2,
        "clients": [],
        "projects": [],
        "repositories": [],
        "checkouts": [],
        "legacy": {"api": str(first), "web": str(second)},
    }


def test_project_migrate_refuses_and_short_circuits_each_edge_case(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config"
    registry = write_store(config, store_document(legacy={"api": "/p"}))
    before = registry.stat()

    migrated = invoke("project", "migrate", env=config_env(config))
    assert migrated.returncode == 0
    assert migrated.stdout == (
        b"Already migrated; the identity store is schema version 2.\n"
    )
    assert registry.stat().st_ino == before.st_ino
    assert registry.stat().st_mtime_ns == before.st_mtime_ns

    missing_config = tmp_path / "missing-config"
    absent = invoke("project", "migrate", env=config_env(missing_config))
    assert absent.returncode == 0
    assert absent.stdout == (b"Nothing to migrate; no Project Registry exists.\n")
    assert not missing_config.exists()

    empty = tmp_path / "empty"
    empty_registry = write_store(empty, {"version": 1, "projects": {}})
    empty_before = empty_registry.read_bytes()
    empty_result = invoke("project", "migrate", env=config_env(empty))
    assert empty_result.returncode == 0
    assert empty_result.stdout == (
        b"Nothing to migrate; the version-one registry has no entries.\n"
    )
    assert empty_registry.read_bytes() == empty_before

    malformed = tmp_path / "malformed"
    broken = write_store(malformed, {"version": 1, "projects": {"bad name": "/p"}})
    broken_before = broken.read_bytes()
    broken_result = invoke("project", "migrate", env=config_env(malformed))
    assert broken_result.returncode == 1
    assert broken_result.stdout == b""
    assert b"malformed Project Registry" in broken_result.stderr
    assert broken.read_bytes() == broken_before


def test_project_listing_and_show_distinguish_bound_and_legacy_entries(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config"
    write_store(
        config,
        store_document(
            clients=[client_record()],
            projects=[project_record(default_checkout_id=CHECKOUT_ID)],
            repositories=[repository_record()],
            checkouts=[checkout_record()],
            legacy={"old": "/legacy", "api": "/api"},
        ),
    )
    env = config_env(config)

    listed = invoke("project", "list", env=env)
    assert listed.returncode == 0
    assert listed.stdout == (
        b'PROJECT\tSTATUS\n"acme/web"\tvalid\n'
        b'LEGACY\tSTATUS\tPATH\n"api"\trequires-re-registration\t"/api"\n'
        b'"old"\trequires-re-registration\t"/legacy"\n'
    )

    shown = invoke("project", "show", "acme/web", env=env)
    assert shown.returncode == 0
    assert (
        shown.stdout
        == (
            'Project: "acme/web"\n'
            'Name: "web"\n'
            'Client: "acme"\n'
            f'Default checkout: "{CHECKOUT_ID}"\n'
            f'Checkout "{CHECKOUT_ID}" at "/wt"\n'
            f'Repository "{REPOSITORY_ID}" at "/repo"\n'
        ).encode()
    )

    legacy = invoke("project", "show", "--legacy", "old", env=env)
    assert legacy.returncode == 0
    assert legacy.stdout == (
        b'Legacy project: "old"\nPath: "/legacy"\nStatus: requires-re-registration\n'
    )

    not_selectable = invoke("project", "show", "old", env=env)
    assert not_selectable.returncode == 1
    assert not_selectable.stderr == (
        b'factory: error: Project "old" is not registered.\n'
    )

    removed = invoke("project", "remove", "--legacy", "api", env=env)
    assert removed.returncode == 0
    assert removed.stdout == b'Removed legacy project "api" from "/api".\n'
    assert read_store(config)["legacy"] == {"old": "/legacy"}  # type: ignore[index]

    missing_legacy = invoke("project", "show", "--legacy", "api", env=env)
    assert missing_legacy.returncode == 1
    assert missing_legacy.stderr == (
        b'factory: error: Legacy project "api" is not registered.\n'
    )


def test_from_legacy_consumes_the_entry_atomically_and_requires_path_equality(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    git_repo(repository)
    elsewhere = tmp_path / "elsewhere"
    git_repo(elsewhere)
    config = tmp_path / "config"
    write_store(config, store_document(legacy={"api": str(repository)}))
    env = config_env(config)

    mismatch = invoke(
        "project",
        "register",
        "acme/web",
        str(elsewhere),
        "--from-legacy",
        "api",
        env=env,
    )
    assert mismatch.returncode == 1
    assert mismatch.stdout == b""
    assert (
        mismatch.stderr
        == (
            f'factory: error: Legacy project "api" records "{repository}" but the '
            f'boundary to register is "{elsewhere}".\n'
        ).encode()
    )
    assert read_store(config)["legacy"] == {"api": str(repository)}  # type: ignore[index]

    missing_client = invoke(
        "project",
        "register",
        "missing/web",
        str(repository),
        "--from-legacy",
        "api",
        env=env,
    )
    assert missing_client.returncode == 1
    assert read_store(config)["legacy"] == {"api": str(repository)}  # type: ignore[index]

    assert invoke("project", "client", "register", "acme", env=env).returncode == 0
    consumed = invoke(
        "project",
        "register",
        "acme/web",
        str(repository),
        "--from-legacy",
        "api",
        env=env,
    )
    assert consumed.returncode == 0, consumed.stderr
    store = read_store(config)
    assert store["legacy"] == {}  # type: ignore[index]
    checkout_id = store["checkouts"][0]["id"]  # type: ignore[index]
    repository_id = store["repositories"][0]["id"]  # type: ignore[index]
    assert (
        consumed.stdout
        == (
            f'Registered project "acme/web" at "{repository}" '
            f'as checkout "{checkout_id}" '
            f'with repository "{repository_id}", '
            'consuming legacy project "api".\n'
        ).encode()
    )


def test_legacy_claims_block_fresh_registration(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    git_repo(repository)
    elsewhere = tmp_path / "elsewhere"
    git_repo(elsewhere)
    config = tmp_path / "config"
    write_store(config, store_document(legacy={"api": str(repository)}))
    env = config_env(config)
    assert invoke("project", "client", "register", "acme", env=env).returncode == 0

    claimed = invoke("project", "register", "acme/web", str(repository), env=env)
    assert claimed.returncode == 1
    assert claimed.stdout == b""
    assert (
        claimed.stderr
        == (
            f'factory: error: "{repository}" is still claimed by legacy project '
            '"api"; re-register it with --from-legacy api, or remove the legacy '
            "entry first.\n"
        ).encode()
    )

    free = invoke("project", "register", "acme/web", str(elsewhere), env=env)
    assert free.returncode == 0

    assert invoke("project", "remove", "--legacy", "api", env=env).returncode == 0
    unclaimed = invoke("project", "register", "acme/other", str(repository), env=env)
    assert unclaimed.returncode == 0


def test_pre_migration_administration_refuses_identity_operations(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config"
    registry = config / "factory" / "projects.json"
    registry.parent.mkdir(parents=True)
    registry.write_text(json.dumps({"version": 1, "projects": {}}))
    env = config_env(config)
    refusals = [
        ("project", "client", "register", "acme"),
        ("project", "register", "acme/web", "/p"),
        ("project", "checkout", "register", "acme/web", "/p"),
        ("project", "checkout", "remove", CHECKOUT_ID),
        ("project", "set-default-checkout", "acme/web", "/p"),
        ("project", "remove", "--legacy", "api"),
        ("project", "show", "--legacy", "api"),
    ]
    for arguments in refusals:
        result = invoke(*arguments, env=env)
        assert result.returncode == 1, arguments
        assert result.stdout == b""
        assert result.stderr == (
            b"factory: error: projects registry schema v1; "
            b"run 'factory project migrate' first.\n"
        ), arguments

    assert registry.read_bytes() == json.dumps({"version": 1, "projects": {}}).encode()


def test_project_add_is_removed_and_points_at_registration(tmp_path: Path) -> None:
    result = invoke("project", "add", "api", str(tmp_path), env={})

    assert result.returncode == 2
    assert result.stdout == b""
    assert b"project add was removed" in result.stderr
    assert b"factory project register" in result.stderr
    assert b"Try 'factory project --help' for more information." in result.stderr


def test_version_two_inspection_reports_stored_identity_without_git(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config"
    write_store(
        config,
        store_document(
            clients=[client_record()],
            projects=[project_record(default_checkout_id=CHECKOUT_ID)],
            repositories=[repository_record()],
            checkouts=[checkout_record()],
        ),
    )
    registry = store_path(config)
    before = registry.read_bytes()
    env = {**config_env(config), "PATH": ""}

    listed = invoke("project", "list", env=env)
    shown = invoke("project", "show", "acme/web", env=env)
    clients = invoke("project", "client", "list", env=env)

    for result in (listed, shown, clients):
        assert result.returncode == 0, result.stderr
    assert listed.stdout == b'PROJECT\tSTATUS\n"acme/web"\tvalid\n'
    assert registry.read_bytes() == before
    assert not (registry.parent / "projects.lock").exists()


def test_client_listing_reports_clients_with_name_fallback(tmp_path: Path) -> None:
    config = tmp_path / "config"
    env = config_env(config)

    empty = invoke("project", "client", "list", env=env)
    assert empty.returncode == 0
    assert empty.stdout == b"No clients registered.\n"

    write_store(
        config,
        store_document(
            clients=[
                client_record(),
                client_record(key="beta", client_id="cli_" + "e" * 32, name="Beta"),
            ]
        ),
    )
    listed = invoke("project", "client", "list", env=env)
    assert listed.returncode == 0
    assert listed.stdout == (b'CLIENT\tNAME\n"acme"\t-\n"beta"\t"Beta"\n')


def test_run_selection_keeps_the_version_one_registry_and_refuses_a_version_two_store(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    git_repo(repository)
    (repository / ".agent").mkdir()
    (repository / ".agent" / "spec.md").write_text("guide")
    config = tmp_path / "config"
    registry = config / "factory" / "projects.json"
    registry.parent.mkdir(parents=True)
    registry.write_text(
        json.dumps({"version": 1, "projects": {"api": str(repository)}})
    )
    env = config_env(config)

    version_one = invoke("--project", "api", "Fix", env=env)
    assert version_one.returncode == 1
    assert b"unrecognized version" in version_one.stderr

    registry.write_text(
        json.dumps(
            store_document(legacy={"api": str(repository)}),
        )
    )
    version_two = invoke("--project", "api", "Fix", env=env)
    assert version_two.returncode == 1
    assert version_two.stdout == b""
    assert b"malformed Project Registry: unsupported version" in version_two.stderr


def test_re_register_commands_report_unknown_identifiers(tmp_path: Path) -> None:
    config = tmp_path / "config"
    write_store(config, store_document())
    env = config_env(config)

    checkout = invoke("project", "checkout", "re-register", CHECKOUT_ID, "/wt", env=env)
    assert checkout.returncode == 1
    assert (
        checkout.stderr
        == (f'factory: error: Checkout "{CHECKOUT_ID}" is not registered.\n').encode()
    )

    repository = invoke(
        "project", "repository", "re-register", REPOSITORY_ID, "/repo", env=env
    )
    assert repository.returncode == 1
    assert (
        repository.stderr
        == (
            f'factory: error: Repository "{REPOSITORY_ID}" is not registered.\n'
        ).encode()
    )

    malformed = invoke("project", "checkout", "remove", "chk_xyz", env=env)
    assert malformed.returncode == 1
    assert malformed.stderr == (
        b'factory: error: Checkout "chk_xyz" is not registered.\n'
    )


def test_identity_module_preserves_file_modes_across_mutations(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config"
    registry = write_store(config, store_document(legacy={"api": "/p"}))
    registry.chmod(0o640)

    env = config_env(config)
    removed = invoke("project", "remove", "--legacy", "api", env=env)
    assert removed.returncode == 0
    assert registry.stat().st_mode & 0o777 == 0o640
    assert read_store(config)["legacy"] == {}  # type: ignore[index]
    assert not list(registry.parent.glob(".projects-*"))
