"""Run Request behavior at the Factory command boundary."""

import io
import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

import factory
from _harness import RunAdmissionError
from _project import Project


def _selected_harness(
    handoff: Callable[[Project, io.BufferedRandom], int],
) -> SimpleNamespace:
    """A Runner stand-in whose run() is the recorded handoff."""

    return SimpleNamespace(run=handoff)


def run_arguments_for_selection(
    selection: str, tmp_path: Path, project: Path,
    monkeypatch: pytest.MonkeyPatch, *, explicit_path: Path | None = None,
) -> list[str]:
    if selection == "explicit":
        return [
            "factory", "--path", str(project if explicit_path is None else explicit_path),
        ]
    if selection == "registered":
        config = tmp_path / "config"
        registry = config / "factory" / "projects.json"
        registry.parent.mkdir(parents=True)
        registry.write_text(json.dumps({
            "version": 1, "projects": {"api": str(project)},
        }))
        monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
        return ["factory", "--project", "api"]
    return ["factory"]


def track_named_spools(
    monkeypatch: pytest.MonkeyPatch, *, fail_rewind: bool = False,
) -> list[tuple[io.BufferedRandom, Path]]:
    named_temporary_file = factory.tempfile.NamedTemporaryFile
    created: list[tuple[io.BufferedRandom, Path]] = []

    def create_named_spool(**_: object) -> io.BufferedRandom:
        spool = named_temporary_file(mode="w+b")
        if fail_rewind:
            def fail_seek(offset: int, whence: int = 0) -> int:
                if offset == 0 and whence == 0:
                    raise OSError("rewind failed")
                return spool.file.seek(offset, whence)

            spool.seek = fail_seek
        created.append((spool, Path(spool.name)))
        return spool

    monkeypatch.setattr(factory.tempfile, "TemporaryFile", create_named_spool)
    return created


def make_downstream_failure(
    attempts: list[Path], kind: str,
) -> Callable[[Project, io.BufferedRandom], int]:
    def fail(project: Project, spool: io.BufferedRandom) -> int:
        attempts.append(project.boundary)
        if kind == "admission":
            raise RunAdmissionError("simultaneous admission failure")
        raise RuntimeError("simultaneous Codex failure")

    return fail


@pytest.mark.parametrize("status", [0, 1, 2])
def test_factory_returns_ordinary_codex_status_without_annotating_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capfdbinary: pytest.CaptureFixture[bytes], status: int,
) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)

    def handoff(project: Project, spool: io.BufferedRandom) -> int:
        assert project == Project(tmp_path, tmp_path)
        os.write(1, b"\xffcodex-stdout\n")
        os.write(2, b"\xfecodex-stderr\n")
        return status

    monkeypatch.setattr(factory, "select_harness", lambda _name: _selected_harness(handoff))
    monkeypatch.setattr(sys, "argv", ["factory", "--path", str(tmp_path), "Fix"])
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"")))
    monkeypatch.chdir(tmp_path)

    assert factory.main() == status
    captured = capfdbinary.readouterr()
    assert captured.out == b"\xffcodex-stdout\n"
    assert captured.err == b"\xfecodex-stderr\n"


def test_registered_run_uses_exact_boundary_and_only_its_spec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    (project / ".agent").mkdir()
    (project / ".agent" / "spec.md").write_text("parent guidance")
    service = project / "service"
    (service / ".agent").mkdir(parents=True)
    (service / ".agent" / "spec.md").write_text("service guidance")
    other = project / "other"
    other.mkdir()
    config = tmp_path / "config"
    registry = config / "factory" / "projects.json"
    registry.parent.mkdir(parents=True)
    registry.write_text(json.dumps({"version": 1, "projects": {
        "Api": str(service), "api": str(other),
    }}))
    before = registry.read_bytes()
    received: list[tuple[Project, bytes]] = []

    def handoff(selected: Project, spool: io.BufferedRandom) -> int:
        received.append((selected, spool.read()))
        return 0

    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    monkeypatch.setattr(factory, "select_harness", lambda _name: _selected_harness(handoff))
    monkeypatch.chdir(project)
    for name in ("Api", "api"):
        monkeypatch.setattr(sys, "argv", ["factory", "--project", name, "Fix"])
        monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"")))
        assert factory.main() == 0

    assert received == [
        (
            Project(service, project),
            (
                b"# Project Guidance\n\nservice guidance\n\n"
                b"# Run Request\n\n## Instruction\n\nFix"
            ),
        ),
        (Project(other, project), b"# Run Request\n\n## Instruction\n\nFix"),
    ]
    assert registry.read_bytes() == before
    assert not (registry.parent / "projects.lock").exists()


def test_registered_run_revalidates_stale_boundary_without_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    service = project / "service"
    service.mkdir()
    config = tmp_path / "config"
    registry = config / "factory" / "projects.json"
    registry.parent.mkdir(parents=True)
    registry.write_text(json.dumps({"version": 1, "projects": {"api": str(service)}}))
    before = registry.read_bytes()
    projects: list[Project] = []

    def handoff(selected: Project, spool: io.BufferedRandom) -> int:
        projects.append(selected)
        return 0

    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    monkeypatch.setattr(factory, "select_harness", lambda _name: _selected_harness(handoff))
    monkeypatch.setattr(sys, "argv", ["factory", "--project=api", "Fix"])
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"")))
    monkeypatch.chdir(project)

    assert factory.main() == 0
    service.rename(project / "moved")
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"")))
    assert factory.main() == 1
    assert projects == [Project(service, project)]
    assert 'Project "api" is stale: path does not exist' in capsys.readouterr().err
    assert registry.read_bytes() == before
    assert not (registry.parent / "projects.lock").exists()


@pytest.mark.parametrize("kind", ["unknown", "invalid-name", "malformed", "symlink"])
def test_registered_run_rejects_invalid_selection_before_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], kind: str,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    service = project / "service"
    service.mkdir()
    alias = project / "alias"
    alias.symlink_to(service, target_is_directory=True)
    config = tmp_path / "config"
    registry = config / "factory" / "projects.json"
    registry.parent.mkdir(parents=True)
    registry.write_text(
        '{"version":1,"projects":{"api":"/one","api":"/two"}}'
        if kind == "malformed" else
        json.dumps({"version": 1, "projects": {"Api": str(alias)}})
    )
    before = registry.read_bytes()
    name = {"unknown": "api", "invalid-name": "bad name",
            "malformed": "Api", "symlink": "Api"}[kind]

    def unexpected_handoff(boundary: Path, spool: io.BufferedRandom) -> int:
        pytest.fail("Run Admission must not start after registered selection fails")

    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    monkeypatch.setattr(factory, "select_harness", lambda _name: _selected_harness(unexpected_handoff))
    monkeypatch.setattr(sys, "argv", ["factory", "--project", name, "Fix"])
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"")))
    monkeypatch.chdir(project)

    assert factory.main() == 1
    error = capsys.readouterr().err
    assert {
        "unknown": 'Project "api" is not registered',
        "invalid-name": "invalid Project Name",
        "malformed": "malformed Project Registry",
        "symlink": "stale: path is not canonical",
    }[kind] in error
    assert registry.read_bytes() == before
    assert not (registry.parent / "projects.lock").exists()


def test_run_without_selector_uses_containing_worktree_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    nested = tmp_path / "service" / "src"
    nested.mkdir(parents=True)
    projects: list[Project] = []

    def handoff(selected: Project, spool: io.BufferedRandom) -> int:
        projects.append(selected)
        return 0

    monkeypatch.setattr(factory, "select_harness", lambda _name: _selected_harness(handoff))
    monkeypatch.setattr(sys, "argv", ["factory", "Fix"])
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"")))
    monkeypatch.chdir(nested)

    assert factory.main() == 0
    assert projects == [Project(tmp_path, tmp_path)]


def test_discovery_selects_nearest_marker_without_ancestor_spec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / ".agent").mkdir()
    (tmp_path / ".agent" / "spec.md").write_text("ancestor guidance")
    service = tmp_path / "service"
    (service / ".agent").mkdir(parents=True)
    invocation = service / "src"
    invocation.mkdir()
    received: list[tuple[Project, bytes]] = []

    def handoff(selected: Project, spool: io.BufferedRandom) -> int:
        received.append((selected, spool.read()))
        return 0

    monkeypatch.setattr(factory, "select_harness", lambda _name: _selected_harness(handoff))
    monkeypatch.setattr(sys, "argv", ["factory", "Fix"])
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"")))
    monkeypatch.chdir(invocation)

    assert factory.main() == 0
    assert received == [(
        Project(service, tmp_path), b"# Run Request\n\n## Instruction\n\nFix",
    )]


@pytest.mark.parametrize("kind", ["file", "dangling", "escaping"])
def test_discovery_refuses_malformed_marker_instead_of_using_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], kind: str,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    (project / ".agent").mkdir()
    service = project / "service"
    service.mkdir()
    marker = service / ".agent"
    if kind == "file":
        marker.write_text("not a directory")
    elif kind == "dangling":
        marker.symlink_to("missing", target_is_directory=True)
    else:
        outside = tmp_path / "outside"
        outside.mkdir()
        marker.symlink_to(outside, target_is_directory=True)
    received: list[Path] = []

    def handoff(boundary: Path, spool: io.BufferedRandom) -> int:
        received.append(boundary)
        return 0

    monkeypatch.setattr(factory, "select_harness", lambda _name: _selected_harness(handoff))
    monkeypatch.setattr(sys, "argv", ["factory", "Fix"])
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"")))
    monkeypatch.chdir(service)

    assert factory.main() == 1
    assert received == []
    assert "Project Marker" in capsys.readouterr().err


def test_discovery_refuses_inaccessible_marker_instead_of_using_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / ".agent").mkdir()
    service = tmp_path / "service"
    service.mkdir()
    marker = service / ".agent"
    marker.mkdir()
    original_lstat = Path.lstat

    def inaccessible_lstat(path: Path) -> object:
        if path == marker:
            raise PermissionError("access denied")
        return original_lstat(path)

    def unexpected_handoff(boundary: Path, spool: io.BufferedRandom) -> int:
        pytest.fail("Codex must not run after an inaccessible Project Marker")

    monkeypatch.setattr(Path, "lstat", inaccessible_lstat)
    monkeypatch.setattr(factory, "select_harness", lambda _name: _selected_harness(unexpected_handoff))
    monkeypatch.setattr(sys, "argv", ["factory", "Fix"])
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"")))
    monkeypatch.chdir(service)

    assert factory.main() == 1
    assert "Project Marker" in capsys.readouterr().err


def test_discovery_accepts_contained_symlinked_marker_and_its_spec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    service = tmp_path / "service"
    guidance = service / "guidance"
    guidance.mkdir(parents=True)
    (guidance / "spec.md").write_text("service guidance")
    (service / ".agent").symlink_to(guidance, target_is_directory=True)
    invocation = service / "src"
    invocation.mkdir()
    received: list[tuple[Project, bytes]] = []

    def handoff(selected: Project, spool: io.BufferedRandom) -> int:
        received.append((selected, spool.read()))
        return 0

    monkeypatch.setattr(factory, "select_harness", lambda _name: _selected_harness(handoff))
    monkeypatch.setattr(sys, "argv", ["factory", "Fix"])
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"")))
    monkeypatch.chdir(invocation)

    assert factory.main() == 0
    assert received == [(
        Project(service, tmp_path),
        (
            b"# Project Guidance\n\nservice guidance\n\n"
            b"# Run Request\n\n## Instruction\n\nFix"
        ),
    )]


def test_explicit_path_refuses_escaping_project_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (project / ".agent").symlink_to(outside, target_is_directory=True)

    def unexpected_handoff(boundary: Path, spool: io.BufferedRandom) -> int:
        pytest.fail("Codex must not run with an escaping Project Marker")

    monkeypatch.setattr(factory, "select_harness", lambda _name: _selected_harness(unexpected_handoff))
    monkeypatch.setattr(sys, "argv", ["factory", "--path", str(project), "Fix"])
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"")))
    monkeypatch.chdir(project)

    assert factory.main() == 1
    assert "Project Spec marker is not a contained directory" in capsys.readouterr().err


def test_discovery_stays_inside_nested_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    outer = tmp_path / "outer"
    inner = outer / "inner"
    inner.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=outer, check=True)
    subprocess.run(["git", "init", "-q"], cwd=inner, check=True)
    (outer / ".agent").mkdir()
    (outer / ".agent" / "spec.md").write_text("outer guidance")
    nested = inner / "service"
    nested.mkdir()
    received: list[tuple[Project, bytes]] = []

    def handoff(selected: Project, spool: io.BufferedRandom) -> int:
        received.append((selected, spool.read()))
        return 0

    monkeypatch.setattr(factory, "select_harness", lambda _name: _selected_harness(handoff))
    monkeypatch.setattr(sys, "argv", ["factory", "Fix"])
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"")))
    monkeypatch.chdir(nested)

    assert factory.main() == 0
    assert received == [(
        Project(inner, inner), b"# Run Request\n\n## Instruction\n\nFix",
    )]


@pytest.mark.parametrize("kind", ["linked", "submodule"])
def test_discovery_uses_linked_worktree_or_submodule_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=source, check=True)
    subprocess.run([
        "git", "-c", "user.name=Test", "-c", "user.email=test@example.com",
        "commit", "--allow-empty", "-m", "initial", "-q",
    ], cwd=source, check=True)
    if kind == "linked":
        root = tmp_path / "linked"
        subprocess.run([
            "git", "worktree", "add", "-q", "-b", "linked", str(root),
        ], cwd=source, check=True)
    else:
        parent = tmp_path / "parent"
        parent.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=parent, check=True)
        subprocess.run([
            "git", "-c", "protocol.file.allow=always", "submodule", "add", "-q",
            str(source), "module",
        ], cwd=parent, check=True)
        root = parent / "module"
    nested = root / "service"
    nested.mkdir()
    projects: list[Project] = []

    def handoff(selected: Project, spool: io.BufferedRandom) -> int:
        projects.append(selected)
        return 0

    monkeypatch.setattr(factory, "select_harness", lambda _name: _selected_harness(handoff))
    monkeypatch.setattr(sys, "argv", ["factory", "Fix"])
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"")))
    monkeypatch.chdir(nested)

    assert factory.main() == 0
    assert projects == [Project(root, root)]


def test_explicit_run_hands_off_exact_request_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    marker = tmp_path / ".agent"
    marker.mkdir()
    (marker / "spec.md").write_bytes(b"\xef\xbb\xbfGuide\r\n")
    (tmp_path / "notes.txt").write_bytes(b"\xef\xbb\xbfFirst\n\n")
    requests: list[bytes] = []

    def handoff(project: Project, spool: io.BufferedRandom) -> int:
        assert project == Project(tmp_path, tmp_path)
        requests.append(spool.read())
        return 7

    monkeypatch.setattr(sys, "argv", ["factory", "--path", str(tmp_path),
                                      "--file", "notes.txt", "Fix", "it"])
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"stdin")))
    monkeypatch.setattr(factory, "select_harness", lambda _name: _selected_harness(handoff), raising=False)
    monkeypatch.chdir(tmp_path)

    assert factory.main() == 7
    assert requests == [
        (
            b"# Project Guidance\n\nGuide\r\n\n# Run Request\n\n"
            b"## Instruction\n\nFix it\n\n## Context\n\n"
            b"### File: \"notes.txt\"\n\nFirst\n\n"
            b"### Standard Input\n\nstdin"
        )
    ]
    assert capsys.readouterr().err == ""


def test_standard_input_inspection_failure_refuses_before_run_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)

    class UnreadableStandardInput:
        def isatty(self) -> bool:
            raise OSError("descriptor unavailable")

    def unexpected_handoff(boundary: Path, spool: io.BufferedRandom) -> int:
        pytest.fail("Run Admission must not start when standard input cannot be checked")

    monkeypatch.setattr(factory, "select_harness", lambda _name: _selected_harness(unexpected_handoff))
    monkeypatch.setattr(sys, "stdin", UnreadableStandardInput())
    monkeypatch.setattr(sys, "argv", ["factory", "--path", str(tmp_path), "Fix"])
    monkeypatch.chdir(tmp_path)

    assert factory.main() == 1
    assert capsys.readouterr().err == (
        "factory: error: standard input could not be read: descriptor unavailable.\n"
    )


def test_closed_standard_input_refuses_before_run_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    closed_input = io.TextIOWrapper(io.BytesIO(b"data"))
    closed_input.close()

    def unexpected_handoff(boundary: Path, spool: io.BufferedRandom) -> int:
        pytest.fail("Run Admission must not start when standard input is closed")

    monkeypatch.setattr(factory, "select_harness", lambda _name: _selected_harness(unexpected_handoff))
    monkeypatch.setattr(sys, "stdin", closed_input)
    monkeypatch.setattr(sys, "argv", ["factory", "--path", str(tmp_path), "Fix"])
    monkeypatch.chdir(tmp_path)

    assert factory.main() == 1
    assert capsys.readouterr().err.startswith(
        "factory: error: standard input could not be read: "
    )


def test_terminal_standard_input_is_never_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)

    class TerminalInput:
        def isatty(self) -> bool:
            return True

        @property
        def buffer(self) -> io.BytesIO:
            pytest.fail("terminal standard input must not be read")

    received: list[bytes] = []

    def handoff(boundary: Path, spool: io.BufferedRandom) -> int:
        received.append(spool.read())
        return 0

    monkeypatch.setattr(factory, "select_harness", lambda _name: _selected_harness(handoff))
    monkeypatch.setattr(sys, "stdin", TerminalInput())
    monkeypatch.setattr(sys, "argv", ["factory", "--path", str(tmp_path), "Fix"])
    monkeypatch.chdir(tmp_path)

    assert factory.main() == 0
    assert received == [b"# Run Request\n\n## Instruction\n\nFix"]


@pytest.mark.parametrize("payload,context", [
    (b"line\r\nend", b"\n\n## Context\n\n### Standard Input\n\nline\r\nend"),
    (b"", b""),
    (b"\xef\xbb\xbf", b""),
])
def test_piped_and_redirected_standard_input_have_identical_context(
    tmp_path: Path, payload: bytes, context: bytes,
) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    redirected = tmp_path / "redirected"
    redirected.write_bytes(payload)
    script = """import sys
sys.path.insert(0, sys.argv[1])
import factory
sys.argv = ['factory', *sys.argv[2:]]
def handoff(boundary, spool):
    sys.stdout.buffer.write(spool.read())
    return 0
class FakeRunner:
    run = staticmethod(handoff)
factory.select_harness = lambda _name: FakeRunner()
raise SystemExit(factory.main())
"""
    command = [sys.executable, "-c", script, str(Path(factory.__file__).parent),
               "--path", str(tmp_path), "Fix"]
    piped = subprocess.run(
        command, cwd=tmp_path, check=True, input=payload, capture_output=True,
    )
    with redirected.open("rb") as source:
        from_file = subprocess.run(
            command, cwd=tmp_path, check=True, stdin=source, capture_output=True,
        )

    expected = b"# Run Request\n\n## Instruction\n\nFix" + context
    for result in (piped, from_file):
        assert result.returncode == 0
        assert result.stdout == expected
        assert result.stderr == b""


def test_standard_input_follows_file_context_and_preserves_payload_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "notes").write_bytes(b"file\r\n")
    received: list[bytes] = []

    def handoff(boundary: Path, spool: io.BufferedRandom) -> int:
        received.append(spool.read())
        return 0

    monkeypatch.setattr(factory, "select_harness", lambda _name: _selected_harness(handoff))
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(
        b"\xef\xbb\xbf\xc3\xa9\r\nlast\r")))
    monkeypatch.setattr(sys, "argv", [
        "factory", "--path", str(tmp_path), "--file", "notes", "Fix",
    ])
    monkeypatch.chdir(tmp_path)

    assert factory.main() == 0
    assert received == [
        (
            b"# Run Request\n\n## Instruction\n\nFix\n\n## Context\n\n"
            b"### File: \"notes\"\n\nfile\r\n\n"
            b"### Standard Input\n\n\xc3\xa9\r\nlast\r"
        )
    ]


@pytest.mark.parametrize("payload,reason", [
    (b"\xff", "is not valid UTF-8"),
    (b"\xef\xbb\xbf\x00", "contains NUL"),
])
def test_invalid_standard_input_refuses_before_run_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], payload: bytes, reason: str,
) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)

    def unexpected_handoff(boundary: Path, spool: io.BufferedRandom) -> int:
        pytest.fail("Run Admission must not start after invalid standard input")

    monkeypatch.setattr(factory, "select_harness", lambda _name: _selected_harness(unexpected_handoff))
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(payload)))
    monkeypatch.setattr(sys, "argv", ["factory", "--path", str(tmp_path), "Fix"])
    monkeypatch.chdir(tmp_path)

    assert factory.main() == 1
    assert capsys.readouterr().err == f"factory: error: standard input {reason}.\n"


@pytest.mark.parametrize("failure", [OSError("broken pipe"), ValueError("closed pipe")])
def test_standard_input_read_failure_refuses_before_run_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    failure: Exception,
) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)

    class FailingStream(io.BytesIO):
        def read(self, size: int = -1) -> bytes:
            raise failure

    def unexpected_handoff(boundary: Path, spool: io.BufferedRandom) -> int:
        pytest.fail("Run Admission must not start after a standard input read failure")

    monkeypatch.setattr(factory, "select_harness", lambda _name: _selected_harness(unexpected_handoff))
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(FailingStream()))
    monkeypatch.setattr(sys, "argv", ["factory", "--path", str(tmp_path), "Fix"])
    monkeypatch.chdir(tmp_path)

    assert factory.main() == 1
    assert capsys.readouterr().err == (
        f"factory: error: standard input could not be read: {failure}.\n"
    )


def test_standard_input_payload_bytes_count_toward_context_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "notes").write_bytes(b"X")
    monkeypatch.setenv("FACTORY_CONTEXT_WARN_BYTES", "2")
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(
        b"\xef\xbb\xbf\xc3\xa9")))
    monkeypatch.setattr(sys, "argv", [
        "factory", "--path", str(tmp_path), "--file", "notes", "Fix",
    ])
    monkeypatch.chdir(tmp_path)

    assert factory.main() == 1
    assert capsys.readouterr().err == (
        "factory: error: Context is 3 bytes; the warning threshold is 2 bytes. "
        "Re-run with --force to continue.\n"
    )


def test_absent_spec_and_empty_context_keep_only_required_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "empty").write_bytes(b"\xef\xbb\xbf")
    received: list[bytes] = []

    def handoff(boundary: Path, spool: io.BufferedRandom) -> int:
        received.append(spool.read())
        return 0

    monkeypatch.setattr(factory, "select_harness", lambda _name: _selected_harness(handoff))
    monkeypatch.setattr(sys, "argv", ["factory", "--path", str(tmp_path),
                                      "--file", "empty", "Do", "work"])
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"")))
    monkeypatch.chdir(tmp_path)

    assert factory.main() == 0
    assert received == [b"# Run Request\n\n## Instruction\n\nDo work"]


@pytest.mark.parametrize("content", [b"\xff", b"before\x00after"])
def test_invalid_project_spec_refuses_before_codex(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    content: bytes,
) -> None:
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    marker = tmp_path / ".agent"
    marker.mkdir()
    (marker / "spec.md").write_bytes(content)
    monkeypatch.setattr(sys, "argv", ["factory", "--path", str(tmp_path), "Fix"])
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"")))
    monkeypatch.chdir(tmp_path)

    assert factory.main() == 1
    assert "Project Spec" in capsys.readouterr().err


def test_project_spec_symlink_outside_boundary_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    import subprocess

    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    marker = project / ".agent"
    marker.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("secret")
    (marker / "spec.md").symlink_to(outside)
    monkeypatch.setattr(sys, "argv", ["factory", "--path", str(project), "Fix"])
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"")))
    monkeypatch.chdir(project)

    assert factory.main() == 1
    assert "outside the Project Boundary" in capsys.readouterr().err


def test_project_spec_parent_swap_refuses_before_codex(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    guidance = tmp_path / "guidance"
    guidance.mkdir()
    (guidance / "spec.md").write_text("inside")
    marker = tmp_path / ".agent"
    marker.mkdir()
    (marker / "spec.md").symlink_to(guidance / "spec.md")
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (outside / "spec.md").write_text("outside")
    original_open = os.open
    swapped = False

    def swap_before_use(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if path == "guidance" and dir_fd is not None and not swapped:
            guidance.rename(tmp_path / "old-guidance")
            guidance.symlink_to(outside, target_is_directory=True)
            swapped = True
        return original_open(path, flags, mode, dir_fd=dir_fd)

    def unexpected_handoff(boundary: Path, spool: io.BufferedRandom) -> int:
        pytest.fail("Codex must not receive Project Guidance after a path swap")

    monkeypatch.setattr(os, "open", swap_before_use)
    monkeypatch.setattr(os, "supports_dir_fd", {*os.supports_dir_fd, swap_before_use})
    monkeypatch.setattr(factory, "select_harness", lambda _name: _selected_harness(unexpected_handoff))
    monkeypatch.setattr(sys, "argv", ["factory", "--path", str(tmp_path), "Fix"])
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"")))
    monkeypatch.chdir(tmp_path)

    assert factory.main() == 1
    assert swapped
    assert "Project Spec cannot be opened" in capsys.readouterr().err


def test_context_gate_counts_utf8_bytes_and_force_only_passes_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    import subprocess

    monkeypatch.setattr("_codex._COMPATIBILITY_SET", ())
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "context").write_bytes(b"\xef\xbb\xbfcaf\xc3\xa9")
    monkeypatch.setenv("FACTORY_CONTEXT_WARN_BYTES", "4")
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"")))
    monkeypatch.chdir(tmp_path)

    monkeypatch.setattr(sys, "argv", ["factory", "--path", str(tmp_path),
                                      "--file", "context", "Fix"])
    assert factory.main() == 1
    assert capsys.readouterr().err == (
        "factory: error: Context is 5 bytes; the warning threshold is 4 bytes. "
        "Re-run with --force to continue.\n"
    )

    monkeypatch.setattr(sys, "argv", ["factory", "--path", str(tmp_path),
                                      "--file", "context", "--force", "Fix"])
    assert factory.main() == 1
    assert capsys.readouterr().err == (
        "factory: warning: Context is 5 bytes; the warning threshold is 4 bytes. "
        "Continuing because --force was supplied.\n"
        "factory: error: Codex Compatibility Set is empty; no Run is supported.\n"
    )


def test_default_context_threshold_accepts_equality_and_force_only_oversize(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / ".agent").mkdir()
    (tmp_path / ".agent" / "spec.md").write_text("Project Guidance")
    context = tmp_path / "context"
    context.write_bytes(b"\xef\xbb\xbf" + b"x" * 1_048_576)
    monkeypatch.delenv("FACTORY_CONTEXT_WARN_BYTES", raising=False)
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"")))
    monkeypatch.chdir(tmp_path)
    received: list[bytes] = []

    def handoff(boundary: Path, spool: io.BufferedRandom) -> int:
        received.append(spool.read())
        return 17

    monkeypatch.setattr(factory, "select_harness", lambda _name: _selected_harness(handoff))
    arguments = ["factory", "--path", str(tmp_path), "--file", "context"]
    monkeypatch.setattr(sys, "argv", [*arguments, "Fix"])
    assert factory.main() == 17
    assert capsys.readouterr().err == ""
    assert len(received) == 1
    assert received[0].startswith(b"# Project Guidance\n\nProject Guidance")

    context.write_bytes(b"\xef\xbb\xbf" + b"x" * 1_048_577)
    assert factory.main() == 1
    assert capsys.readouterr().err == (
        "factory: error: Context is 1048577 bytes; the warning threshold is 1048576 bytes. "
        "Re-run with --force to continue.\n"
    )
    assert len(received) == 1

    monkeypatch.setattr(sys, "argv", [*arguments, "--force", "Fix"])
    assert factory.main() == 17
    assert capsys.readouterr().err == (
        "factory: warning: Context is 1048577 bytes; the warning threshold is 1048576 bytes. "
        "Continuing because --force was supplied.\n"
    )
    assert len(received) == 2


@pytest.mark.parametrize("threshold,payload,expected", [
    ("0", b"", "factory: error: Codex Compatibility Set is empty; no Run is supported.\n"),
    ("0", b"x", (
        "factory: error: Context is 1 bytes; the warning threshold is 0 bytes. "
        "Re-run with --force to continue.\n"
    )),
    ("0002", b"xy", "factory: error: Codex Compatibility Set is empty; no Run is supported.\n"),
])
def test_context_threshold_accepts_unsigned_ascii_decimal_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    threshold: str, payload: bytes, expected: str,
) -> None:
    monkeypatch.setattr("_codex._COMPATIBILITY_SET", ())
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "context").write_bytes(payload)
    monkeypatch.setenv("FACTORY_CONTEXT_WARN_BYTES", threshold)
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"")))
    monkeypatch.setattr(sys, "argv", [
        "factory", "--path", str(tmp_path), "--file", "context", "Fix",
    ])
    monkeypatch.chdir(tmp_path)

    assert factory.main() == 1
    assert capsys.readouterr().err == expected


@pytest.mark.parametrize("downstream_failure", ["admission", "codex"])
@pytest.mark.parametrize("failure", ["parse", "explicit", "registered", "discovered"])
def test_parse_and_project_failures_precede_spooling_and_downstream_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], failure: str, downstream_failure: str,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    missing = tmp_path / "missing"
    spool_attempts: list[None] = []
    admission_attempts: list[Path] = []

    def create_spool(**kwargs: object) -> io.BytesIO:
        spool_attempts.append(None)
        return io.BytesIO()

    if failure == "parse":
        arguments = ["factory", "--unknown", "--path", str(missing), "Fix"]
    elif failure == "explicit":
        arguments = ["factory", "--path", str(missing), "Fix"]
    elif failure == "registered":
        config = tmp_path / "config"
        registry = config / "factory" / "projects.json"
        registry.parent.mkdir(parents=True)
        registry.write_text(json.dumps({
            "version": 1, "projects": {"api": str(missing)},
        }))
        monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
        arguments = ["factory", "--project", "api", "Fix"]
    else:
        (project / ".agent").symlink_to(missing, target_is_directory=True)
        arguments = ["factory", "Fix"]

    monkeypatch.setattr(factory.tempfile, "TemporaryFile", create_spool)
    monkeypatch.setattr(
        factory, "select_harness",
        lambda _name: _selected_harness(make_downstream_failure(
            admission_attempts, downstream_failure)))
    monkeypatch.setattr(sys, "argv", arguments)
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"\xff")))
    monkeypatch.chdir(project)

    expected_status = 2 if failure == "parse" else 1
    assert factory.main() == expected_status
    error = capsys.readouterr().err
    assert {
        "parse": 'unknown option "--unknown"',
        "explicit": "cannot be resolved",
        "registered": 'Project "api" is stale: path does not exist',
        "discovered": "Project Marker",
    }[failure] in error
    assert "simultaneous admission failure" not in error
    assert "simultaneous Codex failure" not in error
    assert spool_attempts == []
    assert admission_attempts == []


@pytest.mark.parametrize("downstream_failure", ["admission", "codex"])
@pytest.mark.parametrize("selection", ["explicit", "registered", "discovered"])
@pytest.mark.parametrize("source", ["spec", "file", "stdin"])
def test_project_selection_and_context_failures_precede_downstream_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], selection: str, source: str,
    downstream_failure: str,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    marker = project / ".agent"
    marker.mkdir()
    (marker / "spec.md").write_bytes(b"\xff" if source == "spec" else b"guidance")
    (project / "context").write_bytes(
        b"\xff" if source in ("spec", "file") else b"context")
    standard_input = b"\xff"
    admission_attempts: list[Path] = []

    arguments = run_arguments_for_selection(
        selection, tmp_path, project, monkeypatch)
    arguments.extend(["--file", "context", "Fix"])

    monkeypatch.setattr(
        factory, "select_harness",
        lambda _name: _selected_harness(make_downstream_failure(
            admission_attempts, downstream_failure)))
    monkeypatch.setenv("FACTORY_CONTEXT_WARN_BYTES", "invalid")
    monkeypatch.setattr(sys, "argv", arguments)
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(standard_input)))
    monkeypatch.chdir(project)

    assert factory.main() == 1
    error = capsys.readouterr().err
    assert {
        "spec": "Project Spec is not valid UTF-8",
        "file": 'Context file "context" is not valid UTF-8',
        "stdin": "standard input is not valid UTF-8",
    }[source] in error
    assert "simultaneous admission failure" not in error
    assert "simultaneous Codex failure" not in error
    assert admission_attempts == []


@pytest.mark.parametrize("selection", ["explicit", "registered", "discovered"])
def test_each_project_selection_hands_off_one_complete_rewound_binary_spool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, selection: str,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    marker = project / ".agent"
    marker.mkdir()
    (marker / "spec.md").write_text("guidance")
    context = tmp_path / "context"
    context.write_text("file context")
    invocation_dir = project / "src"
    invocation_dir.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(project, target_is_directory=True)
    handoffs: list[tuple[Project, bytes]] = []

    def handoff(selected: Project, spool: io.BufferedRandom) -> int:
        assert not spool.closed
        assert spool.tell() == 0
        assert spool.read(0) == b""
        handoffs.append((selected, spool.read()))
        return 23

    arguments = run_arguments_for_selection(
        selection, tmp_path, project, monkeypatch, explicit_path=alias)
    arguments.extend(["--file", str(context), "Fix"])

    monkeypatch.setattr(factory, "select_harness", lambda _name: _selected_harness(handoff))
    monkeypatch.setattr(sys, "argv", arguments)
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"standard input")))
    monkeypatch.chdir(invocation_dir)

    assert factory.main() == 23
    assert handoffs == [(
        Project(project, project),
        b"# Project Guidance\n\nguidance\n\n# Run Request\n\n"
        b"## Instruction\n\nFix\n\n## Context\n\n"
        + f"### File: {json.dumps(str(context))}\n\n".encode()
        + b"file context\n\n### Standard Input\n\nstandard input",
    )]


def test_spool_is_private_rewound_and_closed_after_admission_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    spools: list[io.BufferedRandom] = []

    def refuse(boundary: Path, spool: io.BufferedRandom) -> int:
        assert spool.tell() == 0
        assert not isinstance(spool.name, str)
        spools.append(spool)
        raise RunAdmissionError("unsupported")

    monkeypatch.setattr(factory, "select_harness", lambda _name: _selected_harness(refuse))
    monkeypatch.setattr(sys, "argv", ["factory", "--path", str(tmp_path), "Fix"])
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"")))
    monkeypatch.chdir(tmp_path)

    assert factory.main() == 1
    assert len(spools) == 1 and spools[0].closed


@pytest.mark.parametrize("outcome", ["success", "refusal", "exception", "interruption"])
def test_spool_lifetime_covers_every_codex_handoff_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str,
) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    created = track_named_spools(monkeypatch)

    def handoff(project: Project, spool: io.BufferedRandom) -> int:
        assert project == Project(tmp_path, tmp_path)
        assert not spool.closed
        assert spool.tell() == 0
        assert Path(spool.name).is_file()
        assert spool.read() == b"# Run Request\n\n## Instruction\n\nFix"
        if outcome == "refusal":
            raise RunAdmissionError("unsupported")
        if outcome == "exception":
            raise RuntimeError("unexpected")
        if outcome == "interruption":
            raise KeyboardInterrupt
        return 31

    monkeypatch.setattr(factory, "select_harness", lambda _name: _selected_harness(handoff))
    monkeypatch.setattr(sys, "argv", ["factory", "--path", str(tmp_path), "Fix"])
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"")))
    monkeypatch.chdir(tmp_path)

    if outcome == "success":
        assert factory.main() == 31
    elif outcome == "refusal":
        assert factory.main() == 1
    elif outcome == "exception":
        with pytest.raises(RuntimeError, match="unexpected"):
            factory.main()
    else:
        with pytest.raises(KeyboardInterrupt):
            factory.main()

    assert len(created) == 1
    spool, path = created[0]
    assert spool.closed
    assert not path.exists()


@pytest.mark.parametrize("failure", ["composition", "size gate"])
def test_rejected_composed_spools_are_closed_and_removed_before_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    context = tmp_path / "context"
    context.write_bytes(b"\xff" if failure == "composition" else b"x")
    created = track_named_spools(monkeypatch)
    admission_attempts: list[Path] = []

    monkeypatch.setattr(
        factory, "select_harness",
        lambda _name: _selected_harness(make_downstream_failure(
            admission_attempts, "admission")))
    monkeypatch.setenv("FACTORY_CONTEXT_WARN_BYTES", "0")
    monkeypatch.setattr(sys, "argv", [
        "factory", "--path", str(tmp_path), "--file", "context", "Fix",
    ])
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"")))
    monkeypatch.chdir(tmp_path)

    assert factory.main() == 1
    assert admission_attempts == []
    assert len(created) == 1
    spool, path = created[0]
    assert spool.closed
    assert not path.exists()


@pytest.mark.parametrize("failure", ["size gate", "rewind"])
def test_size_gate_and_rewind_failures_precede_run_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], failure: str,
) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "context").write_bytes(b"x")
    created = track_named_spools(monkeypatch, fail_rewind=True)
    admission_attempts: list[Path] = []

    monkeypatch.setattr(
        factory, "select_harness",
        lambda _name: _selected_harness(make_downstream_failure(
            admission_attempts, "admission")))
    monkeypatch.setenv("FACTORY_CONTEXT_WARN_BYTES", "0" if failure == "size gate" else "1")
    monkeypatch.setattr(sys, "argv", [
        "factory", "--path", str(tmp_path), "--file", "context", "Fix",
    ])
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"")))
    monkeypatch.chdir(tmp_path)

    assert factory.main() == 1
    error = capsys.readouterr().err
    assert (
        "Context is 1 bytes; the warning threshold is 0 bytes"
        if failure == "size gate" else "Run Request spool failed: rewind failed"
    ) in error
    assert "simultaneous admission failure" not in error
    assert admission_attempts == []
    assert len(created) == 1
    spool, path = created[0]
    assert spool.closed
    assert not path.exists()


def test_explicit_subdirectory_does_not_inherit_ancestor_spec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / ".agent").mkdir()
    (tmp_path / ".agent" / "spec.md").write_text("ancestor guidance")
    boundary = tmp_path / "service"
    boundary.mkdir()
    received: list[bytes] = []

    def handoff(project: Project, spool: io.BufferedRandom) -> int:
        assert project == Project(boundary, tmp_path)
        received.append(spool.read())
        return 0

    monkeypatch.setattr(factory, "select_harness", lambda _name: _selected_harness(handoff))
    monkeypatch.setattr(sys, "argv", ["factory", "--path", str(boundary), "Fix"])
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"")))
    monkeypatch.chdir(tmp_path)

    assert factory.main() == 0
    assert received == [b"# Run Request\n\n## Instruction\n\nFix"]


@pytest.mark.parametrize("content", [b"\xff", b"a\x00b"])
def test_invalid_context_refuses_before_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], content: bytes,
) -> None:
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "context").write_bytes(content)
    monkeypatch.setattr(sys, "argv", ["factory", "--path", str(tmp_path),
                                      "--file", "context", "Fix"])
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"")))
    monkeypatch.chdir(tmp_path)

    assert factory.main() == 1
    assert "Context file" in capsys.readouterr().err


def test_context_path_with_nul_refuses_without_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)

    def unexpected_handoff(boundary: Path, spool: io.BufferedRandom) -> int:
        pytest.fail("Codex must not receive Context from an invalid file path")

    monkeypatch.setattr(factory, "select_harness", lambda _name: _selected_harness(unexpected_handoff))
    monkeypatch.setattr(sys, "argv", ["factory", "--file", "bad\x00path", "Fix"])
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"")))
    monkeypatch.chdir(tmp_path)

    assert factory.main() == 1
    assert "Context file" in capsys.readouterr().err


def test_external_context_file_expands_from_invocation_and_keeps_original_labels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    boundary = project / "service"
    boundary.mkdir()
    invocation = project / "runner"
    invocation.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"\xef\xbb\xbf\xef\xbb\xbfA\r\nB\r")
    (invocation / "linked.txt").symlink_to(outside)
    received: list[bytes] = []

    def handoff(selected: Project, spool: io.BufferedRandom) -> int:
        assert selected == Project(boundary, project)
        received.append(spool.read())
        return 0

    monkeypatch.setenv("CONTEXT_FILE", "../../outside.txt")
    monkeypatch.setenv("FACTORY_CONTEXT_WARN_BYTES", "16")
    monkeypatch.setattr(factory, "select_harness", lambda _name: _selected_harness(handoff))
    monkeypatch.setattr(sys, "argv", ["factory", "--path", str(boundary),
                                      "--file", "$CONTEXT_FILE", "--file", "linked.txt", "Fix"])
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"")))
    monkeypatch.chdir(invocation)

    assert factory.main() == 0
    assert received == [
        (
            b'# Run Request\n\n## Instruction\n\nFix\n\n## Context\n\n'
            b'### File: "$CONTEXT_FILE"\n\n\xef\xbb\xbfA\r\nB\r\n\n'
            b'### File: "linked.txt"\n\n\xef\xbb\xbfA\r\nB\r'
        )
    ]


@pytest.mark.parametrize("value", [None, ""])
def test_context_file_rejects_unset_or_empty_environment_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], value: str | None,
) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    if value is None:
        monkeypatch.delenv("CONTEXT_FILE", raising=False)
    else:
        monkeypatch.setenv("CONTEXT_FILE", value)

    def unexpected_handoff(boundary: Path, spool: io.BufferedRandom) -> int:
        pytest.fail("Codex must not receive Context from an invalid path")

    monkeypatch.setattr(factory, "select_harness", lambda _name: _selected_harness(unexpected_handoff))
    monkeypatch.setattr(sys, "argv", ["factory", "--file", "${CONTEXT_FILE}", "Fix"])
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"")))
    monkeypatch.chdir(tmp_path)

    assert factory.main() == 1
    assert 'environment variable "CONTEXT_FILE" is unset or empty' in capsys.readouterr().err


def test_directory_context_source_refuses_before_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "directory").mkdir()

    def unexpected_handoff(boundary: Path, spool: io.BufferedRandom) -> int:
        pytest.fail("Codex must not receive a non-file Context Source")

    monkeypatch.setattr(factory, "select_harness", lambda _name: _selected_harness(unexpected_handoff))
    monkeypatch.setattr(sys, "argv", ["factory", "--file", "directory", "Fix"])
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"")))
    monkeypatch.chdir(tmp_path)

    assert factory.main() == 1
    assert 'Context file "directory" is not a regular file' in capsys.readouterr().err


@pytest.mark.parametrize("setting", [
    "", "-1", "+1", " 1", "1 ", "1\n", "１２", "١", "1.0", "1_0", "0x10",
])
def test_malformed_context_threshold_refuses_even_empty_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], setting: str,
) -> None:
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    monkeypatch.setenv("FACTORY_CONTEXT_WARN_BYTES", setting)
    monkeypatch.setattr(sys, "argv", ["factory", "--path", str(tmp_path), "Fix"])
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"")))
    monkeypatch.chdir(tmp_path)

    assert factory.main() == 1
    assert "unsigned ASCII decimal integer" in capsys.readouterr().err


@pytest.mark.parametrize("failure,expected", [
    ("project", "cannot be resolved"),
    ("context", 'Context file "context" is not valid UTF-8'),
    ("threshold", "FACTORY_CONTEXT_WARN_BYTES must be an unsigned ASCII decimal integer"),
    ("admission", "Codex Compatibility Set is empty"),
])
def test_force_preserves_project_context_threshold_and_run_admission_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    failure: str, expected: str,
) -> None:
    monkeypatch.setattr("_codex._COMPATIBILITY_SET", ())
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "context").write_bytes(b"\xff")
    monkeypatch.setenv("FACTORY_CONTEXT_WARN_BYTES", "invalid" if failure == "threshold" else "0")
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"")))
    monkeypatch.chdir(tmp_path)
    path = str(tmp_path / "missing") if failure == "project" else str(tmp_path)
    file_args = ["--file", "context"] if failure == "context" else []
    diagnostics = []

    for force in ([], ["--force"]):
        monkeypatch.setattr(sys, "argv", ["factory", "--path", path,
                                          *file_args, *force, "Fix"])
        assert factory.main() == 1
        diagnostic = capsys.readouterr().err
        assert expected in diagnostic
        diagnostics.append(diagnostic)
    assert diagnostics[0] == diagnostics[1]


def test_repeated_file_context_uses_original_json_labels_and_preserves_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    name = 'line\n"quote".txt'
    (tmp_path / name).write_bytes(b"x\r\ny\r")
    received: list[bytes] = []

    def handoff(boundary: Path, spool: io.BufferedRandom) -> int:
        received.append(spool.read())
        return 0

    monkeypatch.setattr(factory, "select_harness", lambda _name: _selected_harness(handoff))
    monkeypatch.setattr(sys, "argv", ["factory", "--path", str(tmp_path),
                                      "--file", name, "--file", name, "Fix"])
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"")))
    monkeypatch.chdir(tmp_path)

    assert factory.main() == 0
    assert received == [
        (
            b'# Run Request\n\n## Instruction\n\nFix\n\n## Context\n\n'
            b'### File: "line\\n\\"quote\\".txt"\n\nx\r\ny\r\n\n'
            b'### File: "line\\n\\"quote\\".txt"\n\nx\r\ny\r'
        )
    ]


def test_repeated_file_context_skips_empty_sources_and_preserves_each_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "empty").write_bytes(b"")
    (tmp_path / "bom-only").write_bytes(b"\xef\xbb\xbf")
    (tmp_path / "mixed").write_bytes(b"\xef\xbb\xbfA\r\nB\nC\r")
    (tmp_path / "unicode").write_bytes(b"caf\xc3\xa9\r\n")
    (tmp_path / "trailing").write_bytes(b"end\n\n")
    received: list[bytes] = []

    def handoff(boundary: Path, spool: io.BufferedRandom) -> int:
        received.append(spool.read())
        return 0

    monkeypatch.setattr(factory, "select_harness", lambda _name: _selected_harness(handoff))
    monkeypatch.setattr(sys, "argv", [
        "factory", "--path", str(tmp_path), "--file", "empty", "--file", "mixed",
        "--file", "bom-only", "--file", "unicode", "--file", "mixed",
        "--file", "trailing", "--file", "empty", "Fix",
    ])
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"")))
    monkeypatch.chdir(tmp_path)

    assert factory.main() == 0
    assert received == [
        (
            b'# Run Request\n\n## Instruction\n\nFix\n\n## Context\n\n'
            b'### File: "mixed"\n\nA\r\nB\nC\r\n\n'
            b'### File: "unicode"\n\ncaf\xc3\xa9\r\n\n'
            b'### File: "mixed"\n\nA\r\nB\nC\r\n\n'
            b'### File: "trailing"\n\nend\n\n'
        )
    ]


def test_only_empty_file_context_sources_leave_no_context_section(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "empty").write_bytes(b"")
    (tmp_path / "bom-only").write_bytes(b"\xef\xbb\xbf")
    received: list[bytes] = []

    def handoff(boundary: Path, spool: io.BufferedRandom) -> int:
        received.append(spool.read())
        return 0

    monkeypatch.setattr(factory, "select_harness", lambda _name: _selected_harness(handoff))
    monkeypatch.setattr(sys, "argv", [
        "factory", "--path", str(tmp_path), "--file", "empty",
        "--file", "bom-only", "--file", "empty", "Fix",
    ])
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"")))
    monkeypatch.chdir(tmp_path)

    assert factory.main() == 0
    assert received == [b"# Run Request\n\n## Instruction\n\nFix"]


def test_repeated_file_context_counts_payload_bytes_from_each_occurrence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "unicode").write_bytes(b"\xef\xbb\xbf\xc3\xa9")
    (tmp_path / "empty").write_bytes(b"")
    monkeypatch.setenv("FACTORY_CONTEXT_WARN_BYTES", "3")
    monkeypatch.setattr(sys, "argv", [
        "factory", "--path", str(tmp_path), "--file", "unicode", "--file", "empty",
        "--file", "unicode", "Fix",
    ])
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"")))
    monkeypatch.chdir(tmp_path)

    assert factory.main() == 1
    assert capsys.readouterr().err == (
        "factory: error: Context is 4 bytes; the warning threshold is 3 bytes. "
        "Re-run with --force to continue.\n"
    )


def test_later_file_context_source_is_validated_before_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "valid").write_bytes(b"first")
    (tmp_path / "invalid").write_bytes(b"\xff")

    def unexpected_handoff(boundary: Path, spool: io.BufferedRandom) -> int:
        pytest.fail("Codex must not receive Context after any source fails validation")

    monkeypatch.setattr(factory, "select_harness", lambda _name: _selected_harness(unexpected_handoff))
    monkeypatch.setattr(sys, "argv", [
        "factory", "--path", str(tmp_path), "--file", "valid", "--file", "invalid", "Fix",
    ])
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"")))
    monkeypatch.chdir(tmp_path)

    assert factory.main() == 1
    assert 'Context file "invalid" is not valid UTF-8' in capsys.readouterr().err


def test_crlf_blank_lines_need_no_added_separator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / ".agent").mkdir()
    (tmp_path / ".agent" / "spec.md").write_bytes(b"guide\r\n\r\n")
    (tmp_path / "context").write_bytes(b"context\r\n\r\n")
    received: list[bytes] = []

    def handoff(boundary: Path, spool: io.BufferedRandom) -> int:
        received.append(spool.read())
        return 0

    monkeypatch.setattr(factory, "select_harness", lambda _name: _selected_harness(handoff))
    monkeypatch.setattr(sys, "argv", ["factory", "--path", str(tmp_path),
                                      "--file", "context", "Fix"])
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"next")))
    monkeypatch.chdir(tmp_path)

    assert factory.main() == 0
    assert received == [
        (
            b"# Project Guidance\n\nguide\r\n\r\n# Run Request\n\n"
            b"## Instruction\n\nFix\n\n## Context\n\n"
            b"### File: \"context\"\n\ncontext\r\n\r\n"
            b"### Standard Input\n\nnext"
        )
    ]


def test_spool_creation_failure_identifies_spool_and_cause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)

    def fail_spool(*args: object, **kwargs: object) -> io.BufferedRandom:
        raise OSError("temporary storage unavailable")

    monkeypatch.setattr(factory.tempfile, "TemporaryFile", fail_spool)
    monkeypatch.setattr(sys, "argv", ["factory", "--path", str(tmp_path), "Fix"])
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"")))
    monkeypatch.chdir(tmp_path)

    assert factory.main() == 1
    assert capsys.readouterr().err == (
        "factory: error: Run Request spool failed: temporary storage unavailable.\n"
    )


def test_spool_write_failure_identifies_spool_and_refuses_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / ".agent").mkdir()
    (tmp_path / ".agent" / "spec.md").write_text("guidance")

    class FailingSpool(io.BytesIO):
        def write(self, data: bytes) -> int:
            if data == b"gui":
                raise OSError("disk full")
            return super().write(data)

    def unexpected_handoff(boundary: Path, spool: io.BufferedRandom) -> int:
        pytest.fail("Codex must not run after a spool write failure")

    monkeypatch.setattr(factory.tempfile, "TemporaryFile", lambda **kwargs: FailingSpool())
    monkeypatch.setattr(factory, "select_harness", lambda _name: _selected_harness(unexpected_handoff))
    monkeypatch.setattr(sys, "argv", ["factory", "--path", str(tmp_path), "Fix"])
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"")))
    monkeypatch.chdir(tmp_path)

    assert factory.main() == 1
    assert capsys.readouterr().err == "factory: error: Run Request spool failed: disk full.\n"


def test_instruction_crlf_blank_line_needs_no_added_separator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "context").write_bytes(b"detail")
    received: list[bytes] = []

    def handoff(boundary: Path, spool: io.BufferedRandom) -> int:
        received.append(spool.read())
        return 0

    monkeypatch.setattr(factory, "select_harness", lambda _name: _selected_harness(handoff))
    monkeypatch.setattr(sys, "argv", ["factory", "--path", str(tmp_path),
                                      "--file", "context", "Fix\r\n\r\n"])
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"")))
    monkeypatch.chdir(tmp_path)

    assert factory.main() == 0
    assert received == [
        (
            b"# Run Request\n\n## Instruction\n\nFix\r\n\r\n"
            b"## Context\n\n### File: \"context\"\n\ndetail"
        )
    ]
