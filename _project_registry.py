"""Read and durably mutate the local Project Registry."""

from collections.abc import Callable
from dataclasses import dataclass
import errno
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys

from _project import Project


class RegistryError(Exception):
    """The Project Registry cannot be read or inspected."""


class RegistryCommitUncertain(RegistryError):
    """The registry was replaced, but durable completion is uncertain."""


@dataclass(frozen=True)
class _RegistryUpdate:
    """The user-facing result and commit state of a registry mutation."""

    message: str
    committed: bool


_PROJECT_NAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,62}[A-Za-z0-9])?\Z")
_REGISTRY = "projects.json"
_LOCK = "projects.lock"


def _quoted(value: str) -> str:
    return json.dumps(value, ensure_ascii=True)


def validate_project_name(name: str) -> None:
    """Reject text that cannot identify a Project Registry entry."""

    if not _PROJECT_NAME.fullmatch(name):
        raise RegistryError(f"invalid Project Name {_quoted(name)}")


def _config_directory() -> Path:
    from platformdirs import user_config_path

    return user_config_path("factory", appauthor=False)


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise RegistryError("malformed Project Registry: duplicate key")
        result[key] = value
    return result


def _read_generation(directory: int) -> dict[str, str]:
    try:
        descriptor = os.open(
            _REGISTRY,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=directory,
        )
    except FileNotFoundError:
        return {}
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise RegistryError("malformed Project Registry: not a regular file") from error
        raise RegistryError(f"Project Registry cannot be read: {error}") from error
    try:
        mode = os.fstat(descriptor).st_mode
    except OSError as error:
        os.close(descriptor)
        raise RegistryError(f"Project Registry cannot be read: {error}") from error
    if not stat.S_ISREG(mode):
        os.close(descriptor)
        raise RegistryError("malformed Project Registry: not a regular file")
    try:
        with os.fdopen(descriptor, "rb") as source:
            document: object = json.loads(
                source.read().decode("utf-8", errors="strict"),
                object_pairs_hook=_unique_json_object,
            )
    except (UnicodeError, ValueError, RecursionError) as error:
        raise RegistryError("malformed Project Registry: invalid JSON or UTF-8") from error
    except OSError as error:
        raise RegistryError(f"Project Registry cannot be read: {error}") from error
    if not isinstance(document, dict) or set(document) != {"version", "projects"}:
        raise RegistryError("malformed Project Registry: invalid schema")
    if type(document["version"]) is not int or document["version"] != 1:
        raise RegistryError("malformed Project Registry: unsupported version")
    projects = document["projects"]
    if not isinstance(projects, dict):
        raise RegistryError("malformed Project Registry: invalid projects")
    for name, path in projects.items():
        if (not isinstance(name, str) or not _PROJECT_NAME.fullmatch(name) or
                not isinstance(path, str) or not os.path.isabs(path) or "\x00" in path):
            raise RegistryError("malformed Project Registry: invalid Project Name or path")
        try:
            path.encode("utf-8", errors="strict")
        except UnicodeError as error:
            raise RegistryError("malformed Project Registry: invalid path text") from error
    return projects


def read_projects() -> dict[str, str]:
    """Return one strict registry generation without creating registry state."""

    try:
        directory = _open_config_directory(create=False)
    except FileNotFoundError:
        return {}
    except OSError as error:
        raise RegistryError(f"Project Registry cannot be read: {error}") from error
    try:
        return _read_generation(directory)
    finally:
        os.close(directory)


def _open_config_directory(*, create: bool) -> int:
    """Open the config directory through anchored, nonsymlink directory entries."""

    path = _config_directory()
    if not path.is_absolute() or ".." in path.parts:
        raise RegistryError("Project Registry location is not an absolute normalized path")
    directory = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:]:
            try:
                next_directory = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=directory,
                )
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(part, mode=0o700, dir_fd=directory)
                except FileExistsError:
                    pass
                os.fsync(directory)
                next_directory = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=directory,
                )
            os.close(directory)
            directory = next_directory
        return directory
    except Exception:
        os.close(directory)
        raise


def _temporary_file(directory: int) -> tuple[int, str]:
    for _ in range(100):
        name = f".projects-{secrets.token_hex(8)}"
        try:
            descriptor = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory,
            )
        except FileExistsError:
            continue
        return descriptor, name
    raise FileExistsError(errno.EEXIST, "cannot create unique Project Registry temporary")


def _write_generation(directory: int, projects: dict[str, str]) -> None:
    document = {"version": 1, "projects": dict(sorted(projects.items()))}
    payload = (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    try:
        previous_mode = stat.S_IMODE(
            os.stat(_REGISTRY, dir_fd=directory, follow_symlinks=False).st_mode,
        )
    except FileNotFoundError:
        previous_mode = None

    temporary: str | None = None
    replaced = False
    try:
        descriptor, temporary = _temporary_file(directory)
        with os.fdopen(descriptor, "wb") as target:
            if previous_mode is not None:
                os.fchmod(target.fileno(), previous_mode)
            target.write(payload)
            target.flush()
            os.fsync(target.fileno())
        os.replace(
            temporary,
            _REGISTRY,
            src_dir_fd=directory,
            dst_dir_fd=directory,
        )
        replaced = True
        os.fsync(directory)
    except OSError as error:
        if replaced:
            raise RegistryCommitUncertain(
                f"Project Registry update may have committed: {error}",
            ) from error
        raise RegistryError(f"Project Registry cannot be updated: {error}") from error
    finally:
        if temporary is not None and not replaced:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass


_Mutation = Callable[[dict[str, str]], tuple[str, bool]]


def _mutate_registry(
    mutation: _Mutation,
    *,
    create_directory: bool,
    missing_message: str | None,
) -> _RegistryUpdate:
    try:
        directory = _open_config_directory(create=create_directory)
    except FileNotFoundError as error:
        if missing_message is not None:
            raise RegistryError(missing_message) from error
        raise RegistryError(f"Project Registry cannot be updated: {error}") from error
    except OSError as error:
        raise RegistryError(f"Project Registry cannot be updated: {error}") from error

    committed = False
    try:
        try:
            for attempt in range(3):
                try:
                    descriptor = os.open(
                        _LOCK,
                        os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
                        0o600,
                        dir_fd=directory,
                    )
                    break
                except FileNotFoundError:
                    if attempt == 2:
                        raise
            with os.fdopen(descriptor, "rb+") as lock:
                if not stat.S_ISREG(os.fstat(lock.fileno()).st_mode):
                    raise RegistryError("Project Registry lock is not a regular file")
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                projects = _read_generation(directory)
                message, changed = mutation(projects)
                if changed:
                    _write_generation(directory, projects)
                    committed = True
                update = _RegistryUpdate(message, committed)
        finally:
            os.close(directory)
    except OSError as error:
        if committed:
            raise RegistryCommitUncertain(
                f"Project Registry update may have committed: {error}",
            ) from error
        raise RegistryError(f"Project Registry cannot be updated: {error}") from error
    return update


def _report_update(update: _RegistryUpdate) -> None:
    try:
        sys.stdout.write(update.message)
    except OSError as error:
        if update.committed:
            raise RegistryCommitUncertain(
                f"Project Registry update may have committed: output failed: {error}",
            ) from error
        raise RegistryError(f"Project Registry cannot be updated: {error}") from error
    except UnicodeError as error:
        if update.committed:
            raise RegistryCommitUncertain(
                f"Project Registry update may have committed: output failed: {error}",
            ) from error
        raise


def add_project(name: str, project: Project) -> None:
    """Add or replace one registration through the durable mutation transaction."""

    validate_project_name(name)
    path = str(project.boundary)
    if not os.path.isabs(path) or "\x00" in path:
        raise RegistryError("Project Boundary is not an absolute valid path")
    try:
        path.encode("utf-8", errors="strict")
    except UnicodeError as error:
        raise RegistryError("Project Boundary cannot be encoded as UTF-8") from error

    def add(projects: dict[str, str]) -> tuple[str, bool]:
        old_path = projects.get(name)
        if old_path == path:
            return (
                f"Project {json.dumps(name, ensure_ascii=False)} is already registered "
                f"at {json.dumps(path, ensure_ascii=False)}.\n",
                False,
            )
        projects[name] = path
        if old_path is None:
            message = (
                f"Added project {json.dumps(name, ensure_ascii=False)} "
                f"at {json.dumps(path, ensure_ascii=False)}.\n"
            )
        else:
            message = (
                f"Updated project {json.dumps(name, ensure_ascii=False)} from "
                f"{json.dumps(old_path, ensure_ascii=False)} to "
                f"{json.dumps(path, ensure_ascii=False)}.\n"
            )
        return message, True

    _report_update(
        _mutate_registry(
            add,
            create_directory=True,
            missing_message=None,
        ),
    )


def remove_project(name: str) -> None:
    """Remove one registration through the durable mutation transaction."""

    missing = f"Project {_quoted(name)} is not registered"

    def remove(projects: dict[str, str]) -> tuple[str, bool]:
        try:
            path = projects.pop(name)
        except KeyError as error:
            raise RegistryError(missing) from error
        return (
            f"Removed project {json.dumps(name, ensure_ascii=False)} "
            f"from {json.dumps(path, ensure_ascii=False)}.\n",
            True,
        )

    _report_update(
        _mutate_registry(
            remove,
            create_directory=False,
            missing_message=missing,
        ),
    )
