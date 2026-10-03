"""The version-two identity store: records, administration, and migration."""

import json
import os
import re
import secrets
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TypeVar, cast

from _project import (
    GitInspectionError,
    Project,
    ProjectSelectionError,
    StaleProjectError,
    expand_path,
    inspect_registered_project,
)
from _project_registry import (
    _PROJECT_NAME,
    RegistryError,
    _mutate_store,
    _open_config_directory,
    _parse_registry_document,
    _report_update,
    _validate_v1_document,
    _write_document,
    remove_project,
    validate_project_name,
)


class IdentityError(RegistryError):
    """The identity store cannot be read or updated."""


@dataclass(frozen=True)
class Pin:
    """A pinned filesystem identity: canonical path plus stat identity."""

    path: str
    st_dev: int
    st_ino: int


@dataclass(frozen=True)
class CheckoutProof:
    """The verified Git proof of one checkout directory."""

    worktree_root: Pin
    git_dir: Pin
    common_dir: Pin
    head: str


@dataclass(frozen=True)
class Client:
    """One Client of the identity store."""

    id: str
    key: str
    name: str | None


@dataclass(frozen=True)
class ProjectRecord:
    """One registered Project of the identity store."""

    id: str
    client_id: str
    key: str
    name: str | None
    default_checkout_id: str | None


@dataclass(frozen=True)
class Repository:
    """One retained Repository identity."""

    id: str
    name: str | None
    common_dir: Pin


@dataclass(frozen=True)
class Checkout:
    """One registered Checkout worktree."""

    id: str
    project_id: str
    repository_id: str
    boundary: Pin
    worktree_root: Pin
    git_dir: Pin
    common_dir: Pin


@dataclass(frozen=True)
class IdentityStore:
    """One validated identity-store generation."""

    clients: list[Client]
    projects: list[ProjectRecord]
    repositories: list[Repository]
    checkouts: list[Checkout]
    legacy: dict[str, str]


@dataclass(frozen=True)
class _CheckoutRequest:
    """The resolved Project and verified boundary behind one Checkout write."""

    project: ProjectRecord
    selector: str
    boundary: str
    supplied: str


_ID_PREFIXES = {
    "clients": "cli_",
    "projects": "prj_",
    "repositories": "repo_",
    "checkouts": "chk_",
}
_ID = re.compile(r"(?:cli|prj|repo|chk)_[0-9a-f]{32}\Z")
_STORE_KEYS = {"version", "clients", "projects", "repositories", "checkouts", "legacy"}
_MIGRATE_FIRST = "projects registry schema v1; run 'factory project migrate' first"
_GIT_ENVIRONMENT_OVERRIDES = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
    "GIT_INDEX_FILE",
)


def _quoted(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _malformed(detail: str) -> IdentityError:
    return IdentityError(f"malformed Project Registry: {detail}")


def _empty_store() -> IdentityStore:
    return IdentityStore(
        clients=[], projects=[], repositories=[], checkouts=[], legacy={}
    )


def _fresh_id(store: IdentityStore, namespace: str) -> str:
    """Return one fresh prefixed random identifier unused by the store."""

    taken = {
        record.id
        for records in (
            store.clients,
            store.projects,
            store.repositories,
            store.checkouts,
        )
        for record in records
    }
    while (identifier := _ID_PREFIXES[namespace] + secrets.token_hex(16)) in taken:
        continue
    return identifier


def _strict_text(value: str, detail: str) -> str:
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeError as error:
        raise _malformed(detail) from error
    return value


def _read_pin(value: object) -> Pin:
    if not isinstance(value, dict) or set(value) != {"path", "st_dev", "st_ino"}:
        raise _malformed("invalid pin")
    path, dev, ino = value["path"], value["st_dev"], value["st_ino"]
    if type(path) is not str or type(dev) is not int or type(ino) is not int:
        raise _malformed("invalid pin")
    if not os.path.isabs(path) or "\x00" in path:
        raise _malformed("invalid pin path")
    return Pin(_strict_text(path, "invalid pin path"), dev, ino)


def _read_optional_name(value: object) -> str | None:
    if value is None:
        return None
    if type(value) is not str:
        raise _malformed("invalid display name")
    return _strict_text(value, "invalid display name")


def _read_identifier(value: object, namespace: str) -> str:
    if type(value) is not str or not re.fullmatch(
        _ID_PREFIXES[namespace] + r"[0-9a-f]{32}", value
    ):
        raise _malformed(f"invalid {namespace} identifier")
    return value


def _read_key(value: object) -> str:
    if type(value) is not str:
        raise _malformed("invalid key")
    try:
        validate_project_name(value)
    except RegistryError as error:
        raise _malformed(str(error)) from error
    return value


def _read_section(document: dict[str, object], key: str) -> list[object]:
    entries = document[key]
    if type(entries) is not list:
        raise _malformed(f"invalid {key} section")
    return entries


def _read_record(entry: object, fields: set[str]) -> dict[str, object]:
    if not isinstance(entry, dict) or set(entry) != fields:
        raise _malformed("invalid record")
    return entry


def _read_clients(document: dict[str, object]) -> list[Client]:
    clients = []
    for entry in _read_section(document, "clients"):
        record = _read_record(entry, {"id", "key", "name"})
        clients.append(
            Client(
                _read_identifier(record["id"], "clients"),
                _read_key(record["key"]),
                _read_optional_name(record["name"]),
            )
        )
    return clients


def _read_projects_section(document: dict[str, object]) -> list[ProjectRecord]:
    projects = []
    for entry in _read_section(document, "projects"):
        record = _read_record(
            entry, {"id", "client_id", "key", "name", "default_checkout_id"}
        )
        default = record["default_checkout_id"]
        if default is not None and type(default) is not str:
            raise _malformed("invalid Project default checkout reference")
        projects.append(
            ProjectRecord(
                _read_identifier(record["id"], "projects"),
                _read_identifier(record["client_id"], "clients"),
                _read_key(record["key"]),
                _read_optional_name(record["name"]),
                default,
            )
        )
    return projects


def _read_repositories(document: dict[str, object]) -> list[Repository]:
    repositories = []
    for entry in _read_section(document, "repositories"):
        record = _read_record(entry, {"id", "name", "common_dir"})
        repositories.append(
            Repository(
                _read_identifier(record["id"], "repositories"),
                _read_optional_name(record["name"]),
                _read_pin(record["common_dir"]),
            )
        )
    return repositories


def _read_checkouts(document: dict[str, object]) -> list[Checkout]:
    checkouts = []
    for entry in _read_section(document, "checkouts"):
        record = _read_record(
            entry,
            {
                "id",
                "project_id",
                "repository_id",
                "boundary",
                "worktree_root",
                "git_dir",
                "common_dir",
            },
        )
        checkouts.append(
            Checkout(
                _read_identifier(record["id"], "checkouts"),
                _read_identifier(record["project_id"], "projects"),
                _read_identifier(record["repository_id"], "repositories"),
                _read_pin(record["boundary"]),
                _read_pin(record["worktree_root"]),
                _read_pin(record["git_dir"]),
                _read_pin(record["common_dir"]),
            )
        )
    return checkouts


def _read_legacy(document: dict[str, object]) -> dict[str, str]:
    entries = document["legacy"]
    if type(entries) is not dict:
        raise _malformed("invalid legacy section")
    legacy: dict[str, str] = {}
    for name, path in entries.items():
        if (
            type(name) is not str
            or not _PROJECT_NAME.fullmatch(name)
            or _ID.fullmatch(name)
        ):
            raise _malformed("invalid legacy Project Name")
        if type(path) is not str or not os.path.isabs(path) or "\x00" in path:
            raise _malformed("invalid legacy path")
        legacy[_strict_text(name, "invalid legacy Project Name")] = _strict_text(
            path, "invalid legacy path"
        )
    return legacy


def _validate_unique_identifiers(store: IdentityStore) -> None:
    sections = (
        ("clients", store.clients),
        ("projects", store.projects),
        ("repositories", store.repositories),
        ("checkouts", store.checkouts),
    )
    for namespace, records in sections:
        identifiers = [record.id for record in records]
        if len(set(identifiers)) != len(identifiers):
            raise _malformed(f"duplicate {namespace} identifier")
    client_keys = [client.key for client in store.clients]
    if len(set(client_keys)) != len(client_keys):
        raise _malformed("duplicate Client key")


def _validate_project_references(
    store: IdentityStore,
    checkout_ids: set[str],
    checkout_owners: dict[str, str],
) -> None:
    client_ids = {client.id for client in store.clients}
    project_keys: set[tuple[str, str]] = set()
    for project in store.projects:
        if project.client_id not in client_ids:
            raise _malformed("unknown Project client reference")
        if (project.client_id, project.key) in project_keys:
            raise _malformed("duplicate Project key")
        project_keys.add((project.client_id, project.key))
        default = project.default_checkout_id
        if default is not None:
            if default not in checkout_ids:
                raise _malformed("unknown Project default checkout reference")
            if checkout_owners[default] != project.id:
                raise _malformed("inconsistent Project default checkout reference")


def _validate_checkout_references(
    store: IdentityStore, repository_pins: dict[str, Pin]
) -> None:
    project_ids = {project.id for project in store.projects}
    for checkout in store.checkouts:
        if checkout.project_id not in project_ids:
            raise _malformed("unknown Checkout project reference")
        if checkout.repository_id not in repository_pins:
            raise _malformed("unknown Checkout repository reference")
        if checkout.boundary.path != checkout.worktree_root.path:
            raise _malformed("inconsistent Checkout boundary pin")


def _validate_references(store: IdentityStore) -> None:
    """Refuse dangling references, duplicate records, and inconsistent pins."""

    _validate_unique_identifiers(store)
    checkout_owners = {checkout.id: checkout.project_id for checkout in store.checkouts}
    repository_pins = {
        repository.id: repository.common_dir for repository in store.repositories
    }
    _validate_project_references(store, set(checkout_owners), checkout_owners)
    _validate_checkout_references(store, repository_pins)


def _validate_store(document: object) -> IdentityStore:
    """Return one strict identity-store generation, refusing anything else."""

    if not isinstance(document, dict) or set(document) != _STORE_KEYS:
        raise _malformed("invalid schema")
    version = document["version"]
    if type(version) is not int or version != 2:
        raise _malformed("unsupported version")
    store = IdentityStore(
        clients=_read_clients(document),
        projects=_read_projects_section(document),
        repositories=_read_repositories(document),
        checkouts=_read_checkouts(document),
        legacy=_read_legacy(document),
    )
    _validate_references(store)
    return store


def _is_version(document: object, version: int) -> bool:
    if not isinstance(document, dict):
        return False
    stored = document.get("version")
    return type(stored) is int and stored == version


def _pin_document(value: Pin) -> dict[str, object]:
    return {"path": value.path, "st_dev": value.st_dev, "st_ino": value.st_ino}


def _store_document(store: IdentityStore) -> dict[str, object]:
    """Return the deterministic JSON document of one store generation."""

    return {
        "version": 2,
        "clients": [
            {"id": client.id, "key": client.key, "name": client.name}
            for client in sorted(store.clients, key=lambda record: record.id)
        ],
        "projects": [
            {
                "id": project.id,
                "client_id": project.client_id,
                "key": project.key,
                "name": project.name,
                "default_checkout_id": project.default_checkout_id,
            }
            for project in sorted(store.projects, key=lambda record: record.id)
        ],
        "repositories": [
            {
                "id": repository.id,
                "name": repository.name,
                "common_dir": _pin_document(repository.common_dir),
            }
            for repository in sorted(store.repositories, key=lambda record: record.id)
        ],
        "checkouts": [
            {
                "id": checkout.id,
                "project_id": checkout.project_id,
                "repository_id": checkout.repository_id,
                "boundary": _pin_document(checkout.boundary),
                "worktree_root": _pin_document(checkout.worktree_root),
                "git_dir": _pin_document(checkout.git_dir),
                "common_dir": _pin_document(checkout.common_dir),
            }
            for checkout in sorted(store.checkouts, key=lambda record: record.id)
        ],
        "legacy": dict(sorted(store.legacy.items())),
    }


def _write_store(directory: int, store: IdentityStore) -> None:
    """Serialize and durably replace the identity store."""

    _write_document(directory, _store_document(store))


def _read_raw_document() -> object | None:
    """Return one raw registry document without creating registry state."""

    try:
        directory = _open_config_directory(create=False)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise RegistryError(f"Project Registry cannot be read: {error}") from error
    try:
        return _parse_registry_document(directory)
    finally:
        os.close(directory)


def _read_store_for_inspection() -> IdentityStore:
    """Read the store strictly, refusing version-one registries."""

    document = _read_raw_document()
    if document is None:
        return _empty_store()
    if _is_version(document, 1):
        _validate_v1_document(document)
        raise IdentityError(_MIGRATE_FIRST)
    return _validate_store(document)


def _mutate_identity(
    mutation: Callable[[IdentityStore], tuple[str, bool]],
    *,
    create_directory: bool,
    missing_message: str | None,
) -> None:
    """Run one identity-store mutation and report its committed result."""

    _report_update(
        _mutate_store(
            _read_identity_store,
            _write_store,
            mutation,
            create_directory=create_directory,
            missing_message=missing_message,
        )
    )


def _read_identity_store(directory: int) -> IdentityStore:
    """Read one store generation for mutation, refusing version-one files."""

    document = _parse_registry_document(directory)
    if document is None:
        return _empty_store()
    if _is_version(document, 1):
        _validate_v1_document(document)
        raise IdentityError(_MIGRATE_FIRST)
    return _validate_store(document)


def _route_inspection(
    version_one: Callable[[dict[str, str]], None],
    version_two: Callable[[IdentityStore], None],
) -> None:
    """Serve one inspection command through the stored registry schema."""

    document = _read_raw_document()
    if document is None or _is_version(document, 1):
        version_one({} if document is None else _validate_v1_document(document))
        return
    if _is_version(document, 2):
        version_two(_validate_store(document))
        return
    _validate_v1_document(document)


_GIT_PROOF_FLAGS = (
    "--is-inside-work-tree",
    "--is-bare-repository",
    "--show-toplevel",
    "--absolute-git-dir",
    "--git-common-dir",
)


def _git_environment() -> dict[str, str]:
    """Return the environment for identity Git inspection, without overrides."""

    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in _GIT_ENVIRONMENT_OVERRIDES
    }
    environment["LC_ALL"] = "C"
    return environment


def _run_git_identity(
    boundary: Path, arguments: list[str], label: str
) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            ["git", "-C", str(boundary), *arguments],
            capture_output=True,
            check=False,
            env=_git_environment(),
        )
    except OSError as error:
        raise IdentityError(f"Git inspection failed for {label}: {error}") from error


def _refuse_git_failure(result: subprocess.CompletedProcess[bytes], label: str) -> None:
    if (
        result.stdout == b"false\ntrue\n"
        and b"this operation must be run in a work tree" in result.stderr
    ):
        raise IdentityError(f"{label} is not inside a non-bare Git worktree")
    if b"not a git repository" in result.stderr:
        raise IdentityError(f"{label} is not inside a non-bare Git worktree")
    raise IdentityError(
        f"Git inspection failed for {label}: Git returned {result.returncode}"
    )


def _resolved_pin(path: Path, label: str) -> Pin:
    try:
        info = os.stat(path)
    except OSError as error:
        raise IdentityError(f"Git inspection failed for {label}: {error}") from error
    return Pin(str(path), info.st_dev, info.st_ino)


def _resolve_proof_paths(
    lines: list[bytes], boundary: Path, label: str
) -> tuple[Path, Path, Path]:
    try:
        root = Path(os.fsdecode(lines[2])).resolve(strict=True)
        git_dir = Path(os.fsdecode(lines[3])).resolve(strict=True)
        common_text = os.fsdecode(lines[4])
        common = (
            Path(common_text) if os.path.isabs(common_text) else boundary / common_text
        )
        return root, git_dir, common.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise IdentityError(
            f"Git inspection failed for {label}: invalid worktree path"
        ) from error


def _git_proof(boundary: Path, supplied: str) -> tuple[Pin, Pin, Pin, str]:
    """Inspect one directory and return its root, pins, and commit."""

    label = f"path {_quoted(supplied)}"
    result = _run_git_identity(boundary, ["rev-parse", *_GIT_PROOF_FLAGS], label)
    if result.returncode:
        _refuse_git_failure(result, label)
    lines = result.stdout.splitlines()
    if len(lines) != len(_GIT_PROOF_FLAGS):
        raise IdentityError(f"Git inspection failed for {label}: invalid Git output")
    if lines[0] != b"true" or lines[1] != b"false":
        raise IdentityError(f"{label} is not inside a non-bare Git worktree")
    root, git_dir, common = _resolve_proof_paths(lines, boundary, label)
    if not boundary.is_relative_to(root):
        raise IdentityError(
            f"Git inspection failed for {label}: worktree does not contain path"
        )
    if boundary != root:
        raise IdentityError(
            "the boundary must equal the containing Git worktree root: "
            f"boundary {_quoted(str(boundary))}, worktree root {_quoted(str(root))}"
        )
    head = _run_git_identity(
        boundary, ["rev-parse", "--verify", "HEAD^{commit}"], label
    )
    if head.returncode:
        raise IdentityError(
            f"{label} has no readable commit HEAD; "
            "registration requires a recorded source commit"
        )
    return (
        _resolved_pin(root, label),
        _resolved_pin(git_dir, label),
        _resolved_pin(common, label),
        os.fsdecode(head.stdout.strip()),
    )


def verify_checkout_proof(
    supplied: str, invocation_dir: Path
) -> tuple[str, CheckoutProof]:
    """Verify one checkout path and return its boundary and Git proof."""

    expanded = expand_path(supplied)
    path = Path(expanded)
    if not path.is_absolute():
        path = invocation_dir / path
    try:
        boundary = path.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        cause = (
            error.strerror
            if isinstance(error, OSError) and error.strerror
            else "symlink loop"
        )
        raise IdentityError(
            f"path {_quoted(supplied)} cannot be resolved: {cause}"
        ) from error
    if not boundary.is_dir():
        raise IdentityError(f"path {_quoted(supplied)} is not a directory")
    root_pin, git_dir_pin, common_pin, head = _git_proof(boundary, supplied)
    return str(boundary), CheckoutProof(root_pin, git_dir_pin, common_pin, head)


def _find_client(store: IdentityStore, key: str) -> Client | None:
    for client in store.clients:
        if client.key == key:
            return client
    return None


def _client(store: IdentityStore, key: str) -> Client:
    client = _find_client(store, key)
    if client is None:
        raise IdentityError(f"Client {_quoted(key)} is not registered")
    return client


def _client_by_id(store: IdentityStore, client_id: str) -> Client:
    for client in store.clients:
        if client.id == client_id:
            return client
    raise _malformed("unknown client reference")


def _project_by_id(store: IdentityStore, project_id: str) -> ProjectRecord:
    for project in store.projects:
        if project.id == project_id:
            return project
    raise _malformed("unknown project reference")


def _repository(store: IdentityStore, repository_id: str) -> Repository:
    for repository in store.repositories:
        if repository.id == repository_id:
            return repository
    raise IdentityError(f"Repository {_quoted(repository_id)} is not registered")


def _checkout(store: IdentityStore, checkout_id: str) -> Checkout | None:
    for checkout in store.checkouts:
        if checkout.id == checkout_id:
            return checkout
    return None


def _selector(store: IdentityStore, project: ProjectRecord) -> str:
    return f"{_client_by_id(store, project.client_id).key}/{project.key}"


def _split_selector(selector: str) -> tuple[str, str]:
    """Split a canonical client/key selector after validating both keys."""

    client_key, separator, key = selector.partition("/")
    if not separator or "/" in key:
        raise IdentityError(f"Invalid project selector {_quoted(selector)}")
    validate_project_name(client_key)
    validate_project_name(key)
    return client_key, key


def _sorted_projects(store: IdentityStore) -> list[ProjectRecord]:
    return sorted(store.projects, key=lambda project: _selector(store, project))


def _resolve_project(store: IdentityStore, selector: str) -> ProjectRecord:
    """Resolve a canonical or unambiguous bare Project selector."""

    if "/" in selector:
        client_key, key = _split_selector(selector)
        client = _client(store, client_key)
        for project in store.projects:
            if project.client_id == client.id and project.key == key:
                return project
        raise IdentityError(f"Project {_quoted(selector)} is not registered")
    validate_project_name(selector)
    matches = [project for project in store.projects if project.key == selector]
    if not matches:
        raise IdentityError(f"Project {_quoted(selector)} is not registered")
    if len(matches) > 1:
        selectors = ", ".join(
            _quoted(_selector(store, project))
            for project in _sorted_projects(store)
            if project.key == selector
        )
        raise IdentityError(
            f"Project key {_quoted(selector)} is ambiguous: {selectors}"
        )
    return matches[0]


def _registered_project(path: str) -> Project:
    try:
        return inspect_registered_project(path)
    except (OSError, RuntimeError) as error:
        raise RegistryError(f"Project validation failed: {error}") from error
    except GitInspectionError as error:
        raise RegistryError(str(error)) from error


def _project_status(path: str) -> str:
    try:
        _registered_project(path)
    except StaleProjectError as error:
        return str(error)
    return "valid"


def _list_projects_v1(projects: dict[str, str]) -> None:
    if not projects:
        output = "No projects registered.\n"
    else:
        lines = ["NAME\tSTATUS\tPATH"]
        for name in sorted(projects):
            path = projects[name]
            lines.append(
                f"{json.dumps(name, ensure_ascii=False)}\t{_project_status(path)}\t"
                f"{json.dumps(path, ensure_ascii=False)}"
            )
        output = "\n".join(lines) + "\n"
    sys.stdout.write(output)


def _show_project_v1(projects: dict[str, str], name: str) -> None:
    if name not in projects:
        raise RegistryError(f"Project {_quoted(name)} is not registered")
    path = projects[name]
    status = _project_status(path)
    sys.stdout.write(
        f"Name: {json.dumps(name, ensure_ascii=False)}\n"
        f"Path: {json.dumps(path, ensure_ascii=False)}\n"
        f"Status: {status}\n"
    )


def _project_repositories(
    store: IdentityStore, checkouts: list[Checkout]
) -> list[Repository]:
    seen: dict[str, Repository] = {}
    for checkout in checkouts:
        repository = _repository(store, checkout.repository_id)
        seen[repository.id] = repository
    return [seen[identifier] for identifier in sorted(seen)]


def _list_stored_projects(store: IdentityStore) -> None:
    lines: list[str] = []
    if store.projects:
        lines.append("PROJECT\tSTATUS")
        for project in _sorted_projects(store):
            lines.append(f"{_quoted(_selector(store, project))}\tvalid")
    if store.legacy:
        lines.append("LEGACY\tSTATUS\tPATH")
        for name in sorted(store.legacy):
            lines.append(
                f"{_quoted(name)}\trequires-re-registration\t{_quoted(store.legacy[name])}"
            )
    output = "\n".join(lines) + "\n" if lines else "No projects registered.\n"
    sys.stdout.write(output)


def _show_stored_project(store: IdentityStore, selector: str) -> None:
    project = _resolve_project(store, selector)
    client = _client_by_id(store, project.client_id)
    checkouts = [c for c in store.checkouts if c.project_id == project.id]
    default = project.default_checkout_id
    display = project.name if project.name is not None else project.key
    lines = [
        f"Project: {_quoted(_selector(store, project))}",
        f"Name: {_quoted(display)}",
        f"Client: {_quoted(client.key)}",
        "Default checkout: none"
        if default is None
        else f"Default checkout: {_quoted(default)}",
    ]
    for checkout in sorted(checkouts, key=lambda record: record.id):
        lines.append(
            f"Checkout {_quoted(checkout.id)} at {_quoted(checkout.boundary.path)}"
        )
    for repository in _project_repositories(store, checkouts):
        lines.append(
            f"Repository {_quoted(repository.id)} at {_quoted(repository.common_dir.path)}"
        )
    sys.stdout.write("\n".join(lines) + "\n")


def list_projects() -> None:
    """List Projects across registry schema versions."""

    _route_inspection(_list_projects_v1, _list_stored_projects)


def show_project(name: str, *, legacy: bool) -> None:
    """Show one Project or legacy entry across registry schema versions."""

    if legacy:
        store = _read_store_for_inspection()
        if name not in store.legacy:
            raise IdentityError(f"Legacy project {_quoted(name)} is not registered")
        sys.stdout.write(
            f"Legacy project: {_quoted(name)}\n"
            f"Path: {_quoted(store.legacy[name])}\n"
            "Status: requires-re-registration\n"
        )
        return

    def version_one(projects: dict[str, str]) -> None:
        _show_project_v1(projects, name)

    def version_two(store: IdentityStore) -> None:
        _show_stored_project(store, name)

    _route_inspection(version_one, version_two)


def list_clients() -> None:
    """List registered Clients with their display names."""

    store = _read_store_for_inspection()
    if not store.clients:
        sys.stdout.write("No clients registered.\n")
        return
    lines = ["CLIENT\tNAME"]
    for client in sorted(store.clients, key=lambda record: record.key):
        display = _quoted(client.name if client.name is not None else client.key)
        lines.append(f"{_quoted(client.key)}\t{display}")
    sys.stdout.write("\n".join(lines) + "\n")


_T = TypeVar("_T")


def _replace_record(records: list[_T], current: _T, updated: _T) -> None:
    records[records.index(current)] = updated


def register_client(key: str, name: str | None) -> None:
    """Register one Client under a globally unique key."""

    validate_project_name(key)

    def mutation(store: IdentityStore) -> tuple[str, bool]:
        existing = _find_client(store, key)
        if existing is not None:
            raise IdentityError(
                f"Client {_quoted(key)} already exists as {_quoted(existing.id)}"
            )
        client = Client(_fresh_id(store, "clients"), key, name)
        store.clients.append(client)
        return f"Registered client {_quoted(key)} as {_quoted(client.id)}.\n", True

    _mutate_identity(mutation, create_directory=True, missing_message=None)


def _check_registration(
    store: IdentityStore,
    client_key: str,
    key: str,
    boundary: str,
    from_legacy: str | None,
) -> tuple[Client, str | None]:
    """Validate one registration against the store; return client and legacy."""

    selector = f"{client_key}/{key}"
    consume: str | None = None
    if from_legacy is not None:
        stored = store.legacy.get(from_legacy)
        if stored is None:
            raise IdentityError(
                f"Legacy project {_quoted(from_legacy)} is not registered"
            )
        if stored != boundary:
            raise IdentityError(
                f"Legacy project {_quoted(from_legacy)} records {_quoted(stored)} "
                f"but the boundary to register is {_quoted(boundary)}"
            )
        consume = from_legacy
    client = _find_client(store, client_key)
    if client is None:
        raise IdentityError(f"Client {_quoted(client_key)} is not registered")
    for project in store.projects:
        if project.client_id == client.id and project.key == key:
            raise IdentityError(
                f"Project {_quoted(selector)} already exists as {_quoted(project.id)}"
            )
    if from_legacy is None:
        for legacy_name, legacy_path in store.legacy.items():
            if legacy_path == boundary:
                raise IdentityError(
                    f"{_quoted(boundary)} is still claimed by legacy project "
                    f"{_quoted(legacy_name)}; re-register it with "
                    f"--from-legacy {legacy_name}, or remove the legacy entry first"
                )
    return client, consume


def _pin_identity(pin: Pin) -> tuple[int, int]:
    return (pin.st_dev, pin.st_ino)


def _implicit_repository(store: IdentityStore, fresh: Pin) -> Repository:
    """Reuse a fully matching Repository, refuse a partial match, or create."""

    for repository in store.repositories:
        pin = repository.common_dir
        if pin == fresh:
            return repository
        if pin.path == fresh.path or _pin_identity(pin) == _pin_identity(fresh):
            raise IdentityError(
                f"Repository {_quoted(repository.id)} is pinned at {_quoted(pin.path)} "
                f"but a component of the verified identity matches "
                f"{_quoted(fresh.path)}; repair it with "
                "'factory project repository re-register'"
            )
    return Repository(_fresh_id(store, "repositories"), None, fresh)


def _refuse_checkout_collisions(
    store: IdentityStore,
    boundary: str,
    git_dir: Pin,
    project_id: str | None,
    *,
    exclude: str | None = None,
) -> None:
    """Refuse a checkout whose boundary path or Git identity is already pinned."""

    for checkout in store.checkouts:
        if checkout.id == exclude:
            continue
        if checkout.boundary.path == boundary:
            if project_id is not None and checkout.project_id == project_id:
                raise IdentityError(
                    f"Path {_quoted(boundary)} is already registered as Checkout "
                    f"{_quoted(checkout.id)}; use explicit re-registration to repair"
                )
            raise IdentityError(
                f"Path {_quoted(boundary)} is already pinned by checkout "
                f"{_quoted(checkout.id)}"
            )
        if _pin_identity(checkout.git_dir) == _pin_identity(git_dir):
            raise IdentityError(
                f"Git directory identity of {_quoted(git_dir.path)} is already "
                f"pinned by checkout {_quoted(checkout.id)}"
            )


def _tentative_boundary(supplied: str) -> str | None:
    """Return the canonical boundary of one path, or None when unresolvable."""

    try:
        expanded = expand_path(supplied)
        path = Path(expanded)
        if not path.is_absolute():
            path = Path.cwd() / path
        boundary = path.resolve(strict=True)
    except (OSError, RuntimeError, ProjectSelectionError):
        return None
    if not boundary.is_dir():
        return None
    return str(boundary)


def register_project(
    selector: str, supplied_path: str, name: str | None, from_legacy: str | None
) -> None:
    """Register one Project with its first Repository and Checkout."""

    client_key, key = _split_selector(selector)
    canonical = f"{client_key}/{key}"
    tentative = _tentative_boundary(supplied_path)
    pre = _read_store_for_inspection()
    _check_registration(pre, client_key, key, tentative or supplied_path, from_legacy)
    boundary, proof = verify_checkout_proof(supplied_path, Path.cwd())

    def mutation(store: IdentityStore) -> tuple[str, bool]:
        client, consume = _check_registration(
            store, client_key, key, boundary, from_legacy
        )
        repository = _implicit_repository(store, proof.common_dir)
        _refuse_checkout_collisions(store, boundary, proof.git_dir, None)
        checkout = Checkout(
            _fresh_id(store, "checkouts"),
            "pending",
            repository.id,
            proof.worktree_root,
            proof.worktree_root,
            proof.git_dir,
            proof.common_dir,
        )
        project = ProjectRecord(
            _fresh_id(store, "projects"), client.id, key, name, checkout.id
        )
        checkout = replace(checkout, project_id=project.id)
        if repository not in store.repositories:
            store.repositories.append(repository)
        store.projects.append(project)
        store.checkouts.append(checkout)
        if consume is not None:
            del store.legacy[consume]
        tail = f", consuming legacy project {_quoted(consume)}" if consume else ""
        message = (
            f"Registered project {_quoted(canonical)} at {_quoted(boundary)} "
            f"as checkout {_quoted(checkout.id)} with repository "
            f"{_quoted(repository.id)}{tail}.\n"
        )
        return message, True

    _mutate_identity(mutation, create_directory=True, missing_message=None)


def _repository_for_checkout(
    store: IdentityStore,
    fresh: Pin,
    request: _CheckoutRequest,
    repository_id: str | None,
) -> Repository:
    """Resolve the Repository behind one additional Checkout registration."""

    if repository_id is not None:
        repository = _repository(store, repository_id)
        for checkout in store.checkouts:
            if checkout.repository_id != repository.id:
                continue
            owner = _project_by_id(store, checkout.project_id)
            if owner.client_id != request.project.client_id:
                raise IdentityError(
                    f"Repository {_quoted(repository.id)} is owned by a Checkout "
                    "in another Client; cross-client Repository sharing requires "
                    "the isolation admission policy (#167)"
                )
        if repository.common_dir != fresh:
            raise IdentityError(
                f"Repository {_quoted(repository.id)} is pinned at "
                f"{_quoted(repository.common_dir.path)} but the verified common "
                f"directory of {_quoted(request.supplied)} is "
                f"{_quoted(fresh.path)}; re-register the repository first"
            )
        return repository
    for repository in store.repositories:
        pin = repository.common_dir
        if pin == fresh:
            return _refuse_repository_sharing(store, repository, request)
        if pin.path == fresh.path or _pin_identity(pin) == _pin_identity(fresh):
            raise IdentityError(
                f"Repository {_quoted(repository.id)} is pinned at "
                f"{_quoted(pin.path)} but a component of the verified identity "
                f"matches {_quoted(fresh.path)}; repair it with "
                "'factory project repository re-register'"
            )
    raise IdentityError(
        f"Project {_quoted(request.selector)} already exists and "
        f"{_quoted(request.boundary)} belongs to a new Repository; registering "
        "additional Repositories for an existing Project is not supported "
        "in this release"
    )


def _refuse_repository_sharing(
    store: IdentityStore, repository: Repository, request: _CheckoutRequest
) -> Repository:
    for checkout in store.checkouts:
        if checkout.repository_id != repository.id:
            continue
        owner = _project_by_id(store, checkout.project_id)
        if owner.id == request.project.id:
            return repository
        if owner.client_id != request.project.client_id:
            raise IdentityError(
                f"Repository {_quoted(repository.id)} is owned by a Checkout "
                "in another Client; cross-client Repository sharing requires "
                "the isolation admission policy (#167)"
            )
        raise IdentityError(
            f"Repository {_quoted(repository.id)} is already associated with "
            f"project {_quoted(_selector(store, owner))}; pass --repository "
            f"{repository.id} to share it explicitly"
        )
    return repository


def register_checkout(
    selector: str, supplied_path: str, repository_id: str | None
) -> None:
    """Register one additional Checkout of an existing Project."""

    pre = _read_store_for_inspection()
    _resolve_project(pre, selector)
    boundary, proof = verify_checkout_proof(supplied_path, Path.cwd())

    def mutation(store: IdentityStore) -> tuple[str, bool]:
        project = _resolve_project(store, selector)
        request = _CheckoutRequest(project, selector, boundary, supplied_path)
        if repository_id is not None:
            repository = _repository_for_checkout(
                store, proof.common_dir, request, repository_id
            )
            _refuse_checkout_collisions(store, boundary, proof.git_dir, project.id)
        else:
            _refuse_checkout_collisions(store, boundary, proof.git_dir, project.id)
            repository = _repository_for_checkout(
                store, proof.common_dir, request, None
            )
        checkout = Checkout(
            _fresh_id(store, "checkouts"),
            project.id,
            repository.id,
            proof.worktree_root,
            proof.worktree_root,
            proof.git_dir,
            proof.common_dir,
        )
        store.checkouts.append(checkout)
        message = (
            f"Registered checkout {_quoted(checkout.id)} for project "
            f"{_quoted(selector)} at {_quoted(boundary)} with repository "
            f"{_quoted(repository.id)}.\n"
        )
        return message, True

    _mutate_identity(mutation, create_directory=True, missing_message=None)


def _proof_report(root: Pin, git_dir: Pin, common: Pin) -> str:
    return (
        f"worktree root {_quoted(root.path)}, Git dir {_quoted(git_dir.path)}, "
        f"common dir {_quoted(common.path)}"
    )


def _consistent_repository(
    store: IdentityStore, checkout: Checkout, proof: CheckoutProof
) -> None:
    """Refuse a re-registration whose common directory left the repository pin."""

    repository = _repository(store, checkout.repository_id)
    if _pin_identity(repository.common_dir) != _pin_identity(proof.common_dir):
        raise IdentityError(
            f"Checkout {_quoted(checkout.id)} pins repository "
            f"{_quoted(checkout.repository_id)} at "
            f"{_quoted(repository.common_dir.path)} "
            f"but the verified common directory is {_quoted(proof.common_dir.path)}; "
            "repair the repository with "
            "'factory project repository re-register' first"
        )


def re_register_checkout(checkout_id: str, supplied_path: str) -> None:
    """Replace one Checkout's boundary and proof, preserving its identity."""

    pre = _read_store_for_inspection()
    current = _checkout(pre, checkout_id)
    if current is None:
        raise IdentityError(f"Checkout {_quoted(checkout_id)} is not registered")
    boundary, proof = verify_checkout_proof(supplied_path, Path.cwd())

    def mutation(store: IdentityStore) -> tuple[str, bool]:
        checkout = _checkout(store, checkout_id)
        if checkout is None:
            raise IdentityError(f"Checkout {_quoted(checkout_id)} is not registered")
        _consistent_repository(store, checkout, proof)
        project = _project_by_id(store, checkout.project_id)
        _refuse_checkout_collisions(
            store, boundary, proof.git_dir, None, exclude=checkout.id
        )
        updated = Checkout(
            checkout.id,
            checkout.project_id,
            checkout.repository_id,
            proof.worktree_root,
            proof.worktree_root,
            proof.git_dir,
            proof.common_dir,
        )
        _replace_record(store.checkouts, checkout, updated)
        message = (
            f"Re-registered checkout {_quoted(checkout.id)} in project "
            f"{_quoted(_selector(store, project))}.\n"
            f"Old boundary: {_quoted(checkout.boundary.path)}\n"
            f"New boundary: {_quoted(boundary)}\n"
            f"Old proof: {_proof_report(checkout.worktree_root, checkout.git_dir, checkout.common_dir)}\n"
            f"New proof: {_proof_report(updated.worktree_root, updated.git_dir, updated.common_dir)}\n"
        )
        return message, True

    _mutate_identity(
        mutation,
        create_directory=False,
        missing_message=f"Checkout {_quoted(checkout_id)} is not registered",
    )


def re_register_repository(repository_id: str, supplied_path: str) -> None:
    """Replace one Repository's common-directory pin from fresh evidence."""

    pre = _read_store_for_inspection()
    for repository in pre.repositories:
        if repository.id == repository_id:
            break
    else:
        raise IdentityError(f"Repository {_quoted(repository_id)} is not registered")
    _, proof = verify_checkout_proof(supplied_path, Path.cwd())

    def mutation(store: IdentityStore) -> tuple[str, bool]:
        repository = _repository(store, repository_id)
        fresh = proof.common_dir
        if repository.common_dir == fresh:
            message = (
                f"Repository {_quoted(repository.id)} is already pinned at "
                f"{_quoted(fresh.path)}.\n"
            )
            return message, False
        for other in store.repositories:
            if other.id == repository.id:
                continue
            if other.common_dir == fresh or _pin_identity(
                other.common_dir
            ) == _pin_identity(fresh):
                raise IdentityError(
                    f"Common directory {_quoted(fresh.path)} is already pinned "
                    f"by repository {_quoted(other.id)}"
                )
        updated = Repository(repository.id, repository.name, fresh)
        _replace_record(store.repositories, repository, updated)
        message = (
            f"Re-registered repository {_quoted(repository.id)}.\n"
            f"Old pin: {_quoted(repository.common_dir.path)}\n"
            f"New pin: {_quoted(fresh.path)}\n"
        )
        return message, True

    _mutate_identity(
        mutation,
        create_directory=False,
        missing_message=f"Repository {_quoted(repository_id)} is not registered",
    )


def remove_stored_checkout(checkout_id: str) -> None:
    """Remove one Checkout, clearing a matching default without replacement."""

    def mutation(store: IdentityStore) -> tuple[str, bool]:
        checkout = _checkout(store, checkout_id)
        if checkout is None:
            raise IdentityError(f"Checkout {_quoted(checkout_id)} is not registered")
        project = _project_by_id(store, checkout.project_id)
        selector = _selector(store, project)
        store.checkouts.remove(checkout)
        lines = [
            (
                f"Removed checkout {_quoted(checkout.id)} from project "
                f"{_quoted(selector)}.\n"
            )
        ]
        for record in store.projects:
            if record.default_checkout_id == checkout.id:
                cleared = replace(record, default_checkout_id=None)
                _replace_record(store.projects, record, cleared)
                lines.append(
                    f"Cleared the default checkout of project {_quoted(selector)}.\n"
                )
        return "".join(lines), True

    _mutate_identity(
        mutation,
        create_directory=False,
        missing_message=f"Checkout {_quoted(checkout_id)} is not registered",
    )


def remove_stored_project(selector: str) -> None:
    """Remove one registered Project while it owns no checkouts."""

    def mutation(store: IdentityStore) -> tuple[str, bool]:
        project = _resolve_project(store, selector)
        for checkout in store.checkouts:
            if checkout.project_id == project.id:
                raise IdentityError(
                    f"Project {_quoted(_selector(store, project))} still owns "
                    f"checkout {_quoted(checkout.id)}; remove its checkouts first"
                )
        store.projects.remove(project)
        message = f"Removed project {_quoted(_selector(store, project))}.\n"
        return message, True

    _mutate_identity(
        mutation,
        create_directory=False,
        missing_message=f"Project {_quoted(selector)} is not registered",
    )


def remove_client(key: str) -> None:
    """Remove one Client while it owns no projects."""

    def mutation(store: IdentityStore) -> tuple[str, bool]:
        client = _find_client(store, key)
        if client is None:
            raise IdentityError(f"Client {_quoted(key)} is not registered")
        for project in store.projects:
            if project.client_id == client.id:
                raise IdentityError(
                    f"Client {_quoted(key)} still owns project "
                    f"{_quoted(_selector(store, project))}; remove its projects first"
                )
        store.clients.remove(client)
        return f"Removed client {_quoted(key)}.\n", True

    _mutate_identity(
        mutation,
        create_directory=False,
        missing_message=f"Client {_quoted(key)} is not registered",
    )


def remove_legacy_project(name: str) -> None:
    """Remove one legacy entry from a migrated identity store."""

    def mutation(store: IdentityStore) -> tuple[str, bool]:
        if name not in store.legacy:
            raise IdentityError(f"Legacy project {_quoted(name)} is not registered")
        path = store.legacy.pop(name)
        message = f"Removed legacy project {_quoted(name)} from {_quoted(path)}.\n"
        return message, True

    _mutate_identity(
        mutation,
        create_directory=False,
        missing_message=f"Legacy project {_quoted(name)} is not registered",
    )


def remove_project_entry(name: str, *, legacy: bool) -> None:
    """Remove one Project or legacy entry across registry schema versions."""

    if legacy:
        remove_legacy_project(name)
        return
    document = _read_raw_document()
    if _is_version(document, 2):
        remove_stored_project(name)
        return
    remove_project(name)


def set_client_key(key: str, new_key: str) -> None:
    """Change one Client key after validating grammar and uniqueness."""

    validate_project_name(new_key)

    def mutation(store: IdentityStore) -> tuple[str, bool]:
        client = _client(store, key)
        if new_key != key and _find_client(store, new_key) is not None:
            other = _client(store, new_key)
            raise IdentityError(
                f"Key {_quoted(new_key)} already belongs to client {_quoted(other.id)}"
            )
        _replace_record(store.clients, client, replace(client, key=new_key))
        message = f"Changed the key of client {_quoted(key)} to {_quoted(new_key)}.\n"
        return message, True

    _mutate_identity(
        mutation,
        create_directory=False,
        missing_message=f"Client {_quoted(key)} is not registered",
    )


def set_client_name(key: str, name: str | None) -> None:
    """Set or clear the display name of one Client."""

    def mutation(store: IdentityStore) -> tuple[str, bool]:
        client = _client(store, key)
        _replace_record(store.clients, client, replace(client, name=name))
        if name is None:
            message = f"Cleared the name of client {_quoted(key)}.\n"
        else:
            message = f"Changed the name of client {_quoted(key)} to {_quoted(name)}.\n"
        return message, True

    _mutate_identity(
        mutation,
        create_directory=False,
        missing_message=f"Client {_quoted(key)} is not registered",
    )


def set_project_key(selector: str, new_key: str) -> None:
    """Change one Project key after validating grammar and uniqueness."""

    validate_project_name(new_key)

    def mutation(store: IdentityStore) -> tuple[str, bool]:
        project = _resolve_project(store, selector)
        for other in store.projects:
            if other.client_id == project.client_id and other.key == new_key:
                raise IdentityError(
                    f"Key {_quoted(new_key)} already belongs to project "
                    f"{_quoted(other.id)}"
                )
        _replace_record(store.projects, project, replace(project, key=new_key))
        message = (
            f"Changed the key of project {_quoted(selector)} to {_quoted(new_key)}.\n"
        )
        return message, True

    _mutate_identity(
        mutation,
        create_directory=False,
        missing_message=f"Project {_quoted(selector)} is not registered",
    )


def set_project_name(selector: str, name: str | None) -> None:
    """Set or clear the display name of one Project."""

    def mutation(store: IdentityStore) -> tuple[str, bool]:
        project = _resolve_project(store, selector)
        _replace_record(store.projects, project, replace(project, name=name))
        if name is None:
            message = f"Cleared the name of project {_quoted(selector)}.\n"
        else:
            message = (
                f"Changed the name of project {_quoted(selector)} to {_quoted(name)}.\n"
            )
        return message, True

    _mutate_identity(
        mutation,
        create_directory=False,
        missing_message=f"Project {_quoted(selector)} is not registered",
    )


def _resolve_boundary(supplied: str) -> str:
    """Return the canonical path of one supplied checkout path."""

    try:
        expanded = expand_path(supplied)
        path = Path(expanded)
        if not path.is_absolute():
            path = Path.cwd() / path
        boundary = path.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        cause = (
            error.strerror
            if isinstance(error, OSError) and error.strerror
            else "symlink loop"
        )
        raise IdentityError(
            f"path {_quoted(supplied)} cannot be resolved: {cause}"
        ) from error
    except ProjectSelectionError as error:
        raise IdentityError(str(error)) from error
    if not boundary.is_dir():
        raise IdentityError(f"path {_quoted(supplied)} is not a directory")
    return str(boundary)


def set_default_checkout(selector: str, supplied_path: str) -> None:
    """Point one Project's default Checkout at one of its own checkouts."""

    _read_store_for_inspection()
    boundary = _resolve_boundary(supplied_path)

    def mutation(store: IdentityStore) -> tuple[str, bool]:
        project = _resolve_project(store, selector)
        for checkout in store.checkouts:
            if checkout.project_id == project.id and checkout.boundary.path == boundary:
                updated = replace(project, default_checkout_id=checkout.id)
                _replace_record(store.projects, project, updated)
                message = (
                    f"Set the default checkout of project {_quoted(selector)} "
                    f"to {_quoted(checkout.id)}.\n"
                )
                return message, True
        raise IdentityError(
            f"Path {_quoted(supplied_path)} is not a checkout of project "
            f"{_quoted(_selector(store, project))}"
        )

    _mutate_identity(
        mutation,
        create_directory=False,
        missing_message=f"Project {_quoted(selector)} is not registered",
    )


def migrate() -> None:
    """Migrate a version-one registry into the identity store schema."""

    try:
        directory = _open_config_directory(create=False)
    except FileNotFoundError:
        sys.stdout.write("Nothing to migrate; no Project Registry exists.\n")
        return
    except OSError as error:
        raise RegistryError(f"Project Registry cannot be read: {error}") from error
    os.close(directory)

    def read_migration(file_descriptor: int) -> object | None:
        return _parse_registry_document(file_descriptor)

    def write_migration(file_descriptor: int, document: object | None) -> None:
        if document is not None:
            _write_document(file_descriptor, cast("dict[str, object]", document))

    def mutation(document: object | None) -> tuple[str, bool]:
        if document is None:
            return "Nothing to migrate; no Project Registry exists.\n", False
        if _is_version(document, 2):
            _validate_store(document)
            return (
                "Already migrated; the identity store is schema version 2.\n",
                False,
            )
        projects = _validate_v1_document(document)
        if not projects:
            return (
                "Nothing to migrate; the version-one registry has no entries.\n",
                False,
            )
        store = IdentityStore([], [], [], [], dict(projects))
        payload = cast("dict[str, object]", document)
        payload.clear()
        payload.update(_store_document(store))
        return f"Migrated {len(projects)} legacy entries.\n", True

    _report_update(
        _mutate_store(
            read_migration,
            write_migration,
            mutation,
            create_directory=False,
            missing_message=None,
        )
    )
