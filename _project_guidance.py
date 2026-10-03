"""Find and safely open the optional Project Spec for a Project."""

import os
import stat
from pathlib import Path
from typing import BinaryIO


class ProjectMarkerError(Exception):
    """A Project Marker is malformed or inaccessible."""


class ProjectGuidanceError(Exception):
    """Project Guidance cannot be opened safely for a Run."""


def _project_marker(boundary: Path) -> Path | None:
    """Return a contained Project Marker or reject malformed marker state."""

    marker = boundary / ".agent"
    try:
        marker.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise ProjectMarkerError(f"cannot be inspected: {error}") from error
    try:
        resolved = marker.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise ProjectMarkerError(f"cannot be resolved: {error}") from error
    if not resolved.is_relative_to(boundary) or not resolved.is_dir():
        raise ProjectMarkerError("is not a contained directory")
    return marker


def open_project_spec(boundary: Path) -> BinaryIO | None:
    """Open the contained Project Spec, or return absent, without exposing its path."""

    try:
        marker = _project_marker(boundary)
    except ProjectMarkerError as error:
        raise ProjectGuidanceError(f"Project Spec marker {error}") from error
    if marker is None:
        return None

    try:
        spec = marker / "spec.md"
        try:
            spec.lstat()
        except FileNotFoundError:
            return None
        resolved = spec.resolve(strict=True)
        if not resolved.is_relative_to(boundary):
            raise ProjectGuidanceError("Project Spec is outside the Project Boundary")
    except (OSError, RuntimeError) as error:
        raise ProjectGuidanceError(
            f"Project Spec cannot be resolved: {error}",
        ) from error

    if (
        not hasattr(os, "O_DIRECTORY")
        or not hasattr(os, "O_NOFOLLOW")
        or os.open not in os.supports_dir_fd
    ):
        raise ProjectGuidanceError(
            "Project Spec cannot be opened safely on this platform",
        )

    directory = -1
    try:
        directory = os.open(
            boundary,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        )
        parts = resolved.relative_to(boundary).parts
        for part in parts[:-1]:
            next_directory = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=directory,
            )
            os.close(directory)
            directory = next_directory
        descriptor = os.open(
            parts[-1],
            os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW,
            dir_fd=directory,
        )
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ProjectGuidanceError("Project Spec is not a regular file")
            return os.fdopen(descriptor, "rb")
        except Exception:
            os.close(descriptor)
            raise
    except OSError as error:
        raise ProjectGuidanceError(
            f"Project Spec cannot be opened: {error}",
        ) from error
    finally:
        if directory != -1:
            os.close(directory)
