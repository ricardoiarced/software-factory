"""Public command-line behavior before a Run or registry operation begins."""

import os
from pathlib import Path
import subprocess
import sys

import pytest

from factory import _parse_run


FACTORY = Path(__file__).resolve().parents[1] / "factory.py"


def invoke(*args: str, columns: str = "80") -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [sys.executable, str(FACTORY), *args],
        capture_output=True,
        env={**os.environ, "COLUMNS": columns},
    )


def test_explicit_root_help_is_stable_stdout() -> None:
    wide = invoke("--help", columns="200")
    narrow = invoke("-h", columns="20")

    assert wide.returncode == narrow.returncode == 0
    assert wide.stdout == narrow.stdout
    assert wide.stderr == narrow.stderr == b""
    assert wide.stdout.startswith(
        b"Usage:\n"
        b"  factory [--project NAME | --path PATH] [--file PATH]... [--force] [--] INSTRUCTION...\n"
        b"  factory project COMMAND ...\n"
        b"  factory (-h | --help)\n"
        b"  factory --version\n"
    )
    for detail in (
        b"automatic discovery",
        b"first non-option",
        b"Context Sources",
        b"--file",
        b"standard input",
        b"--force",
        b"factory -- project list",
        b"git diff | factory Review this patch",
    ):
        assert detail in wide.stdout


def test_explicit_project_help_is_stable_stdout() -> None:
    wide = invoke("project", "--help", columns="200")
    narrow = invoke("project", "-h", columns="20")

    assert wide.returncode == narrow.returncode == 0
    assert wide.stdout == narrow.stdout
    assert wide.stderr == narrow.stderr == b""
    assert wide.stdout.startswith(
        b"Usage:\n"
        b"  factory project add [--] NAME PATH\n"
        b"  factory project list\n"
        b"  factory project show [--] NAME\n"
        b"  factory project remove [--] NAME\n"
    )
    for detail in (b"Project Registry", b"add", b"list", b"show", b"remove"):
        assert detail in wide.stdout


@pytest.mark.parametrize(
    ("args", "hint"),
    [
        ((), b"Try 'factory --help' for more information."),
        (("--project", "api"), b"Try 'factory --help' for more information."),
        (("--path", "./api", "--project", "api", "Fix"), b"Try 'factory --help' for more information."),
        (("--force", "--force", "Fix"), b"Try 'factory --help' for more information."),
        (("--file", "-oops", "Fix"), b"Try 'factory --help' for more information."),
        (("--unknown", "Fix"), b"Try 'factory --help' for more information."),
        (("--help", "Fix"), b"Try 'factory --help' for more information."),
        (("--version", "--help"), b"Try 'factory --help' for more information."),
        (("--", "  "), b"Try 'factory --help' for more information."),
        (("project",), b"Try 'factory project --help' for more information."),
        (("project", "add", "api"), b"Try 'factory project --help' for more information."),
        (("project", "list", "extra"), b"Try 'factory project --help' for more information."),
        (("project", "show", "--help"), b"Try 'factory project --help' for more information."),
        (("project", "--version"), b"Try 'factory project --help' for more information."),
    ],
)
def test_malformed_commands_have_relevant_usage_and_no_traceback(
    args: tuple[str, ...], hint: bytes
) -> None:
    result = invoke(*args)

    assert result.returncode == 2
    assert result.stdout == b""
    assert result.stderr.startswith(b"factory: error: ")
    assert (b"factory project add [--] NAME PATH" if args[:1] == ("project",) else
            b"factory [--project NAME | --path PATH]") in result.stderr
    assert hint in result.stderr
    assert b"Traceback" not in result.stderr


@pytest.mark.parametrize(
    "args",
    [
        ("Fix", "--unknown"),
        ("--", "project", "list"),
        ("--project=api", "Fix"),
        ("--file=-log", "Fix"),
        ("project", "add", "--", "api", "/some/path"),
        ("project", "list"),
    ],
)
def test_valid_grammar_reaches_operational_stage(args: tuple[str, ...]) -> None:
    result = invoke(*args)

    assert result.returncode == 1
    assert result.stdout == b""
    assert result.stderr.startswith(b"factory: error: ")
    assert b"Usage:" not in result.stderr
    assert b"Traceback" not in result.stderr


def test_run_arguments_preserve_instruction_and_context_source_order() -> None:
    parsed = _parse_run([
        "--file", "first.txt", "--project=api", "--file=first.txt",
        "--force", "--", "  Fix  this  ", "--file", "last\tword",
    ])

    assert parsed.instruction == "  Fix  this   --file last\tword"
    assert parsed.selector == ("project", "api")
    assert parsed.files == ("first.txt", "first.txt")
    assert parsed.force is True


def test_run_prefix_stops_at_first_instruction_token() -> None:
    parsed = _parse_run(["--path", "./api", "Fix", "--force", "--file=ignored"])

    assert parsed.instruction == "Fix --force --file=ignored"
    assert parsed.selector == ("path", "./api")
    assert parsed.files == ()
    assert parsed.force is False


def test_run_terminator_allows_reserved_project_as_instruction() -> None:
    parsed = _parse_run(["--", "project", "list"])
    result = invoke("--", "project", "list")

    assert parsed.instruction == "project list"
    assert parsed.selector is None
    assert result.returncode == 1
    assert b"Run execution is not yet available" in result.stderr


@pytest.mark.parametrize(
    ("args", "selector", "files"),
    [
        (("--project", "api", "Fix"), ("project", "api"), ()),
        (("--path=-workspace", "Fix"), ("path", "-workspace"), ()),
        (("--file=-log", "--file", "next.log", "Fix"), None, ("-log", "next.log")),
        (("-", "--force"), None, ()),
    ],
    ids=["separate-selector", "equals-selector", "repeatable-files", "lone-dash-instruction"],
)
def test_run_accepts_documented_value_forms(
    args: tuple[str, ...], selector: tuple[str, str] | None, files: tuple[str, ...]
) -> None:
    parsed = _parse_run(list(args))

    assert parsed.selector == selector
    assert parsed.files == files


@pytest.mark.parametrize(
    ("args", "reason"),
    [
        (("--project", "api", "--project", "api", "Fix"), b"--project may appear only once"),
        (("--path=a", "--path=b", "Fix"), b"--path may appear only once"),
        (("--project=api", "--path=./api", "Fix"), b"cannot be combined"),
        (("--force", "--force", "Fix"), b"--force may appear only once"),
        (("--file", "-log", "Fix"), b"--file requires a value"),
        (("--project", "--", "Fix"), b"--project requires a value"),
        (("--file=", "Fix"), b"--file requires a non-empty value"),
        (("--path",), b"--path requires a non-empty value"),
        (("--force=1", "Fix"), b"unknown option"),
        (("--unknown", "Fix"), b"unknown option"),
        (("--", " \t", "\n"), b"non-whitespace text is required"),
    ],
    ids=[
        "duplicate-project", "duplicate-path", "conflicting-selectors", "duplicate-force",
        "separate-dash-file", "separate-terminator-selector", "empty-equals-file",
        "missing-path", "force-has-no-value", "unknown-option", "blank-instruction",
    ],
)
def test_run_grammar_failures_exit_two(args: tuple[str, ...], reason: bytes) -> None:
    result = invoke(*args)

    assert result.returncode == 2
    assert result.stdout == b""
    assert reason in result.stderr
    assert b"Try 'factory --help' for more information." in result.stderr
    assert b"Traceback" not in result.stderr


def test_only_exact_first_project_enters_administration() -> None:
    administration = invoke("project", "list")
    run = invoke("Project", "list")
    escaped = invoke("--", "project", "list")

    assert administration.returncode == run.returncode == escaped.returncode == 1
    assert b"Project administration is not yet available" in administration.stderr
    assert b"Run execution is not yet available" in run.stderr
    assert b"Run execution is not yet available" in escaped.stderr
