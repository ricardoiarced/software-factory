"""Entry point for the Factory command."""

import codecs
import json
import os
import stat
import sys
import tempfile
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import BinaryIO

from _harness import (
    DEFAULT_HARNESS,
    HARNESS_NAMES,
    RunAdmissionError,
    UnknownHarnessError,
    select_harness,
)
from _project import (
    GitInspectionError,
    Project,
    ProjectSelectionError,
    StaleProjectError,
    discover_project,
    expand_path,
    inspect_registered_project,
    select_explicit_project,
)
from _project_guidance import ProjectGuidanceError, open_project_spec
from _project_registry import (
    RegistryCommitUncertain,
    RegistryError,
    add_project,
    read_projects,
    remove_project,
    validate_project_name,
)

ROOT_SYNOPSIS = """Usage:
  factory [--harness codex|claude] [--project NAME | --path PATH] [--file PATH]... [--force] [--] INSTRUCTION...
  factory project COMMAND ...
  factory (-h | --help)
  factory --version
"""

ROOT_HELP = ROOT_SYNOPSIS + """
Run one Instruction in a selected Git-backed Project.

Options:
  --harness NAME  Select the Harness for the Run: codex (the default) or
                  claude.
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

The selected Harness must pass its own latest-release and Run Admission
checks before the Run Request is submitted. An unavailable or uncertified
Harness fails closed; Factory never falls back to the other Harness.

Context Sources are read after the Instruction: each --file in flag order,
then non-terminal standard input (pipe or redirection). Empty sources add no
Context. --force only bypasses the Context size warning; it never bypasses
Run Admission or Project Data Isolation.

Only project as the first raw argument enters Project administration. Use
factory -- project ... to start an Instruction with the word project.

Examples:
  factory Fix the failing tests
  factory --harness claude Fix the failing tests
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


class RunInputError(Exception):
    """A Project Spec or Context Source cannot be used for this Run."""


@dataclass(frozen=True)
class RunArguments:
    instruction: str
    harness: str
    selector: tuple[str, str] | None
    files: tuple[str, ...]
    force: bool


def _quoted(value: str) -> str:
    return json.dumps(value, ensure_ascii=True)


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
                try:
                    target.write(chunk)
                except (OSError, ValueError) as error:
                    raise RunInputError(f"Run Request spool failed: {error}") from error
                size += len(chunk)
                trailing_lfs = _terminal_lfs(chunk, trailing_lfs)
            chunk = source.read(65536)
            if not chunk:
                break
        decoder.decode(b"", final=True)
    except UnicodeError as error:
        raise RunInputError(f"{label} is not valid UTF-8") from error
    except (OSError, ValueError) as error:
        raise RunInputError(f"{label} could not be read: {error}") from error
    return size, trailing_lfs


def _terminal_lfs(chunk: bytes, previous: int = 0) -> int:
    last_non_newline = len(chunk.rstrip(b"\r\n"))
    return min(2, chunk[last_non_newline:].count(b"\n") +
               (previous if last_non_newline == 0 else 0))


def _separator(spool: BinaryIO, trailing_lfs: int) -> None:
    spool.write(b"\n" * (2 - trailing_lfs))


def _open_regular(path: Path, label: str) -> BinaryIO:
    descriptor = -1
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise RunInputError(f"{label} is not a regular file")
        source = os.fdopen(descriptor, "rb")
        descriptor = -1
        return source
    except OSError as error:
        raise RunInputError(f"{label} cannot be opened: {error}") from error
    finally:
        if descriptor != -1:
            os.close(descriptor)


def _context_path(supplied: str, invocation_dir: Path) -> Path:
    try:
        expanded_text = expand_path(supplied)
        if "\x00" in expanded_text:
            raise RunInputError(f"Context file {_quoted(supplied)} path contains NUL")
        expanded = Path(expanded_text)
        return expanded if expanded.is_absolute() else invocation_dir / expanded
    except ProjectSelectionError as error:
        raise RunInputError(str(error)) from error


def _compose_run(run: RunArguments, boundary: Path, invocation_dir: Path,
                 spool: BinaryIO) -> int:
    try:
        project_spec = open_project_spec(boundary)
    except ProjectGuidanceError as error:
        raise RunInputError(str(error)) from error
    trailing_lfs = 2
    if project_spec is not None:
        with project_spec as source:
            spool.write(b"# Project Guidance\n\n")
            _, trailing_lfs = _copy_utf8(source, spool, "Project Spec")
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
    try:
        terminal_input = sys.stdin.isatty()
    except (OSError, ValueError) as error:
        raise RunInputError(f"standard input could not be read: {error}") from error
    if not terminal_input:
        size, trailing_lfs, context_started = _append_context(
            sys.stdin.buffer, spool, b"### Standard Input\n\n", "standard input",
            trailing_lfs, context_started)
        context_bytes += size
    return context_bytes


def _append_context(source: BinaryIO, spool: BinaryIO, heading: bytes, label: str,
                    trailing_lfs: int, started: bool) -> tuple[int, int, bool]:
    checkpoint = spool.tell()
    _separator(spool, trailing_lfs)
    if not started:
        spool.write(b"## Context\n\n")
    spool.write(heading)
    size, payload_lfs = _copy_utf8(source, spool, label)
    if not size:
        spool.seek(checkpoint)
        spool.truncate()
        return 0, trailing_lfs, started
    return size, payload_lfs, True


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
    seen_harness = False
    selector: tuple[str, str] | None = None
    harness = DEFAULT_HARNESS
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
        if option in ("--project", "--path", "--file", "--harness"):
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
            elif option == "--harness":
                if seen_harness:
                    raise UsageError("--harness may appear only once")
                seen_harness = True
                if value not in HARNESS_NAMES:
                    raise UsageError(f"unknown harness {_quoted(value)}")
                harness = value
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
    return RunArguments(instruction, harness, selector, tuple(files), seen_force)


def _parse_project(args: list[str]) -> tuple[str, list[str]]:
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
    return command, values


def _add_project(name: str, supplied_path: str) -> None:
    validate_project_name(name)
    project = select_explicit_project(supplied_path, Path.cwd())
    add_project(name, project)


def _registered_project(path: str) -> Project:
    try:
        return inspect_registered_project(path)
    except (OSError, RuntimeError) as error:
        raise RegistryError(f"Project validation failed: {error}") from error
    except GitInspectionError as error:
        raise RegistryError(str(error)) from error


def _project_status(path: str) -> str:
    try:
        _registered_project(path)
    except StaleProjectError as error:
        return str(error)
    return "valid"


def _select_registered_project(name: str) -> Project:
    validate_project_name(name)
    projects = read_projects()
    if name not in projects:
        raise RegistryError(f"Project {_quoted(name)} is not registered")
    path = projects[name]
    try:
        return _registered_project(path)
    except StaleProjectError as error:
        raise RegistryError(f"Project {_quoted(name)} is {error}") from error


def _list_projects() -> None:
    projects = read_projects()
    if not projects:
        output = "No projects registered.\n"
    else:
        lines = ["NAME\tSTATUS\tPATH"]
        for name in sorted(projects):
            path = projects[name]
            lines.append(
                f"{json.dumps(name, ensure_ascii=False)}\t{_project_status(path)}\t"
                f"{json.dumps(path, ensure_ascii=False)}"
            )
        output = "\n".join(lines) + "\n"
    sys.stdout.write(output)


def _show_project(name: str) -> None:
    projects = read_projects()
    if name not in projects:
        raise RegistryError(f"Project {_quoted(name)} is not registered")
    path = projects[name]
    status = _project_status(path)
    sys.stdout.write(
        f"Name: {json.dumps(name, ensure_ascii=False)}\n"
        f"Path: {json.dumps(path, ensure_ascii=False)}\n"
        f"Status: {status}\n"
    )


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
            command, values = _parse_project(sys.argv[2:])
            if command == "list":
                _list_projects()
                return 0
            if command == "show":
                _show_project(values[0])
                return 0
            if command == "add":
                _add_project(values[0], values[1])
                return 0
            if command == "remove":
                remove_project(values[0])
                return 0
            sys.stderr.write("factory: error: Project administration is not yet available.\n")
        else:
            run = _parse_run(sys.argv[1:])
            invocation_dir = Path.cwd()
            if run.selector is None:
                project = discover_project(invocation_dir)
            elif run.selector[0] == "path":
                project = select_explicit_project(run.selector[1], invocation_dir)
            else:
                project = _select_registered_project(run.selector[1])
            try:
                spool = tempfile.TemporaryFile(mode="w+b")  # noqa: SIM115
            except OSError as error:
                raise RunInputError(f"Run Request spool failed: {error}") from error
            with spool:
                try:
                    context_bytes = _compose_run(
                        run, project.boundary, invocation_dir, spool,
                    )
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
                return select_harness(run.harness).run(project, spool)
        return 1
    except UsageError as error:
        return _usage(error)
    except ProjectSelectionError as error:
        sys.stderr.write(f"factory: error: {error}.\n")
        return 1
    except RegistryCommitUncertain as error:
        sys.stderr.write(f"factory: warning: {error}.\n")
        return 1
    except UnknownHarnessError as error:
        return _usage(UsageError(str(error)))
    except (RunInputError, RunAdmissionError, RegistryError) as error:
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
