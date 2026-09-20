"""Deterministic Codex Run Admission behavior."""

import errno
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tomllib

import pytest

import factory
from _codex import (
    _codex_0_155_1_permission_profile,
    _CompatibilityEntry,
    _match_compatibility,
    RunAdmissionError,
    run,
)


def make_executable(path: Path, version: str = "0.154.0") -> None:
    path.write_text(f"#!/bin/sh\nif [ \"$1\" = --version ]; then\n  echo 'codex {version}'\nfi\n")
    path.chmod(0o755)


def certify(executable: Path) -> _CompatibilityEntry:
    metadata = executable.stat()
    with executable.open("rb") as artifact:
        digest = hashlib.file_digest(artifact, "sha256").hexdigest()
    return _CompatibilityEntry(
        version="0.154.0",
        sha256=digest,
        operating_system=platform.system(),
        architecture=platform.machine(),
        canonical_path=executable,
        filesystem_identity=(metadata.st_dev, metadata.st_ino),
    )


def install_compatibility_fixture(
    executable: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("_codex._COMPATIBILITY_SET", (certify(executable),))


def test_codex_0_155_1_permission_profile_is_deterministic_and_exact(tmp_path: Path) -> None:
    executable = tmp_path / "Codex Runtime" / "codex"

    profile = _codex_0_155_1_permission_profile(executable)

    assert profile == (
        '{extends = ":workspace", filesystem = {":root" = "deny", '
        '":minimal" = "read", ":tmpdir" = "deny", ":slash_tmp" = "deny", '
        f'{json.dumps(str(executable.parent.absolute()), ensure_ascii=False)} = "read", '
        '":workspace_roots" = {"." = "write"}}, network = {enabled = false}}'
    )
    assert _codex_0_155_1_permission_profile(executable) == profile


def test_codex_0_155_1_permission_profile_allows_only_the_required_access(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "runtime" / "codex"

    profile = tomllib.loads(
        "profile = " + _codex_0_155_1_permission_profile(executable),
    )["profile"]

    assert profile == {
        "extends": ":workspace",
        "filesystem": {
            ":root": "deny",
            ":minimal": "read",
            ":tmpdir": "deny",
            ":slash_tmp": "deny",
            str(executable.parent.absolute()): "read",
            ":workspace_roots": {".": "write"},
        },
        "network": {"enabled": False},
    }


def test_codex_0_155_1_permission_profile_reopens_the_canonical_executable_directory(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "installation" / "codex"
    executable.parent.mkdir()
    executable.write_text("codex")
    linked_executable = tmp_path / "bin" / "codex"
    linked_executable.parent.mkdir()
    linked_executable.symlink_to(executable)

    profile = tomllib.loads(
        "profile = " + _codex_0_155_1_permission_profile(linked_executable),
    )["profile"]

    assert profile["filesystem"][str(executable.parent)] == "read"
    assert str(linked_executable.parent) not in profile["filesystem"]


def test_identity_matches_an_exact_private_compatibility_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "codex"
    make_executable(executable)
    install_compatibility_fixture(executable, monkeypatch)

    assert _match_compatibility(executable) == certify(executable)


def test_identity_refuses_malformed_version_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "codex"
    make_executable(executable)
    install_compatibility_fixture(executable, monkeypatch)
    monkeypatch.setattr(
        "_codex.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, b"0.154.0\n", b""),
    )

    with pytest.raises(RunAdmissionError, match="reported an unrecognized version"):
        _match_compatibility(executable)


def test_identity_refuses_a_changed_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "codex"
    make_executable(executable)
    install_compatibility_fixture(executable, monkeypatch)
    executable.write_text(executable.read_text() + "# changed\n")
    executable.chmod(0o755)

    with pytest.raises(RunAdmissionError, match="SHA-256 does not match"):
        _match_compatibility(executable)


def test_identity_refuses_another_platform(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "codex"
    make_executable(executable)
    entry = certify(executable)
    monkeypatch.setattr(
        "_codex._COMPATIBILITY_SET",
        (_CompatibilityEntry(
            version=entry.version,
            sha256=entry.sha256,
            operating_system="other-os",
            architecture=entry.architecture,
            canonical_path=entry.canonical_path,
            filesystem_identity=entry.filesystem_identity,
        ),),
    )

    with pytest.raises(RunAdmissionError, match="platform does not match"):
        _match_compatibility(executable)


def test_identity_refuses_another_architecture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "codex"
    make_executable(executable)
    entry = certify(executable)
    monkeypatch.setattr(
        "_codex._COMPATIBILITY_SET",
        (_CompatibilityEntry(
            version=entry.version,
            sha256=entry.sha256,
            operating_system=entry.operating_system,
            architecture="other-architecture",
            canonical_path=entry.canonical_path,
            filesystem_identity=entry.filesystem_identity,
        ),),
    )

    with pytest.raises(RunAdmissionError, match="platform does not match"):
        _match_compatibility(executable)


def test_identity_uses_the_canonical_target_of_a_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "installation" / "codex"
    executable.parent.mkdir()
    make_executable(executable)
    symlink = tmp_path / "bin" / "codex"
    symlink.parent.mkdir()
    symlink.symlink_to(executable)
    install_compatibility_fixture(executable, monkeypatch)

    assert _match_compatibility(symlink.resolve()) == certify(executable)


def test_identity_refuses_a_replaced_filesystem_object(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "codex"
    make_executable(executable)
    install_compatibility_fixture(executable, monkeypatch)
    replacement = tmp_path / "replacement"
    shutil.copyfile(executable, replacement)
    replacement.chmod(0o755)
    replacement.replace(executable)

    with pytest.raises(RunAdmissionError, match="filesystem identity does not match"):
        _match_compatibility(executable)


def test_identity_refuses_replacement_between_version_and_artifact_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "codex"
    make_executable(executable)
    install_compatibility_fixture(executable, monkeypatch)
    replacement = tmp_path / "replacement"
    shutil.copyfile(executable, replacement)
    replacement.chmod(0o755)
    original_open = Path.open

    def replace_before_open(path: Path, *args: object, **kwargs: object) -> io.IOBase:
        if path == executable:
            replacement.replace(executable)
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", replace_before_open)

    with pytest.raises(RunAdmissionError, match="filesystem identity does not match"):
        _match_compatibility(executable)


def test_run_refuses_when_codex_is_missing_from_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    empty_path = tmp_path / "empty"
    empty_path.mkdir()
    monkeypatch.setenv("PATH", str(empty_path))

    with pytest.raises(
        RunAdmissionError,
        match=r'^Codex executable "codex" was not found on PATH; install Codex or add it to PATH$',
    ):
        run(tmp_path, io.BytesIO())


def test_run_canonicalizes_a_direct_codex_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "bin" / "codex"
    executable.parent.mkdir()
    make_executable(executable)
    monkeypatch.setenv("PATH", os.fspath(executable.parent))

    with pytest.raises(RunAdmissionError) as raised:
        run(tmp_path, io.BytesIO())

    assert str(raised.value) == "Codex Compatibility Set is empty; no Run is supported"
    assert raised.value.executable == executable


def test_run_canonicalizes_a_symlinked_codex_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "installation" / "codex-real"
    executable.parent.mkdir()
    make_executable(executable)
    path_directory = tmp_path / "bin"
    path_directory.mkdir()
    (path_directory / "codex").symlink_to(executable)
    monkeypatch.setenv("PATH", os.fspath(path_directory))

    with pytest.raises(RunAdmissionError) as raised:
        run(tmp_path, io.BytesIO())

    assert str(raised.value) == "Codex Compatibility Set is empty; no Run is supported"
    assert raised.value.executable == executable


def test_run_refuses_a_dangling_codex_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path_directory = tmp_path / "bin"
    path_directory.mkdir()
    candidate = path_directory / "codex"
    candidate.symlink_to(tmp_path / "missing-codex")
    monkeypatch.setenv("PATH", os.fspath(path_directory))

    with pytest.raises(RunAdmissionError) as raised:
        run(tmp_path, io.BytesIO())

    assert str(raised.value) == (
        f'Codex executable "{candidate}" is a dangling symlink; '
        "repair or remove it from PATH"
    )


def test_run_refuses_a_codex_symlink_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path_directory = tmp_path / "bin"
    path_directory.mkdir()
    candidate = path_directory / "codex"
    candidate.symlink_to(candidate)
    monkeypatch.setenv("PATH", os.fspath(path_directory))

    with pytest.raises(RunAdmissionError) as raised:
        run(tmp_path, io.BytesIO())

    assert str(raised.value) == (
        f'Codex executable "{candidate}" contains a symlink loop; '
        "repair or remove it from PATH"
    )


def test_run_refuses_a_codex_symlink_loop_reported_as_os_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path_directory = tmp_path / "bin"
    path_directory.mkdir()
    candidate = path_directory / "codex"
    make_executable(candidate)
    original_resolve = Path.resolve

    def looped_resolve(path: Path, *, strict: bool = False) -> Path:
        if path == candidate and strict:
            raise OSError(errno.ELOOP, "Too many levels of symbolic links", os.fspath(path))
        return original_resolve(path, strict=strict)

    monkeypatch.setenv("PATH", os.fspath(path_directory))
    monkeypatch.setattr(Path, "resolve", looped_resolve)

    with pytest.raises(RunAdmissionError) as raised:
        run(tmp_path, io.BytesIO())

    assert str(raised.value) == (
        f'Codex executable "{candidate}" contains a symlink loop; '
        "repair or remove it from PATH"
    )


@pytest.mark.parametrize("kind", ["non-regular", "non-executable"])
def test_run_refuses_an_unusable_codex_path_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str,
) -> None:
    path_directory = tmp_path / "bin"
    path_directory.mkdir()
    candidate = path_directory / "codex"
    if kind == "non-regular":
        candidate.mkdir()
        expected = (
            f'Codex executable "{candidate}" is not a regular file; '
            "install Codex or remove it from PATH"
        )
    else:
        candidate.write_text("#!/bin/sh\nexit 0\n")
        candidate.chmod(0o644)
        expected = (
            f'Codex executable "{candidate}" is not executable; '
            "grant execute permission or replace it"
        )
    monkeypatch.setenv("PATH", os.fspath(path_directory))

    with pytest.raises(RunAdmissionError) as raised:
        run(tmp_path, io.BytesIO())

    assert str(raised.value) == expected


def test_run_reports_an_inaccessible_codex_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path_directory = tmp_path / "bin"
    path_directory.mkdir()
    candidate = path_directory / "codex"
    make_executable(candidate)
    original_lstat = Path.lstat

    def inaccessible_lstat(path: Path) -> os.stat_result:
        if path == candidate:
            raise PermissionError(errno.EACCES, "Permission denied", os.fspath(path))
        return original_lstat(path)

    monkeypatch.setenv("PATH", os.fspath(path_directory))
    monkeypatch.setattr(Path, "lstat", inaccessible_lstat)

    with pytest.raises(RunAdmissionError) as raised:
        run(tmp_path, io.BytesIO())

    assert str(raised.value) == (
        f'Codex executable "{candidate}" could not be inspected: '
        "Permission denied; check its permissions"
    )


def test_run_reports_an_inaccessible_resolved_codex_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path_directory = tmp_path / "bin"
    path_directory.mkdir()
    candidate = path_directory / "codex"
    make_executable(candidate)
    original_resolve = Path.resolve

    def inaccessible_resolve(path: Path, *, strict: bool = False) -> Path:
        if path == candidate and strict:
            raise PermissionError(errno.EACCES, "Permission denied", os.fspath(path))
        return original_resolve(path, strict=strict)

    monkeypatch.setenv("PATH", os.fspath(path_directory))
    monkeypatch.setattr(Path, "resolve", inaccessible_resolve)

    with pytest.raises(RunAdmissionError) as raised:
        run(tmp_path, io.BytesIO())

    assert str(raised.value) == (
        f'Codex executable "{candidate}" could not be inspected: '
        "Permission denied; check its permissions"
    )


def test_factory_reports_an_admission_failure_without_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    git = shutil.which("git")
    assert git is not None
    subprocess.run([git, "init", "-q"], cwd=tmp_path, check=True)
    path_directory = tmp_path / "path"
    path_directory.mkdir()
    (path_directory / "git").symlink_to(Path(git).resolve())
    monkeypatch.setenv("PATH", os.fspath(path_directory))
    monkeypatch.setattr(sys, "argv", ["factory", "--path", str(tmp_path), "Fix"])
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"")))
    monkeypatch.chdir(tmp_path)

    assert factory.main() == 1
    assert capsys.readouterr().err == (
        'factory: error: Codex executable "codex" was not found on PATH; '
        "install Codex or add it to PATH.\n"
    )
