"""Deterministic Codex Run Admission behavior."""

import errno
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

import factory
from _codex import RunAdmissionError, run


def make_executable(path: Path) -> None:
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o755)


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
