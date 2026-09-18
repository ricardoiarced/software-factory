"""Run Request behavior at the Factory command boundary."""

import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

import factory
from _codex import RunAdmissionError


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
    received: list[tuple[Path, bytes]] = []

    def handoff(boundary: Path, spool: io.BufferedRandom) -> int:
        received.append((boundary, spool.read()))
        return 0

    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    monkeypatch.setattr(factory, "_codex_run", handoff)
    monkeypatch.chdir(project)
    for name in ("Api", "api"):
        monkeypatch.setattr(sys, "argv", ["factory", "--project", name, "Fix"])
        monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"")))
        assert factory.main() == 0

    assert received == [
        (service, b"# Project Guidance\n\nservice guidance\n\n"
         b"# Run Request\n\n## Instruction\n\nFix"),
        (other, b"# Run Request\n\n## Instruction\n\nFix"),
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
    boundaries: list[Path] = []

    def handoff(boundary: Path, spool: io.BufferedRandom) -> int:
        boundaries.append(boundary)
        return 0

    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    monkeypatch.setattr(factory, "_codex_run", handoff)
    monkeypatch.setattr(sys, "argv", ["factory", "--project=api", "Fix"])
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"")))
    monkeypatch.chdir(project)

    assert factory.main() == 0
    service.rename(project / "moved")
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"")))
    assert factory.main() == 1
    assert boundaries == [service]
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
    monkeypatch.setattr(factory, "_codex_run", unexpected_handoff)
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

    monkeypatch.setattr(factory, "_codex_run", unexpected_handoff)
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
    original_project_spec = factory._project_spec

    def swap_after_check(boundary: Path) -> Path | None:
        spec = original_project_spec(boundary)
        guidance.rename(tmp_path / "old-guidance")
        guidance.symlink_to(outside, target_is_directory=True)
        return spec

    def unexpected_handoff(boundary: Path, spool: io.BufferedRandom) -> int:
        pytest.fail("Codex must not receive Project Guidance after a path swap")

    monkeypatch.setattr(factory, "_project_spec", swap_after_check)
    monkeypatch.setattr(factory, "_codex_run", unexpected_handoff)
    monkeypatch.setattr(sys, "argv", ["factory", "--path", str(tmp_path), "Fix"])
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"")))
    monkeypatch.chdir(tmp_path)

    assert factory.main() == 1
    assert "Project Spec cannot be opened" in capsys.readouterr().err


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


def test_context_path_with_nul_refuses_without_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)

    def unexpected_handoff(boundary: Path, spool: io.BufferedRandom) -> int:
        pytest.fail("Codex must not receive Context from an invalid file path")

    monkeypatch.setattr(factory, "_codex_run", unexpected_handoff)
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

    def handoff(selected: Path, spool: io.BufferedRandom) -> int:
        assert selected == boundary
        received.append(spool.read())
        return 0

    monkeypatch.setenv("CONTEXT_FILE", "../../outside.txt")
    monkeypatch.setenv("FACTORY_CONTEXT_WARN_BYTES", "16")
    monkeypatch.setattr(factory, "_codex_run", handoff)
    monkeypatch.setattr(sys, "argv", ["factory", "--path", str(boundary),
                                      "--file", "$CONTEXT_FILE", "--file", "linked.txt", "Fix"])
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"")))
    monkeypatch.chdir(invocation)

    assert factory.main() == 0
    assert received == [
        b'# Run Request\n\n## Instruction\n\nFix\n\n## Context\n\n'
        b'### File: "$CONTEXT_FILE"\n\n\xef\xbb\xbfA\r\nB\r\n\n'
        b'### File: "linked.txt"\n\n\xef\xbb\xbfA\r\nB\r'
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

    monkeypatch.setattr(factory, "_codex_run", unexpected_handoff)
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

    monkeypatch.setattr(factory, "_codex_run", unexpected_handoff)
    monkeypatch.setattr(sys, "argv", ["factory", "--file", "directory", "Fix"])
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"")))
    monkeypatch.chdir(tmp_path)

    assert factory.main() == 1
    assert 'Context file "directory" is not a regular file' in capsys.readouterr().err


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

    monkeypatch.setattr(factory, "_codex_run", handoff)
    monkeypatch.setattr(sys, "argv", [
        "factory", "--path", str(tmp_path), "--file", "empty", "--file", "mixed",
        "--file", "bom-only", "--file", "unicode", "--file", "mixed",
        "--file", "trailing", "--file", "empty", "Fix",
    ])
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"")))
    monkeypatch.chdir(tmp_path)

    assert factory.main() == 0
    assert received == [
        b'# Run Request\n\n## Instruction\n\nFix\n\n## Context\n\n'
        b'### File: "mixed"\n\nA\r\nB\nC\r\n\n'
        b'### File: "unicode"\n\ncaf\xc3\xa9\r\n\n'
        b'### File: "mixed"\n\nA\r\nB\nC\r\n\n'
        b'### File: "trailing"\n\nend\n\n'
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

    monkeypatch.setattr(factory, "_codex_run", handoff)
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

    monkeypatch.setattr(factory, "_codex_run", unexpected_handoff)
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
    monkeypatch.setattr(factory, "_codex_run", unexpected_handoff)
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
