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
from typing import BinaryIO, NoReturn

from _harness import (
    DEFAULT_HARNESS,
    HARNESS_NAMES,
    RunAdmissionError,
    UnknownHarnessError,
    select_harness,
)
from _identity import (
    RunSelection,
    list_clients,
    list_projects,
    migrate,
    re_register_checkout,
    re_register_repository,
    register_checkout,
    register_client,
    register_project,
    remove_client,
    remove_project_entry,
    remove_stored_checkout,
    resolve_run_checkout,
    resolve_run_discovery,
    resolve_run_project,
    set_client_key,
    set_client_name,
    set_default_checkout,
    set_project_key,
    set_project_name,
    show_project,
)
from _project import Project, ProjectSelectionError, expand_path
from _project_guidance import ProjectGuidanceError, open_project_spec
from _project_registry import RegistryCommitUncertain, RegistryError
from _rungate import RunGateError, run_gate
from _runrecord import RunRecord, RunRecordError, run_record

ROOT_SYNOPSIS = """Usage:
  factory [--harness codex|claude] [--project NAME] [--checkout PATH] [--file PATH]... [--force] [--] INSTRUCTION...
  factory project COMMAND ...
  factory (-h | --help)
  factory --version
"""

ROOT_HELP = (
    ROOT_SYNOPSIS
    + """
Run one Instruction in a selected Git-backed Project.

Options:
  --harness NAME   Select the Harness for the Run: codex (the default) or
                   claude.
  --project NAME   Select a Project by its registered name.
  --checkout PATH  Select a registered Checkout by its boundary path;
                   combine with --project to require that Checkout to belong
                   to it.
  --file PATH      Add a file as a Context Source; repeat to add more.
  --force          Continue when Context exceeds the warning threshold.
  -h, --help       Show this help.
  --version        Show the installed Factory version.

With no selector, automatic discovery starts at the invocation directory and
resolves the nearest enclosing Git worktree root to a registered Checkout.
Registration is what makes a directory Run-admissible. Explicit selectors
never fall back.

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
  factory --checkout ./services/api Update dependencies
  factory -- project list
  factory project show api
  factory project remove api
"""
)

PROJECT_SYNOPSIS = """Usage:
  factory project register [--name NAME] [--from-legacy NAME] [--] SELECTOR PATH
  factory project list
  factory project show [--legacy] [--] SELECTOR
  factory project remove [--legacy] [--] NAME
  factory project migrate
  factory project set-default-checkout [--] SELECTOR PATH
  factory project set-key [--] SELECTOR NEW_KEY
  factory project set-name [--] SELECTOR [NAME]
  factory project client register [--name NAME] [--] KEY
  factory project client list
  factory project client set-key [--] KEY NEW_KEY
  factory project client set-name [--] KEY [NAME]
  factory project client remove [--] KEY
  factory project checkout register [--repository ID] [--] SELECTOR PATH
  factory project checkout re-register [--] CHECKOUT_ID PATH
  factory project checkout remove [--] CHECKOUT_ID
  factory project repository re-register [--] REPOSITORY_ID PATH
  factory project (-h | --help)
"""

PROJECT_HELP = (
    PROJECT_SYNOPSIS
    + """
Manage the local Project Registry. Project Names are case-sensitive. A
registered path must identify a directory inside a non-bare Git worktree with
a readable commit HEAD.

Client commands manage the Clients that own Projects. register creates a
Client under a globally unique key; set-key and set-name repair one later.

Project commands manage the Projects themselves. register records one Project
under an existing Client together with its first Repository and Checkout.
show, set-key, set-name, and set-default-checkout repair one registered
Project; remove removes one only after its Checkouts are gone.

Checkout commands register additional worktrees of one Project, re-register a
moved worktree in place, or remove one. repository re-register re-pins a
retained Repository common directory from fresh evidence.

migrate converts a version-one registry into the version-two identity store.
Administration refuses a version-one registry with a migrate-first error, and
list, show, and remove keep serving version-one registries until then. The
--legacy form of show and remove addresses pre-migration entries after the
migration; register --from-legacy consumes one legacy entry when it registers
the same path as a real Project.

Value options accept --option VALUE or --option=VALUE and may appear once
each; --legacy is a flag. Use -- before a value that resembles an option.
Only exact lowercase project as the first argument enters this namespace.
"""
)


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
    project: str | None
    checkout: str | None
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
    return min(
        2,
        chunk[last_non_newline:].count(b"\n")
        + (previous if last_non_newline == 0 else 0),
    )


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


def _compose_run(
    run: RunArguments, boundary: Path, invocation_dir: Path, spool: BinaryIO
) -> int:
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
    context = _ContextComposition(spool, _terminal_lfs(instruction), False)
    context_bytes = 0
    for supplied in run.files:
        label = f"Context file {_quoted(supplied)}"
        heading = b"### File: " + _quoted(supplied).encode("utf-8") + b"\n\n"
        with _open_regular(_context_path(supplied, invocation_dir), label) as source:
            context_bytes += context.append(source, heading, label)
    try:
        terminal_input = sys.stdin.isatty()
    except (OSError, ValueError) as error:
        raise RunInputError(f"standard input could not be read: {error}") from error
    if not terminal_input:
        context_bytes += context.append(
            sys.stdin.buffer,
            b"### Standard Input\n\n",
            "standard input",
        )
    return context_bytes


@dataclass
class _ContextComposition:
    """Composed Context accumulating in one Run Request spool.

    Tracks the spool's separator state so every appended Context Source
    begins two fresh lines after the previous non-empty one.
    """

    spool: BinaryIO
    trailing_lfs: int
    started: bool

    def append(self, source: BinaryIO, heading: bytes, label: str) -> int:
        checkpoint = self.spool.tell()
        _separator(self.spool, self.trailing_lfs)
        if not self.started:
            self.spool.write(b"## Context\n\n")
        self.spool.write(heading)
        size, payload_lfs = _copy_utf8(source, self.spool, label)
        if not size:
            self.spool.seek(checkpoint)
            self.spool.truncate()
            return 0
        self.started = True
        self.trailing_lfs = payload_lfs
        return size


def _context_threshold() -> int:
    supplied = os.environ.get("FACTORY_CONTEXT_WARN_BYTES")
    if supplied is None:
        return 1_048_576
    if (
        not supplied
        or not supplied.isascii()
        or not all("0" <= char <= "9" for char in supplied)
    ):
        raise RunInputError(
            "FACTORY_CONTEXT_WARN_BYTES must be an unsigned ASCII decimal integer"
        )
    threshold = 0
    for char in supplied:
        threshold = threshold * 10 + ord(char) - ord("0")
    return threshold


def _option_value(
    args: list[str], position: int, argument: str
) -> tuple[str, str, int]:
    """Return one valued option's name and value, and the position past it."""
    option, equals, inline_value = argument.partition("=")
    if equals:
        value = inline_value
    else:
        position += 1
        value = args[position] if position < len(args) else ""
        if value.startswith("-"):
            raise UsageError(
                f"{option} requires a value; use {option}=VALUE for values beginning with -"
            )
    if not value:
        raise UsageError(f"{option} requires a non-empty value")
    return option, value, position


def _check_harness_name(value: str) -> None:
    """Accept one Harness name only when it is a registered Harness."""
    if value not in HARNESS_NAMES:
        raise UsageError(f"unknown harness {_quoted(value)}")


def _parse_valued_run_option(
    args: list[str],
    position: int,
    argument: str,
    seen: dict[str, str],
    files: list[str],
) -> int:
    """Record one valued Run option; selectors and --harness only once."""
    option, value, position = _option_value(args, position, argument)
    if option == "--file":
        files.append(value)
        return position
    if option in seen:
        raise UsageError(f"{option} may appear only once")
    if option == "--harness":
        _check_harness_name(value)
    seen[option] = value
    return position


def _run_instruction(args: list[str], position: int) -> str:
    """Return the Instruction text that follows the parsed options."""
    instruction = " ".join(args[position:])
    if not args[position:] or not instruction.strip():
        raise UsageError("an Instruction with non-whitespace text is required")
    return instruction


def _parse_run(args: list[str]) -> RunArguments:
    seen: dict[str, str] = {}
    files: list[str] = []
    position = 0
    while position < len(args):
        argument = args[position]
        if argument == "--":
            position += 1
            break
        if not argument.startswith("-") or argument == "-":
            break
        option = argument.partition("=")[0]
        if option == "--path":
            raise UsageError(
                "--path was removed; use --checkout PATH to select a "
                "registered Checkout."
            )
        if option in ("--project", "--checkout", "--file", "--harness"):
            position = _parse_valued_run_option(args, position, argument, seen, files)
        elif argument == "--force":
            if "--force" in seen:
                raise UsageError("--force may appear only once")
            seen["--force"] = ""
        elif argument in ("-h", "--help", "--version"):
            raise UsageError(f"{argument} must be used alone")
        else:
            raise UsageError(f"unknown option {_quoted(argument)}")
        position += 1
    instruction = _run_instruction(args, position)
    return RunArguments(
        instruction,
        seen.get("--harness", DEFAULT_HARNESS),
        seen.get("--project"),
        seen.get("--checkout"),
        tuple(files),
        "--force" in seen,
    )


@dataclass(frozen=True)
class ProjectInvocation:
    """One parsed Project administration command line."""

    route: str
    values: tuple[str, ...]
    options: dict[str, str]


@dataclass(frozen=True)
class _RouteSpec:
    """The grammar of one Project administration route."""

    minimum: int
    maximum: int
    valued: tuple[str, ...] = ()
    flags: tuple[str, ...] = ()


_PROJECT_ROUTES: dict[tuple[str, ...], _RouteSpec] = {
    ("register",): _RouteSpec(2, 2, valued=("--name", "--from-legacy")),
    ("list",): _RouteSpec(0, 0),
    ("show",): _RouteSpec(1, 1, flags=("--legacy",)),
    ("remove",): _RouteSpec(1, 1, flags=("--legacy",)),
    ("migrate",): _RouteSpec(0, 0),
    ("set-default-checkout",): _RouteSpec(2, 2),
    ("set-key",): _RouteSpec(2, 2),
    ("set-name",): _RouteSpec(1, 2),
    ("client", "register"): _RouteSpec(1, 1, valued=("--name",)),
    ("client", "list"): _RouteSpec(0, 0),
    ("client", "set-key"): _RouteSpec(2, 2),
    ("client", "set-name"): _RouteSpec(1, 2),
    ("client", "remove"): _RouteSpec(1, 1),
    ("checkout", "register"): _RouteSpec(2, 2, valued=("--repository",)),
    ("checkout", "re-register"): _RouteSpec(2, 2),
    ("checkout", "remove"): _RouteSpec(1, 1),
    ("repository", "re-register"): _RouteSpec(2, 2),
}


def _match_project_route(
    arguments: list[str],
) -> tuple[tuple[str, ...], _RouteSpec] | None:
    """Return the longest route prefix matching the command line."""
    for width in (2, 1):
        route = tuple(arguments[:width])
        if route in _PROJECT_ROUTES:
            return route, _PROJECT_ROUTES[route]
    return None


def _project_option(
    arguments: list[str], position: int, argument: str
) -> tuple[str, int]:
    """Return one valued Project option's value and the position past it."""
    try:
        _, value, position = _option_value(arguments, position, argument)
    except UsageError as error:
        raise UsageError(str(error), project=True) from error
    return value, position


def _record_project_option(
    spec: _RouteSpec,
    options: dict[str, str],
    arguments: list[str],
    position: int,
    argument: str,
) -> int:
    """Record one Project option and return the position past its value."""
    option = argument.partition("=")[0]
    if option not in spec.valued and (option not in spec.flags or option != argument):
        raise UsageError(f"unknown Project option {_quoted(argument)}", project=True)
    if option in options:
        raise UsageError(f"{option} may appear only once", project=True)
    if option in spec.valued:
        value, position = _project_option(arguments, position, argument)
        options[option] = value
    else:
        options[option] = ""
    return position


def _parse_project_options(
    spec: _RouteSpec, arguments: list[str]
) -> tuple[list[str], dict[str, str]]:
    """Split one command tail into positional values and accepted options."""
    values: list[str] = []
    options: dict[str, str] = {}
    positionals_only = False
    position = 0
    while position < len(arguments):
        argument = arguments[position]
        if positionals_only or not argument.startswith("-") or argument == "-":
            values.append(argument)
        elif argument in ("-h", "--help", "--version"):
            raise UsageError(f"{argument} must be used alone", project=True)
        elif argument == "--":
            positionals_only = True
        elif argument.startswith("--"):
            position = _record_project_option(
                spec, options, arguments, position, argument
            )
        else:
            values.append(argument)
        position += 1
    return values, options


def _parse_project(arguments: list[str]) -> ProjectInvocation:
    """Parse one Project administration command line against the route table."""
    if not arguments:
        raise UsageError("a Project command is required", project=True)
    if arguments[0] == "add":
        raise UsageError(
            "project add was removed; "
            "use 'factory project register' to register Projects",
            project=True,
        )
    matched = _match_project_route(arguments)
    if matched is None:
        raise UsageError(
            f"unknown Project command {_quoted(arguments[0])}", project=True
        )
    route, spec = matched
    values, options = _parse_project_options(spec, list(arguments[len(route) :]))
    if not spec.minimum <= len(values) <= spec.maximum:
        expected = (
            str(spec.minimum)
            if spec.minimum == spec.maximum
            else f"{spec.minimum} or {spec.maximum}"
        )
        raise UsageError(
            f"project {' '.join(route)} expects {expected} argument(s)",
            project=True,
        )
    return ProjectInvocation(" ".join(route), tuple(values), options)


def _usage(error: UsageError) -> int:
    synopsis = PROJECT_SYNOPSIS if error.project else ROOT_SYNOPSIS
    help_command = "factory project --help" if error.project else "factory --help"
    sys.stderr.write(
        f"factory: error: {error}\n{synopsis}Try '{help_command}' for more information.\n"
    )
    return 2


def _route(arguments: list[str]) -> int:
    """Return the exit status for one routed command line."""
    if arguments in (["-h"], ["--help"]):
        sys.stdout.write(ROOT_HELP)
        return 0
    if arguments in (["project", "-h"], ["project", "--help"]):
        sys.stdout.write(PROJECT_HELP)
        return 0
    if arguments == ["--version"]:
        sys.stdout.write(f"factory {version('software-factory')}\n")
        return 0
    if arguments[:1] == ["project"]:
        return _dispatch_project(_parse_project(arguments[1:]))
    return _dispatch_run(_parse_run(arguments))


def _dispatch_client(invocation: ProjectInvocation) -> int:
    """Run one Client administration command to completion."""
    values = invocation.values
    if invocation.route == "client register":
        register_client(values[0], invocation.options.get("--name"))
        return 0
    if invocation.route == "client list":
        list_clients()
        return 0
    if invocation.route == "client set-key":
        set_client_key(values[0], values[1])
        return 0
    if invocation.route == "client set-name":
        set_client_name(values[0], values[1] if len(values) > 1 else None)
        return 0
    remove_client(values[0])
    return 0


def _dispatch_shape(invocation: ProjectInvocation) -> int:
    """Run one Checkout or Repository administration command to completion."""
    values = invocation.values
    if invocation.route == "checkout register":
        register_checkout(values[0], values[1], invocation.options.get("--repository"))
        return 0
    if invocation.route == "checkout re-register":
        re_register_checkout(values[0], values[1])
        return 0
    if invocation.route == "checkout remove":
        remove_stored_checkout(values[0])
        return 0
    re_register_repository(values[0], values[1])
    return 0


def _dispatch_project(invocation: ProjectInvocation) -> int:
    """Run one Project administration command to completion."""
    route = invocation.route
    if route.startswith("client "):
        return _dispatch_client(invocation)
    if route.startswith(("checkout ", "repository ")):
        return _dispatch_shape(invocation)
    values = invocation.values
    if route == "register":
        register_project(
            values[0],
            values[1],
            invocation.options.get("--name"),
            invocation.options.get("--from-legacy"),
        )
        return 0
    if route == "list":
        list_projects()
        return 0
    if route == "show":
        show_project(values[0], legacy="--legacy" in invocation.options)
        return 0
    if route == "remove":
        remove_project_entry(values[0], legacy="--legacy" in invocation.options)
        return 0
    if route == "migrate":
        migrate()
        return 0
    if route == "set-default-checkout":
        set_default_checkout(values[0], values[1])
        return 0
    if route == "set-key":
        set_project_key(values[0], values[1])
        return 0
    set_project_name(values[0], values[1] if len(values) > 1 else None)
    return 0


def _select_run_project(run: RunArguments, invocation_dir: Path) -> RunSelection:
    """Resolve the Run Request's registered Checkout, never falling back."""
    if run.checkout is not None:
        return resolve_run_checkout(run.checkout, run.project, invocation_dir)
    if run.project is not None:
        return resolve_run_project(run.project)
    return resolve_run_discovery(invocation_dir)


def _admission_facts(error: RunAdmissionError) -> dict[str, str] | None:
    """Return the structured admission facts one admission failure carries."""
    if error.executable is None:
        return None
    return {"executable": str(error.executable)}


def _refuse_spool_failure(record: RunRecord, error: OSError) -> NoReturn:
    """Refuse the composition stage for one failed Run Request spool."""

    record.refuse("composition", f"Run Request spool failed: {error}")
    raise RunInputError(f"Run Request spool failed: {error}") from error


def _dispatch_run(run: RunArguments) -> int:
    """Open the Run Record, then compose, gate, and hand off the Run Request."""
    with run_record(sys.argv[1:], run.harness) as record:
        return _run_request(run, record)


def _run_request(run: RunArguments, record: RunRecord) -> int:
    """Compose the Context, gate its size, and hand the spool to the Harness."""
    try:
        invocation_dir = Path.cwd()
        selection = _select_run_project(run, invocation_dir)
    except (ProjectSelectionError, RegistryError) as error:
        record.refuse("selection", str(error))
        raise
    project = Project(
        boundary=selection.boundary,
        enclosing_git_root=selection.boundary,
    )
    record.selected(selection)
    try:
        spool = tempfile.TemporaryFile(mode="w+b")  # noqa: SIM115
    except OSError as error:
        _refuse_spool_failure(record, error)
    with spool:
        try:
            context_bytes = _compose_run(run, project.boundary, invocation_dir, spool)
        except OSError as error:
            _refuse_spool_failure(record, error)
        except RunInputError as error:
            record.refuse("composition", str(error))
            raise
        try:
            threshold = _context_threshold()
        except RunInputError as error:
            record.refuse("composition", str(error))
            raise
        if context_bytes > threshold:
            message = f"Context is {context_bytes} bytes; the warning threshold is {threshold} bytes."
            if not run.force:
                record.refuse("context-gate", message)
                sys.stderr.write(
                    f"factory: error: {message} Re-run with --force to continue.\n"
                )
                return 1
            sys.stderr.write(
                f"factory: warning: {message} Continuing because --force was supplied.\n"
            )
        try:
            spool.seek(0)
        except OSError as error:
            _refuse_spool_failure(record, error)
        try:
            run_gate(record, selection, run.harness)
        except RunGateError as error:
            sys.stderr.write(f"factory: error: {error}\n")
            return 1
        try:
            return select_harness(run.harness).run(project, spool)
        except RunAdmissionError as error:
            record.refuse("admission", str(error), facts=_admission_facts(error))
            raise


def _dispatch() -> int:
    """Route the command line, mapping every failure to an exit status."""
    try:
        return _route(sys.argv[1:])
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
    except (RunInputError, RunAdmissionError, RegistryError, RunRecordError) as error:
        message = str(error)
        if not message.endswith("."):
            message = f"{message}."
        sys.stderr.write(f"factory: error: {message}\n")
        return 1
    except PackageNotFoundError:
        sys.stderr.write(
            "factory: error: software-factory distribution metadata is unavailable.\n"
        )
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
