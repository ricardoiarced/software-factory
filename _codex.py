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


def _codex_0_155_1_permission_profile(executable: Path) -> str:
    """Serialize the isolated permission profile certified for Codex 0.155.1."""

    return (
        '{extends = ":workspace", filesystem = {":root" = "deny", '
        '":minimal" = "read", ":tmpdir" = "deny", ":slash_tmp" = "deny", '
        f'{_quoted_absolute_path(executable.resolve().parent)} = "read", '
        '":workspace_roots" = {"." = "write"}}, network = {enabled = false}}'
    )


def _codex_0_155_1_invocation(
    executable: Path, project_boundary: Path, enclosing_git_root: Path,
) -> list[str]:
    """Build the complete strict invocation certified for Codex 0.155.1."""

    return [
        os.fspath(executable.resolve()),
        "exec",
        "--ephemeral",
        "--strict-config",
        "--ignore-user-config",
        "--ignore-rules",
        "-C",
        os.fspath(project_boundary),
        "-c", "approval_policy=\"never\"",
        "-c", "project_doc_max_bytes=0",
        "-c", "project_root_markers=[]",
        "-c", "allow_login_shell=false",
        "-c", "shell_environment_policy.inherit=\"core\"",
        "-c", "shell_environment_policy.ignore_default_excludes=false",
        "-c", "features.apps=false",
        "-c", "features.plugins=false",
        "-c", "features.remote_plugin=false",
        "-c", "features.browser_use=false",
        "-c", "features.browser_use_external=false",
        "-c", "features.browser_use_full_cdp=false",
        "-c", "features.computer_use=false",
        "-c", "features.multi_agent=false",
        "-c", "features.memories=false",
        "-c", "features.skip_host_skill_discovery=true",
        "-c", "default_permissions=\"factory-isolated\"",
        "-c", "permissions.factory-isolated=" + _codex_0_155_1_permission_profile(executable),
        "-c", (
            "projects."
            + json.dumps(os.fspath(enclosing_git_root), ensure_ascii=False)
            + ".trust_level=\"untrusted\""
        ),
        "-",
    ]


def _enclosing_git_root(project_boundary: Path) -> Path:
    """Find the trusted-for-discovery Git root that Codex must treat as untrusted."""

    try:
        result = subprocess.run(
            ["git", "-C", os.fspath(project_boundary), "rev-parse", "--show-toplevel"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError as error:
        raise RunAdmissionError(
            f"Git could not identify the enclosing root: {error.strerror or error}",
        ) from error
    if result.returncode or not result.stdout.endswith(b"\n"):
        raise RunAdmissionError("Git could not identify the enclosing root")
    try:
        return Path(result.stdout[:-1].decode("utf-8")).resolve(strict=True)
    except (OSError, UnicodeError) as error:
        raise RunAdmissionError("Git reported an unusable enclosing root") from error


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
    enclosing_git_root = _enclosing_git_root(project_boundary)
    invocation = _codex_0_155_1_invocation(
        executable, project_boundary, enclosing_git_root,
    )
    # Invocation construction can inspect the filesystem; close that TOCTOU window.
    _match_compatibility(executable)
    try:
        process = subprocess.Popen(
            invocation,
            cwd=project_boundary,
            stdin=run_request_spool,
        )
    except OSError as error:
        raise RunAdmissionError(
            f"Codex executable {_quoted_absolute_path(executable)} could not start: "
            f"{error.strerror or error}",
            executable=executable,
        ) from error
    return process.wait()
