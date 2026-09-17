"""Entry point for the Factory command."""

from importlib.metadata import PackageNotFoundError, version
from dataclasses import dataclass
import json
import sys


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


@dataclass(frozen=True)
class RunArguments:
    instruction: str
    selector: tuple[str, str] | None
    files: tuple[str, ...]
    force: bool


def _quoted(value: str) -> str:
    return json.dumps(value, ensure_ascii=True)


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
            _parse_run(sys.argv[1:])
            sys.stderr.write("factory: error: Run execution is not yet available.\n")
        return 1
    except UsageError as error:
        return _usage(error)
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
