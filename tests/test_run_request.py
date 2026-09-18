"""Run Request behavior at the Factory command boundary."""

import io
import subprocess
import sys
from pathlib import Path

import pytest

import factory
from _codex import RunAdmissionError


def test_run_without_selector_uses_containing_worktree_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    nested = tmp_path / "service" / "src"
    nested.mkdir(parents=True)
    boundaries: list[Path] = []

    def handoff(boundary: Path, spool: io.BufferedRandom) -> int:
        boundaries.append(boundary)
        return 0

    monkeypatch.setattr(factory, "_codex_run", handoff)
    monkeypatch.setattr(sys, "argv", ["factory", "Fix"])
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"")))
    monkeypatch.chdir(nested)

    assert factory.main() == 0
    assert boundaries == [tmp_path]


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
    received: list[tuple[Path, bytes]] = []

    def handoff(boundary: Path, spool: io.BufferedRandom) -> int:
        received.append((boundary, spool.read()))
        return 0

    monkeypatch.setattr(factory, "_codex_run", handoff)
    monkeypatch.setattr(sys, "argv", ["factory", "Fix"])
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"")))
    monkeypatch.chdir(invocation)

    assert factory.main() == 0
    assert received == [(service, b"# Run Request\n\n## Instruction\n\nFix")]


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

    monkeypatch.setattr(factory, "_codex_run", handoff)
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
    monkeypatch.setattr(factory, "_codex_run", unexpected_handoff)
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
    received: list[tuple[Path, bytes]] = []

    def handoff(boundary: Path, spool: io.BufferedRandom) -> int:
        received.append((boundary, spool.read()))
        return 0

    monkeypatch.setattr(factory, "_codex_run", handoff)
    monkeypatch.setattr(sys, "argv", ["factory", "Fix"])
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"")))
    monkeypatch.chdir(invocation)

    assert factory.main() == 0
    assert received == [(
        service,
        b"# Project Guidance\n\nservice guidance\n\n"
        b"# Run Request\n\n## Instruction\n\nFix",
    )]


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
    received: list[tuple[Path, bytes]] = []

    def handoff(boundary: Path, spool: io.BufferedRandom) -> int:
        received.append((boundary, spool.read()))
        return 0

    monkeypatch.setattr(factory, "_codex_run", handoff)
    monkeypatch.setattr(sys, "argv", ["factory", "Fix"])
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"")))
    monkeypatch.chdir(nested)

    assert factory.main() == 0
    assert received == [(inner, b"# Run Request\n\n## Instruction\n\nFix")]


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
    boundaries: list[Path] = []

    def handoff(boundary: Path, spool: io.BufferedRandom) -> int:
        boundaries.append(boundary)
        return 0

    monkeypatch.setattr(factory, "_codex_run", handoff)
    monkeypatch.setattr(sys, "argv", ["factory", "Fix"])
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"")))
    monkeypatch.chdir(nested)

    assert factory.main() == 0
    assert boundaries == [root]


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

    def handoff(boundary: Path, spool: io.BufferedRandom) -> int:
        assert boundary == tmp_path
        requests.append(spool.read())
        return 7

    monkeypatch.setattr(sys, "argv", ["factory", "--path", str(tmp_path),
                                      "--file", "notes.txt", "Fix", "it"])
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"stdin")))
    monkeypatch.setattr(factory, "_codex_run", handoff, raising=False)
    monkeypatch.chdir(tmp_path)

    assert factory.main() == 7
    assert requests == [
        b"# Project Guidance\n\nGuide\r\n\n# Run Request\n\n"
        b"## Instruction\n\nFix it\n\n## Context\n\n"
        b"### File: \"notes.txt\"\n\nFirst\n\n"
        b"### Standard Input\n\nstdin"
    ]
    assert capsys.readouterr().err == ""


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

    monkeypatch.setattr(factory, "_codex_run", handoff)
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


def test_context_gate_counts_utf8_bytes_and_force_only_passes_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    import subprocess

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

    monkeypatch.setattr(factory, "_codex_run", refuse)
    monkeypatch.setattr(sys, "argv", ["factory", "--path", str(tmp_path), "Fix"])
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"")))
    monkeypatch.chdir(tmp_path)

    assert factory.main() == 1
    assert len(spools) == 1 and spools[0].closed


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

    def handoff(project: Path, spool: io.BufferedRandom) -> int:
        assert project == boundary
        received.append(spool.read())
        return 0

    monkeypatch.setattr(factory, "_codex_run", handoff)
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


@pytest.mark.parametrize("setting", ["", "-1", "+1", " 1", "1 ", "１２", "1.0"])
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

    monkeypatch.setattr(factory, "_codex_run", handoff)
    monkeypatch.setattr(sys, "argv", ["factory", "--path", str(tmp_path),
                                      "--file", name, "--file", name, "Fix"])
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"")))
    monkeypatch.chdir(tmp_path)

    assert factory.main() == 0
    assert received == [
        b'# Run Request\n\n## Instruction\n\nFix\n\n## Context\n\n'
        b'### File: "line\\n\\"quote\\".txt"\n\nx\r\ny\r\n\n'
        b'### File: "line\\n\\"quote\\".txt"\n\nx\r\ny\r'
    ]


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

    monkeypatch.setattr(factory, "_codex_run", handoff)
    monkeypatch.setattr(sys, "argv", ["factory", "--path", str(tmp_path),
                                      "--file", "context", "Fix"])
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"next")))
    monkeypatch.chdir(tmp_path)

    assert factory.main() == 0
    assert received == [
        b"# Project Guidance\n\nguide\r\n\r\n# Run Request\n\n"
        b"## Instruction\n\nFix\n\n## Context\n\n"
        b"### File: \"context\"\n\ncontext\r\n\r\n"
        b"### Standard Input\n\nnext"
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

    monkeypatch.setattr(factory, "_codex_run", handoff)
    monkeypatch.setattr(sys, "argv", ["factory", "--path", str(tmp_path),
                                      "--file", "context", "Fix\r\n\r\n"])
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"")))
    monkeypatch.chdir(tmp_path)

    assert factory.main() == 0
    assert received == [
        b"# Run Request\n\n## Instruction\n\nFix\r\n\r\n"
        b"## Context\n\n### File: \"context\"\n\ndetail"
    ]
