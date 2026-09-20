"""The selected Project and the Git proof established during selection."""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Project:
    """A canonical Project Boundary proven to be inside one Git worktree."""

    boundary: Path
    enclosing_git_root: Path
