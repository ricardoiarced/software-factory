"""Entry point for the Factory command."""

from importlib.metadata import PackageNotFoundError, version
from dataclasses import dataclass
import codecs
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import BinaryIO

from _codex import RunAdmissionError, run as _codex_run


ROOT_SYNOPSIS = """Usage:
  factory [--project NAME | --path PATH] [--file PATH]... [--force] [--] INSTRUCTION...
  factory project COMMAND ...
  factory (-h | --help)
  factory --version
"""

ROOT_HELP = ROOT_SYNOPSIS + """
Run one Instruction in a selected Git-backed Project.

Options:
  --project NAME  Select a Project by its registered name.
  --path PATH     Select an explicit Project Boundary.
  --file PATH     Add a file as a Context Source; repeat to add more.
  --force         Continue when Context exceeds the warning threshold.
  -h, --help      Show this help.
  --version       Show the installed Factory version.

With no selector, automatic discovery starts at the invocation directory. It
uses the nearest .agent/ Project Marker within the containing Git worktree, or
the worktree root when no marker exists. Explicit selectors never fall back.

Run options are parsed left to right until the first non-option or --. Every
following argument is Instruction text, even if it resembles an option.
Value options accept --option VALUE or --option=VALUE. Use the equals form
when a value starts with -. Only -h has a short form.

Context Sources are read after the Instruction: each --file in flag order,
then non-terminal standard input (pipe or redirection). Empty sources add no
Context. --force only bypasses the Context size warning; it never bypasses
Run Admission or Project Data Isolation.

Only project as the first raw argument enters Project administration. Use
factory -- project ... to start an Instruction with the word project.

Examples:
  factory Fix the failing tests
  factory --project api --file error.log -- Diagnose this failure
  git diff | factory Review this patch
  factory --path ./services/api Update dependencies
  factory -- project list
  factory project add api ~/work/api
  factory project show api
  factory project remove api
"""

PROJECT_SYNOPSIS = """Usage:
  factory project add [--] NAME PATH
  factory project list
  factory project show [--] NAME
  factory project remove [--] NAME
  factory project (-h | --help)
"""

PROJECT_HELP = PROJECT_SYNOPSIS + """
Manage names in the local Project Registry. Project Names are case-sensitive.
An added path must identify a directory inside a non-bare Git worktree.

Commands:
  add NAME PATH  Register or update a Project Boundary.
  list           List registered Projects and their current status.
  show NAME      Show one Project and its current status.
  remove NAME    Remove one registered Project.

Use -- before NAME when the name resembles an option. Only exact lowercase
project as the first argument enters this namespace.
"""


class UsageError(Exception):
    """A malformed command line and its relevant namespace."""

    def __init__(self, reason: str, *, project: bool = False) -> None:
        super().__init__(reason)
        self.project = project


class ProjectSelectionError(Exception):
    """An explicit Project Boundary could not be selected."""


class RunInputError(Exception):
    """A Project Spec or Context Source cannot be used for this Run."""


@dataclass(frozen=True)
class RunArguments:
    instruction: str
    selector: tuple[str, str] | None
    files: tuple[str, ...]
    force: bool


def _quoted(value: str) -> str:
    return json.dumps(value, ensure_ascii=True)


_ENV_REFERENCE = re.compile(r"\$(\w+|\{[^}]*\})")


def _expand_path(supplied: str) -> str:
    def expand_variable(match: re.Match[str]) -> str:
        name = match.group(1).strip("{}")
        value = os.environ.get(name)
        if not value:
            raise ProjectSelectionError(f"environment variable {_quoted(name)} is unset or empty")
        return value

    expanded = _ENV_REFERENCE.sub(expand_variable, supplied)
    expanded_home = os.path.expanduser(expanded)
    if expanded_home.startswith("~"):
        raise ProjectSelectionError(f"home directory in {_quoted(supplied)} could not be expanded")
    return expanded_home


def _resolve_explicit_path(supplied: str, invocation_dir: Path) -> Path:
    """Resolve and validate an exact explicit Project Boundary."""

    expanded_home = _expand_path(supplied)
    try:
        path = Path(expanded_home)
        if not path.is_absolute():
            path = invocation_dir / path
        path = path.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        cause = error.strerror if isinstance(error, OSError) and error.strerror else "symlink loop"
        raise ProjectSelectionError(
            f"path {_quoted(supplied)} cannot be resolved: {cause}"
        ) from error
    if not path.is_dir():
        raise ProjectSelectionError(f"path {_quoted(supplied)} is not a directory")

    _containing_worktree_root(path, f"path {_quoted(supplied)}")
    return path


def _containing_worktree_root(path: Path, label: str) -> Path:
    """Ask Git for the canonical root containing a directory in a worktree."""

    try:
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--is-inside-work-tree",
             "--is-bare-repository", "--show-toplevel"],
            capture_output=True, check=False,
        )
    except OSError as error:
        raise ProjectSelectionError(f"Git inspection failed for {label}: {error}") from error
    lines = result.stdout.split(b"\n", 2)
    if result.returncode or len(lines) != 3 or lines[0] != b"true" or lines[1] != b"false":
        raise ProjectSelectionError(f"{label} is not inside a non-bare Git worktree")
    try:
        top_level = Path(os.fsdecode(lines[2].removesuffix(b"\n"))).resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise ProjectSelectionError(
            f"Git inspection failed for {label}: invalid worktree path"
        ) from error
    if not path.is_relative_to(top_level):
        raise ProjectSelectionError(f"Git inspection failed for {label}: worktree does not contain path")
    return top_level


def _project_spec(boundary: Path) -> Path | None:
    marker = boundary / ".agent"
    try:
        marker.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise RunInputError(f"Project Spec marker cannot be inspected: {error}") from error
    try:
        resolved_marker = marker.resolve(strict=True)
        if not resolved_marker.is_relative_to(boundary) or not resolved_marker.is_dir():
            raise RunInputError("Project Spec marker is not a contained directory")
        spec = marker / "spec.md"
        try:
            spec.lstat()
        except FileNotFoundError:
            return None
        resolved = spec.resolve(strict=True)
        if not resolved.is_relative_to(boundary):
            raise RunInputError("Project Spec is outside the Project Boundary")
        return resolved
    except (OSError, RuntimeError) as error:
        raise RunInputError(f"Project Spec cannot be resolved: {error}") from error


def _copy_utf8(source: BinaryIO, target: BinaryIO, label: str) -> tuple[int, int]:
    """Copy one source, validating bytes and counting terminal line feeds."""
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    size = 0
    trailing_lfs = 0
    try:
        prefix = b""
        while len(prefix) < 3:
            part = source.read(3 - len(prefix))
            if not part:
                break
            prefix += part
        chunk = prefix.removeprefix(codecs.BOM_UTF8)
        while True:
            if chunk:
                if b"\x00" in chunk:
                    raise RunInputError(f"{label} contains NUL")
                decoder.decode(chunk)
                target.write(chunk)
                size += len(chunk)
                trailing_lfs = _terminal_lfs(chunk, trailing_lfs)
            chunk = source.read(65536)
            if not chunk:
                break
        decoder.decode(b"", final=True)
    except UnicodeError as error:
        raise RunInputError(f"{label} is not valid UTF-8") from error
    except OSError as error:
        raise RunInputError(f"{label} could not be read: {error}") from error
    return size, trailing_lfs


def _terminal_lfs(chunk: bytes, previous: int = 0) -> int:
    last_non_newline = len(chunk.rstrip(b"\r\n"))
    return min(2, chunk[last_non_newline:].count(b"\n") +
               (previous if last_non_newline == 0 else 0))


def _separator(spool: BinaryIO, trailing_lfs: int) -> None:
    spool.write(b"\n" * (2 - trailing_lfs))


def _read_source(source: BinaryIO, spool: BinaryIO, heading: bytes | None,
                 label: str, trailing_lfs: int) -> tuple[int, int]:
    with tempfile.TemporaryFile(mode="w+b") as staged:
        size, payload_lfs = _copy_utf8(source, staged, label)
        if not size and heading is not None:
            return 0, trailing_lfs
        if heading is not None:
            _separator(spool, trailing_lfs)
            spool.write(heading)
        staged.seek(0)
        shutil.copyfileobj(staged, spool, 65536)
        return size, payload_lfs if size else 2


def _open_regular(path: Path, label: str) -> BinaryIO:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            os.close(descriptor)
            raise RunInputError(f"{label} is not a regular file")
        return os.fdopen(descriptor, "rb")
    except OSError as error:
        raise RunInputError(f"{label} cannot be opened: {error}") from error


def _context_path(supplied: str, invocation_dir: Path) -> Path:
    try:
        expanded = Path(_expand_path(supplied))
        return expanded if expanded.is_absolute() else invocation_dir / expanded
    except ProjectSelectionError as error:
        raise RunInputError(str(error)) from error


def _compose_run(run: RunArguments, boundary: Path, invocation_dir: Path,
                 spool: BinaryIO) -> int:
    spec = _project_spec(boundary)
    trailing_lfs = 2
    if spec is not None:
        spool.write(b"# Project Guidance\n\n")
        with _open_regular(spec, "Project Spec") as source:
            _, trailing_lfs = _read_source(source, spool, None, "Project Spec", 2)
        _separator(spool, trailing_lfs)
    spool.write(b"# Run Request\n\n## Instruction\n\n")
    try:
        instruction = run.instruction.encode("utf-8", errors="strict")
    except UnicodeError as error:
        raise RunInputError("Instruction is not valid UTF-8") from error
    spool.write(instruction)
    trailing_lfs = _terminal_lfs(instruction)
    context_bytes = 0
    context_started = False
    for supplied in run.files:
        label = f"Context file {_quoted(supplied)}"
        heading = b"### File: " + _quoted(supplied).encode("utf-8") + b"\n\n"
        with _open_regular(_context_path(supplied, invocation_dir), label) as source:
            size, trailing_lfs, context_started = _append_context(
                source, spool, heading, label, trailing_lfs, context_started)
            context_bytes += size
    if not sys.stdin.isatty():
        size, trailing_lfs, context_started = _append_context(
            sys.stdin.buffer, spool, b"### Standard Input\n\n", "standard input",
            trailing_lfs, context_started)
        context_bytes += size
    return context_bytes


def _append_context(source: BinaryIO, spool: BinaryIO, heading: bytes, label: str,
                    trailing_lfs: int, started: bool) -> tuple[int, int, bool]:
    with tempfile.TemporaryFile(mode="w+b") as staged:
        size, payload_lfs = _copy_utf8(source, staged, label)
        if not size:
            return 0, trailing_lfs, started
        if not started:
            _separator(spool, trailing_lfs)
            spool.write(b"## Context\n\n")
            started = True
        else:
            _separator(spool, trailing_lfs)
        spool.write(heading)
        staged.seek(0)
        shutil.copyfileobj(staged, spool, 65536)
        return size, payload_lfs, started


def _context_threshold() -> int:
    supplied = os.environ.get("FACTORY_CONTEXT_WARN_BYTES")
    if supplied is None:
        return 1_048_576
    if not supplied or not supplied.isascii() or not all("0" <= char <= "9" for char in supplied):
        raise RunInputError("FACTORY_CONTEXT_WARN_BYTES must be an unsigned ASCII decimal integer")
    threshold = 0
    for char in supplied:
        threshold = threshold * 10 + ord(char) - ord("0")
    return threshold


def _parse_run(args: list[str]) -> RunArguments:
    selectors: set[str] = set()
    seen_force = False
    selector: tuple[str, str] | None = None
    files: list[str] = []
    position = 0
    while position < len(args):
        argument = args[position]
        if argument == "--":
            position += 1
            break
        if not argument.startswith("-") or argument == "-":
            break
        option, has_equals, inline_value = argument.partition("=")
        if option in ("--project", "--path", "--file"):
            if has_equals:
                value = inline_value
            else:
                position += 1
                value = args[position] if position < len(args) else ""
                if value.startswith("-"):
                    raise UsageError(f"{option} requires a value; use {option}=VALUE for values beginning with -")
            if not value:
                raise UsageError(f"{option} requires a non-empty value")
            if option in ("--project", "--path"):
                if option in selectors:
                    raise UsageError(f"{option} may appear only once")
                selectors.add(option)
                if len(selectors) > 1:
                    raise UsageError("--project and --path cannot be combined")
                selector = (option[2:], value)
            else:
                files.append(value)
        elif argument == "--force":
            if seen_force:
                raise UsageError("--force may appear only once")
            seen_force = True
        elif argument in ("-h", "--help", "--version"):
            raise UsageError(f"{argument} must be used alone")
        else:
            raise UsageError(f"unknown option {_quoted(argument)}")
        position += 1
    instruction = " ".join(args[position:])
    if not args[position:] or not instruction.strip():
        raise UsageError("an Instruction with non-whitespace text is required")
    return RunArguments(instruction, selector, tuple(files), seen_force)


def _parse_project(args: list[str]) -> None:
    if not args:
        raise UsageError("a Project command is required", project=True)
    command, *values = args
    arities = {"add": 2, "list": 0, "show": 1, "remove": 1}
    if command not in arities:
        raise UsageError(f"unknown Project command {_quoted(command)}", project=True)
    if values[:1] == ["--"] and command != "list":
        values = values[1:]
    elif any(value.startswith("-") for value in values):
        raise UsageError("Project options are not accepted here", project=True)
    if len(values) != arities[command]:
        raise UsageError(f"project {command} expects {arities[command]} argument(s)", project=True)


def _usage(error: UsageError) -> int:
    synopsis = PROJECT_SYNOPSIS if error.project else ROOT_SYNOPSIS
    help_command = "factory project --help" if error.project else "factory --help"
    sys.stderr.write(f"factory: error: {error}\n{synopsis}Try '{help_command}' for more information.\n")
    return 2


def _dispatch() -> int:
    try:
        if sys.argv[1:] in (["-h"], ["--help"]):
            sys.stdout.write(ROOT_HELP)
            return 0
        if sys.argv[1:] in (["project", "-h"], ["project", "--help"]):
            sys.stdout.write(PROJECT_HELP)
            return 0
        if sys.argv[1:] == ["--version"]:
            sys.stdout.write(f"factory {version('software-factory')}\n")
            return 0
        if sys.argv[1:2] == ["project"]:
            _parse_project(sys.argv[2:])
            sys.stderr.write("factory: error: Project administration is not yet available.\n")
        else:
            run = _parse_run(sys.argv[1:])
            if run.selector is None or run.selector[0] == "path":
                invocation_dir = Path.cwd()
                boundary = (
                    _containing_worktree_root(invocation_dir, "invocation directory")
                    if run.selector is None else
                    _resolve_explicit_path(run.selector[1], invocation_dir)
                )
                try:
                    spool = tempfile.TemporaryFile(mode="w+b")
                except OSError as error:
                    raise RunInputError(f"Run Request spool failed: {error}") from error
                with spool:
                    try:
                        context_bytes = _compose_run(run, boundary, invocation_dir, spool)
                    except OSError as error:
                        raise RunInputError(f"Run Request spool failed: {error}") from error
                    threshold = _context_threshold()
                    if context_bytes > threshold:
                        message = f"Context is {context_bytes} bytes; the warning threshold is {threshold} bytes."
                        if not run.force:
                            sys.stderr.write(f"factory: error: {message} Re-run with --force to continue.\n")
                            return 1
                        sys.stderr.write(f"factory: warning: {message} Continuing because --force was supplied.\n")
                    try:
                        spool.seek(0)
                    except OSError as error:
                        raise RunInputError(f"Run Request spool failed: {error}") from error
                    return _codex_run(boundary, spool)
            sys.stderr.write("factory: error: Run execution is not yet available.\n")
        return 1
    except UsageError as error:
        return _usage(error)
    except ProjectSelectionError as error:
        sys.stderr.write(f"factory: error: {error}.\n")
        return 1
    except (RunInputError, RunAdmissionError) as error:
        sys.stderr.write(f"factory: error: {error}.\n")
        return 1
    except PackageNotFoundError:
        sys.stderr.write("factory: error: software-factory distribution metadata is unavailable.\n")
        return 1


def main() -> int:
    """Handle the Factory command line without exposing Python exceptions."""
    try:
        return _dispatch()
    except (OSError, UnicodeError):
        try:
            sys.stderr.write("factory: error: output failed.\n")
        except (OSError, UnicodeError):
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
