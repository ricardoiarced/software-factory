"""Public command-line behavior before a Run or registry operation begins."""

import errno
import fcntl
import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Self

import pytest

import _project_registry as project_registry
import factory
from _identity import IdentityError, _canonical_run_path, verify_checkout_proof
from factory import _parse_run

FACTORY = Path(__file__).resolve().parents[1] / "factory.py"


def invoke(
    *args: str,
    columns: str = "80",
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [sys.executable, str(FACTORY), *args],
        check=False,
        capture_output=True,
        cwd=cwd,
        env={**os.environ, "COLUMNS": columns, **(env or {})},
    )


def git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def show_registry(tmp_path: Path, document: str) -> tuple[Path, Path]:
    config = tmp_path / "config"
    registry = config / "factory" / "projects.json"
    registry.parent.mkdir(parents=True)
    registry.write_text(document)
    return config, registry


@pytest.mark.parametrize("name", ["", ".a", "a-", "a b", "é", "a/b", "a" * 65])
def test_client_register_rejects_invalid_project_name_without_writing(
    tmp_path: Path,
    name: str,
) -> None:
    config = tmp_path / "config"

    result = invoke(
        "project", "client", "register", name, env={"XDG_CONFIG_HOME": str(config)}
    )

    assert result.returncode == 1
    assert result.stdout == b""
    assert b"invalid Project Name" in result.stderr
    assert not config.exists()


def test_client_register_rejects_malformed_registry_without_replacing_it(
    tmp_path: Path,
) -> None:
    config, registry = show_registry(tmp_path, '{"version":2,"clients":[]}')
    before = registry.read_bytes()

    result = invoke(
        "project", "client", "register", "api", env={"XDG_CONFIG_HOME": str(config)}
    )

    assert result.returncode == 1
    assert result.stdout == b""
    assert b"malformed Project Registry" in result.stderr
    assert registry.read_bytes() == before


def test_client_register_creates_private_files_and_preserves_registry_mode(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config"
    env = {"XDG_CONFIG_HOME": str(config)}
    first = invoke("project", "client", "register", "acme", env=env)
    directory = config / "factory"
    registry = directory / "projects.json"
    lock = directory / "projects.lock"

    assert first.returncode == 0
    assert [
        path.stat().st_mode & 0o777 for path in (config, directory, registry, lock)
    ] == [
        0o700,
        0o700,
        0o600,
        0o600,
    ]
    registry.chmod(0o640)
    second = invoke("project", "client", "register", "beta", env=env)
    assert second.returncode == 0
    assert registry.stat().st_mode & 0o777 == 0o640


def test_project_lock_persists_across_mutations(tmp_path: Path) -> None:
    config, _registry = show_registry(
        tmp_path,
        json.dumps({"version": 1, "projects": {"api": str(tmp_path)}}),
    )
    env = {"XDG_CONFIG_HOME": str(config)}
    assert invoke("project", "migrate", env=env).returncode == 0
    lock = config / "factory" / "projects.lock"
    first = lock.stat()

    assert invoke("project", "remove", "--legacy", "api", env=env).returncode == 0
    assert invoke("project", "migrate", env=env).returncode == 0

    last = lock.stat()
    assert (last.st_dev, last.st_ino) == (first.st_dev, first.st_ino)
    assert lock.stat().st_mode & 0o777 == 0o600


def test_client_register_syncs_new_configuration_directory_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "config"
    actual_fsync = os.fsync
    synced: list[tuple[int, int]] = []

    def record_sync(descriptor: int) -> None:
        info = os.fstat(descriptor)
        if stat.S_ISDIR(info.st_mode):
            synced.append((info.st_dev, info.st_ino))
        actual_fsync(descriptor)

    monkeypatch.setattr(project_registry.os, "fsync", record_sync)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    monkeypatch.setattr(
        sys, "argv", ["factory", "project", "client", "register", "api"]
    )

    assert factory.main() == 0
    for directory in (tmp_path, config, config / "factory"):
        info = directory.stat()
        assert (info.st_dev, info.st_ino) in synced


def test_client_register_parent_sync_failure_stops_before_registry_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = tmp_path / "config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    monkeypatch.setattr(
        sys, "argv", ["factory", "project", "client", "register", "api"]
    )
    parent = tmp_path.stat()
    actual_fsync = os.fsync

    def fail_parent_sync(descriptor: int) -> None:
        info = os.fstat(descriptor)
        if (info.st_dev, info.st_ino) == (parent.st_dev, parent.st_ino):
            raise OSError("parent directory sync failed")
        actual_fsync(descriptor)

    monkeypatch.setattr(project_registry.os, "fsync", fail_parent_sync)

    assert factory.main() == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert "factory: error: Project Registry cannot be updated" in output.err
    assert not (config / "factory" / "projects.json").exists()


def test_client_register_concurrent_writers_keep_every_client(tmp_path: Path) -> None:
    config = tmp_path / "config"
    env = {**os.environ, "XDG_CONFIG_HOME": str(config)}
    writers = [
        subprocess.Popen(
            [
                sys.executable,
                str(FACTORY),
                "project",
                "client",
                "register",
                f"p{index}",
            ],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for index in range(8)
    ]

    for writer in writers:
        stdout, stderr = writer.communicate(timeout=10)
        assert writer.returncode == 0, stderr
        assert stdout.startswith(b"Registered client")
    registry = config / "factory" / "projects.json"
    document = json.loads(registry.read_text())
    assert {client["key"] for client in document["clients"]} == {
        f"p{index}" for index in range(8)
    }


def test_client_register_recovers_from_transient_lock_creation_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    monkeypatch.setattr(
        sys, "argv", ["factory", "project", "client", "register", "api"]
    )
    actual_open = os.open
    lock_open_attempts = 0

    def transient_lock_failure(
        path: str, flags: int, *args: object, **kwargs: object
    ) -> int:
        nonlocal lock_open_attempts
        if path == "projects.lock":
            lock_open_attempts += 1
            if lock_open_attempts == 1:
                raise FileNotFoundError(errno.ENOENT, "No such file or directory", path)
        return actual_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(project_registry.os, "open", transient_lock_failure)

    assert factory.main() == 0
    assert lock_open_attempts == 2
    document = json.loads((config / "factory" / "projects.json").read_text())
    assert [client["key"] for client in document["clients"]] == ["api"]


def test_concurrent_legacy_removal_and_client_registration_keep_generations(
    tmp_path: Path,
) -> None:
    original = {f"old{index}": str(tmp_path) for index in range(8)}
    config, registry = show_registry(
        tmp_path,
        json.dumps(
            {
                "version": 1,
                "projects": original,
            }
        ),
    )
    env = {**os.environ, "XDG_CONFIG_HOME": str(config)}
    assert invoke("project", "migrate", env=env).returncode == 0
    commands = [("remove", "--legacy", f"old{index}") for index in range(8)] + [
        ("client", "register", f"new{index}") for index in range(8)
    ]
    writers = [
        subprocess.Popen(
            [sys.executable, str(FACTORY), "project", *command],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for command in commands
    ]

    deadline = time.monotonic() + 20
    while True:
        document = json.loads(registry.read_bytes())
        assert document["version"] == 2
        assert set(document) == {
            "version",
            "clients",
            "projects",
            "repositories",
            "checkouts",
            "legacy",
        }
        if all(writer.poll() is not None for writer in writers):
            break
        assert time.monotonic() < deadline, "concurrent registry writers did not finish"
        time.sleep(0.001)

    for writer in writers:
        _, stderr = writer.communicate(timeout=10)
        assert writer.returncode == 0, stderr
    final = json.loads(registry.read_bytes())
    assert final["legacy"] == {}
    assert {client["key"] for client in final["clients"]} == {
        f"new{index}" for index in range(8)
    }


def test_project_mutation_rereads_strict_registry_after_acquiring_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config, registry = show_registry(
        tmp_path,
        json.dumps(
            {
                "version": 1,
                "projects": {"old": str(tmp_path)},
            }
        ),
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    monkeypatch.setattr(sys, "argv", ["factory", "project", "remove", "old"])
    actual_flock = fcntl.flock

    def change_registry_at_lock(descriptor: int, operation: int) -> None:
        actual_flock(descriptor, operation)
        registry.write_text('{"version":3,"projects":{}}')

    monkeypatch.setattr(project_registry.fcntl, "flock", change_registry_at_lock)

    assert factory.main() == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert "malformed Project Registry: unsupported version" in output.err
    assert registry.read_text() == '{"version":3,"projects":{}}'


@pytest.mark.parametrize("kind", ["symlink", "directory"])
def test_project_mutation_refuses_nonregular_registry_state(
    tmp_path: Path,
    kind: str,
) -> None:
    config, registry = show_registry(
        tmp_path,
        json.dumps(
            {
                "version": 1,
                "projects": {"old": str(tmp_path)},
            }
        ),
    )
    registry.unlink()
    if kind == "symlink":
        target = tmp_path / "target.json"
        target.write_text('{"version":1,"projects":{}}')
        registry.symlink_to(target)
    else:
        registry.mkdir()

    result = invoke("project", "remove", "old", env={"XDG_CONFIG_HOME": str(config)})

    assert result.returncode == 1
    assert result.stdout == b""
    assert b"malformed Project Registry: not a regular file" in result.stderr
    if kind == "symlink":
        assert registry.is_symlink()
    else:
        assert registry.is_dir()


def test_project_migrate_replace_failure_keeps_previous_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config, registry = show_registry(
        tmp_path, json.dumps({"version": 1, "projects": {"api": str(tmp_path)}})
    )
    before = registry.read_bytes()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    monkeypatch.setattr(sys, "argv", ["factory", "project", "migrate"])

    def fail_replace(*_: object, **__: object) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(project_registry.os, "replace", fail_replace)

    result = factory.main()

    assert result == 1
    assert capsys.readouterr().out == ""
    assert registry.read_bytes() == before
    assert list(registry.parent.glob(".projects-*")) == []


@pytest.mark.parametrize("failure", ["temporary creation", "write", "file sync"])
def test_project_migrate_pre_replace_failure_keeps_previous_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: str,
) -> None:
    config, registry = show_registry(
        tmp_path, json.dumps({"version": 1, "projects": {"api": str(tmp_path)}})
    )
    before = registry.read_bytes()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    monkeypatch.setattr(sys, "argv", ["factory", "project", "migrate"])

    if failure == "temporary creation":
        actual_open = os.open

        def fail_temporary_creation(
            path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
            flags: int,
            mode: int = 0o777,
            *,
            dir_fd: int | None = None,
        ) -> int:
            if isinstance(path, str) and path.startswith(".projects-"):
                raise OSError("temporary creation failed")
            return actual_open(path, flags, mode, dir_fd=dir_fd)

        monkeypatch.setattr(project_registry.os, "open", fail_temporary_creation)
    elif failure == "write":
        actual_fdopen = os.fdopen

        class FailingWriter:
            def __init__(self, descriptor: int) -> None:
                self.file = actual_fdopen(descriptor, "wb")

            def __enter__(self) -> Self:
                self.file.__enter__()
                return self

            def __exit__(self, *args: object) -> object:
                return self.file.__exit__(*args)

            def fileno(self) -> int:
                return self.file.fileno()

            def write(self, _: bytes) -> None:
                raise OSError("write failed")

        def fdopen(descriptor: int, mode: str) -> object:
            return (
                FailingWriter(descriptor)
                if mode == "wb"
                else actual_fdopen(descriptor, mode)
            )

        monkeypatch.setattr(project_registry.os, "fdopen", fdopen)
    else:

        def fail_file_sync(descriptor: int) -> None:
            raise OSError("file sync failed")

        monkeypatch.setattr(project_registry.os, "fsync", fail_file_sync)

    result = factory.main()
    output = capsys.readouterr()

    assert result == 1
    assert output.out == ""
    assert "Project Registry cannot be updated" in output.err
    assert registry.read_bytes() == before
    assert list(registry.parent.glob(".projects-*")) == []


def test_project_migrate_directory_sync_failure_warns_update_may_have_committed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config, registry = show_registry(
        tmp_path, json.dumps({"version": 1, "projects": {"api": str(tmp_path)}})
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    monkeypatch.setattr(sys, "argv", ["factory", "project", "migrate"])
    actual_fsync = os.fsync

    def fail_directory_sync(descriptor: int) -> None:
        if registry.exists() and stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError("directory sync failed")
        actual_fsync(descriptor)

    monkeypatch.setattr(project_registry.os, "fsync", fail_directory_sync)

    result = factory.main()
    output = capsys.readouterr()

    assert result == 1
    assert output.out == ""
    assert "factory: warning: Project Registry update may have committed" in output.err
    migrated = json.loads((config / "factory" / "projects.json").read_text())
    assert migrated["version"] == 2
    assert migrated["legacy"] == {"api": str(tmp_path)}


def test_project_remove_preserves_other_registration_and_inspection(
    tmp_path: Path,
) -> None:
    git("init", "-q", cwd=tmp_path)
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    config, registry = show_registry(
        tmp_path,
        json.dumps(
            {
                "version": 1,
                "projects": {"Api": str(first), "other": str(second)},
            }
        ),
    )
    env = {"XDG_CONFIG_HOME": str(config)}

    result = invoke("project", "remove", "Api", env=env)

    assert result.returncode == 0
    assert result.stdout == f'Removed project "Api" from "{first}".\n'.encode()
    assert result.stderr == b""
    assert (
        registry.read_bytes()
        == (
            '{\n  "version": 1,\n  "projects": {\n    "other": "'
            + str(second)
            + '"\n  }\n}\n'
        ).encode()
    )
    assert invoke("project", "list", env=env).stdout == (
        f'NAME\tSTATUS\tPATH\n"other"\tvalid\t"{second}"\n'.encode()
    )
    assert invoke("project", "show", "other", env=env).stdout == (
        f'Name: "other"\nPath: "{second}"\nStatus: valid\n'.encode()
    )


def test_project_remove_unknown_name_keeps_registry_unchanged(tmp_path: Path) -> None:
    config, registry = show_registry(
        tmp_path,
        json.dumps(
            {
                "version": 1,
                "projects": {"Api": str(tmp_path)},
            }
        ),
    )
    before = registry.read_bytes()
    before_stat = registry.stat()

    result = invoke("project", "remove", "api", env={"XDG_CONFIG_HOME": str(config)})

    assert result.returncode == 1
    assert result.stdout == b""
    assert result.stderr == b'factory: error: Project "api" is not registered.\n'
    assert registry.read_bytes() == before
    assert registry.stat().st_ino == before_stat.st_ino
    assert registry.stat().st_mtime_ns == before_stat.st_mtime_ns


def test_project_remove_last_registration_writes_valid_empty_registry(
    tmp_path: Path,
) -> None:
    config, registry = show_registry(
        tmp_path,
        json.dumps(
            {
                "version": 1,
                "projects": {"api": str(tmp_path)},
            }
        ),
    )
    env = {"XDG_CONFIG_HOME": str(config)}

    result = invoke("project", "remove", "--", "api", env=env)

    assert result.returncode == 0
    assert result.stdout == f'Removed project "api" from "{tmp_path}".\n'.encode()
    assert result.stderr == b""
    assert registry.read_bytes() == b'{\n  "version": 1,\n  "projects": {}\n}\n'
    assert invoke("project", "list", env=env).stdout == b"No projects registered.\n"
    show = invoke("project", "show", "api", env=env)
    assert show.returncode == 1
    assert show.stdout == b""
    assert show.stderr == b'factory: error: Project "api" is not registered.\n'


def test_project_remove_unknown_from_missing_registry_creates_no_state(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config"

    result = invoke("project", "remove", "api", env={"XDG_CONFIG_HOME": str(config)})

    assert result.returncode == 1
    assert result.stdout == b""
    assert result.stderr == b'factory: error: Project "api" is not registered.\n'
    assert not config.exists()


def test_project_remove_replace_failure_keeps_previous_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config, registry = show_registry(
        tmp_path,
        json.dumps(
            {
                "version": 1,
                "projects": {"api": str(tmp_path)},
            }
        ),
    )
    before = registry.read_bytes()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    monkeypatch.setattr(sys, "argv", ["factory", "project", "remove", "api"])

    def fail_replace(*_: object, **__: object) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(project_registry.os, "replace", fail_replace)

    result = factory.main()
    output = capsys.readouterr()

    assert result == 1
    assert output.out == ""
    assert "Project Registry cannot be updated" in output.err
    assert registry.read_bytes() == before
    assert list(registry.parent.glob(".projects-*")) == []


def test_project_remove_directory_sync_failure_warns_update_may_have_committed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config, registry = show_registry(
        tmp_path,
        json.dumps(
            {
                "version": 1,
                "projects": {"api": str(tmp_path)},
            }
        ),
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    monkeypatch.setattr(sys, "argv", ["factory", "project", "remove", "api"])
    actual_fsync = os.fsync

    def fail_directory_sync(descriptor: int) -> None:
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError("directory sync failed")
        actual_fsync(descriptor)

    monkeypatch.setattr(project_registry.os, "fsync", fail_directory_sync)

    result = factory.main()
    output = capsys.readouterr()

    assert result == 1
    assert output.out == ""
    assert "factory: warning: Project Registry update may have committed" in output.err
    assert registry.read_bytes() == b'{\n  "version": 1,\n  "projects": {}\n}\n'


def test_project_list_missing_registry_is_empty_without_creating_state(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config"

    result = invoke("project", "list", env={"XDG_CONFIG_HOME": str(config)})

    assert result.returncode == 0
    assert result.stdout == b"No projects registered.\n"
    assert result.stderr == b""
    assert not config.exists()


def test_project_list_valid_empty_registry_prints_empty_message(tmp_path: Path) -> None:
    config = tmp_path / "config"
    registry = config / "factory" / "projects.json"
    registry.parent.mkdir(parents=True)
    registry.write_text('{"version":1,"projects":{}}')

    result = invoke("project", "list", env={"XDG_CONFIG_HOME": str(config)})

    assert result.returncode == 0
    assert result.stdout == b"No projects registered.\n"
    assert result.stderr == b""


def test_project_show_prints_exact_registered_name_path_and_valid_status(
    tmp_path: Path,
) -> None:
    project = tmp_path / "café\nproject"
    project.mkdir()
    git("init", "-q", cwd=project)
    config, registry = show_registry(
        tmp_path,
        json.dumps({"version": 1, "projects": {"Api": str(project)}}),
    )
    before = registry.read_bytes()

    result = invoke("project", "show", "Api", env={"XDG_CONFIG_HOME": str(config)})

    assert result.returncode == 0
    assert result.stdout == (
        b'Name: "Api"\nPath: "'
        + str(project).replace("\n", "\\n").encode()
        + b'"\nStatus: valid\n'
    )
    assert result.stderr == b""
    assert registry.read_bytes() == before
    assert not (registry.parent / "projects.lock").exists()


def test_project_show_distinguishes_each_stale_status_without_repair(
    tmp_path: Path,
) -> None:
    file_path = tmp_path / "file"
    file_path.write_text("data")
    outside = tmp_path / "outside"
    outside.mkdir()
    missing = tmp_path / "missing"
    paths = {"missing": missing, "file": file_path, "outside": outside}
    config, registry = show_registry(
        tmp_path,
        json.dumps(
            {
                "version": 1,
                "projects": {name: str(path) for name, path in paths.items()},
            }
        ),
    )
    before = registry.read_bytes()
    statuses = {
        "missing": "stale: path does not exist",
        "file": "stale: path is not a directory",
        "outside": "stale: path is not inside a non-bare Git worktree",
    }

    for name, path in paths.items():
        result = invoke("project", "show", name, env={"XDG_CONFIG_HOME": str(config)})
        assert result.returncode == 0
        assert result.stdout == (
            f'Name: "{name}"\nPath: "{path}"\nStatus: {statuses[name]}\n'.encode()
        )
        assert result.stderr == b""
    assert registry.read_bytes() == before
    assert not (registry.parent / "projects.lock").exists()


def test_project_show_unknown_name_fails_operationally_and_case_sensitively(
    tmp_path: Path,
) -> None:
    config, _ = show_registry(
        tmp_path,
        json.dumps({"version": 1, "projects": {"Api": str(tmp_path)}}),
    )

    for name in ("api", "missing"):
        result = invoke("project", "show", name, env={"XDG_CONFIG_HOME": str(config)})
        assert result.returncode == 1
        assert result.stdout == b""
        assert f'Project "{name}" is not registered'.encode() in result.stderr
        assert b"Traceback" not in result.stderr


def test_project_show_rejects_malformed_registry_without_stdout(tmp_path: Path) -> None:
    config, _ = show_registry(
        tmp_path,
        '{"version":1,"projects":{"api":"/one","api":"/two"}}',
    )

    result = invoke("project", "show", "api", env={"XDG_CONFIG_HOME": str(config)})

    assert result.returncode == 1
    assert result.stdout == b""
    assert b"malformed Project Registry" in result.stderr


def test_project_show_accepts_separator_before_name(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    git("init", "-q", cwd=project)
    config, _ = show_registry(
        tmp_path,
        json.dumps({"version": 1, "projects": {"Api": str(project)}}),
    )

    result = invoke(
        "project", "show", "--", "Api", env={"XDG_CONFIG_HOME": str(config)}
    )

    assert result.returncode == 0
    assert result.stdout == f'Name: "Api"\nPath: "{project}"\nStatus: valid\n'.encode()
    assert result.stderr == b""


def test_project_show_validation_failure_emits_no_partial_stdout(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    git("init", "-q", cwd=project)
    config, _ = show_registry(
        tmp_path,
        json.dumps({"version": 1, "projects": {"api": str(project)}}),
    )

    result = invoke(
        "project",
        "show",
        "api",
        env={
            "XDG_CONFIG_HOME": str(config),
            "PATH": "",
        },
    )

    assert result.returncode == 1
    assert result.stdout == b""
    assert b"Git inspection failed" in result.stderr


def test_project_list_sorts_and_reports_stale_entries_without_writing(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config"
    registry = config / "factory" / "projects.json"
    registry.parent.mkdir(parents=True)
    valid = tmp_path / "café\nproject"
    valid.mkdir()
    git("init", "-q", cwd=valid)
    file_path = tmp_path / "file"
    file_path.write_text("data")
    outside = tmp_path / "outside"
    outside.mkdir()
    missing = tmp_path / "missing"
    registry.write_text(
        json.dumps(
            {
                "version": 1,
                "projects": {
                    "z": str(outside),
                    "a": str(file_path),
                    "Alpha": str(valid),
                    "b": str(missing),
                },
            }
        )
    )
    before = registry.read_bytes()

    result = invoke("project", "list", env={"XDG_CONFIG_HOME": str(config)})

    assert result.returncode == 0
    assert result.stdout == (
        b"NAME\tSTATUS\tPATH\n"
        + b'"Alpha"\tvalid\t"'
        + str(valid).replace("\n", "\\n").encode()
        + b'"\n'
        + b'"a"\tstale: path is not a directory\t"'
        + str(file_path).encode()
        + b'"\n'
        + b'"b"\tstale: path does not exist\t"'
        + str(missing).encode()
        + b'"\n'
        + b'"z"\tstale: path is not inside a non-bare Git worktree\t"'
        + str(outside).encode()
        + b'"\n'
    )
    assert result.stderr == b""
    assert registry.read_bytes() == before
    assert not (registry.parent / "projects.lock").exists()


@pytest.mark.parametrize(
    "document",
    [
        '{"version":1,"version":1,"projects":{}}',
        '{"version":1,"projects":{"a":"/one","a":"/two"}}',
        '{"version":1,"projects":{},"extra":0}',
        '{"version":2,"projects":{}}',
        '{"version":true,"projects":{}}',
        '{"version":1,"projects":[]}',
        '{"version":1,"projects":{"bad name":"/tmp"}}',
        '{"version":1,"projects":{"a":"relative"}}',
        '{"version":1,"projects":{"a":12}}',
        '{"version":1,"projects":{"a":"/bad\\u0000path"}}',
        '{"version":1,"projects":{"a":"/bad\\ud800path"}}',
        '{"version":1,"projects":{}} trailing',
    ],
)
def test_project_list_rejects_malformed_registry_as_a_whole(
    tmp_path: Path, document: str
) -> None:
    config = tmp_path / "config"
    registry = config / "factory" / "projects.json"
    registry.parent.mkdir(parents=True)
    registry.write_text(document)

    result = invoke("project", "list", env={"XDG_CONFIG_HOME": str(config)})

    assert result.returncode == 1
    assert result.stdout == b""
    assert b"malformed Project Registry" in result.stderr
    assert b"Traceback" not in result.stderr


def test_project_list_rejects_symlink_and_invalid_utf8_registry(tmp_path: Path) -> None:
    config = tmp_path / "config"
    registry = config / "factory" / "projects.json"
    registry.parent.mkdir(parents=True)
    target = tmp_path / "target"
    target.write_text('{"version":1,"projects":{}}')
    registry.symlink_to(target)
    env = {"XDG_CONFIG_HOME": str(config)}

    linked = invoke("project", "list", env=env)
    registry.unlink()
    registry.write_bytes(b"\xff")
    invalid = invoke("project", "list", env=env)

    for result in (linked, invalid):
        assert result.returncode == 1
        assert result.stdout == b""
        assert b"malformed Project Registry" in result.stderr


def test_project_list_git_failure_emits_no_partial_stdout(tmp_path: Path) -> None:
    config = tmp_path / "config"
    registry = config / "factory" / "projects.json"
    registry.parent.mkdir(parents=True)
    project = tmp_path / "project"
    project.mkdir()
    git("init", "-q", cwd=project)
    registry.write_text(
        json.dumps(
            {
                "version": 1,
                "projects": {
                    "a": str(tmp_path / "missing"),
                    "b": str(project),
                },
            }
        )
    )

    result = invoke("project", "list", env={"XDG_CONFIG_HOME": str(config), "PATH": ""})

    assert result.returncode == 1
    assert result.stdout == b""
    assert b"Git inspection failed" in result.stderr


def test_project_list_rejects_git_command_failure_as_a_whole(tmp_path: Path) -> None:
    config = tmp_path / "config"
    registry = config / "factory" / "projects.json"
    registry.parent.mkdir(parents=True)
    project = tmp_path / "project"
    project.mkdir()
    registry.write_text(
        json.dumps(
            {
                "version": 1,
                "projects": {
                    "a": str(tmp_path / "missing"),
                    "b": str(project),
                },
            }
        )
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_git = fake_bin / "git"
    fake_git.write_text("#!/bin/sh\necho 'fatal: bad config' >&2\nexit 128\n")
    fake_git.chmod(0o755)

    result = invoke(
        "project",
        "list",
        env={
            "XDG_CONFIG_HOME": str(config),
            "PATH": str(fake_bin),
        },
    )

    assert result.returncode == 1
    assert result.stdout == b""
    assert b"Git inspection failed" in result.stderr


def test_checkout_path_preserves_canonical_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    boundary = tmp_path / "service"
    boundary.mkdir()
    (tmp_path / "link").symlink_to(boundary, target_is_directory=True)
    monkeypatch.setenv("FACTORY_TEST_BOUNDARY", "link")
    monkeypatch.setenv("HOME", str(tmp_path))

    assert _canonical_run_path("$FACTORY_TEST_BOUNDARY", tmp_path) == boundary
    assert _canonical_run_path("${FACTORY_TEST_BOUNDARY}", tmp_path) == boundary
    assert _canonical_run_path("~/service", tmp_path) == boundary


@pytest.mark.parametrize("value", ["missing", "empty"])
def test_checkout_path_rejects_unset_or_empty_variable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    env = registered_run_root(tmp_path)
    if value == "missing":
        monkeypatch.delenv("FACTORY_TEST_MISSING", raising=False)
        supplied = "${FACTORY_TEST_MISSING}"
    else:
        monkeypatch.setenv("FACTORY_TEST_EMPTY", "")
        supplied = "$FACTORY_TEST_EMPTY"

    result = invoke("--checkout", supplied, "Fix", cwd=tmp_path, env=env)

    assert result.returncode == 1
    assert result.stdout == b""
    assert b"environment variable" in result.stderr
    assert b"Usage:" not in result.stderr


@pytest.mark.parametrize("kind", ["missing", "file", "dangling", "outside-git"])
def test_checkout_selection_refuses_invalid_directory_without_fallback(
    tmp_path: Path, kind: str
) -> None:
    env = registered_run_root(tmp_path)
    if kind == "file":
        (tmp_path / "target").write_text("data")
        target = tmp_path / "target"
    elif kind == "dangling":
        target = tmp_path / "target"
        target.symlink_to(tmp_path / "missing")
    elif kind == "outside-git":
        target = tmp_path / "plain"
        target.mkdir()
    else:
        target = tmp_path / "missing"

    result = invoke("--checkout", str(target), "Fix", cwd=tmp_path, env=env)

    assert result.returncode == 1
    assert result.stdout == b""
    assert b"factory: error:" in result.stderr
    assert b"Usage:" not in result.stderr
    assert b"Traceback" not in result.stderr


@pytest.mark.parametrize("kind", ["nested", "linked", "submodule", "non-utf8"])
def test_checkout_proof_uses_the_containing_worktree_root(
    tmp_path: Path, kind: str
) -> None:
    if kind == "nested":
        outer = tmp_path / "outer"
        outer.mkdir()
        git("init", "-q", cwd=outer)
        root = outer / "inner"
        root.mkdir()
        git("init", "-q", cwd=root)
        git(
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "--allow-empty",
            "-m",
            "initial",
            "-q",
            cwd=root,
        )
    elif kind == "linked":
        source = tmp_path / "source"
        source.mkdir()
        git("init", "-q", cwd=source)
        git(
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "--allow-empty",
            "-m",
            "initial",
            "-q",
            cwd=source,
        )
        root = tmp_path / "linked"
        git("worktree", "add", "-q", "-b", "linked", str(root), cwd=source)
    elif kind == "submodule":
        source = tmp_path / "source"
        source.mkdir()
        git("init", "-q", cwd=source)
        git(
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "--allow-empty",
            "-m",
            "initial",
            "-q",
            cwd=source,
        )
        parent = tmp_path / "parent"
        parent.mkdir()
        git("init", "-q", cwd=parent)
        git(
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            "-q",
            str(source),
            "module",
            cwd=parent,
        )
        root = parent / "module"
    elif kind == "non-utf8":
        root = tmp_path / os.fsdecode(b"project-\xff")
        try:
            root.mkdir()
        except OSError:
            pytest.skip("this filesystem cannot store non-UTF-8 paths")
        git("init", "-q", cwd=root)
        git(
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "--allow-empty",
            "-m",
            "initial",
            "-q",
            cwd=root,
        )

    interior = root / "service"
    interior.mkdir()
    boundary, _proof = verify_checkout_proof(str(root), tmp_path)
    assert boundary == str(root)
    with pytest.raises(IdentityError, match="boundary must equal"):
        verify_checkout_proof(str(interior), tmp_path)


def test_checkout_proof_refuses_an_unparsable_worktree_path(tmp_path: Path) -> None:
    root = tmp_path / "project\nname"
    root.mkdir()
    git("init", "-q", cwd=root)
    git(
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "--allow-empty",
        "-m",
        "initial",
        "-q",
        cwd=root,
    )

    with pytest.raises(IdentityError, match="invalid Git output"):
        verify_checkout_proof(str(root), tmp_path)


def test_checkout_proof_rejects_bare_repository(tmp_path: Path) -> None:
    bare = tmp_path / "bare.git"
    git("init", "--bare", "-q", str(bare), cwd=tmp_path)

    with pytest.raises(IdentityError, match="non-bare Git worktree"):
        verify_checkout_proof(str(bare), tmp_path)


def test_checkout_proof_rejects_missing_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", "")

    with pytest.raises(IdentityError, match="Git inspection failed"):
        verify_checkout_proof(str(tmp_path), tmp_path)


def test_valid_checkout_selection_reaches_run_stage(tmp_path: Path) -> None:
    env = registered_run_root(tmp_path)

    result = invoke(
        "--checkout=$FACTORY_TEST_BOUNDARY",
        "Fix",
        cwd=tmp_path,
        env={**env, "FACTORY_TEST_BOUNDARY": str(tmp_path)},
    )

    assert result.returncode == 1
    assert result.stdout == b""
    assert result.stderr.endswith(
        b" reported an unrecognized version; install a certified Codex release.\n"
    )


@pytest.mark.parametrize("case", ["outside", "bare", "missing-git"])
def test_discovery_refuses_unusable_git_context(tmp_path: Path, case: str) -> None:
    env = registered_run_root(tmp_path, directory="repo")
    cwd = tmp_path / "repo"
    if case == "bare":
        cwd = tmp_path / "bare.git"
        git("init", "--bare", "-q", str(cwd), cwd=tmp_path)
    elif case == "outside":
        cwd = tmp_path / "outside"
        cwd.mkdir()
    elif case == "missing-git":
        env = {**env, "PATH": ""}

    result = invoke("Fix", cwd=cwd, env=env)

    assert result.returncode == 1
    assert result.stdout == b""
    expected = (
        b"Git inspection failed"
        if case == "missing-git"
        else b"No Git worktree encloses the invocation directory"
    )
    assert expected in result.stderr
    assert b"Traceback" not in result.stderr


def test_explicit_root_help_is_stable_stdout() -> None:
    wide = invoke("--help", columns="200")
    narrow = invoke("-h", columns="20")

    assert wide.returncode == narrow.returncode == 0
    assert wide.stdout == narrow.stdout
    assert wide.stderr == narrow.stderr == b""
    assert wide.stdout.startswith(
        b"Usage:\n"
        b"  factory [--harness codex|claude] [--project NAME] [--checkout PATH] "
        b"[--file PATH]... [--force] [--re-probe] [--] INSTRUCTION...\n"
        b"  factory project COMMAND ...\n"
        b"  factory doctor project [--] SELECTOR [--checkout PATH] "
        b"[--harness NAME]\n"
        b"  factory (-h | --help)\n"
        b"  factory --version\n"
    )
    for detail in (
        b"automatic discovery",
        b"first non-option",
        b"Context Sources",
        b"--file",
        b"standard input",
        b"--force",
        b"codex (the default)",
        b"latest-release and Run Admission",
        b"never falls back to the other Harness",
        b"factory --harness claude Fix the failing tests",
        b"factory -- project list",
        b"git diff | factory Review this patch",
    ):
        assert detail in wide.stdout


def test_explicit_project_help_is_stable_stdout() -> None:
    wide = invoke("project", "--help", columns="200")
    narrow = invoke("project", "-h", columns="20")

    assert wide.returncode == narrow.returncode == 0
    assert wide.stdout == narrow.stdout
    assert wide.stderr == narrow.stderr == b""
    assert wide.stdout.startswith(
        b"Usage:\n"
        b"  factory project register [--name NAME] [--from-legacy NAME] "
        b"[--] SELECTOR PATH\n"
        b"  factory project list\n"
        b"  factory project show [--legacy] [--] SELECTOR\n"
    )
    for detail in (
        b"Project Registry",
        b"migrate",
        b"--legacy",
        b"client register",
        b"checkout re-register",
        b"repository re-register",
        b"set-default-checkout",
        b"Only exact lowercase",
    ):
        assert detail in wide.stdout


@pytest.mark.parametrize(
    ("args", "hint"),
    [
        ((), b"Try 'factory --help' for more information."),
        (("--project", "api"), b"Try 'factory --help' for more information."),
        (
            ("--path", "./api", "--project", "api", "Fix"),
            b"Try 'factory --help' for more information.",
        ),
        (("--force", "--force", "Fix"), b"Try 'factory --help' for more information."),
        (("--file", "-oops", "Fix"), b"Try 'factory --help' for more information."),
        (("--unknown", "Fix"), b"Try 'factory --help' for more information."),
        (("--help", "Fix"), b"Try 'factory --help' for more information."),
        (("--version", "--help"), b"Try 'factory --help' for more information."),
        (("--", "  "), b"Try 'factory --help' for more information."),
        (("project",), b"Try 'factory project --help' for more information."),
        (
            ("project", "add", "api"),
            b"Try 'factory project --help' for more information.",
        ),
        (
            ("project", "list", "extra"),
            b"Try 'factory project --help' for more information.",
        ),
        (
            ("project", "show", "--help"),
            b"Try 'factory project --help' for more information.",
        ),
        (
            ("project", "--version"),
            b"Try 'factory project --help' for more information.",
        ),
    ],
)
def test_malformed_commands_have_relevant_usage_and_no_traceback(
    args: tuple[str, ...], hint: bytes
) -> None:
    result = invoke(*args)

    assert result.returncode == 2
    assert result.stdout == b""
    assert result.stderr.startswith(b"factory: error: ")
    assert (
        b"factory project register [--name NAME] [--from-legacy NAME] "
        if args[:1] == ("project",)
        else b"factory [--harness codex|claude] [--project NAME] [--checkout PATH]"
    ) in result.stderr
    assert hint in result.stderr
    assert b"Traceback" not in result.stderr


@pytest.mark.parametrize(
    "args",
    [
        ("Fix", "--unknown"),
        ("--", "project", "list"),
        ("--project=api", "Fix"),
        ("--file=-log", "Fix"),
        ("project", "show", "no-such-project"),
    ],
)
def test_valid_grammar_reaches_operational_stage(args: tuple[str, ...]) -> None:
    result = invoke(*args)

    assert result.returncode == 1
    assert result.stdout == b""
    assert result.stderr.startswith(b"factory: error: ")
    assert b"Usage:" not in result.stderr
    assert b"Traceback" not in result.stderr


@pytest.mark.parametrize(
    "args",
    [
        (
            "--file",
            "first.txt",
            "--project=api",
            "--file=first.txt",
            "--force",
            "--",
            "  Fix  this  ",
            "--file",
            "last\tword",
        ),
        ("--", "project", "list"),
        ("--project", "api", "Fix"),
        ("--file=-log", "--file", "next.log", "Fix"),
        ("-", "--force"),
    ],
    ids=[
        "repeated-context-and-whitespace",
        "terminator-escapes-project",
        "separate-selector",
        "repeatable-files",
        "lone-dash-instruction",
    ],
)
def registered_run_root(tmp_path: Path, directory: str = "") -> dict[str, str]:
    """Register one worktree as acme/web's Checkout in a v2 store; return env."""
    root = tmp_path / directory if directory else tmp_path
    root.mkdir(exist_ok=True)
    git("init", "-q", cwd=root)
    git(
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "--allow-empty",
        "-m",
        "initial",
        "-q",
        cwd=root,
    )
    root_info = root.stat()
    git_dir_info = (root / ".git").stat()
    boundary = {
        "path": str(root),
        "st_dev": root_info.st_dev,
        "st_ino": root_info.st_ino,
    }
    git_dir = {
        "path": str(root / ".git"),
        "st_dev": git_dir_info.st_dev,
        "st_ino": git_dir_info.st_ino,
    }
    config = tmp_path / "config"
    registry = config / "factory" / "projects.json"
    registry.parent.mkdir(parents=True)
    registry.write_text(
        json.dumps(
            {
                "version": 2,
                "clients": [{"id": "cli_" + "a" * 32, "key": "acme", "name": None}],
                "projects": [
                    {
                        "id": "prj_" + "b" * 32,
                        "client_id": "cli_" + "a" * 32,
                        "key": "web",
                        "name": None,
                        "default_checkout_id": "chk_" + "d" * 32,
                    }
                ],
                "repositories": [
                    {
                        "id": "repo_" + "c" * 32,
                        "name": None,
                        "common_dir": git_dir,
                    }
                ],
                "checkouts": [
                    {
                        "id": "chk_" + "d" * 32,
                        "project_id": "prj_" + "b" * 32,
                        "repository_id": "repo_" + "c" * 32,
                        "boundary": boundary,
                        "worktree_root": boundary,
                        "git_dir": git_dir,
                        "common_dir": git_dir,
                    }
                ],
                "legacy": {},
            }
        )
    )
    return {"XDG_CONFIG_HOME": str(config)}


@pytest.mark.parametrize(
    "args",
    [
        (
            "--file",
            "first.txt",
            "--project=api",
            "--file=first.txt",
            "--force",
            "--",
            "  Fix  this  ",
            "--file",
            "last\tword",
        ),
        ("--", "project", "list"),
        ("--project", "api", "Fix"),
        ("--file=-log", "--file", "next.log", "Fix"),
        ("-", "--force"),
    ],
    ids=[
        "repeated-context-and-whitespace",
        "terminator-escapes-project",
        "separate-selector",
        "repeatable-files",
        "lone-dash-instruction",
    ],
)
def test_run_accepts_documented_forms_through_cli(
    args: tuple[str, ...], tmp_path: Path
) -> None:
    env = registered_run_root(tmp_path)
    (tmp_path / "-log").write_text("context")
    (tmp_path / "next.log").write_text("context")
    result = invoke(*args, cwd=tmp_path, env=env)

    assert result.returncode == 1
    assert result.stdout == b""
    if "--project" in args or "--project=api" in args:
        assert result.stderr == b'factory: error: Project "api" is not registered.\n'
    else:
        assert result.stderr.endswith(
            b" reported an unrecognized version; install a certified Codex release.\n"
        )


def test_run_parser_returns_instruction_selector_context_sources_and_force() -> None:
    parsed = _parse_run(
        [
            "--file",
            "first.txt",
            "--project=api",
            "--file=first.txt",
            "--force",
            "--",
            "  Fix  this  ",
            "--file",
            "last\tword",
        ]
    )

    assert parsed.instruction == "  Fix  this   --file last\tword"
    assert parsed.harness == "codex"
    assert parsed.project == "api"
    assert parsed.checkout is None
    assert parsed.files == ("first.txt", "first.txt")
    assert parsed.force is True

    combined = _parse_run(["--project", "acme/web", "--checkout", ".", "Fix"])

    assert combined.project == "acme/web"
    assert combined.checkout == "."
    assert combined.instruction == "Fix"


def test_run_parser_returns_selected_harness() -> None:
    assert _parse_run(["--harness", "claude", "Fix"]).harness == "claude"
    assert _parse_run(["--harness=codex", "--file=x", "Fix"]).harness == "codex"
    assert (
        _parse_run(
            [
                "--file=x",
                "--force",
                "--harness=claude",
                "--",
                "Fix",
            ]
        ).harness
        == "claude"
    )

    trailing = _parse_run(["Fix", "--harness", "claude"])
    assert trailing.harness == "codex"
    assert trailing.instruction == "Fix --harness claude"


def test_selected_claude_harness_fails_closed_before_submission(tmp_path: Path) -> None:
    """The silent harness fake refuses at the earliest local step; only the
    quoted executable path varies with the temporary directory.
    """
    root = tmp_path / "worktree"
    env = registered_run_root(tmp_path, "worktree")
    (root / ".agent").mkdir()
    (root / ".agent" / "spec.md").write_text("Project Guidance")
    git("add", "-A", cwd=root)
    git(
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-m",
        "fixture",
        "-q",
        cwd=root,
    )

    result = invoke(
        "--harness",
        "claude",
        "Fix",
        cwd=root,
        env=env,
    )

    assert result.returncode == 1
    assert result.stdout == b""
    assert result.stderr.startswith(b'factory: error: Claude Code executable "')
    assert result.stderr.endswith(
        b" reported an unrecognized version; install a stable Claude Code release.\n",
    )


@pytest.mark.parametrize(
    ("harness", "version_output", "label"),
    [
        ("codex", "codex-cli 9.9.9\n", "Codex"),
        ("claude", "9.9.9 (Claude Code)\n", "Claude Code"),
    ],
)
def test_a_dead_publisher_connection_refuses_the_run_before_any_submission(
    tmp_path: Path,
    harness: str,
    version_output: str,
    label: str,
) -> None:
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    executable = fake_bin / harness
    executable.write_text(f"#!/bin/sh\nprintf '{version_output}'\n")
    executable.chmod(0o755)
    root = tmp_path / "worktree"
    env = registered_run_root(tmp_path, "worktree")
    (root / ".agent").mkdir()
    (root / ".agent" / "spec.md").write_text("Project Guidance")
    git("add", "-A", cwd=root)
    git(
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-m",
        "fixture",
        "-q",
        cwd=root,
    )

    result = invoke(
        "--harness",
        harness,
        "Fix",
        cwd=root,
        env={
            **env,
            "PATH": os.pathsep.join((str(fake_bin), os.environ["PATH"])),
        },
    )

    assert result.returncode == 1
    assert result.stdout == b""
    assert result.stderr.startswith(
        f"factory: error: Factory could not reach the {label} publisher".encode(),
    )
    assert result.stderr.endswith(b"check connectivity and retry.\n")


def test_explicit_default_harness_matches_omission(tmp_path: Path) -> None:
    root = tmp_path / "worktree"
    env = registered_run_root(tmp_path, "worktree")
    explicit = invoke(
        "--harness",
        "codex",
        "Fix",
        cwd=root,
        env=env,
    )
    omitted = invoke(
        "Fix",
        cwd=root,
        env=env,
    )

    assert explicit.returncode == omitted.returncode == 1
    assert explicit.stdout == omitted.stdout == b""
    assert explicit.stderr == omitted.stderr
    assert explicit.stderr.endswith(
        b" reported an unrecognized version; install a certified Codex release.\n"
    )


@pytest.mark.parametrize(
    ("args", "reason"),
    [
        (
            ("--project", "api", "--project", "api", "Fix"),
            b"--project may appear only once",
        ),
        (("--checkout=a", "--checkout=b", "Fix"), b"--checkout may appear only once"),
        (("--force", "--force", "Fix"), b"--force may appear only once"),
        (("--file", "-log", "Fix"), b"--file requires a value"),
        (("--project", "--", "Fix"), b"--project requires a value"),
        (("--file=", "Fix"), b"--file requires a non-empty value"),
        (("--checkout",), b"--checkout requires a non-empty value"),
        (
            ("--harness", "claude", "--harness", "codex", "Fix"),
            b"--harness may appear only once",
        ),
        (("--harness=", "Fix"), b"--harness requires a non-empty value"),
        (("--harness",), b"--harness requires a non-empty value"),
        (("--harness", "-claude", "Fix"), b"--harness requires a value"),
        (("--harness", "vim", "Fix"), b'unknown harness "vim"'),
        (("--harness=vim", "Fix"), b'unknown harness "vim"'),
        (("--force=1", "Fix"), b"unknown option"),
        (("--unknown", "Fix"), b"unknown option"),
        (("--", " \t", "\n"), b"non-whitespace text is required"),
    ],
    ids=[
        "duplicate-project",
        "duplicate-checkout",
        "duplicate-force",
        "separate-dash-file",
        "separate-terminator-selector",
        "empty-equals-file",
        "missing-checkout",
        "duplicate-harness",
        "empty-equals-harness",
        "missing-harness",
        "separate-dash-harness",
        "unknown-separate-harness",
        "unknown-equals-harness",
        "force-has-no-value",
        "unknown-option",
        "blank-instruction",
    ],
)
def test_run_grammar_failures_exit_two(args: tuple[str, ...], reason: bytes) -> None:
    result = invoke(*args)

    assert result.returncode == 2
    assert result.stdout == b""
    assert reason in result.stderr
    assert b"Try 'factory --help' for more information." in result.stderr
    assert b"Traceback" not in result.stderr


@pytest.mark.parametrize("argument", [["--path", "./api"], ["--path=./api"]])
def test_removed_path_flag_is_a_usage_error_naming_checkout(
    argument: list[str],
) -> None:
    result = invoke(*argument, "Fix")

    assert result.returncode == 2
    assert result.stdout == b""
    assert result.stderr == (
        b"factory: error: --path was removed; use --checkout PATH to select a "
        b"registered Checkout.\n"
        b"Usage:\n"
        b"  factory [--harness codex|claude] [--project NAME] [--checkout PATH] "
        b"[--file PATH]... [--force] [--re-probe] [--] INSTRUCTION...\n"
        b"  factory project COMMAND ...\n"
        b"  factory doctor project [--] SELECTOR [--checkout PATH] "
        b"[--harness NAME]\n"
        b"  factory (-h | --help)\n"
        b"  factory --version\n"
        b"Try 'factory --help' for more information.\n"
    )


def test_the_re_probe_flag_parses_once_without_a_value(tmp_path: Path) -> None:
    env = registered_run_root(tmp_path)

    accepted = invoke("--checkout", str(tmp_path), "--re-probe", "Fix", env=env)
    repeated = invoke("--re-probe", "--re-probe", "Fix", env=env)
    valued = invoke("--re-probe=1", "Fix", env=env)

    assert repeated.returncode == valued.returncode == 2
    assert b"--re-probe may appear only once" in repeated.stderr
    assert b"unknown option" in valued.stderr
    assert accepted.returncode != 2
    assert b"Traceback" not in accepted.stderr


def test_only_exact_first_project_enters_administration(tmp_path: Path) -> None:
    administration = invoke("project", "list", env={"XDG_CONFIG_HOME": str(tmp_path)})
    env = registered_run_root(tmp_path)
    run = invoke("Project", "list", cwd=tmp_path, env=env)
    escaped = invoke("--", "project", "list", cwd=tmp_path, env=env)

    assert administration.returncode == 0
    assert administration.stdout == b"No projects registered.\n"
    assert administration.stderr == b""
    assert run.returncode == escaped.returncode == 1
    rejection = b"reported an unrecognized version; install a certified Codex release."
    assert rejection in run.stderr
    assert rejection in escaped.stderr


class _BrokenStream:
    """A stream whose every write fails like a closed descriptor."""

    def write(self, _text: str) -> int:
        raise OSError("stream is closed")


@pytest.mark.parametrize(
    "argv, broken_stream",
    [
        (["factory", "--help"], "stdout"),
        (["factory", "--bogus"], "stderr"),
    ],
    ids=["help-stdout", "usage-stderr"],
)
def test_failed_factory_output_write_exits_one_without_a_traceback(
    argv: list[str],
    broken_stream: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(sys, broken_stream, _BrokenStream())

    assert factory.main() == 1
    output = capsys.readouterr()
    if broken_stream == "stdout":
        assert output.out == ""
        assert output.err == "factory: error: output failed.\n"
    else:
        assert output.out == output.err == ""
    assert "Traceback" not in output.err
