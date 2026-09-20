"""Deterministic Codex Run Admission behavior."""

import errno
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import time
import tomllib

import pytest

import factory
from _codex import (
    _codex_0_155_1_invocation,
    _codex_0_155_1_permission_profile,
    _CompatibilityEntry,
    _match_compatibility,
    RunAdmissionError,
    run,
)


def make_executable(path: Path, version: str = "0.154.0") -> None:
    path.write_text(f"#!/bin/sh\nif [ \"$1\" = --version ]; then\n  echo 'codex {version}'\nfi\n")
    path.chmod(0o755)


def make_inspector_executable(path: Path) -> None:
    path.write_text("""#!/bin/sh
if [ "$1" = --version ]; then
  echo 'codex 0.155.1'
elif [ "$1:$2" = 'features:list' ]; then
  printf '%s' "$FACTORY_FEATURES"
  exit "${FACTORY_FEATURE_EXIT:-0}"
elif [ "$1:$2" = 'mcp:list' ]; then
  printf '%s' "$FACTORY_MCP"
  exit "${FACTORY_MCP_EXIT:-0}"
elif [ "$1" = sandbox ]; then
  printf 'factory-host-admission-probe-v1\n'
fi
""")
    path.chmod(0o755)


def safe_feature_output(active_feature: str | None = None) -> bytes:
    feature_lines = [
        ("apps", "stable", False),
        ("plugins", "stable", False),
        ("remote_plugin", "stable", False),
        ("browser_use", "stable", False),
        ("browser_use_external", "stable", False),
        ("browser_use_full_cdp", "stable", False),
        ("computer_use", "stable", False),
        ("multi_agent", "stable", False),
        ("memories", "stable", False),
        ("skip_host_skill_discovery", "under development", True),
    ]
    return b"".join(
        f"{name:<29}{stage:<20}{str(value or name == active_feature).lower()}\n".encode()
        for name, stage, value in feature_lines
    )


def certify(executable: Path, version: str = "0.154.0") -> _CompatibilityEntry:
    metadata = executable.stat()
    with executable.open("rb") as artifact:
        digest = hashlib.file_digest(artifact, "sha256").hexdigest()
    return _CompatibilityEntry(
        version=version,
        sha256=digest,
        operating_system=platform.system(),
        architecture=platform.machine(),
        canonical_path=executable,
        filesystem_identity=(metadata.st_dev, metadata.st_ino),
    )


def install_compatibility_fixture(
    executable: Path, monkeypatch: pytest.MonkeyPatch, version: str = "0.154.0",
) -> None:
    monkeypatch.setattr("_codex._COMPATIBILITY_SET", (certify(executable, version),))


def make_probe_executable(path: Path) -> None:
    path.write_text("""#!/bin/sh
if [ "$1" = --version ]; then
  echo 'codex 0.155.1'
elif [ "$1:$2" = 'features:list' ]; then
  printf '%s' "$FACTORY_FEATURES"
elif [ "$1:$2" = 'mcp:list' ]; then
  printf '[]\n'
elif [ "$1" = sandbox ]; then
  for argument in "$@"; do
    if [ "$argument" = "$FACTORY_SELECTED_PROJECT" ]; then
      exit 91
    fi
  done
  [ "$2" = -C ] || exit 92
  [ "$4" = -c ] || exit 92
  [ "$6:$7:$8" = '-P:factory-isolated:--' ] || exit 92
  [ "$9:${10}" = '/bin/sh:-c' ] || exit 92
  [ "${12}" = factory-host-admission-probe ] || exit 92
  [ "${13}" = "$3" ] || exit 92
  [ -f "${13}/inside-read" ] || exit 92
  [ "$(cat "${13}/inside-read")" = inside-boundary ] || exit 92
  [ -f "${13%/*}/parent-read" ] || exit 92
  [ -f "${13%/*}/sibling/outside-read" ] || exit 92
  case "${14}" in "${13}"/*) exit 92;; esac
  case "${15}" in /tmp/*) :;; *) exit 92;; esac
  [ "${16}" = "$0" ] || exit 92
  case "$5" in
    *'":root" = "deny"'*'":minimal" = "read"'*'":tmpdir" = "deny"'*) :;;
    *) exit 92;;
  esac
  case "$5" in
    *'":slash_tmp" = "deny"'*'":workspace_roots" = {"." = "write"}'*) :;;
    *) exit 92;;
  esac
  case "${11}" in
    *'read_exact "$inside_read"'*'can_write "$inside_write"'*) :;;
    *) exit 93;;
  esac
  case "${11}" in
    *'can_read "$parent_read"'*'can_write "$parent_write"'*) :;;
    *) exit 93;;
  esac
  case "${11}" in
    *'can_read "$sibling_read"'*'can_write "$sibling_write"'*) :;;
    *) exit 93;;
  esac
  case "${11}" in
    *'can_write "$temporary_write"'*'can_write "$slash_tmp_write"'*) :;;
    *) exit 93;;
  esac
  case "${11}" in
    *'can_read "/bin/sh"'*'can_read "$executable"'*) :;;
    *) exit 93;;
  esac
  : > "$FACTORY_PROBE_GATE"
  if [ -n "${FACTORY_PROBE_DESCENDANT_SLEEP:-}" ]; then
    (
      trap '' HUP TERM
      sleep "$FACTORY_PROBE_DESCENDANT_SLEEP"
      : > "$FACTORY_PROBE_DESCENDANT_SURVIVED"
    ) &
    wait
  elif [ -n "${FACTORY_PROBE_SLEEP:-}" ]; then
    sleep "$FACTORY_PROBE_SLEEP"
  fi
  if [ "${FACTORY_PROBE_STDOUT+x}" = x ]; then
    printf '%s' "$FACTORY_PROBE_STDOUT"
  else
    printf 'factory-host-admission-probe-v1\n'
  fi
  if [ "${FACTORY_PROBE_STDERR+x}" = x ]; then
    printf '%s' "$FACTORY_PROBE_STDERR" >&2
  fi
  exit "${FACTORY_PROBE_EXIT:-0}"
elif [ "$1" = exec ]; then
  if [ -n "${FACTORY_LAUNCH_GATE:-}" ]; then
    : > "$FACTORY_LAUNCH_GATE"
  fi
  if [ -f "$FACTORY_PROBE_GATE" ]; then
    exit 23
  fi
  exit 99
fi
""")
    path.chmod(0o755)


def prepare_probe_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, Path, Path]:
    boundary = tmp_path / "selected-project"
    boundary.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=boundary, check=True)
    executable = tmp_path / "bin" / "codex"
    executable.parent.mkdir()
    make_probe_executable(executable)
    probe_gate = tmp_path / "probe-ran"
    launch_gate = tmp_path / "run-launched"
    monkeypatch.setenv("PATH", os.pathsep.join((str(executable.parent), os.environ["PATH"])))
    monkeypatch.setenv("FACTORY_FEATURES", safe_feature_output().decode("ascii"))
    monkeypatch.setenv("FACTORY_SELECTED_PROJECT", str(boundary))
    monkeypatch.setenv("FACTORY_PROBE_GATE", str(probe_gate))
    monkeypatch.setenv("FACTORY_LAUNCH_GATE", str(launch_gate))
    install_compatibility_fixture(executable, monkeypatch, version="0.155.1")
    return boundary, probe_gate, launch_gate


def test_codex_0_155_1_permission_profile_is_deterministic_and_exact(tmp_path: Path) -> None:
    executable = tmp_path / "Codex Runtime" / "codex"

    profile = _codex_0_155_1_permission_profile(executable)

    assert profile == (
        '{extends = ":workspace", filesystem = {":root" = "deny", '
        '":minimal" = "read", ":tmpdir" = "deny", ":slash_tmp" = "deny", '
        f'{json.dumps(str(executable.parent.absolute()), ensure_ascii=False)} = "read", '
        '":workspace_roots" = {"." = "write"}}, network = {enabled = false}}'
    )
    assert _codex_0_155_1_permission_profile(executable) == profile


def test_codex_0_155_1_permission_profile_allows_only_the_required_access(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "runtime" / "codex"

    profile = tomllib.loads(
        "profile = " + _codex_0_155_1_permission_profile(executable),
    )["profile"]

    assert profile == {
        "extends": ":workspace",
        "filesystem": {
            ":root": "deny",
            ":minimal": "read",
            ":tmpdir": "deny",
            ":slash_tmp": "deny",
            str(executable.parent.absolute()): "read",
            ":workspace_roots": {".": "write"},
        },
        "network": {"enabled": False},
    }


def test_codex_0_155_1_permission_profile_reopens_the_canonical_executable_directory(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "installation" / "codex"
    executable.parent.mkdir()
    executable.write_text("codex")
    linked_executable = tmp_path / "bin" / "codex"
    linked_executable.parent.mkdir()
    linked_executable.symlink_to(executable)

    profile = tomllib.loads(
        "profile = " + _codex_0_155_1_permission_profile(linked_executable),
    )["profile"]

    assert profile["filesystem"][str(executable.parent)] == "read"
    assert str(linked_executable.parent) not in profile["filesystem"]


def test_codex_0_155_1_invocation_is_strict_and_version_specific(tmp_path: Path) -> None:
    executable = tmp_path / "Codex Runtime" / "codex"
    boundary = tmp_path / "Project Boundary"
    git_root = tmp_path / "Git Root"

    invocation = _codex_0_155_1_invocation(executable, boundary, git_root)

    assert invocation == [
        os.fspath(executable.resolve()),
        "exec",
        "--ephemeral",
        "--strict-config",
        "--ignore-user-config",
        "--ignore-rules",
        "-C",
        os.fspath(boundary),
        "-c", "approval_policy=\"never\"",
        "-c", "project_doc_max_bytes=0",
        "-c", "project_root_markers=[]",
        "-c", "allow_login_shell=false",
        "-c", "shell_environment_policy.inherit=\"core\"",
        "-c", "shell_environment_policy.ignore_default_excludes=false",
        "-c", "features.apps=false",
        "-c", "features.plugins=false",
        "-c", "features.remote_plugin=false",
        "-c", "features.browser_use=false",
        "-c", "features.browser_use_external=false",
        "-c", "features.browser_use_full_cdp=false",
        "-c", "features.computer_use=false",
        "-c", "features.multi_agent=false",
        "-c", "features.memories=false",
        "-c", "features.skip_host_skill_discovery=true",
        "-c", "default_permissions=\"factory-isolated\"",
        "-c", "permissions.factory-isolated=" + _codex_0_155_1_permission_profile(executable),
        "-c", (
            "projects."
            + json.dumps(os.fspath(git_root), ensure_ascii=False)
            + ".trust_level=\"untrusted\""
        ),
        "-",
    ]


def test_run_launches_the_certified_codex_with_matching_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "bin" / "codex"
    executable.parent.mkdir()
    make_executable(executable, version="0.155.1")
    boundary = tmp_path / "project"
    boundary.mkdir()
    git_root = tmp_path / "git root"
    git_root.mkdir()
    spool = io.BytesIO(b"# Run Request\n")
    monkeypatch.setenv("PATH", os.fspath(executable.parent))
    monkeypatch.setattr("_codex._COMPATIBILITY_SET", (certify(executable),))
    monkeypatch.setattr("_codex._match_compatibility", lambda path: certify(executable))
    monkeypatch.setattr("_codex._enclosing_git_root", lambda path: git_root)
    monkeypatch.setattr("_codex._inspect_strict_configuration", lambda *args: None)
    monkeypatch.setattr("_codex._run_host_admission_probe", lambda *args: None)

    calls: list[tuple[list[str], Path, io.BytesIO]] = []

    class Process:
        def wait(self) -> int:
            return 17

    def launch(command: list[str], *, cwd: Path, stdin: io.BytesIO) -> Process:
        calls.append((command, cwd, stdin))
        return Process()

    monkeypatch.setattr("_codex.subprocess.Popen", launch)

    assert run(boundary, spool) == 17
    assert calls == [(
        _codex_0_155_1_invocation(executable, boundary, git_root), boundary, spool,
    )]


def test_run_requires_a_successful_synthetic_probe_before_launching_codex(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    boundary, probe_gate, launch_gate = prepare_probe_run(tmp_path, monkeypatch)

    spool = tmp_path / "run-request"
    spool.write_bytes(b"# Run Request\n")
    with spool.open("rb") as request:
        assert run(boundary, request) == 23
    assert probe_gate.is_file()
    assert launch_gate.is_file()


@pytest.mark.parametrize(("stdout", "stderr", "exit_status", "reason"), [
    ("", "", "0", "unexpected output"),
    ("factory-host-admission-probe-v1", "", "0", "unexpected output"),
    (
        "factory-host-admission-probe-v1\nfactory-host-admission-probe-v1\n",
        "", "0", "unexpected output",
    ),
    ("unexpected\n", "", "0", "unexpected output"),
    ("factory-host-admission-probe-v1\n", "warning\n", "0", "unexpected output"),
    ("factory-host-admission-probe-v1\n", "", "7", "exit status 7"),
])
def test_run_refuses_partial_ambiguous_or_failed_host_probe_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    stdout: str, stderr: str, exit_status: str, reason: str,
) -> None:
    boundary, probe_gate, launch_gate = prepare_probe_run(tmp_path, monkeypatch)
    monkeypatch.setenv("FACTORY_PROBE_STDOUT", stdout)
    monkeypatch.setenv("FACTORY_PROBE_STDERR", stderr)
    monkeypatch.setenv("FACTORY_PROBE_EXIT", exit_status)

    with pytest.raises(RunAdmissionError, match=reason):
        run(boundary, io.BytesIO())

    assert probe_gate.is_file()
    assert not launch_gate.exists()


def test_run_refuses_a_timed_out_host_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    boundary, probe_gate, launch_gate = prepare_probe_run(tmp_path, monkeypatch)
    descendant_survived = tmp_path / "probe-descendant-survived"
    monkeypatch.setenv("FACTORY_PROBE_DESCENDANT_SLEEP", "0.1")
    monkeypatch.setenv("FACTORY_PROBE_DESCENDANT_SURVIVED", str(descendant_survived))
    monkeypatch.setattr("_codex._HOST_PROBE_TIMEOUT_SECONDS", 0.01)

    started = time.monotonic()
    with pytest.raises(RunAdmissionError, match="Host Admission Probe failed: timed out"):
        run(boundary, io.BytesIO())

    assert time.monotonic() - started < 0.5
    time.sleep(0.2)
    assert probe_gate.is_file()
    assert not launch_gate.exists()
    assert not descendant_survived.exists()


def test_run_refuses_when_synthetic_probe_data_cannot_be_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    boundary, probe_gate, launch_gate = prepare_probe_run(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "_codex.tempfile.TemporaryDirectory",
        lambda **kwargs: (_ for _ in ()).throw(OSError(errno.ENOSPC, "No space left")),
    )
    with pytest.raises(
        RunAdmissionError, match="Host Admission Probe failed: No space left",
    ):
        run(boundary, io.BytesIO())

    assert not probe_gate.exists()
    assert not launch_gate.exists()


def test_run_requires_complete_strict_configuration_inspection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "bin" / "codex"
    executable.parent.mkdir()
    make_executable(executable, version="0.155.1")
    boundary = tmp_path / "project"
    boundary.mkdir()
    git_root = tmp_path / "git-root"
    git_root.mkdir()
    monkeypatch.setenv("PATH", os.fspath(executable.parent))
    install_compatibility_fixture(executable, monkeypatch)
    monkeypatch.setattr("_codex._match_compatibility", lambda path: certify(executable))
    monkeypatch.setattr("_codex._enclosing_git_root", lambda path: git_root)

    inspected: list[list[str]] = []

    def inspect(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        inspected.append(command)
        if command[1:3] == ["features", "list"]:
            return subprocess.CompletedProcess(command, 0, (
                b"apps                         stable             false\n"
                b"plugins                      stable             false\n"
                b"remote_plugin                stable             false\n"
                b"browser_use                  stable             false\n"
                b"browser_use_external         stable             false\n"
                b"browser_use_full_cdp         stable             false\n"
                b"computer_use                 stable             false\n"
                b"multi_agent                  stable             false\n"
                b"memories                     stable             false\n"
                b"skip_host_skill_discovery    under development  true\n"
            ), b"")
        if command[1:3] == ["mcp", "list"]:
            return subprocess.CompletedProcess(command, 0, b"[]\n", b"")
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr("_codex.subprocess.run", inspect)
    monkeypatch.setattr("_codex._run_host_admission_probe", lambda *args: None)
    monkeypatch.setattr("_codex.subprocess.Popen", lambda *args, **kwargs: type(
        "Process", (), {"wait": lambda self: 0},
    )())

    assert run(boundary, io.BytesIO()) == 0
    assert inspected == [
        [
            os.fspath(executable), "features", "list",
            "-c", 'approval_policy="never"',
            "-c", "project_doc_max_bytes=0",
            "-c", "project_root_markers=[]",
            "-c", "allow_login_shell=false",
            "-c", 'shell_environment_policy.inherit="core"',
            "-c", "shell_environment_policy.ignore_default_excludes=false",
            "-c", "features.apps=false",
            "-c", "features.plugins=false",
            "-c", "features.remote_plugin=false",
            "-c", "features.browser_use=false",
            "-c", "features.browser_use_external=false",
            "-c", "features.browser_use_full_cdp=false",
            "-c", "features.computer_use=false",
            "-c", "features.multi_agent=false",
            "-c", "features.memories=false",
            "-c", "features.skip_host_skill_discovery=true",
            "-c", "default_permissions=\"factory-isolated\"",
            "-c", "permissions.factory-isolated="
            + _codex_0_155_1_permission_profile(executable),
            "-c", "projects."
            + json.dumps(os.fspath(git_root), ensure_ascii=False)
            + ".trust_level=\"untrusted\"",
        ],
        [
            os.fspath(executable), "mcp", "list", "--json",
            "-c", 'approval_policy="never"',
            "-c", "project_doc_max_bytes=0",
            "-c", "project_root_markers=[]",
            "-c", "allow_login_shell=false",
            "-c", "shell_environment_policy.inherit=\"core\"",
            "-c", "shell_environment_policy.ignore_default_excludes=false",
            "-c", "features.apps=false",
            "-c", "features.plugins=false",
            "-c", "features.remote_plugin=false",
            "-c", "features.browser_use=false",
            "-c", "features.browser_use_external=false",
            "-c", "features.browser_use_full_cdp=false",
            "-c", "features.computer_use=false",
            "-c", "features.multi_agent=false",
            "-c", "features.memories=false",
            "-c", "features.skip_host_skill_discovery=true",
            "-c", "default_permissions=\"factory-isolated\"",
            "-c", "permissions.factory-isolated="
            + _codex_0_155_1_permission_profile(executable),
            "-c", "projects."
            + json.dumps(os.fspath(git_root), ensure_ascii=False)
            + ".trust_level=\"untrusted\"",
        ],
    ]


@pytest.mark.parametrize("features, mcp", [
    (b"apps stable false\n", b"[]\n"),
    (b"apps                         stable             true\n", b"[]\n"),
    (b"apps                         stable             false unexpected\n", b"[]\n"),
    (b"apps                         stable             false\n" * 2, b"[]\n"),
    (b"apps                         stable             false\n", b"{}\n"),
    (b"apps                         stable             false\n", b"[{\"name\": \"unexpected\"}]\n"),
])
def test_run_refuses_incomplete_or_unrecognized_inspector_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, features: bytes, mcp: bytes,
) -> None:
    executable = tmp_path / "bin" / "codex"
    executable.parent.mkdir()
    make_executable(executable, version="0.155.1")
    monkeypatch.setenv("PATH", os.fspath(executable.parent))
    install_compatibility_fixture(executable, monkeypatch)
    monkeypatch.setattr("_codex._match_compatibility", lambda path: certify(executable))
    monkeypatch.setattr("_codex._enclosing_git_root", lambda path: tmp_path)
    launched = False

    def inspect(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        output = features if command[1:3] == ["features", "list"] else mcp
        return subprocess.CompletedProcess(command, 0, output, b"")

    def launch(*args: object, **kwargs: object) -> None:
        nonlocal launched
        launched = True

    monkeypatch.setattr("_codex.subprocess.run", inspect)
    monkeypatch.setattr("_codex.subprocess.Popen", launch)

    with pytest.raises(RunAdmissionError, match="strict configuration"):
        run(tmp_path, io.BytesIO())

    assert not launched


@pytest.mark.parametrize("forbidden_feature", [
    "apps",
    "plugins",
    "remote_plugin",
    "browser_use",
    "browser_use_external",
    "browser_use_full_cdp",
    "computer_use",
    "multi_agent",
    "memories",
])
def test_run_refuses_each_active_effective_capability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, forbidden_feature: str,
) -> None:
    executable = tmp_path / "bin" / "codex"
    executable.parent.mkdir()
    make_executable(executable, version="0.155.1")
    monkeypatch.setenv("PATH", os.fspath(executable.parent))
    install_compatibility_fixture(executable, monkeypatch)
    monkeypatch.setattr("_codex._match_compatibility", lambda path: certify(executable))
    monkeypatch.setattr("_codex._enclosing_git_root", lambda path: tmp_path)
    launched = False
    features = safe_feature_output(forbidden_feature)

    def inspect(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        output = features if command[1:3] == ["features", "list"] else b"[]\n"
        return subprocess.CompletedProcess(command, 0, output, b"")

    def launch(*args: object, **kwargs: object) -> None:
        nonlocal launched
        launched = True

    monkeypatch.setattr("_codex.subprocess.run", inspect)
    monkeypatch.setattr("_codex.subprocess.Popen", launch)

    with pytest.raises(
        RunAdmissionError, match=f"forbidden capability {forbidden_feature}",
    ):
        run(tmp_path, io.BytesIO())

    assert not launched


@pytest.mark.parametrize("features, mcp, expected_reason", [
    (
        safe_feature_output(),
        b'[{"name": "managed-server"}]\n',
        "active MCP servers",
    ),
    (b"hooks                        stable             true\n", b"[]\n", "unrecognized feature output"),
])
def test_run_refuses_active_or_ambiguous_extension_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, features: bytes, mcp: bytes,
    expected_reason: str,
) -> None:
    executable = tmp_path / "bin" / "codex"
    executable.parent.mkdir()
    make_executable(executable, version="0.155.1")
    monkeypatch.setenv("PATH", os.fspath(executable.parent))
    install_compatibility_fixture(executable, monkeypatch)
    monkeypatch.setattr("_codex._match_compatibility", lambda path: certify(executable))
    monkeypatch.setattr("_codex._enclosing_git_root", lambda path: tmp_path)
    monkeypatch.setattr(
        "_codex.subprocess.run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command, 0, features if command[1:3] == ["features", "list"] else mcp, b"",
        ),
    )

    with pytest.raises(RunAdmissionError, match=expected_reason):
        run(tmp_path, io.BytesIO())


def test_force_and_hidden_environment_state_cannot_bypass_capability_rejection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    executable = tmp_path / "bin" / "codex"
    executable.parent.mkdir()
    make_inspector_executable(executable)
    monkeypatch.setenv("PATH", os.pathsep.join((str(executable.parent), os.environ["PATH"])))
    # The executable's effective result contradicts Factory's runtime disable,
    # modelling a higher-precedence hidden or environment policy.
    monkeypatch.setenv("FACTORY_FEATURES", safe_feature_output("apps").decode("ascii"))
    monkeypatch.setenv("FACTORY_MCP", "[]\n")
    monkeypatch.setenv("FACTORY_CONTEXT_WARN_BYTES", "0")
    install_compatibility_fixture(executable, monkeypatch)
    monkeypatch.setattr("_codex._match_compatibility", lambda path: certify(executable))
    (tmp_path / "context").write_text("context")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        sys, "argv", ["factory", "--path", str(tmp_path), "--file", "context", "--force", "Fix"],
    )
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"")))

    assert factory.main() == 1
    assert "forbidden capability apps is active" in capsys.readouterr().err


@pytest.mark.parametrize("features, mcp, environment", [
    (
        b"apps                         stable             false\n"
        b"plugins                      stable             false\n"
        b"remote_plugin                stable             false\n"
        b"browser_use                  stable             false\n"
        b"browser_use_external         stable             false\n"
        b"browser_use_full_cdp         stable             false\n"
        b"computer_use                 stable             false\n"
        b"multi_agent                  stable             false\n"
        b"memories                     stable             false\n"
        b"skip_host_skill_discovery    under development  true\n",
        b"[]\n", {},
    ),
    (
        b"apps                         stable             false\n"
        b"plugins                      stable             false\n"
        b"remote_plugin                stable             false\n"
        b"browser_use                  stable             false\n"
        b"browser_use_external         stable             false\n"
        b"browser_use_full_cdp         stable             false\n"
        b"computer_use                 stable             false\n"
        b"multi_agent                  stable             false\n"
        b"memories                     stable             false\n"
        b"skip_host_skill_discovery    under development  true\n"
        b"future_feature               stable             true\n",
        b"[]\n", {},
    ),
    (b"apps                         stable             true\n", b"[]\n", {}),
    (b"apps                         stable             false\n", b"[]\n", {"FACTORY_FEATURE_EXIT": "1"}),
    (b"apps                         stable             false\n", b"not json\n", {}),
])
def test_run_uses_executable_fakes_for_strict_inspector_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, features: bytes, mcp: bytes,
    environment: dict[str, str],
) -> None:
    executable = tmp_path / "bin" / "codex"
    executable.parent.mkdir()
    make_inspector_executable(executable)
    monkeypatch.setenv("PATH", os.fspath(executable.parent))
    monkeypatch.setenv("FACTORY_FEATURES", features.decode("ascii"))
    monkeypatch.setenv("FACTORY_MCP", mcp.decode("ascii"))
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    install_compatibility_fixture(executable, monkeypatch)
    monkeypatch.setattr("_codex._match_compatibility", lambda path: certify(executable))
    monkeypatch.setattr("_codex._enclosing_git_root", lambda path: tmp_path)

    if environment:
        with pytest.raises(RunAdmissionError, match="strict configuration"):
            run(tmp_path, io.BytesIO())
    elif (
        features.startswith(b"apps                         stable             false\n")
        and b"future_feature" not in features
        and mcp == b"[]\n"
    ):
        spool = tmp_path / "request"
        spool.write_bytes(b"# Run Request\n")
        with spool.open("rb") as request:
            assert run(tmp_path, request) == 0
    else:
        with pytest.raises(RunAdmissionError, match="strict configuration"):
            run(tmp_path, io.BytesIO())


def test_identity_matches_an_exact_private_compatibility_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "codex"
    make_executable(executable)
    install_compatibility_fixture(executable, monkeypatch)

    assert _match_compatibility(executable) == certify(executable)


def test_identity_refuses_malformed_version_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "codex"
    make_executable(executable)
    install_compatibility_fixture(executable, monkeypatch)
    monkeypatch.setattr(
        "_codex.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, b"0.154.0\n", b""),
    )

    with pytest.raises(RunAdmissionError, match="reported an unrecognized version"):
        _match_compatibility(executable)


def test_identity_refuses_a_changed_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "codex"
    make_executable(executable)
    install_compatibility_fixture(executable, monkeypatch)
    executable.write_text(executable.read_text() + "# changed\n")
    executable.chmod(0o755)

    with pytest.raises(RunAdmissionError, match="SHA-256 does not match"):
        _match_compatibility(executable)


def test_identity_refuses_another_platform(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "codex"
    make_executable(executable)
    entry = certify(executable)
    monkeypatch.setattr(
        "_codex._COMPATIBILITY_SET",
        (_CompatibilityEntry(
            version=entry.version,
            sha256=entry.sha256,
            operating_system="other-os",
            architecture=entry.architecture,
            canonical_path=entry.canonical_path,
            filesystem_identity=entry.filesystem_identity,
        ),),
    )

    with pytest.raises(RunAdmissionError, match="platform does not match"):
        _match_compatibility(executable)


def test_identity_refuses_another_architecture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "codex"
    make_executable(executable)
    entry = certify(executable)
    monkeypatch.setattr(
        "_codex._COMPATIBILITY_SET",
        (_CompatibilityEntry(
            version=entry.version,
            sha256=entry.sha256,
            operating_system=entry.operating_system,
            architecture="other-architecture",
            canonical_path=entry.canonical_path,
            filesystem_identity=entry.filesystem_identity,
        ),),
    )

    with pytest.raises(RunAdmissionError, match="platform does not match"):
        _match_compatibility(executable)


def test_identity_uses_the_canonical_target_of_a_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "installation" / "codex"
    executable.parent.mkdir()
    make_executable(executable)
    symlink = tmp_path / "bin" / "codex"
    symlink.parent.mkdir()
    symlink.symlink_to(executable)
    install_compatibility_fixture(executable, monkeypatch)

    assert _match_compatibility(symlink.resolve()) == certify(executable)


def test_identity_refuses_a_replaced_filesystem_object(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "codex"
    make_executable(executable)
    install_compatibility_fixture(executable, monkeypatch)
    replacement = tmp_path / "replacement"
    shutil.copyfile(executable, replacement)
    replacement.chmod(0o755)
    replacement.replace(executable)

    with pytest.raises(RunAdmissionError, match="filesystem identity does not match"):
        _match_compatibility(executable)


def test_identity_refuses_replacement_between_version_and_artifact_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "codex"
    make_executable(executable)
    install_compatibility_fixture(executable, monkeypatch)
    replacement = tmp_path / "replacement"
    shutil.copyfile(executable, replacement)
    replacement.chmod(0o755)
    original_open = Path.open

    def replace_before_open(path: Path, *args: object, **kwargs: object) -> io.IOBase:
        if path == executable:
            replacement.replace(executable)
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", replace_before_open)

    with pytest.raises(RunAdmissionError, match="filesystem identity does not match"):
        _match_compatibility(executable)


def test_run_refuses_when_codex_is_missing_from_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    empty_path = tmp_path / "empty"
    empty_path.mkdir()
    monkeypatch.setenv("PATH", str(empty_path))

    with pytest.raises(
        RunAdmissionError,
        match=r'^Codex executable "codex" was not found on PATH; install Codex or add it to PATH$',
    ):
        run(tmp_path, io.BytesIO())


def test_run_canonicalizes_a_direct_codex_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "bin" / "codex"
    executable.parent.mkdir()
    make_executable(executable)
    monkeypatch.setenv("PATH", os.fspath(executable.parent))

    with pytest.raises(RunAdmissionError) as raised:
        run(tmp_path, io.BytesIO())

    assert str(raised.value) == "Codex Compatibility Set is empty; no Run is supported"
    assert raised.value.executable == executable


def test_run_canonicalizes_a_symlinked_codex_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "installation" / "codex-real"
    executable.parent.mkdir()
    make_executable(executable)
    path_directory = tmp_path / "bin"
    path_directory.mkdir()
    (path_directory / "codex").symlink_to(executable)
    monkeypatch.setenv("PATH", os.fspath(path_directory))

    with pytest.raises(RunAdmissionError) as raised:
        run(tmp_path, io.BytesIO())

    assert str(raised.value) == "Codex Compatibility Set is empty; no Run is supported"
    assert raised.value.executable == executable


def test_run_refuses_a_dangling_codex_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path_directory = tmp_path / "bin"
    path_directory.mkdir()
    candidate = path_directory / "codex"
    candidate.symlink_to(tmp_path / "missing-codex")
    monkeypatch.setenv("PATH", os.fspath(path_directory))

    with pytest.raises(RunAdmissionError) as raised:
        run(tmp_path, io.BytesIO())

    assert str(raised.value) == (
        f'Codex executable "{candidate}" is a dangling symlink; '
        "repair or remove it from PATH"
    )


def test_run_refuses_a_codex_symlink_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path_directory = tmp_path / "bin"
    path_directory.mkdir()
    candidate = path_directory / "codex"
    candidate.symlink_to(candidate)
    monkeypatch.setenv("PATH", os.fspath(path_directory))

    with pytest.raises(RunAdmissionError) as raised:
        run(tmp_path, io.BytesIO())

    assert str(raised.value) == (
        f'Codex executable "{candidate}" contains a symlink loop; '
        "repair or remove it from PATH"
    )


def test_run_refuses_a_codex_symlink_loop_reported_as_os_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path_directory = tmp_path / "bin"
    path_directory.mkdir()
    candidate = path_directory / "codex"
    make_executable(candidate)
    original_resolve = Path.resolve

    def looped_resolve(path: Path, *, strict: bool = False) -> Path:
        if path == candidate and strict:
            raise OSError(errno.ELOOP, "Too many levels of symbolic links", os.fspath(path))
        return original_resolve(path, strict=strict)

    monkeypatch.setenv("PATH", os.fspath(path_directory))
    monkeypatch.setattr(Path, "resolve", looped_resolve)

    with pytest.raises(RunAdmissionError) as raised:
        run(tmp_path, io.BytesIO())

    assert str(raised.value) == (
        f'Codex executable "{candidate}" contains a symlink loop; '
        "repair or remove it from PATH"
    )


@pytest.mark.parametrize("kind", ["non-regular", "non-executable"])
def test_run_refuses_an_unusable_codex_path_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str,
) -> None:
    path_directory = tmp_path / "bin"
    path_directory.mkdir()
    candidate = path_directory / "codex"
    if kind == "non-regular":
        candidate.mkdir()
        expected = (
            f'Codex executable "{candidate}" is not a regular file; '
            "install Codex or remove it from PATH"
        )
    else:
        candidate.write_text("#!/bin/sh\nexit 0\n")
        candidate.chmod(0o644)
        expected = (
            f'Codex executable "{candidate}" is not executable; '
            "grant execute permission or replace it"
        )
    monkeypatch.setenv("PATH", os.fspath(path_directory))

    with pytest.raises(RunAdmissionError) as raised:
        run(tmp_path, io.BytesIO())

    assert str(raised.value) == expected


def test_run_reports_an_inaccessible_codex_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path_directory = tmp_path / "bin"
    path_directory.mkdir()
    candidate = path_directory / "codex"
    make_executable(candidate)
    original_lstat = Path.lstat

    def inaccessible_lstat(path: Path) -> os.stat_result:
        if path == candidate:
            raise PermissionError(errno.EACCES, "Permission denied", os.fspath(path))
        return original_lstat(path)

    monkeypatch.setenv("PATH", os.fspath(path_directory))
    monkeypatch.setattr(Path, "lstat", inaccessible_lstat)

    with pytest.raises(RunAdmissionError) as raised:
        run(tmp_path, io.BytesIO())

    assert str(raised.value) == (
        f'Codex executable "{candidate}" could not be inspected: '
        "Permission denied; check its permissions"
    )


def test_run_reports_an_inaccessible_resolved_codex_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path_directory = tmp_path / "bin"
    path_directory.mkdir()
    candidate = path_directory / "codex"
    make_executable(candidate)
    original_resolve = Path.resolve

    def inaccessible_resolve(path: Path, *, strict: bool = False) -> Path:
        if path == candidate and strict:
            raise PermissionError(errno.EACCES, "Permission denied", os.fspath(path))
        return original_resolve(path, strict=strict)

    monkeypatch.setenv("PATH", os.fspath(path_directory))
    monkeypatch.setattr(Path, "resolve", inaccessible_resolve)

    with pytest.raises(RunAdmissionError) as raised:
        run(tmp_path, io.BytesIO())

    assert str(raised.value) == (
        f'Codex executable "{candidate}" could not be inspected: '
        "Permission denied; check its permissions"
    )


def test_factory_reports_an_admission_failure_without_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    git = shutil.which("git")
    assert git is not None
    subprocess.run([git, "init", "-q"], cwd=tmp_path, check=True)
    path_directory = tmp_path / "path"
    path_directory.mkdir()
    (path_directory / "git").symlink_to(Path(git).resolve())
    monkeypatch.setenv("PATH", os.fspath(path_directory))
    monkeypatch.setattr(sys, "argv", ["factory", "--path", str(tmp_path), "Fix"])
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"")))
    monkeypatch.chdir(tmp_path)

    assert factory.main() == 1
    assert capsys.readouterr().err == (
        'factory: error: Codex executable "codex" was not found on PATH; '
        "install Codex or add it to PATH.\n"
    )
