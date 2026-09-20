"""Codex Run Admission and lifecycle boundary."""

from dataclasses import dataclass
import errno
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import stat
import subprocess
from typing import BinaryIO


class RunAdmissionError(Exception):
    """Codex cannot be launched under the certified compatibility contract."""

    def __init__(self, message: str, *, executable: Path | None = None) -> None:
        super().__init__(message)
        self.executable = executable


@dataclass(frozen=True)
class _CompatibilityEntry:
    """One immutable Codex artifact and host identity certified by Factory."""

    version: str
    sha256: str
    operating_system: str
    architecture: str
    canonical_path: Path
    filesystem_identity: tuple[int, int]


# Certification adds complete exact entries here. Keeping production empty is
# fail-closed until an artifact has passed the real-host certification suite.
_COMPATIBILITY_SET: tuple[_CompatibilityEntry, ...] = ()
_VERSION_OUTPUT = re.compile(rb"codex ([0-9]+(?:\.[0-9]+)*(?:[-+][0-9A-Za-z.-]+)?)\n\Z")


def _quoted_absolute_path(path: Path) -> str:
    return json.dumps(str(path.absolute()), ensure_ascii=False)


def _inspection_failure(path: Path, error: OSError) -> RunAdmissionError:
    cause = error.strerror or str(error)
    return RunAdmissionError(
        f"Codex executable {_quoted_absolute_path(path)} could not be inspected: "
        f"{cause}; check its permissions"
    )


def _resolve_executable() -> Path:
    first_failure: RunAdmissionError | None = None
    for directory in os.get_exec_path():
        candidate = Path(directory or os.curdir) / "codex"
        try:
            candidate.lstat()
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError as error:
            if first_failure is None:
                first_failure = _inspection_failure(candidate, error)
            continue

        try:
            executable = candidate.resolve(strict=True)
            metadata = executable.stat()
        except FileNotFoundError:
            failure = RunAdmissionError(
                f"Codex executable {_quoted_absolute_path(candidate)} is a dangling symlink; "
                "repair or remove it from PATH"
            )
        except RuntimeError:
            failure = RunAdmissionError(
                f"Codex executable {_quoted_absolute_path(candidate)} contains a symlink loop; "
                "repair or remove it from PATH"
            )
        except OSError as error:
            if error.errno == errno.ELOOP:
                failure = RunAdmissionError(
                    f"Codex executable {_quoted_absolute_path(candidate)} contains a symlink loop; "
                    "repair or remove it from PATH"
                )
            else:
                failure = _inspection_failure(candidate, error)
        else:
            if not stat.S_ISREG(metadata.st_mode):
                failure = RunAdmissionError(
                    f"Codex executable {_quoted_absolute_path(executable)} "
                    "is not a regular file; "
                    "install Codex or remove it from PATH"
                )
            elif not os.access(executable, os.X_OK):
                failure = RunAdmissionError(
                    f"Codex executable {_quoted_absolute_path(executable)} is not executable; "
                    "grant execute permission or replace it"
                )
            else:
                return executable
        if first_failure is None:
            first_failure = failure

    if first_failure is not None:
        raise first_failure
    raise RunAdmissionError(
        'Codex executable "codex" was not found on PATH; '
        "install Codex or add it to PATH"
    )


def _reported_version(executable: Path) -> str:
    try:
        result = subprocess.run(
            [os.fspath(executable), "--version"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError as error:
        raise RunAdmissionError(
            f"Codex executable {_quoted_absolute_path(executable)} could not report its version: "
            f"{error.strerror or error}; check its permissions",
            executable=executable,
        ) from error
    if result.returncode:
        raise RunAdmissionError(
            f"Codex executable {_quoted_absolute_path(executable)} could not report its version: "
            f"exit status {result.returncode}",
            executable=executable,
        )
    match = _VERSION_OUTPUT.fullmatch(result.stdout)
    if match is None:
        raise RunAdmissionError(
            f"Codex executable {_quoted_absolute_path(executable)} reported an unrecognized version; "
            "install a certified Codex release",
            executable=executable,
        )
    return match.group(1).decode("ascii")


def _artifact_identity(executable: Path) -> tuple[str, tuple[int, int]]:
    """Read digest and filesystem identity from the same opened artifact."""

    try:
        with executable.open("rb") as artifact:
            metadata = os.fstat(artifact.fileno())
            digest = hashlib.file_digest(artifact, "sha256").hexdigest()
    except OSError as error:
        raise _inspection_failure(executable, error) from error
    return digest, (metadata.st_dev, metadata.st_ino)


def _match_compatibility(executable: Path) -> _CompatibilityEntry:
    """Return the sole entry whose complete immutable identity matches."""

    version = _reported_version(executable)
    digest, filesystem_identity = _artifact_identity(executable)
    operating_system = platform.system()
    architecture = platform.machine()

    for entry in _COMPATIBILITY_SET:
        if (
            entry.version == version
            and entry.sha256 == digest
            and entry.operating_system == operating_system
            and entry.architecture == architecture
            and entry.canonical_path == executable
            and entry.filesystem_identity == filesystem_identity
        ):
            return entry

    candidates = tuple(entry for entry in _COMPATIBILITY_SET if entry.version == version)
    if not candidates:
        reason = "version is not in the Codex Compatibility Set"
    elif not any(
        entry.operating_system == operating_system and entry.architecture == architecture
        for entry in candidates
    ):
        reason = "platform does not match the Codex Compatibility Set"
    elif not any(entry.sha256 == digest for entry in candidates):
        reason = "SHA-256 does not match the Codex Compatibility Set"
    elif not any(entry.canonical_path == executable for entry in candidates):
        reason = "canonical path does not match the Codex Compatibility Set"
    else:
        reason = "filesystem identity does not match the Codex Compatibility Set"
    raise RunAdmissionError(
        f"Codex executable {_quoted_absolute_path(executable)} {reason}; "
        "install the certified artifact",
        executable=executable,
    )


def run(project_boundary: Path, run_request_spool: BinaryIO) -> int:
    """Refuse every Run until a Codex artifact and host tuple is certified."""
    executable = _resolve_executable()
    if not _COMPATIBILITY_SET:
        raise RunAdmissionError(
            "Codex Compatibility Set is empty; no Run is supported",
            executable=executable,
        )
    _match_compatibility(executable)
    raise RunAdmissionError(
        "Codex launch configuration has not been certified",
        executable=executable,
    )
