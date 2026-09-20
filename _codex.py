"""Codex Run Admission and lifecycle boundary."""

import json
import os
from pathlib import Path
import stat
from typing import BinaryIO


class RunAdmissionError(Exception):
    """Codex cannot be launched under the certified compatibility contract."""

    def __init__(self, message: str, *, executable: Path | None = None) -> None:
        super().__init__(message)
        self.executable = executable


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


def run(project_boundary: Path, run_request_spool: BinaryIO) -> int:
    """Refuse every Run until a Codex artifact and host tuple is certified."""
    executable = _resolve_executable()
    raise RunAdmissionError(
        "Codex Compatibility Set is empty; no Run is supported",
        executable=executable,
    )
