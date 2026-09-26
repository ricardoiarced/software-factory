"""Select a canonical Project together with its authoritative Git proof."""

import json
import os
import re
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

from _project_guidance import ProjectMarkerError, has_project_marker


class ProjectSelectionError(Exception):
    """A Project could not be selected."""


class GitInspectionError(ProjectSelectionError):
    """Git could not reliably determine a Project's worktree."""


class StaleProjectError(Exception):
    """A registered Project no longer satisfies its stored selection proof."""


@dataclass(frozen=True)
class Project:
    """A canonical Project Boundary proven to be inside one Git worktree."""

    boundary: Path
    enclosing_git_root: Path


_ENV_REFERENCE = re.compile(r"\$(\w+|\{[^}]*\})")


def _quoted(value: str) -> str:
    return json.dumps(value, ensure_ascii=True)


def expand_path(supplied: str) -> str:
    """Expand the path syntax shared by Project and Context selection."""

    def expand_variable(match: re.Match[str]) -> str:
        name = match.group(1).strip("{}")
        value = os.environ.get(name)
        if not value:
            raise ProjectSelectionError(
                f"environment variable {_quoted(name)} is unset or empty",
            )
        return value

    expanded = _ENV_REFERENCE.sub(expand_variable, supplied)
    expanded_home = os.path.expanduser(expanded)
    if expanded_home.startswith("~"):
        raise ProjectSelectionError(
            f"home directory in {_quoted(supplied)} could not be expanded",
        )
    return expanded_home


def _containing_worktree_root(path: Path, label: str) -> Path:
    """Ask Git for the canonical root containing a directory in a worktree."""

    try:
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--is-inside-work-tree",
             "--is-bare-repository", "--show-toplevel"],
            capture_output=True, check=False, env={**os.environ, "LC_ALL": "C"},
        )
    except OSError as error:
        raise GitInspectionError(f"Git inspection failed for {label}: {error}") from error
    if result.returncode:
        if (result.stdout == b"false\ntrue\n" and
                b"this operation must be run in a work tree" in result.stderr):
            raise ProjectSelectionError(f"{label} is not inside a non-bare Git worktree")
        if (b"not a git repository" in result.stderr and
                not os.environ.get("GIT_DIR")):
            raise ProjectSelectionError(f"{label} is not inside a non-bare Git worktree")
        raise GitInspectionError(
            f"Git inspection failed for {label}: Git returned {result.returncode}",
        )
    lines = result.stdout.split(b"\n", 2)
    if len(lines) != 3:
        raise GitInspectionError(f"Git inspection failed for {label}: invalid Git output")
    if lines[0] != b"true" or lines[1] != b"false":
        raise ProjectSelectionError(f"{label} is not inside a non-bare Git worktree")
    try:
        top_level = Path(os.fsdecode(lines[2].removesuffix(b"\n"))).resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise GitInspectionError(
            f"Git inspection failed for {label}: invalid worktree path",
        ) from error
    if not path.is_relative_to(top_level):
        raise GitInspectionError(
            f"Git inspection failed for {label}: worktree does not contain path",
        )
    return top_level


def select_explicit_project(supplied: str, invocation_dir: Path) -> Project:
    """Resolve and validate an exact explicit Project Boundary."""

    expanded_home = expand_path(supplied)
    try:
        boundary = Path(expanded_home)
        if not boundary.is_absolute():
            boundary = invocation_dir / boundary
        boundary = boundary.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        cause = error.strerror if isinstance(error, OSError) and error.strerror else "symlink loop"
        raise ProjectSelectionError(
            f"path {_quoted(supplied)} cannot be resolved: {cause}",
        ) from error
    if not boundary.is_dir():
        raise ProjectSelectionError(f"path {_quoted(supplied)} is not a directory")

    enclosing_git_root = _containing_worktree_root(
        boundary, f"path {_quoted(supplied)}",
    )
    return Project(boundary=boundary, enclosing_git_root=enclosing_git_root)


def discover_project(invocation_dir: Path) -> Project:
    """Select the nearest Project Marker within the containing worktree."""

    root = _containing_worktree_root(invocation_dir, "invocation directory")
    candidate = invocation_dir
    while True:
        try:
            marker_present = has_project_marker(candidate)
        except ProjectMarkerError as error:
            raise ProjectSelectionError(
                f"Project Marker at {_quoted(str(candidate / '.agent'))} {error}",
            ) from error
        if marker_present or candidate == root:
            return Project(boundary=candidate, enclosing_git_root=root)
        candidate = candidate.parent


def inspect_registered_project(path: str) -> Project:
    """Re-establish the stored Project Boundary and its Git proof."""

    selected = Path(path)
    try:
        mode = selected.stat().st_mode
    except (FileNotFoundError, NotADirectoryError) as error:
        raise StaleProjectError("stale: path does not exist") from error
    if not stat.S_ISDIR(mode):
        raise StaleProjectError("stale: path is not a directory")

    canonical = selected.resolve(strict=True)
    if str(canonical) != path:
        raise StaleProjectError("stale: path is not canonical")
    try:
        enclosing_git_root = _containing_worktree_root(
            canonical, f"path {_quoted(path)}",
        )
    except GitInspectionError:
        raise
    except ProjectSelectionError as error:
        raise StaleProjectError(
            "stale: path is not inside a non-bare Git worktree",
        ) from error
    return Project(boundary=canonical, enclosing_git_root=enclosing_git_root)
