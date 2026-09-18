"""Public command-line behavior before a Run or registry operation begins."""

import os
import json
import fcntl
from pathlib import Path
import stat
import subprocess
import sys
import time

import pytest

import factory
from factory import ProjectSelectionError, _parse_run, _resolve_explicit_path


FACTORY = Path(__file__).resolve().parents[1] / "factory.py"


def invoke(
    *args: str, columns: str = "80", cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [sys.executable, str(FACTORY), *args],
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


def test_project_add_registers_canonical_boundary_with_exact_output(tmp_path: Path) -> None:
    git("init", "-q", cwd=tmp_path)
    boundary = tmp_path / "café"
    boundary.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(boundary, target_is_directory=True)
    config = tmp_path / "config"

    result = invoke("project", "add", "Api", "alias", cwd=tmp_path,
                    env={"XDG_CONFIG_HOME": str(config)})

    assert result.returncode == 0
    assert result.stdout == f'Added project "Api" at "{boundary}".\n'.encode()
    assert result.stderr == b""
    assert (config / "factory" / "projects.json").read_bytes() == (
        '{\n  "version": 1,\n  "projects": {\n    "Api": "'
        + str(boundary) + '"\n  }\n}\n'
    ).encode()


def test_project_add_identical_mapping_does_not_rewrite_registry(tmp_path: Path) -> None:
    git("init", "-q", cwd=tmp_path)
    boundary = tmp_path / "project"
    boundary.mkdir()
    config = tmp_path / "config"
    env = {"XDG_CONFIG_HOME": str(config)}
    first = invoke("project", "add", "api", str(boundary), env=env)
    registry = config / "factory" / "projects.json"
    before = registry.stat()

    second = invoke("project", "add", "api", str(boundary), env=env)

    assert first.returncode == second.returncode == 0
    assert second.stdout == f'Project "api" is already registered at "{boundary}".\n'.encode()
    assert second.stderr == b""
    assert registry.stat().st_ino == before.st_ino
    assert registry.stat().st_mtime_ns == before.st_mtime_ns


def test_project_add_replaces_mapping_and_sorts_names(tmp_path: Path) -> None:
    git("init", "-q", cwd=tmp_path)
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    config = tmp_path / "config"
    env = {"XDG_CONFIG_HOME": str(config)}
    assert invoke("project", "add", "z", str(first), env=env).returncode == 0
    assert invoke("project", "add", "A", str(first), env=env).returncode == 0

    replaced = invoke("project", "add", "A", str(second), env=env)

    assert replaced.returncode == 0
    assert replaced.stdout == f'Updated project "A" from "{first}" to "{second}".\n'.encode()
    assert replaced.stderr == b""
    assert (config / "factory" / "projects.json").read_bytes() == (
        '{\n  "version": 1,\n  "projects": {\n    "A": "' + str(second)
        + '",\n    "z": "' + str(first) + '"\n  }\n}\n'
    ).encode()


@pytest.mark.parametrize("name", ["", ".a", "a-", "a b", "é", "a/b", "a" * 65])
def test_project_add_rejects_invalid_project_name_without_writing(
    tmp_path: Path, name: str,
) -> None:
    git("init", "-q", cwd=tmp_path)
    config = tmp_path / "config"

    result = invoke("project", "add", name, str(tmp_path),
                    env={"XDG_CONFIG_HOME": str(config)})

    assert result.returncode == 1
    assert result.stdout == b""
    assert b"invalid Project Name" in result.stderr
    assert not config.exists()


def test_project_add_rejects_malformed_registry_without_replacing_it(tmp_path: Path) -> None:
    git("init", "-q", cwd=tmp_path)
    config, registry = show_registry(tmp_path, '{"version":1,"projects":{"bad name":"/tmp"}}')
    before = registry.read_bytes()

    result = invoke("project", "add", "api", str(tmp_path),
                    env={"XDG_CONFIG_HOME": str(config)})

    assert result.returncode == 1
    assert result.stdout == b""
    assert b"malformed Project Registry" in result.stderr
    assert registry.read_bytes() == before


def test_project_add_creates_private_files_and_preserves_registry_mode(tmp_path: Path) -> None:
    git("init", "-q", cwd=tmp_path)
    config = tmp_path / "config"
    env = {"XDG_CONFIG_HOME": str(config)}
    first = invoke("project", "add", "api", str(tmp_path), env=env)
    directory = config / "factory"
    registry = directory / "projects.json"
    lock = directory / "projects.lock"

    assert first.returncode == 0
    assert [path.stat().st_mode & 0o777 for path in (config, directory, registry, lock)] == [
        0o700, 0o700, 0o600, 0o600,
    ]
    registry.chmod(0o640)
    second = invoke("project", "add", "Api", str(tmp_path), env=env)
    assert second.returncode == 0
    assert registry.stat().st_mode & 0o777 == 0o640


def test_project_lock_persists_across_mutations(tmp_path: Path) -> None:
    git("init", "-q", cwd=tmp_path)
    config = tmp_path / "config"
    env = {"XDG_CONFIG_HOME": str(config)}
    assert invoke("project", "add", "api", str(tmp_path), env=env).returncode == 0
    lock = config / "factory" / "projects.lock"
    first = lock.stat()

    assert invoke("project", "remove", "api", env=env).returncode == 0
    assert invoke("project", "add", "api", str(tmp_path), env=env).returncode == 0

    last = lock.stat()
    assert (last.st_dev, last.st_ino) == (first.st_dev, first.st_ino)
    assert lock.stat().st_mode & 0o777 == 0o600


def test_project_add_syncs_new_configuration_directory_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    git("init", "-q", cwd=tmp_path)
    config = tmp_path / "config"
    actual_fsync = os.fsync
    synced: list[tuple[int, int]] = []

    def record_sync(descriptor: int) -> None:
        info = os.fstat(descriptor)
        if stat.S_ISDIR(info.st_mode):
            synced.append((info.st_dev, info.st_ino))
        actual_fsync(descriptor)

    monkeypatch.setattr(factory.os, "fsync", record_sync)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    monkeypatch.setattr(sys, "argv", ["factory", "project", "add", "api", str(tmp_path)])

    assert factory.main() == 0
    for directory in (tmp_path, config, config / "factory"):
        info = directory.stat()
        assert (info.st_dev, info.st_ino) in synced


def test_project_add_parent_sync_failure_stops_before_registry_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    git("init", "-q", cwd=tmp_path)
    config = tmp_path / "config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    monkeypatch.setattr(sys, "argv", ["factory", "project", "add", "api", str(tmp_path)])
    parent = tmp_path.stat()
    actual_fsync = os.fsync

    def fail_parent_sync(descriptor: int) -> None:
        info = os.fstat(descriptor)
        if (info.st_dev, info.st_ino) == (parent.st_dev, parent.st_ino):
            raise OSError("parent directory sync failed")
        actual_fsync(descriptor)

    monkeypatch.setattr(factory.os, "fsync", fail_parent_sync)

    assert factory.main() == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert "factory: error: Project Registry cannot be updated" in output.err
    assert not (config / "factory" / "projects.json").exists()


def test_project_add_concurrent_writers_keep_every_mapping(tmp_path: Path) -> None:
    git("init", "-q", cwd=tmp_path)
    config = tmp_path / "config"
    env = {**os.environ, "XDG_CONFIG_HOME": str(config)}
    writers = [
        subprocess.Popen([sys.executable, str(FACTORY), "project", "add", f"p{index}",
                          str(tmp_path)], env=env, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE)
        for index in range(8)
    ]

    for writer in writers:
        stdout, stderr = writer.communicate(timeout=10)
        assert writer.returncode == 0, stderr
        assert stdout.startswith(b"Added project")
    registry = config / "factory" / "projects.json"
    assert json.loads(registry.read_text())["projects"] == {
        f"p{index}": str(tmp_path) for index in range(8)
    }


def test_concurrent_add_and_remove_keep_complete_registry_generations(tmp_path: Path) -> None:
    git("init", "-q", cwd=tmp_path)
    original = {f"old{index}": str(tmp_path) for index in range(8)}
    config, registry = show_registry(tmp_path, json.dumps({
        "version": 1, "projects": original,
    }))
    env = {**os.environ, "XDG_CONFIG_HOME": str(config)}
    commands = [
        ("remove", f"old{index}") for index in range(8)
    ] + [
        ("add", f"new{index}", str(tmp_path)) for index in range(8)
    ]
    writers = [
        subprocess.Popen([sys.executable, str(FACTORY), "project", *command],
                         env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        for command in commands
    ]

    deadline = time.monotonic() + 20
    while True:
        document = json.loads(registry.read_bytes())
        assert document["version"] == 1
        assert set(document) == {"version", "projects"}
        assert all(path == str(tmp_path) for path in document["projects"].values())
        if all(writer.poll() is not None for writer in writers):
            break
        assert time.monotonic() < deadline, "concurrent registry writers did not finish"
        time.sleep(0.001)

    for writer in writers:
        _, stderr = writer.communicate(timeout=10)
        assert writer.returncode == 0, stderr
    assert json.loads(registry.read_bytes())["projects"] == {
        f"new{index}": str(tmp_path) for index in range(8)
    }


@pytest.mark.parametrize("command", ["add", "remove"])
def test_project_mutation_rereads_strict_registry_after_acquiring_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], command: str,
) -> None:
    git("init", "-q", cwd=tmp_path)
    config, registry = show_registry(tmp_path, json.dumps({
        "version": 1, "projects": {"old": str(tmp_path)},
    }))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    args = ["factory", "project", command, "old"]
    if command == "add":
        args.append(str(tmp_path))
    monkeypatch.setattr(sys, "argv", args)
    actual_flock = fcntl.flock

    def change_registry_at_lock(descriptor: int, operation: int) -> None:
        actual_flock(descriptor, operation)
        registry.write_text('{"version":2,"projects":{}}')

    monkeypatch.setattr(factory.fcntl, "flock", change_registry_at_lock)

    assert factory.main() == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert "malformed Project Registry: unsupported version" in output.err
    assert registry.read_text() == '{"version":2,"projects":{}}'


@pytest.mark.parametrize("command", ["add", "remove"])
@pytest.mark.parametrize("kind", ["symlink", "directory"])
def test_project_mutation_refuses_nonregular_registry_state(
    tmp_path: Path, command: str, kind: str,
) -> None:
    git("init", "-q", cwd=tmp_path)
    config, registry = show_registry(tmp_path, json.dumps({
        "version": 1, "projects": {"old": str(tmp_path)},
    }))
    registry.unlink()
    if kind == "symlink":
        target = tmp_path / "target.json"
        target.write_text('{"version":1,"projects":{}}')
        registry.symlink_to(target)
    else:
        registry.mkdir()
    args = ("project", command, "old", str(tmp_path)) if command == "add" else (
        "project", command, "old",
    )

    result = invoke(*args, env={"XDG_CONFIG_HOME": str(config)})

    assert result.returncode == 1
    assert result.stdout == b""
    assert b"malformed Project Registry: not a regular file" in result.stderr
    if kind == "symlink":
        assert registry.is_symlink()
    else:
        assert registry.is_dir()


def test_project_add_replace_failure_keeps_previous_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    git("init", "-q", cwd=tmp_path)
    config, registry = show_registry(tmp_path, '{"version":1,"projects":{}}')
    before = registry.read_bytes()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    monkeypatch.setattr(sys, "argv", ["factory", "project", "add", "api", str(tmp_path)])

    def fail_replace(*_: object) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(factory.os, "replace", fail_replace)

    result = factory.main()

    assert result == 1
    assert capsys.readouterr().out == ""
    assert registry.read_bytes() == before
    assert list(registry.parent.glob(".projects-*")) == []


@pytest.mark.parametrize("failure", ["temporary creation", "write", "file sync"])
def test_project_add_pre_replace_failure_keeps_previous_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], failure: str,
) -> None:
    git("init", "-q", cwd=tmp_path)
    config, registry = show_registry(tmp_path, '{"version":1,"projects":{}}')
    before = registry.read_bytes()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    monkeypatch.setattr(sys, "argv", ["factory", "project", "add", "api", str(tmp_path)])

    if failure == "temporary creation":
        def fail_temporary_creation(*_: object, **__: object) -> tuple[int, str]:
            raise OSError("temporary creation failed")

        monkeypatch.setattr(factory.tempfile, "mkstemp", fail_temporary_creation)
    elif failure == "write":
        actual_fdopen = os.fdopen

        class FailingWriter:
            def __init__(self, descriptor: int) -> None:
                self.file = actual_fdopen(descriptor, "wb")

            def __enter__(self) -> "FailingWriter":
                self.file.__enter__()
                return self

            def __exit__(self, *args: object) -> object:
                return self.file.__exit__(*args)

            def fileno(self) -> int:
                return self.file.fileno()

            def write(self, _: bytes) -> None:
                raise OSError("write failed")

        def fdopen(descriptor: int, mode: str) -> object:
            return FailingWriter(descriptor) if mode == "wb" else actual_fdopen(descriptor, mode)

        monkeypatch.setattr(factory.os, "fdopen", fdopen)
    else:
        def fail_file_sync(descriptor: int) -> None:
            raise OSError("file sync failed")

        monkeypatch.setattr(factory.os, "fsync", fail_file_sync)

    result = factory.main()
    output = capsys.readouterr()

    assert result == 1
    assert output.out == ""
    assert "Project Registry cannot be updated" in output.err
    assert registry.read_bytes() == before
    assert list(registry.parent.glob(".projects-*")) == []


def test_project_add_directory_sync_failure_warns_update_may_have_committed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    git("init", "-q", cwd=tmp_path)
    config = tmp_path / "config"
    registry = config / "factory" / "projects.json"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    monkeypatch.setattr(sys, "argv", ["factory", "project", "add", "api", str(tmp_path)])
    actual_fsync = os.fsync

    def fail_directory_sync(descriptor: int) -> None:
        if registry.exists() and stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError("directory sync failed")
        actual_fsync(descriptor)

    monkeypatch.setattr(factory.os, "fsync", fail_directory_sync)

    result = factory.main()
    output = capsys.readouterr()

    assert result == 1
    assert output.out == ""
    assert "factory: warning: Project Registry update may have committed" in output.err
    assert json.loads((config / "factory" / "projects.json").read_text())["projects"] == {
        "api": str(tmp_path)
    }


def test_project_remove_preserves_other_registration_and_inspection(tmp_path: Path) -> None:
    git("init", "-q", cwd=tmp_path)
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    config, registry = show_registry(tmp_path, json.dumps({
        "version": 1, "projects": {"Api": str(first), "other": str(second)},
    }))
    env = {"XDG_CONFIG_HOME": str(config)}

    result = invoke("project", "remove", "Api", env=env)

    assert result.returncode == 0
    assert result.stdout == f'Removed project "Api" from "{first}".\n'.encode()
    assert result.stderr == b""
    assert registry.read_bytes() == (
        '{\n  "version": 1,\n  "projects": {\n    "other": "' + str(second) + '"\n  }\n}\n'
    ).encode()
    assert invoke("project", "list", env=env).stdout == (
        f'NAME\tSTATUS\tPATH\n"other"\tvalid\t"{second}"\n'.encode()
    )
    assert invoke("project", "show", "other", env=env).stdout == (
        f'Name: "other"\nPath: "{second}"\nStatus: valid\n'.encode()
    )


def test_project_remove_unknown_name_keeps_registry_unchanged(tmp_path: Path) -> None:
    config, registry = show_registry(tmp_path, json.dumps({
        "version": 1, "projects": {"Api": str(tmp_path)},
    }))
    before = registry.read_bytes()
    before_stat = registry.stat()

    result = invoke("project", "remove", "api", env={"XDG_CONFIG_HOME": str(config)})

    assert result.returncode == 1
    assert result.stdout == b""
    assert result.stderr == b'factory: error: Project "api" is not registered.\n'
    assert registry.read_bytes() == before
    assert registry.stat().st_ino == before_stat.st_ino
    assert registry.stat().st_mtime_ns == before_stat.st_mtime_ns


def test_project_remove_last_registration_writes_valid_empty_registry(tmp_path: Path) -> None:
    config, registry = show_registry(tmp_path, json.dumps({
        "version": 1, "projects": {"api": str(tmp_path)},
    }))
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


def test_project_remove_unknown_from_missing_registry_creates_no_state(tmp_path: Path) -> None:
    config = tmp_path / "config"

    result = invoke("project", "remove", "api", env={"XDG_CONFIG_HOME": str(config)})

    assert result.returncode == 1
    assert result.stdout == b""
    assert result.stderr == b'factory: error: Project "api" is not registered.\n'
    assert not config.exists()


def test_project_remove_replace_failure_keeps_previous_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    config, registry = show_registry(tmp_path, json.dumps({
        "version": 1, "projects": {"api": str(tmp_path)},
    }))
    before = registry.read_bytes()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    monkeypatch.setattr(sys, "argv", ["factory", "project", "remove", "api"])

    def fail_replace(*_: object) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(factory.os, "replace", fail_replace)

    result = factory.main()
    output = capsys.readouterr()

    assert result == 1
    assert output.out == ""
    assert "Project Registry cannot be updated" in output.err
    assert registry.read_bytes() == before
    assert list(registry.parent.glob(".projects-*")) == []


def test_project_remove_directory_sync_failure_warns_update_may_have_committed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    config, registry = show_registry(tmp_path, json.dumps({
        "version": 1, "projects": {"api": str(tmp_path)},
    }))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    monkeypatch.setattr(sys, "argv", ["factory", "project", "remove", "api"])
    actual_fsync = os.fsync

    def fail_directory_sync(descriptor: int) -> None:
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError("directory sync failed")
        actual_fsync(descriptor)

    monkeypatch.setattr(factory.os, "fsync", fail_directory_sync)

    result = factory.main()
    output = capsys.readouterr()

    assert result == 1
    assert output.out == ""
    assert "factory: warning: Project Registry update may have committed" in output.err
    assert registry.read_bytes() == b'{\n  "version": 1,\n  "projects": {}\n}\n'


def test_project_list_missing_registry_is_empty_without_creating_state(tmp_path: Path) -> None:
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


def test_project_show_prints_exact_registered_name_path_and_valid_status(tmp_path: Path) -> None:
    project = tmp_path / "café\nproject"
    project.mkdir()
    git("init", "-q", cwd=project)
    config, registry = show_registry(
        tmp_path, json.dumps({"version": 1, "projects": {"Api": str(project)}}),
    )
    before = registry.read_bytes()

    result = invoke("project", "show", "Api", env={"XDG_CONFIG_HOME": str(config)})

    assert result.returncode == 0
    assert result.stdout == (
        b'Name: "Api"\nPath: "' + str(project).replace("\n", "\\n").encode()
        + b'"\nStatus: valid\n'
    )
    assert result.stderr == b""
    assert registry.read_bytes() == before
    assert not (registry.parent / "projects.lock").exists()


def test_project_show_distinguishes_each_stale_status_without_repair(tmp_path: Path) -> None:
    file_path = tmp_path / "file"
    file_path.write_text("data")
    outside = tmp_path / "outside"
    outside.mkdir()
    missing = tmp_path / "missing"
    paths = {"missing": missing, "file": file_path, "outside": outside}
    config, registry = show_registry(tmp_path, json.dumps({"version": 1, "projects": {
        name: str(path) for name, path in paths.items()
    }}))
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


def test_project_show_unknown_name_fails_operationally_and_case_sensitively(tmp_path: Path) -> None:
    config, _ = show_registry(
        tmp_path, json.dumps({"version": 1, "projects": {"Api": str(tmp_path)}}),
    )

    for name in ("api", "missing"):
        result = invoke("project", "show", name, env={"XDG_CONFIG_HOME": str(config)})
        assert result.returncode == 1
        assert result.stdout == b""
        assert f'Project "{name}" is not registered'.encode() in result.stderr
        assert b"Traceback" not in result.stderr


def test_project_show_rejects_malformed_registry_without_stdout(tmp_path: Path) -> None:
    config, _ = show_registry(
        tmp_path, '{"version":1,"projects":{"api":"/one","api":"/two"}}',
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
        tmp_path, json.dumps({"version": 1, "projects": {"Api": str(project)}}),
    )

    result = invoke("project", "show", "--", "Api", env={"XDG_CONFIG_HOME": str(config)})

    assert result.returncode == 0
    assert result.stdout == f'Name: "Api"\nPath: "{project}"\nStatus: valid\n'.encode()
    assert result.stderr == b""


def test_project_show_validation_failure_emits_no_partial_stdout(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    git("init", "-q", cwd=project)
    config, _ = show_registry(
        tmp_path, json.dumps({"version": 1, "projects": {"api": str(project)}}),
    )

    result = invoke("project", "show", "api", env={
        "XDG_CONFIG_HOME": str(config), "PATH": "",
    })

    assert result.returncode == 1
    assert result.stdout == b""
    assert b"Git inspection failed" in result.stderr


def test_project_list_sorts_and_reports_stale_entries_without_writing(tmp_path: Path) -> None:
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
    registry.write_text(json.dumps({"version": 1, "projects": {
        "z": str(outside), "a": str(file_path), "Alpha": str(valid),
        "b": str(missing),
    }}))
    before = registry.read_bytes()

    result = invoke("project", "list", env={"XDG_CONFIG_HOME": str(config)})

    assert result.returncode == 0
    assert result.stdout == (
        b'NAME\tSTATUS\tPATH\n'
        + b'"Alpha"\tvalid\t"' + str(valid).replace("\n", "\\n").encode() + b'"\n'
        + b'"a"\tstale: path is not a directory\t"' + str(file_path).encode() + b'"\n'
        + b'"b"\tstale: path does not exist\t"' + str(missing).encode() + b'"\n'
        + b'"z"\tstale: path is not inside a non-bare Git worktree\t"'
        + str(outside).encode() + b'"\n'
    )
    assert result.stderr == b""
    assert registry.read_bytes() == before
    assert not (registry.parent / "projects.lock").exists()


@pytest.mark.parametrize("document", [
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
])
def test_project_list_rejects_malformed_registry_as_a_whole(tmp_path: Path, document: str) -> None:
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
    registry.write_text(json.dumps({"version": 1, "projects": {
        "a": str(tmp_path / "missing"), "b": str(project),
    }}))

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
    registry.write_text(json.dumps({"version": 1, "projects": {
        "a": str(tmp_path / "missing"), "b": str(project),
    }}))
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_git = fake_bin / "git"
    fake_git.write_text("#!/bin/sh\necho 'fatal: bad config' >&2\nexit 128\n")
    fake_git.chmod(0o755)

    result = invoke("project", "list", env={
        "XDG_CONFIG_HOME": str(config), "PATH": str(fake_bin),
    })

    assert result.returncode == 1
    assert result.stdout == b""
    assert b"Git inspection failed" in result.stderr


def test_explicit_path_preserves_canonical_subdirectory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    git("init", "-q", cwd=tmp_path)
    boundary = tmp_path / "service"
    boundary.mkdir()
    (tmp_path / "link").symlink_to(boundary, target_is_directory=True)
    monkeypatch.setenv("FACTORY_TEST_BOUNDARY", "link")
    monkeypatch.setenv("HOME", str(tmp_path))

    assert _resolve_explicit_path("$FACTORY_TEST_BOUNDARY", tmp_path) == boundary
    assert _resolve_explicit_path("${FACTORY_TEST_BOUNDARY}", tmp_path) == boundary
    assert _resolve_explicit_path("~/service", tmp_path) == boundary


@pytest.mark.parametrize("value", ["missing", "empty"])
def test_explicit_path_rejects_unset_or_empty_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str,
) -> None:
    git("init", "-q", cwd=tmp_path)
    if value == "missing":
        monkeypatch.delenv("FACTORY_TEST_MISSING", raising=False)
        supplied = "${FACTORY_TEST_MISSING}"
    else:
        monkeypatch.setenv("FACTORY_TEST_EMPTY", "")
        supplied = "$FACTORY_TEST_EMPTY"

    result = invoke("--path", supplied, "Fix", cwd=tmp_path)

    assert result.returncode == 1
    assert result.stdout == b""
    assert b"environment variable" in result.stderr
    assert b"Run execution is not yet available" not in result.stderr
    assert b"Usage:" not in result.stderr


@pytest.mark.parametrize("kind", ["missing", "file", "dangling", "outside-git"])
def test_explicit_path_refuses_invalid_directory_without_fallback(tmp_path: Path, kind: str) -> None:
    project = tmp_path / "project"
    project.mkdir()
    git("init", "-q", cwd=project)
    if kind == "file":
        (project / "target").write_text("data")
        target = project / "target"
    elif kind == "dangling":
        target = project / "target"
        target.symlink_to(project / "missing")
    elif kind == "outside-git":
        target = tmp_path / "other"
        target.mkdir()
    else:
        target = project / "missing"

    result = invoke("--path", str(target), "Fix", cwd=project)

    assert result.returncode == 1
    assert result.stdout == b""
    assert b"factory: error:" in result.stderr
    assert b"Run execution is not yet available" not in result.stderr
    assert b"Usage:" not in result.stderr


def test_explicit_path_uses_nested_git_worktree(tmp_path: Path) -> None:
    outer = tmp_path / "outer"
    outer.mkdir()
    git("init", "-q", cwd=outer)
    inner = outer / "inner"
    inner.mkdir()
    git("init", "-q", cwd=inner)
    boundary = inner / "service"
    boundary.mkdir()

    assert _resolve_explicit_path(str(boundary), outer) == boundary


def test_explicit_path_handles_newline_in_git_worktree_path(tmp_path: Path) -> None:
    project = tmp_path / "project\nname"
    project.mkdir()
    git("init", "-q", cwd=project)
    boundary = project / "service"
    boundary.mkdir()

    assert _resolve_explicit_path(str(boundary), tmp_path) == boundary


@pytest.mark.skipif(os.name != "posix", reason="POSIX filesystem byte paths")
def test_explicit_path_handles_non_utf8_git_worktree_path(tmp_path: Path) -> None:
    project = tmp_path / os.fsdecode(b"project-\xff")
    project.mkdir()
    git("init", "-q", cwd=project)
    boundary = project / "service"
    boundary.mkdir()

    assert _resolve_explicit_path(str(boundary), tmp_path) == boundary


def test_explicit_path_accepts_linked_worktree(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    git("init", "-q", cwd=source)
    git("-c", "user.name=Test", "-c", "user.email=test@example.com",
        "commit", "--allow-empty", "-m", "initial", "-q", cwd=source)
    linked = tmp_path / "linked"
    git("worktree", "add", "-q", "-b", "linked", str(linked), cwd=source)
    boundary = linked / "service"
    boundary.mkdir()

    assert _resolve_explicit_path(str(boundary), source) == boundary


def test_explicit_path_accepts_submodule(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    git("init", "-q", cwd=source)
    git("-c", "user.name=Test", "-c", "user.email=test@example.com",
        "commit", "--allow-empty", "-m", "initial", "-q", cwd=source)
    parent = tmp_path / "parent"
    parent.mkdir()
    git("init", "-q", cwd=parent)
    git("-c", "protocol.file.allow=always", "submodule", "add", "-q", str(source),
        "module", cwd=parent)
    boundary = parent / "module" / "service"
    boundary.mkdir()

    assert _resolve_explicit_path(str(boundary), parent) == boundary


def test_explicit_path_rejects_bare_repository(tmp_path: Path) -> None:
    bare = tmp_path / "bare.git"
    git("init", "--bare", "-q", str(bare), cwd=tmp_path)

    with pytest.raises(ProjectSelectionError, match="non-bare Git worktree"):
        _resolve_explicit_path(str(bare), tmp_path)


def test_explicit_path_rejects_missing_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "")

    with pytest.raises(ProjectSelectionError, match="Git inspection failed"):
        _resolve_explicit_path(str(tmp_path), tmp_path)


def test_valid_explicit_path_reaches_run_stage(tmp_path: Path) -> None:
    git("init", "-q", cwd=tmp_path)
    boundary = tmp_path / "service"
    boundary.mkdir()

    result = invoke(
        "--path=$FACTORY_TEST_BOUNDARY", "Fix", cwd=tmp_path,
        env={"FACTORY_TEST_BOUNDARY": "service"},
    )

    assert result.returncode == 1
    assert result.stdout == b""
    assert result.stderr == b"factory: error: Codex Compatibility Set is empty; no Run is supported.\n"


@pytest.mark.parametrize("case", ["outside", "bare", "missing-git", "inspection-failure"])
def test_discovery_refuses_unusable_git_context(tmp_path: Path, case: str) -> None:
    env: dict[str, str] = {}
    if case == "bare":
        git("init", "--bare", "-q", cwd=tmp_path)
    elif case != "outside":
        git("init", "-q", cwd=tmp_path)
    if case == "missing-git":
        env["PATH"] = ""
    elif case == "inspection-failure":
        env["GIT_DIR"] = str(tmp_path / "missing-git-dir")

    result = invoke("Fix", cwd=tmp_path, env=env)

    assert result.returncode == 1
    assert result.stdout == b""
    expected = (b"Git inspection failed" if case in ("missing-git", "inspection-failure")
                else b"non-bare Git worktree")
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
        b"  factory [--project NAME | --path PATH] [--file PATH]... [--force] [--] INSTRUCTION...\n"
        b"  factory project COMMAND ...\n"
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
        b"  factory project add [--] NAME PATH\n"
        b"  factory project list\n"
        b"  factory project show [--] NAME\n"
        b"  factory project remove [--] NAME\n"
    )
    for detail in (b"Project Registry", b"add", b"list", b"show", b"remove"):
        assert detail in wide.stdout


@pytest.mark.parametrize(
    ("args", "hint"),
    [
        ((), b"Try 'factory --help' for more information."),
        (("--project", "api"), b"Try 'factory --help' for more information."),
        (("--path", "./api", "--project", "api", "Fix"), b"Try 'factory --help' for more information."),
        (("--force", "--force", "Fix"), b"Try 'factory --help' for more information."),
        (("--file", "-oops", "Fix"), b"Try 'factory --help' for more information."),
        (("--unknown", "Fix"), b"Try 'factory --help' for more information."),
        (("--help", "Fix"), b"Try 'factory --help' for more information."),
        (("--version", "--help"), b"Try 'factory --help' for more information."),
        (("--", "  "), b"Try 'factory --help' for more information."),
        (("project",), b"Try 'factory project --help' for more information."),
        (("project", "add", "api"), b"Try 'factory project --help' for more information."),
        (("project", "list", "extra"), b"Try 'factory project --help' for more information."),
        (("project", "show", "--help"), b"Try 'factory project --help' for more information."),
        (("project", "--version"), b"Try 'factory project --help' for more information."),
    ],
)
def test_malformed_commands_have_relevant_usage_and_no_traceback(
    args: tuple[str, ...], hint: bytes
) -> None:
    result = invoke(*args)

    assert result.returncode == 2
    assert result.stdout == b""
    assert result.stderr.startswith(b"factory: error: ")
    assert (b"factory project add [--] NAME PATH" if args[:1] == ("project",) else
            b"factory [--project NAME | --path PATH]") in result.stderr
    assert hint in result.stderr
    assert b"Traceback" not in result.stderr


@pytest.mark.parametrize(
    "args",
    [
        ("Fix", "--unknown"),
        ("--", "project", "list"),
        ("--project=api", "Fix"),
        ("--file=-log", "Fix"),
        ("project", "add", "--", "api", "/some/path"),
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
        ("--file", "first.txt", "--project=api", "--file=first.txt", "--force",
         "--", "  Fix  this  ", "--file", "last\tword"),
        ("--", "project", "list"),
        ("--project", "api", "Fix"),
        ("--file=-log", "--file", "next.log", "Fix"),
        ("-", "--force"),
    ],
    ids=[
        "repeated-context-and-whitespace", "terminator-escapes-project",
        "separate-selector",
        "repeatable-files", "lone-dash-instruction",
    ],
)
def test_run_accepts_documented_forms_through_cli(args: tuple[str, ...], tmp_path: Path) -> None:
    git("init", "-q", cwd=tmp_path)
    (tmp_path / "-log").write_text("context")
    (tmp_path / "next.log").write_text("context")
    result = invoke(*args, cwd=tmp_path)

    assert result.returncode == 1
    assert result.stdout == b""
    expected = (
        b"factory: error: Run execution is not yet available.\n"
        if "--project" in args or "--project=api" in args else
        b"factory: error: Codex Compatibility Set is empty; no Run is supported.\n"
    )
    assert result.stderr == expected


def test_run_parser_returns_instruction_selector_context_sources_and_force() -> None:
    parsed = _parse_run([
        "--file", "first.txt", "--project=api", "--file=first.txt",
        "--force", "--", "  Fix  this  ", "--file", "last\tword",
    ])

    assert parsed.instruction == "  Fix  this   --file last\tword"
    assert parsed.selector == ("project", "api")
    assert parsed.files == ("first.txt", "first.txt")
    assert parsed.force is True


@pytest.mark.parametrize(
    ("args", "reason"),
    [
        (("--project", "api", "--project", "api", "Fix"), b"--project may appear only once"),
        (("--path=a", "--path=b", "Fix"), b"--path may appear only once"),
        (("--project=api", "--path=./api", "Fix"), b"cannot be combined"),
        (("--force", "--force", "Fix"), b"--force may appear only once"),
        (("--file", "-log", "Fix"), b"--file requires a value"),
        (("--project", "--", "Fix"), b"--project requires a value"),
        (("--file=", "Fix"), b"--file requires a non-empty value"),
        (("--path",), b"--path requires a non-empty value"),
        (("--force=1", "Fix"), b"unknown option"),
        (("--unknown", "Fix"), b"unknown option"),
        (("--", " \t", "\n"), b"non-whitespace text is required"),
    ],
    ids=[
        "duplicate-project", "duplicate-path", "conflicting-selectors", "duplicate-force",
        "separate-dash-file", "separate-terminator-selector", "empty-equals-file",
        "missing-path", "force-has-no-value", "unknown-option", "blank-instruction",
    ],
)
def test_run_grammar_failures_exit_two(args: tuple[str, ...], reason: bytes) -> None:
    result = invoke(*args)

    assert result.returncode == 2
    assert result.stdout == b""
    assert reason in result.stderr
    assert b"Try 'factory --help' for more information." in result.stderr
    assert b"Traceback" not in result.stderr


def test_only_exact_first_project_enters_administration(tmp_path: Path) -> None:
    administration = invoke("project", "list", env={"XDG_CONFIG_HOME": str(tmp_path)})
    run = invoke("Project", "list")
    escaped = invoke("--", "project", "list")

    assert administration.returncode == 0
    assert administration.stdout == b"No projects registered.\n"
    assert administration.stderr == b""
    assert run.returncode == escaped.returncode == 1
    assert b"Codex Compatibility Set is empty" in run.stderr
    assert b"Codex Compatibility Set is empty" in escaped.stderr
