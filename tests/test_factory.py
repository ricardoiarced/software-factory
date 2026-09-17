"""Public command-line behavior before a Run or registry operation begins."""

import os
from pathlib import Path
import subprocess
import sys

import pytest

from factory import ProjectSelectionError, _parse_run, _resolve_explicit_path


FACTORY = Path(__file__).resolve().parents[1] / "factory.py"


def invoke(
    *args: str, columns: str = "80", cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [sys.executable, str(FACTORY), *args],
        capture_output=True,
        cwd=cwd,
        env={**os.environ, "COLUMNS": columns, **(env or {})},
    )


def git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def test_explicit_path_preserves_canonical_subdirectory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    git("init", "-q", cwd=tmp_path)
    boundary = tmp_path / "service"
    boundary.mkdir()
    (tmp_path / "link").symlink_to(boundary, target_is_directory=True)
    monkeypatch.setenv("FACTORY_TEST_BOUNDARY", "link")
    monkeypatch.setenv("HOME", str(tmp_path))

    assert _resolve_explicit_path("$FACTORY_TEST_BOUNDARY", tmp_path) == boundary
    assert _resolve_explicit_path("${FACTORY_TEST_BOUNDARY}", tmp_path) == boundary
    assert _resolve_explicit_path("~/service", tmp_path) == boundary


@pytest.mark.parametrize("value", ["missing", "empty"])
def test_explicit_path_rejects_unset_or_empty_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str,
) -> None:
    git("init", "-q", cwd=tmp_path)
    if value == "missing":
        monkeypatch.delenv("FACTORY_TEST_MISSING", raising=False)
        supplied = "${FACTORY_TEST_MISSING}"
    else:
        monkeypatch.setenv("FACTORY_TEST_EMPTY", "")
        supplied = "$FACTORY_TEST_EMPTY"

    result = invoke("--path", supplied, "Fix", cwd=tmp_path)

    assert result.returncode == 1
    assert result.stdout == b""
    assert b"environment variable" in result.stderr
    assert b"Run execution is not yet available" not in result.stderr
    assert b"Usage:" not in result.stderr


@pytest.mark.parametrize("kind", ["missing", "file", "dangling", "outside-git"])
def test_explicit_path_refuses_invalid_directory_without_fallback(tmp_path: Path, kind: str) -> None:
    project = tmp_path / "project"
    project.mkdir()
    git("init", "-q", cwd=project)
    if kind == "file":
        (project / "target").write_text("data")
        target = project / "target"
    elif kind == "dangling":
        target = project / "target"
        target.symlink_to(project / "missing")
    elif kind == "outside-git":
        target = tmp_path / "other"
        target.mkdir()
    else:
        target = project / "missing"

    result = invoke("--path", str(target), "Fix", cwd=project)

    assert result.returncode == 1
    assert result.stdout == b""
    assert b"factory: error:" in result.stderr
    assert b"Run execution is not yet available" not in result.stderr
    assert b"Usage:" not in result.stderr


def test_explicit_path_uses_nested_git_worktree(tmp_path: Path) -> None:
    outer = tmp_path / "outer"
    outer.mkdir()
    git("init", "-q", cwd=outer)
    inner = outer / "inner"
    inner.mkdir()
    git("init", "-q", cwd=inner)
    boundary = inner / "service"
    boundary.mkdir()

    assert _resolve_explicit_path(str(boundary), outer) == boundary


def test_explicit_path_handles_newline_in_git_worktree_path(tmp_path: Path) -> None:
    project = tmp_path / "project\nname"
    project.mkdir()
    git("init", "-q", cwd=project)
    boundary = project / "service"
    boundary.mkdir()

    assert _resolve_explicit_path(str(boundary), tmp_path) == boundary


def test_explicit_path_accepts_linked_worktree(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    git("init", "-q", cwd=source)
    git("-c", "user.name=Test", "-c", "user.email=test@example.com",
        "commit", "--allow-empty", "-m", "initial", "-q", cwd=source)
    linked = tmp_path / "linked"
    git("worktree", "add", "-q", "-b", "linked", str(linked), cwd=source)
    boundary = linked / "service"
    boundary.mkdir()

    assert _resolve_explicit_path(str(boundary), source) == boundary


def test_explicit_path_accepts_submodule(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    git("init", "-q", cwd=source)
    git("-c", "user.name=Test", "-c", "user.email=test@example.com",
        "commit", "--allow-empty", "-m", "initial", "-q", cwd=source)
    parent = tmp_path / "parent"
    parent.mkdir()
    git("init", "-q", cwd=parent)
    git("-c", "protocol.file.allow=always", "submodule", "add", "-q", str(source),
        "module", cwd=parent)
    boundary = parent / "module" / "service"
    boundary.mkdir()

    assert _resolve_explicit_path(str(boundary), parent) == boundary


def test_explicit_path_rejects_bare_repository(tmp_path: Path) -> None:
    bare = tmp_path / "bare.git"
    git("init", "--bare", "-q", str(bare), cwd=tmp_path)

    with pytest.raises(ProjectSelectionError, match="non-bare Git worktree"):
        _resolve_explicit_path(str(bare), tmp_path)


def test_explicit_path_rejects_missing_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "")

    with pytest.raises(ProjectSelectionError, match="Git inspection failed"):
        _resolve_explicit_path(str(tmp_path), tmp_path)


def test_valid_explicit_path_reaches_run_stage(tmp_path: Path) -> None:
    git("init", "-q", cwd=tmp_path)
    boundary = tmp_path / "service"
    boundary.mkdir()

    result = invoke(
        "--path=$FACTORY_TEST_BOUNDARY", "Fix", cwd=tmp_path,
        env={"FACTORY_TEST_BOUNDARY": "service"},
    )

    assert result.returncode == 1
    assert result.stdout == b""
    assert result.stderr == b"factory: error: Run execution is not yet available.\n"


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


@pytest.mark.parametrize(
    "args",
    [
        ("--file", "first.txt", "--project=api", "--file=first.txt", "--force",
         "--", "  Fix  this  ", "--file", "last\tword"),
        ("--", "project", "list"),
        ("--project", "api", "Fix"),
        ("--file=-log", "--file", "next.log", "Fix"),
        ("-", "--force"),
    ],
    ids=[
        "repeated-context-and-whitespace", "terminator-escapes-project",
        "separate-selector",
        "repeatable-files", "lone-dash-instruction",
    ],
)
def test_run_accepts_documented_forms_through_cli(args: tuple[str, ...]) -> None:
    result = invoke(*args)

    assert result.returncode == 1
    assert result.stdout == b""
    assert result.stderr == b"factory: error: Run execution is not yet available.\n"


def test_run_parser_returns_instruction_selector_context_sources_and_force() -> None:
    parsed = _parse_run([
        "--file", "first.txt", "--project=api", "--file=first.txt",
        "--force", "--", "  Fix  this  ", "--file", "last\tword",
    ])

    assert parsed.instruction == "  Fix  this   --file last\tword"
    assert parsed.selector == ("project", "api")
    assert parsed.files == ("first.txt", "first.txt")
    assert parsed.force is True


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
