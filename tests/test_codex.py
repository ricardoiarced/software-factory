"""Deterministic Codex Run Admission behavior."""

import errno
import hashlib
import io
import json
import os
import platform
import select
import shutil
import signal
import subprocess
import sys
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import BinaryIO, Self

import pytest
from conftest import (
    CODEX_GITHUB_RELEASES,
    CODEX_NPM_DIST_TAGS,
    PublisherResponses,
    github_release,
    npm_dist_tags,
)

import _codex
import factory
from _codex import (
    CodexRunner,
    _Codex01580Policy,
    _match_compatibility,
    _reported_version,
)
from _harness import RunAdmissionError
from _project import Project


def run(project: Project, run_request_spool: BinaryIO) -> int:
    """Exercise the certified Runner exactly as the Factory command does."""

    return CodexRunner().run(project, run_request_spool)


HOST_PROBE_PROMPT = (
    "# Project Guidance\n\n"
    "factory-host-admission-project-guidance-v1\n\n"
    "# Run Request\n\n"
    "## Instruction\n\n"
    "factory-host-admission-run-request-v1"
)
HOST_PROBE_PROMPT_INPUT = json.dumps(
    [
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": HOST_PROBE_PROMPT}],
        }
    ],
    separators=(",", ":"),
)
MALFORMED_PROFILE_REJECTION = (
    b'Error: invalid type: string "{", expected struct PermissionProfileToml\n'
    b"in `permissions`\n\n"
)
OFFICIAL_FEATURE_OUTPUT = (
    Path(__file__).parent / "data" / "codex-0.158.0-features.txt"
).read_bytes()


@pytest.fixture(autouse=True)
def isolated_host_admission_probe_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("_codex._cache_directory", lambda: tmp_path / "cache")


def make_executable(path: Path, version: str = "0.158.0") -> None:
    path.write_text(
        f"#!/bin/sh\nif [ \"$1\" = --version ]; then\n  echo 'codex-cli {version}'\nfi\n"
    )
    path.chmod(0o755)


def make_inspector_executable(path: Path) -> None:
    script = """#!/bin/sh
if [ "$1" = --version ]; then
  echo 'codex-cli 0.158.0'
elif [ "$1:$2" = 'features:list' ]; then
  printf '%s' "$FACTORY_FEATURES"
  exit "${FACTORY_FEATURE_EXIT:-0}"
elif [ "$1:$2" = 'mcp:list' ]; then
  printf '%s' "$FACTORY_MCP"
  exit "${FACTORY_MCP_EXIT:-0}"
elif [ "$1:$2" = 'debug:prompt-input' ]; then
  printf '%s\n' 'FACTORY_HOST_PROBE_PROMPT_INPUT'
elif [ "$1" = sandbox ]; then
  if [ "$7" = factory-malformed ]; then
    printf '%s\n' 'Error: invalid type: string "{", expected struct PermissionProfileToml' 'in `permissions`' >&2
    printf '\n' >&2
    exit 1
  fi
  printf 'factory-host-admission-probe-v1\n'
fi
"""
    path.write_text(
        script.replace(
            "FACTORY_HOST_PROBE_PROMPT_INPUT",
            HOST_PROBE_PROMPT_INPUT,
        )
    )
    path.chmod(0o755)


def safe_feature_output(active_feature: str | None = None) -> bytes:
    if active_feature is None:
        return OFFICIAL_FEATURE_OUTPUT
    lines = OFFICIAL_FEATURE_OUTPUT.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if line.split(maxsplit=1)[0] == active_feature.encode():
            assert line.endswith(b"false\n")
            lines[index] = line[:-6] + b"true\n"
            return b"".join(lines)
    raise AssertionError(f"unknown fixture feature: {active_feature}")


def certify(
    executable: Path,
    *,
    host_probe_timeout_seconds: float = 10,
) -> _Codex01580Policy:
    metadata = executable.stat()
    with executable.open("rb") as artifact:
        digest = hashlib.file_digest(artifact, "sha256").hexdigest()
    return _Codex01580Policy(
        sha256=digest,
        operating_system=platform.system(),
        architecture=platform.machine(),
        canonical_path=executable,
        filesystem_identity=(metadata.st_dev, metadata.st_ino),
        host_probe_timeout_seconds=host_probe_timeout_seconds,
    )


def policy_for(executable: Path) -> _Codex01580Policy:
    """Build a policy when its identity is irrelevant to the behavior under test."""

    return _Codex01580Policy(
        sha256="unused",
        operating_system="unused",
        architecture="unused",
        canonical_path=executable,
        filesystem_identity=(0, 0),
    )


def expected_permission_profile(executable: Path) -> str:
    return (
        '{extends = ":workspace", filesystem = {":root" = "deny", '
        '":minimal" = "read", ":tmpdir" = "deny", ":slash_tmp" = "deny", '
        f'{json.dumps(str(executable.parent.absolute()), ensure_ascii=False)} = "read", '
        '":workspace_roots" = {"." = "write"}}, network = {enabled = false}}'
    )


def expected_configuration(executable: Path, git_root: Path) -> list[str]:
    return [
        "-c",
        'approval_policy="never"',
        "-c",
        "project_doc_max_bytes=0",
        "-c",
        "skills.include_instructions=false",
        "-c",
        "project_root_markers=[]",
        "-c",
        "allow_login_shell=false",
        "-c",
        'shell_environment_policy.inherit="core"',
        "-c",
        "shell_environment_policy.ignore_default_excludes=false",
        "-c",
        "features.apps=false",
        "-c",
        "features.hooks=false",
        "-c",
        "features.plugins=false",
        "-c",
        "features.remote_plugin=false",
        "-c",
        "features.browser_use=false",
        "-c",
        "features.browser_use_external=false",
        "-c",
        "features.browser_use_full_cdp_access=false",
        "-c",
        "features.in_app_browser=false",
        "-c",
        "features.computer_use=false",
        "-c",
        "features.multi_agent=false",
        "-c",
        "features.memories=false",
        "-c",
        "features.skip_host_skill_discovery=true",
        "-c",
        'default_permissions="factory-isolated"',
        "-c",
        "permissions.factory-isolated=" + expected_permission_profile(executable),
        "-c",
        (
            "projects."
            + json.dumps(os.fspath(git_root), ensure_ascii=False)
            + '.trust_level="untrusted"'
        ),
    ]


def expected_invocation(executable: Path, boundary: Path, git_root: Path) -> list[str]:
    return [
        os.fspath(executable),
        "exec",
        "--ephemeral",
        "--strict-config",
        "--ignore-user-config",
        "--ignore-rules",
        "-C",
        os.fspath(boundary),
        *expected_configuration(executable, git_root),
        "-",
    ]


def permission_profile_from(arguments: list[str]) -> dict[str, object]:
    prefix = "permissions.factory-isolated="
    serialized = next(
        argument.removeprefix(prefix)
        for argument in arguments
        if argument.startswith(prefix)
    )
    return tomllib.loads("profile = " + serialized)["profile"]


def version_reply(command: list[str]) -> subprocess.CompletedProcess[bytes] | None:
    """The version-read response every certified-executable fake serves."""

    if command[1] == "--version":
        return subprocess.CompletedProcess(command, 0, b"codex-cli 0.158.0\n", b"")
    return None


def successful_inspection(
    command: list[str],
    **kwargs: object,
) -> subprocess.CompletedProcess[bytes]:
    reply = version_reply(command)
    if reply is not None:
        return reply
    if command[1:3] == ["features", "list"]:
        return subprocess.CompletedProcess(command, 0, safe_feature_output(), b"")
    if command[1:3] == ["debug", "prompt-input"]:
        output = HOST_PROBE_PROMPT_INPUT.encode()
        return subprocess.CompletedProcess(command, 0, output, b"")
    if command[1] == "sandbox":
        return subprocess.CompletedProcess(
            command,
            1,
            b"",
            MALFORMED_PROFILE_REJECTION,
        )
    return subprocess.CompletedProcess(command, 0, b"[]\n", b"")


class SuccessfulProbeProcess:
    returncode: int = 0

    def communicate(self, timeout: float | None = None) -> tuple[bytes, bytes]:
        return b"factory-host-admission-probe-v1\n", b""


class ListeningSocket:
    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        pass

    def bind(self, address: tuple[str, int]) -> None:
        pass

    def listen(self, backlog: int) -> None:
        pass

    def getsockname(self) -> tuple[str, int]:
        return "127.0.0.1", 43123


def capture_policy_preparation(
    policy: _Codex01580Policy,
    boundary: Path,
    git_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[str], list[list[str]], list[list[str]]]:
    inspections: list[list[str]] = []
    probes: list[list[str]] = []

    def inspect(
        command: list[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[bytes]:
        inspections.append(command)
        return successful_inspection(command, **kwargs)

    def probe(command: list[str], **kwargs: object) -> SuccessfulProbeProcess:
        probes.append(command)
        return SuccessfulProbeProcess()

    monkeypatch.setattr("_codex.subprocess.run", inspect)
    monkeypatch.setattr("_codex.subprocess.Popen", probe)
    invocation = policy.prepare_run(boundary, git_root)
    return invocation, inspections, probes


def install_compatibility_fixture(
    executable: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    host_probe_timeout_seconds: float = 10,
) -> _Codex01580Policy:
    policy = certify(
        executable,
        host_probe_timeout_seconds=host_probe_timeout_seconds,
    )
    monkeypatch.setattr(
        "_codex._COMPATIBILITY_SET",
        (policy,),
    )
    return policy


def install_launch_fixture(
    executable: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    make_executable(executable)
    monkeypatch.setenv("PATH", os.fspath(executable.parent))
    monkeypatch.setattr("_codex.subprocess.run", successful_inspection)
    monkeypatch.setattr("_codex.socket.socket", lambda *args: ListeningSocket())
    install_compatibility_fixture(executable, monkeypatch)


def install_faked_launch_fixture(
    executable: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    """Serve the version read from the executable while faking only the launch."""

    class Process:
        def __init__(self, command: list[str], **kwargs: object) -> None:
            self.args = command
            self._version_read = command[1] == "--version"

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def communicate(
            self,
            input: object = None,
            timeout: float | None = None,
        ) -> tuple[bytes, bytes]:
            assert self._version_read
            return b"codex-cli 0.158.0\n", b""

        def poll(self) -> int:
            return 0 if self._version_read else status

        def wait(self) -> int:
            assert not self._version_read
            return status

    monkeypatch.setattr("_codex.subprocess.Popen", Process)


def policy_with_recorded_host_admission_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, _Codex01580Policy, list[list[str]]]:
    executable = tmp_path / "bin" / "codex"
    executable.parent.mkdir()
    make_executable(executable)
    policy = certify(executable)
    probes: list[list[str]] = []

    def probe(command: list[str], **kwargs: object) -> SuccessfulProbeProcess:
        probes.append(command)
        return SuccessfulProbeProcess()

    monkeypatch.setattr("_codex.subprocess.run", successful_inspection)
    monkeypatch.setattr("_codex.subprocess.Popen", probe)
    return executable, policy, probes


def make_probe_executable(path: Path) -> None:
    script = """#!/bin/sh
if [ "$1" = --version ]; then
  echo 'codex-cli 0.158.0'
elif [ "$1:$2" = 'features:list' ]; then
  printf '%s' "$FACTORY_FEATURES"
elif [ "$1:$2" = 'mcp:list' ]; then
  printf '[]\n'
elif [ "$1:$2" = 'debug:prompt-input' ]; then
  [ -f AGENTS.md ] || exit 92
  [ -f .agents/skills/factory-probe/SKILL.md ] || exit 92
  [ -f .codex/config.toml ] || exit 92
  : > "$FACTORY_PROMPT_PROBE_GATE"
  printf '%s\n' 'FACTORY_HOST_PROBE_PROMPT_INPUT'
elif [ "$1" = sandbox ]; then
  if [ "$7" = factory-malformed ]; then
    : > "$FACTORY_NEGATIVE_PROFILE_GATE"
    if [ "${FACTORY_NEGATIVE_PROFILE_STDOUT+x}" = x ]; then
      printf '%s' "$FACTORY_NEGATIVE_PROFILE_STDOUT"
    fi
    if [ "${FACTORY_NEGATIVE_PROFILE_STDERR+x}" = x ]; then
      printf '%s' "$FACTORY_NEGATIVE_PROFILE_STDERR" >&2
    else
      printf '%s\n' 'Error: invalid type: string "{", expected struct PermissionProfileToml' 'in `permissions`' >&2
      printf '\n' >&2
    fi
    exit "${FACTORY_NEGATIVE_PROFILE_EXIT:-1}"
  fi
  [ -z "${BASH_ENV+x}" ] || exit 92
  for argument in "$@"; do
    if [ "$argument" = "$FACTORY_SELECTED_PROJECT" ]; then
      exit 91
    fi
  done
  [ "$2" = -C ] || exit 92
  [ "$4" = -c ] || exit 92
  [ "$6:$7:$8" = '-P:factory-isolated:--' ] || exit 92
  [ "$9:${10}:${11}" = '/bin/bash:-p:-c' ] || exit 92
  [ "${13}" = factory-host-admission-probe ] || exit 92
  [ "${14}" = "$3" ] || exit 92
  [ -f "${14}/inside-read" ] || exit 92
  [ "$(cat "${14}/inside-read")" = inside-boundary ] || exit 92
  [ -f "${14%/*}/parent-read" ] || exit 92
  [ -f "${14%/*}/sibling/outside-read" ] || exit 92
  case "${15}" in "${14}"/*) exit 92;; esac
  case "${16}" in /tmp/*) :;; *) exit 92;; esac
  [ "${17}" = "$0" ] || exit 92
  case "${18}" in *[!0-9]*|'') exit 92;; esac
  case "$5" in
    *'":root" = "deny"'*'":minimal" = "read"'*'":tmpdir" = "deny"'*) :;;
    *) exit 92;;
  esac
  case "$5" in
    *'":slash_tmp" = "deny"'*'":workspace_roots" = {"." = "write"}'*) :;;
    *) exit 92;;
  esac
  require_script_fragment() {
    case "$1" in *"$2"*) :;; *) exit 93;; esac
  }
  require_script_fragment "${12}" 'read_exact "$inside_read"'
  require_script_fragment "${12}" 'can_write "$inside_write"'
  require_script_fragment "${12}" 'can_read "$parent_read"'
  require_script_fragment "${12}" 'can_write "$parent_write"'
  require_script_fragment "${12}" 'can_read "$sibling_read"'
  require_script_fragment "${12}" 'can_write "$sibling_write"'
  require_script_fragment "${12}" 'can_write "$temporary_write"'
  require_script_fragment "${12}" 'can_write "$slash_tmp_write"'
  require_script_fragment "${12}" 'can_read "/bin/sh"'
  require_script_fragment "${12}" 'can_read "$executable"'
  require_script_fragment "${12}" 'can_network "$network_port"'
  : > "$FACTORY_PROBE_GATE"
  if [ -n "${FACTORY_PROBE_DESCENDANT_SLEEP:-}" ]; then
    (
      trap '' HUP TERM
      sleep "$FACTORY_PROBE_DESCENDANT_SLEEP"
      : > "$FACTORY_PROBE_DESCENDANT_SURVIVED"
    ) &
    wait
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
  if [ "${FACTORY_RUNTIME_PROBE:-}" = signal-wait ]; then
    exec "$FACTORY_RUNTIME_PYTHON" -c '
import os
from pathlib import Path
import signal
import sys

signum = int(os.environ["FACTORY_RUNTIME_SIGNAL"])
if "FACTORY_RUNTIME_EXIT" in os.environ:
    exit_status = int(os.environ["FACTORY_RUNTIME_EXIT"])
    signal.signal(signum, lambda *_: sys.exit(exit_status))
else:
    signal.signal(signum, signal.SIG_DFL)
if "FACTORY_RUNTIME_RESUME_READY" in os.environ:
    resume_ready = Path(os.environ["FACTORY_RUNTIME_RESUME_READY"])
    signal.signal(signal.SIGCONT, lambda *_: resume_ready.touch())
Path(os.environ["FACTORY_RUNTIME_SIGNAL_READY"]).write_text(
    f"{os.getpid()} {os.getpgrp()}\\n"
)
while True:
    signal.pause()
'
  fi
  if [ "${FACTORY_RUNTIME_PROBE:-}" = signal-self ]; then
    kill -"$FACTORY_RUNTIME_SIGNAL" "$$"
    exit 90
  fi
  if [ "${FACTORY_RUNTIME_PROBE:-}" = native ]; then
    [ "${CODEX_HOME:-}" = "$FACTORY_EXPECTED_CODEX_HOME" ] || exit 90
    cat > "$FACTORY_RUNTIME_SPOOL_COPY" || exit 90
    printf '\\377codex-stdout\n'
    printf '\\376codex-stderr\n' >&2
    exit "$FACTORY_RUNTIME_EXIT"
  fi
  if [ "${FACTORY_RUNTIME_PROBE:-}" = tty ]; then
    [ ! -t 0 ] && [ -t 1 ] && [ -t 2 ] || exit 90
    printf 'codex-tty\n'
    exit 0
  fi
  if [ -f "$FACTORY_PROBE_GATE" ]; then
    exit 23
  fi
  exit 99
fi
"""
    path.write_text(
        script.replace(
            "FACTORY_HOST_PROBE_PROMPT_INPUT",
            HOST_PROBE_PROMPT_INPUT,
        )
    )
    path.chmod(0o755)


def prepare_probe_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    host_probe_timeout_seconds: float = 10,
) -> tuple[Project, Path, Path]:
    boundary = tmp_path / "selected-project"
    boundary.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=boundary, check=True)
    executable = tmp_path / "bin" / "codex"
    executable.parent.mkdir()
    make_probe_executable(executable)
    probe_gate = tmp_path / "probe-ran"
    prompt_probe_gate = tmp_path / "prompt-probe-ran"
    negative_profile_gate = tmp_path / "negative-profile-ran"
    launch_gate = tmp_path / "run-launched"
    monkeypatch.setenv(
        "PATH", os.pathsep.join((str(executable.parent), os.environ["PATH"]))
    )
    monkeypatch.setenv("FACTORY_FEATURES", safe_feature_output().decode("ascii"))
    monkeypatch.setenv("FACTORY_SELECTED_PROJECT", str(boundary))
    monkeypatch.setenv("FACTORY_PROBE_GATE", str(probe_gate))
    monkeypatch.setenv("FACTORY_PROMPT_PROBE_GATE", str(prompt_probe_gate))
    monkeypatch.setenv("FACTORY_NEGATIVE_PROFILE_GATE", str(negative_profile_gate))
    monkeypatch.setenv("FACTORY_LAUNCH_GATE", str(launch_gate))
    install_compatibility_fixture(
        executable,
        monkeypatch,
        host_probe_timeout_seconds=host_probe_timeout_seconds,
    )
    return Project(boundary, boundary), probe_gate, launch_gate


def fork_runtime_run(
    project: Project,
    tmp_path: Path,
    *,
    default_signal: signal.Signals | None = None,
) -> int:
    """Run the certified Runner in a forked double, returning its pid.

    The child catches every ``BaseException`` and converts it to exit
    status 125: an escapee (``SystemExit``, ``KeyboardInterrupt``, any
    failure) must never unwind into the parent's pytest process, and the
    launcher under test reports failures through distinct child exits.
    """
    factory_pid = os.fork()
    if factory_pid == 0:
        os.setpgid(0, 0)
        if default_signal is not None:
            signal.signal(default_signal, signal.SIG_DFL)
        try:
            spool_path = tmp_path / f"run-request-{os.getpid()}"
            spool_path.write_bytes(b"# Run Request\n")
            with spool_path.open("rb") as spool:
                status = run(project, spool)
        except BaseException:  # noqa: BLE001
            os._exit(125)
        os._exit(status)
    return factory_pid


def wait_for_runtime_marker(
    marker: Path,
    factory_pid: int,
    failure: str,
) -> None:
    deadline = time.monotonic() + 5
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    if not marker.exists():
        try:
            os.killpg(factory_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        os.waitpid(factory_pid, 0)
        pytest.fail(failure)


def wait_for_signal_runtime(ready: Path, factory_pid: int) -> tuple[int, int]:
    """Poll for the pid/process-group content, not bare marker existence.

    The runtime shim creates the marker and then writes into it, so a bare
    existence check can observe the file between creation and write.
    """
    deadline = time.monotonic() + 5
    while True:
        try:
            tokens = ready.read_text().split()
        except FileNotFoundError:
            tokens = []
        if len(tokens) == 2:
            break
        if time.monotonic() >= deadline:
            try:
                os.killpg(factory_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            os.waitpid(factory_pid, 0)
            pytest.fail("signal runtime did not become ready")
        time.sleep(0.01)
    child_pid, process_group = tokens
    return int(child_pid), int(process_group)


def start_signal_runtime(
    project: Project,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runtime_signal: signal.Signals,
    *,
    exit_status: int | None = None,
) -> tuple[int, int, int]:
    ready = tmp_path / "signal-ready"
    monkeypatch.setenv("FACTORY_RUNTIME_PROBE", "signal-wait")
    monkeypatch.setenv("FACTORY_RUNTIME_PYTHON", sys.executable)
    monkeypatch.setenv("FACTORY_RUNTIME_SIGNAL", str(runtime_signal))
    monkeypatch.setenv("FACTORY_RUNTIME_SIGNAL_READY", str(ready))
    if exit_status is not None:
        monkeypatch.setenv("FACTORY_RUNTIME_EXIT", str(exit_status))

    factory_pid = fork_runtime_run(project, tmp_path)
    child_pid, process_group = wait_for_signal_runtime(ready, factory_pid)
    return factory_pid, child_pid, process_group


def test_codex_0_155_1_policy_preparation_is_deterministic_and_exact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "Codex Runtime" / "codex"
    boundary = tmp_path / "Project Boundary"
    git_root = tmp_path / "Git Root"
    policy = policy_for(executable)

    first, _, _ = capture_policy_preparation(policy, boundary, git_root, monkeypatch)
    second, _, _ = capture_policy_preparation(policy, boundary, git_root, monkeypatch)

    assert first == expected_invocation(executable, boundary, git_root)
    assert second == first


def test_codex_0_155_1_permission_profile_allows_only_the_required_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "runtime" / "codex"
    policy = policy_for(executable)

    invocation, _, _ = capture_policy_preparation(
        policy,
        tmp_path / "boundary",
        tmp_path / "git-root",
        monkeypatch,
    )

    assert permission_profile_from(invocation) == {
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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "installation" / "codex"
    executable.parent.mkdir()
    executable.write_text("codex")
    linked_executable = tmp_path / "bin" / "codex"
    linked_executable.parent.mkdir()
    linked_executable.symlink_to(executable)
    policy = policy_for(executable)

    invocation, _, _ = capture_policy_preparation(
        policy,
        tmp_path / "boundary",
        tmp_path / "git-root",
        monkeypatch,
    )
    profile = permission_profile_from(invocation)

    assert profile["filesystem"][str(executable.parent)] == "read"
    assert str(linked_executable.parent) not in profile["filesystem"]


def test_codex_0_155_1_policy_uses_one_configuration_for_evidence_and_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "Codex Runtime" / "codex"
    boundary = tmp_path / "Project Boundary"
    git_root = tmp_path / "Git Root"
    policy = policy_for(executable)

    invocation, inspections, probes = capture_policy_preparation(
        policy,
        boundary,
        git_root,
        monkeypatch,
    )

    configuration = expected_configuration(executable, git_root)
    assert invocation == expected_invocation(executable, boundary, git_root)
    assert inspections[:2] == [
        [os.fspath(executable), "features", "list", *configuration],
        [os.fspath(executable), "mcp", "list", "--json", *configuration],
    ]
    host_features = next(
        command for command in inspections[2:] if command[1:3] == ["features", "list"]
    )
    prompt_input = next(
        command for command in inspections if command[1:3] == ["debug", "prompt-input"]
    )
    assert prompt_input == [
        os.fspath(executable),
        "debug",
        "prompt-input",
        *host_features[3:],
        HOST_PROBE_PROMPT,
    ]
    assert probes[0][:3] == [os.fspath(executable), "sandbox", "-C"]
    assert probes[0][4:6] == [
        "-c",
        "permissions.factory-isolated=" + expected_permission_profile(executable),
    ]
    probe_boundary = probes[0][3]
    malformed_profile = next(
        command
        for command in inspections
        if command[1] == "sandbox" and "factory-malformed" in command
    )
    assert malformed_profile == [
        os.fspath(executable),
        "sandbox",
        "-C",
        probe_boundary,
        "-c",
        "permissions.factory-malformed={",
        "-P",
        "factory-malformed",
        "--",
        "/bin/sh",
        "-c",
        "exit 0",
    ]


def test_run_creates_one_native_foreground_codex_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "bin" / "codex"
    executable.parent.mkdir()
    boundary = tmp_path / "project"
    boundary.mkdir()
    git_root = tmp_path / "git root"
    git_root.mkdir()
    spool = io.BytesIO(b"# Run Request\n")
    invocation = tuple(expected_invocation(executable, boundary, git_root))
    install_launch_fixture(executable, monkeypatch)
    for name in _codex._CODEX_MANAGED_MARKERS:
        monkeypatch.setenv(name, "inherited")

    calls: list[tuple[list[str], dict[str, object], bool, int]] = []

    class Process:
        def wait(self) -> int:
            return 17

    def launch(
        command: list[str], **kwargs: object
    ) -> SuccessfulProbeProcess | Process:
        if command[1] == "sandbox":
            return SuccessfulProbeProcess()
        calls.append((command, kwargs, spool.closed, spool.tell()))
        return Process()

    monkeypatch.setattr("_codex.subprocess.Popen", launch)

    run(Project(boundary, git_root), spool)

    assert calls == [
        (
            invocation,
            {
                "cwd": boundary,
                "stdin": spool,
                "stdout": None,
                "stderr": None,
                "env": {
                    name: value
                    for name, value in os.environ.items()
                    if name not in _codex._CODEX_MANAGED_MARKERS
                },
                "shell": False,
                "start_new_session": False,
            },
            False,
            0,
        )
    ]


def assert_run_uses_native_signal_disposition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runtime_signal: signal.Signals,
    *,
    during_process_creation: bool,
) -> None:
    executable = tmp_path / "bin" / "codex"
    executable.parent.mkdir()
    boundary = tmp_path / "project"
    boundary.mkdir()
    install_launch_fixture(executable, monkeypatch)

    class Process:
        def wait(self) -> int:
            if not during_process_creation:
                os.kill(os.getpid(), runtime_signal)
            return 0

    def launch(
        command: list[str],
        **_kwargs: object,
    ) -> SuccessfulProbeProcess | Process:
        if command[1] == "sandbox":
            return SuccessfulProbeProcess()
        if during_process_creation:
            os.kill(os.getpid(), runtime_signal)
        return Process()

    monkeypatch.setattr("_codex.subprocess.Popen", launch)

    factory_pid = fork_runtime_run(
        Project(boundary, boundary),
        tmp_path,
        default_signal=runtime_signal,
    )
    _, status = os.waitpid(factory_pid, 0)

    assert os.WIFSIGNALED(status)
    assert os.WTERMSIG(status) == runtime_signal


def test_run_does_not_swallow_a_signal_during_process_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert_run_uses_native_signal_disposition(
        tmp_path,
        monkeypatch,
        signal.SIGTERM,
        during_process_creation=True,
    )


def test_run_does_not_defer_a_factory_local_timer_signal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert_run_uses_native_signal_disposition(
        tmp_path,
        monkeypatch,
        signal.SIGALRM,
        during_process_creation=False,
    )


@pytest.mark.parametrize("runtime_signal", [signal.SIGTERM, signal.SIGKILL])
def test_run_exposes_signal_terminated_child_as_the_same_signal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runtime_signal: signal.Signals,
) -> None:
    project, _, _ = prepare_probe_run(tmp_path, monkeypatch)
    monkeypatch.setenv("FACTORY_RUNTIME_PROBE", "signal-self")
    monkeypatch.setenv("FACTORY_RUNTIME_SIGNAL", str(runtime_signal))

    factory_pid = fork_runtime_run(project, tmp_path)
    _, status = os.waitpid(factory_pid, 0)

    assert os.WIFSIGNALED(status)
    assert os.WTERMSIG(status) == runtime_signal


@pytest.mark.parametrize("runtime_signal", [signal.SIGTERM, signal.SIGUSR1])
def test_run_preserves_status_when_foreground_child_handles_signal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runtime_signal: signal.Signals,
) -> None:
    project, _, _ = prepare_probe_run(tmp_path, monkeypatch)
    factory_pid, child_pid, child_process_group = start_signal_runtime(
        project,
        tmp_path,
        monkeypatch,
        runtime_signal,
        exit_status=23,
    )
    assert child_pid != factory_pid
    assert child_process_group == factory_pid

    os.killpg(factory_pid, runtime_signal)
    _, status = os.waitpid(factory_pid, 0)

    assert os.WIFEXITED(status)
    assert os.WEXITSTATUS(status) == 23


@pytest.mark.parametrize(
    "runtime_signal",
    [signal.SIGHUP, signal.SIGINT, signal.SIGQUIT, signal.SIGTERM],
)
def test_run_preserves_foreground_termination_signals_without_tracebacks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfdbinary: pytest.CaptureFixture[bytes],
    runtime_signal: signal.Signals,
) -> None:
    project, _, _ = prepare_probe_run(tmp_path, monkeypatch)
    factory_pid, _, child_process_group = start_signal_runtime(
        project,
        tmp_path,
        monkeypatch,
        runtime_signal,
    )
    assert child_process_group == factory_pid

    os.killpg(factory_pid, runtime_signal)
    _, status = os.waitpid(factory_pid, 0)

    captured = capfdbinary.readouterr()
    assert os.WIFSIGNALED(status)
    assert os.WTERMSIG(status) == runtime_signal
    assert captured.err == b""


def test_run_preserves_foreground_suspend_and_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, _, _ = prepare_probe_run(tmp_path, monkeypatch)
    resume_ready = tmp_path / "signal-resumed"
    monkeypatch.setenv("FACTORY_RUNTIME_RESUME_READY", str(resume_ready))
    factory_pid, _, child_process_group = start_signal_runtime(
        project,
        tmp_path,
        monkeypatch,
        signal.SIGTERM,
        exit_status=0,
    )
    assert child_process_group == factory_pid

    os.killpg(factory_pid, signal.SIGTSTP)
    _, stopped_status = os.waitpid(factory_pid, os.WUNTRACED)
    assert os.WIFSTOPPED(stopped_status)
    assert os.WSTOPSIG(stopped_status) == signal.SIGTSTP

    os.killpg(factory_pid, signal.SIGCONT)
    wait_for_runtime_marker(
        resume_ready,
        factory_pid,
        "foreground child did not resume",
    )

    os.killpg(factory_pid, signal.SIGTERM)
    exit_deadline = time.monotonic() + 5
    while True:
        resumed_pid, resumed_status = os.waitpid(factory_pid, os.WNOHANG)
        if resumed_pid != 0 or time.monotonic() >= exit_deadline:
            break
        time.sleep(0.01)
    if resumed_pid == 0:
        os.killpg(factory_pid, signal.SIGKILL)
        os.waitpid(factory_pid, 0)
        pytest.fail("foreground process group did not terminate")

    assert os.WIFEXITED(resumed_status)
    assert os.WEXITSTATUS(resumed_status) == 0


@pytest.mark.parametrize("status", [0, 1, 2])
def test_run_preserves_native_output_environment_spool_and_ordinary_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfdbinary: pytest.CaptureFixture[bytes],
    status: int,
) -> None:
    project, _, _ = prepare_probe_run(tmp_path, monkeypatch)
    codex_home = tmp_path / "codex-home-with-authentication"
    spool_copy = tmp_path / "received-run-request"
    request = b"# Run Request\n\n## Instruction\n\nPreserve native behavior"
    request_path = tmp_path / "run-request"
    request_path.write_bytes(request)
    monkeypatch.setenv("CODEX_HOME", os.fspath(codex_home))
    monkeypatch.setenv("FACTORY_EXPECTED_CODEX_HOME", os.fspath(codex_home))
    monkeypatch.setenv("FACTORY_RUNTIME_PROBE", "native")
    monkeypatch.setenv("FACTORY_RUNTIME_SPOOL_COPY", os.fspath(spool_copy))
    monkeypatch.setenv("FACTORY_RUNTIME_EXIT", str(status))

    with request_path.open("rb") as spool:
        result = run(project, spool)
        assert not spool.closed

    captured = capfdbinary.readouterr()
    assert result == status
    assert captured.out == b"\xffcodex-stdout\n"
    assert captured.err == b"\xfecodex-stderr\n"
    assert spool_copy.read_bytes() == request


def test_run_preserves_child_tty_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drain the master while the slave stays open: BSD ptys discard the
    buffered output when the last slave descriptor closes, so the queued
    output must be collected before that teardown.
    """
    project, _, _ = prepare_probe_run(tmp_path, monkeypatch)
    request_path = tmp_path / "run-request"
    request_path.write_bytes(b"# Run Request\n")
    monkeypatch.setenv("FACTORY_RUNTIME_PROBE", "tty")
    master, slave = os.openpty()
    saved_stdout = os.dup(1)
    saved_stderr = os.dup(2)

    output = b""
    try:
        os.dup2(slave, 1)
        os.dup2(slave, 2)
        with request_path.open("rb") as spool:
            result = run(project, spool)
    finally:
        os.dup2(saved_stdout, 1)
        os.dup2(saved_stderr, 2)
        os.close(saved_stdout)
        os.close(saved_stderr)
        while True:
            readable, _, _ = select.select((master,), (), (), 1.0)
            if not readable:
                break
            try:
                chunk = os.read(master, 4096)
            except OSError as error:
                if error.errno == errno.EIO:
                    break
                raise
            if not chunk:
                break
            output += chunk
        os.close(slave)
    os.close(master)

    assert result == 0
    assert output == b"codex-tty\r\n"


def test_run_does_not_retry_failed_process_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "bin" / "codex"
    executable.parent.mkdir()
    boundary = tmp_path / "project"
    boundary.mkdir()
    install_launch_fixture(executable, monkeypatch)
    attempts = 0

    def fail_to_start(
        command: list[str],
        **kwargs: object,
    ) -> SuccessfulProbeProcess | None:
        nonlocal attempts
        if command[1] == "sandbox":
            return SuccessfulProbeProcess()
        attempts += 1
        raise OSError(errno.EAGAIN, "Resource temporarily unavailable")

    monkeypatch.setattr("_codex.subprocess.Popen", fail_to_start)

    try:
        run(Project(boundary, boundary), io.BytesIO())
    except RunAdmissionError as error:
        message = str(error)
    else:
        message = None

    assert (message, attempts) == (
        (
            f'Codex executable "{executable}" could not start: '
            "Resource temporarily unavailable"
        ),
        1,
    )


def test_run_uses_the_project_selection_git_root_without_rediscovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "bin" / "codex"
    executable.parent.mkdir()
    make_executable(executable, version="0.158.0")
    boundary = tmp_path / "project"
    boundary.mkdir()
    selected_git_root = tmp_path / "selected-git-root"
    selected_git_root.mkdir()
    invocation = [os.fspath(executable), "exec", "-"]
    prepared_with: list[tuple[Path, Path]] = []

    policy = install_compatibility_fixture(executable, monkeypatch)

    def prepare_run(
        self: _Codex01580Policy,
        project_boundary: Path,
        enclosing_git_root: Path,
    ) -> list[str]:
        assert self is policy
        prepared_with.append((project_boundary, enclosing_git_root))
        return invocation

    monkeypatch.setenv("PATH", os.fspath(executable.parent))
    monkeypatch.setattr(_Codex01580Policy, "prepare_run", prepare_run)
    monkeypatch.setattr("_codex._match_compatibility", lambda path, version: policy)
    install_faked_launch_fixture(executable, monkeypatch, status=17)

    project = Project(boundary=boundary, enclosing_git_root=selected_git_root)

    assert run(project, io.BytesIO()) == 17
    assert prepared_with == [(boundary, selected_git_root)]


def test_run_uses_the_matched_policy_as_its_only_certified_behavior(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "bin" / "codex"
    executable.parent.mkdir()
    make_executable(executable, version="0.158.0")
    boundary = tmp_path / "project"
    boundary.mkdir()
    git_root = tmp_path / "git-root"
    git_root.mkdir()
    invocation = [os.fspath(executable), "exec", "-"]
    calls: list[tuple[Path, Path]] = []

    policy = install_compatibility_fixture(executable, monkeypatch)

    def prepare_run(
        self: _Codex01580Policy,
        project_boundary: Path,
        enclosing_git_root: Path,
    ) -> list[str]:
        assert self is policy
        calls.append((project_boundary, enclosing_git_root))
        return invocation

    monkeypatch.setenv("PATH", os.fspath(executable.parent))
    monkeypatch.setattr(_Codex01580Policy, "prepare_run", prepare_run)
    monkeypatch.setattr("_codex._match_compatibility", lambda path, version: policy)
    install_faked_launch_fixture(executable, monkeypatch, status=17)

    assert run(Project(boundary, git_root), io.BytesIO()) == 17
    assert calls == [(boundary, git_root)]


@pytest.mark.parametrize("change", ["replacement", "self-update"])
def test_run_refuses_an_executable_change_without_evaluating_it_before_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    executable = tmp_path / "bin" / "codex"
    executable.parent.mkdir()
    make_executable(executable)
    boundary = tmp_path / "project"
    boundary.mkdir()
    git_root = tmp_path / "git-root"
    git_root.mkdir()

    policy = install_compatibility_fixture(executable, monkeypatch)
    cache_payload = policy._host_probe_cache_payload()
    policy._store_host_probe_success(cache_payload)
    actual_prepare_run = _Codex01580Policy.prepare_run
    version_checks = 0
    launched = False

    def prepare_then_change(
        self: _Codex01580Policy,
        project_boundary: Path,
        enclosing_git_root: Path,
    ) -> list[str]:
        invocation = actual_prepare_run(
            self,
            project_boundary,
            enclosing_git_root,
        )
        changed = executable.read_text() + "# changed\n"
        if change == "replacement":
            replacement = tmp_path / "replacement"
            replacement.write_text(changed)
            replacement.chmod(0o755)
            replacement.replace(executable)
        else:
            executable.write_text(changed)
            executable.chmod(0o755)
        return invocation

    def inspect(
        command: list[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[bytes]:
        nonlocal version_checks
        if command[1:] == ["--version"]:
            version_checks += 1
            return subprocess.CompletedProcess(
                command,
                0,
                b"codex-cli 0.158.0\n",
                b"",
            )
        return successful_inspection(command, **kwargs)

    def launch(command: list[str], **kwargs: object) -> SuccessfulProbeProcess | None:
        nonlocal launched
        if command[1] == "sandbox":
            return SuccessfulProbeProcess()
        launched = True
        return None

    monkeypatch.setenv("PATH", os.fspath(executable.parent))
    monkeypatch.setattr(_Codex01580Policy, "prepare_run", prepare_then_change)
    monkeypatch.setattr("_codex.subprocess.run", inspect)
    monkeypatch.setattr("_codex.subprocess.Popen", launch)

    with pytest.raises(
        RunAdmissionError,
        match="changed during Run Admission; refusing to evaluate its replacement",
    ):
        run(Project(boundary, git_root), io.BytesIO())

    assert not launched
    assert version_checks == 1
    assert policy.sha256 != hashlib.sha256(executable.read_bytes()).hexdigest()


def test_run_requires_a_successful_synthetic_probe_before_launching_codex(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    boundary, probe_gate, launch_gate = prepare_probe_run(tmp_path, monkeypatch)

    spool = tmp_path / "run-request"
    spool.write_bytes(b"# Run Request\n")
    with spool.open("rb") as request:
        assert run(boundary, request) == 23
    assert probe_gate.is_file()
    assert (tmp_path / "prompt-probe-ran").is_file()
    assert (tmp_path / "negative-profile-ran").is_file()
    assert launch_gate.is_file()


def test_run_reuses_a_complete_host_admission_probe_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "bin" / "codex"
    executable.parent.mkdir()
    make_executable(executable)
    policy = certify(executable)
    inspections: list[list[str]] = []
    probes: list[list[str]] = []

    def inspect(
        command: list[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[bytes]:
        inspections.append(command)
        return successful_inspection(command, **kwargs)

    def probe(command: list[str], **kwargs: object) -> SuccessfulProbeProcess:
        probes.append(command)
        return SuccessfulProbeProcess()

    monkeypatch.setattr("_codex.subprocess.run", inspect)
    monkeypatch.setattr("_codex.subprocess.Popen", probe)

    policy.prepare_run(tmp_path, tmp_path)
    policy.prepare_run(tmp_path, tmp_path)

    assert len(probes) == 1
    assert [command[1:3] for command in inspections].count(["features", "list"]) == 3
    assert [command[1:3] for command in inspections].count(["mcp", "list"]) == 3


@pytest.mark.parametrize(
    "replacement",
    [
        b"{",
        b'{"schema":1}\n',
        b'{"schema":0,"success":true}\n',
        b'{"schema":1,"success":true,"factory_version":"other"}\n',
    ],
)
def test_run_reprobes_malformed_stale_partial_or_mismatched_cache_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement: bytes,
) -> None:
    _, policy, probes = policy_with_recorded_host_admission_probe(
        tmp_path,
        monkeypatch,
    )
    policy.prepare_run(tmp_path, tmp_path)
    cached = next((tmp_path / "cache" / "host-admission-probe").iterdir())
    cached.write_bytes(replacement)

    policy.prepare_run(tmp_path, tmp_path)

    assert len(probes) == 2


def test_run_reprobes_after_factory_or_host_upgrade(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, policy, probes = policy_with_recorded_host_admission_probe(
        tmp_path,
        monkeypatch,
    )
    factory_version = "1.0"
    host_release = "kernel-1"

    monkeypatch.setattr("_codex._factory_version", lambda: factory_version)
    monkeypatch.setattr(
        "_codex._host_identity",
        lambda: {"release": host_release, "version": "host-version"},
    )
    policy.prepare_run(tmp_path, tmp_path)
    factory_version = "1.1"
    policy.prepare_run(tmp_path, tmp_path)
    monkeypatch.setattr(_Codex01580Policy, "version", "0.155.2")
    policy.prepare_run(tmp_path, tmp_path)
    host_release = "kernel-2"
    policy.prepare_run(tmp_path, tmp_path)

    assert len(probes) == 4


def test_run_reprobes_after_codex_artifact_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable, first_policy, probes = policy_with_recorded_host_admission_probe(
        tmp_path,
        monkeypatch,
    )

    first_policy.prepare_run(tmp_path, tmp_path)
    executable.write_text(executable.read_text() + "# replacement\n")
    executable.chmod(0o755)
    certify(executable).prepare_run(tmp_path, tmp_path)

    assert len(probes) == 2


def test_failed_host_admission_probe_is_not_cached(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "bin" / "codex"
    executable.parent.mkdir()
    make_executable(executable)
    policy = certify(executable)
    probes = 0

    class FailedProbeProcess(SuccessfulProbeProcess):
        returncode: int = 7

    def probe(command: list[str], **kwargs: object) -> SuccessfulProbeProcess:
        nonlocal probes
        probes += 1
        return FailedProbeProcess() if probes == 1 else SuccessfulProbeProcess()

    monkeypatch.setattr("_codex.subprocess.run", successful_inspection)
    monkeypatch.setattr("_codex.subprocess.Popen", probe)

    with pytest.raises(RunAdmissionError, match="exit status 7"):
        policy.prepare_run(tmp_path, tmp_path)
    assert not tuple((tmp_path / "cache").rglob("*.json"))
    policy.prepare_run(tmp_path, tmp_path)

    assert probes == 2


def test_host_admission_probe_success_is_atomically_stored_with_its_complete_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "bin" / "codex"
    executable.parent.mkdir()
    make_executable(executable)
    policy = certify(executable)
    replaced: list[tuple[Path, Path, bytes]] = []
    actual_replace = os.replace

    def replace(source: Path, destination: Path) -> None:
        replaced.append((source, destination, source.read_bytes()))
        assert source.parent == destination.parent
        assert not destination.exists()
        actual_replace(source, destination)

    monkeypatch.setattr("_codex._factory_version", lambda: "factory-version")
    monkeypatch.setattr(
        "_codex._host_identity",
        lambda: {"release": "host-release", "version": "host-version"},
    )
    monkeypatch.setattr("_codex.os.replace", replace)
    monkeypatch.setattr("_codex.subprocess.run", successful_inspection)
    monkeypatch.setattr(
        "_codex.subprocess.Popen",
        lambda *args, **kwargs: SuccessfulProbeProcess(),
    )

    policy.prepare_run(tmp_path, tmp_path)

    assert len(replaced) == 1
    source, destination, payload = replaced[0]
    assert source.name.startswith(".host-admission-probe-")
    assert destination.parent == tmp_path / "cache" / "host-admission-probe"
    assert destination.read_bytes() == payload
    assert destination.stat().st_mode & 0o777 == 0o600
    assert json.loads(payload) == {
        "schema": 1,
        "success": True,
        "factory_version": "factory-version",
        "codex_version": "0.158.0",
        "executable_sha256": policy.sha256,
        "operating_system": policy.operating_system,
        "architecture": policy.architecture,
        "host_identity": {"release": "host-release", "version": "host-version"},
    }


def test_cache_write_failure_leaves_admission_uncached(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "bin" / "codex"
    executable.parent.mkdir()
    make_executable(executable)
    policy = certify(executable)
    probes = 0

    def probe(command: list[str], **kwargs: object) -> SuccessfulProbeProcess:
        nonlocal probes
        probes += 1
        return SuccessfulProbeProcess()

    monkeypatch.setattr("_codex.subprocess.run", successful_inspection)
    monkeypatch.setattr("_codex.subprocess.Popen", probe)
    monkeypatch.setattr(
        "_codex.os.replace",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError(errno.ENOSPC, "full")),
    )

    policy.prepare_run(tmp_path, tmp_path)
    policy.prepare_run(tmp_path, tmp_path)

    assert probes == 2
    assert not tuple((tmp_path / "cache").rglob("*.json"))


def test_cache_hit_still_rejects_a_forbidden_effective_capability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, policy, probes = policy_with_recorded_host_admission_probe(
        tmp_path,
        monkeypatch,
    )
    policy.prepare_run(tmp_path, tmp_path)

    def forbidden_inspection(
        command: list[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[bytes]:
        output = (
            safe_feature_output("apps")
            if command[1:3] == ["features", "list"]
            else b"[]\n"
        )
        return subprocess.CompletedProcess(command, 0, output, b"")

    monkeypatch.setattr("_codex.subprocess.run", forbidden_inspection)

    with pytest.raises(RunAdmissionError, match="forbidden capability apps is active"):
        policy.prepare_run(tmp_path, tmp_path)

    assert len(probes) == 1


def test_cache_hit_still_rechecks_the_executable_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "bin" / "codex"
    executable.parent.mkdir()
    make_executable(executable)
    boundary = tmp_path / "project"
    boundary.mkdir()
    policy = install_compatibility_fixture(executable, monkeypatch)
    probes = 0
    launches = 0

    class RunProcess:
        def wait(self) -> int:
            return 0

    def inspect(
        command: list[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[bytes]:
        if command[1:] == ["--version"]:
            return subprocess.CompletedProcess(command, 0, b"codex-cli 0.158.0\n", b"")
        return successful_inspection(command, **kwargs)

    def launch(
        command: list[str],
        **kwargs: object,
    ) -> SuccessfulProbeProcess | RunProcess:
        nonlocal probes, launches
        if command[1] == "sandbox":
            probes += 1
            return SuccessfulProbeProcess()
        launches += 1
        return RunProcess()

    monkeypatch.setenv("PATH", os.fspath(executable.parent))
    monkeypatch.setattr("_codex.subprocess.run", inspect)
    monkeypatch.setattr("_codex.subprocess.Popen", launch)

    assert run(Project(boundary, boundary), io.BytesIO()) == 0
    executable.write_text(executable.read_text() + "# replacement\n")
    executable.chmod(0o755)

    with pytest.raises(RunAdmissionError, match="SHA-256 does not match"):
        run(Project(boundary, boundary), io.BytesIO())

    assert policy.sha256 != hashlib.sha256(executable.read_bytes()).hexdigest()
    assert probes == 1
    assert launches == 1


def test_concurrent_cache_readers_observe_a_complete_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, policy, probes = policy_with_recorded_host_admission_probe(
        tmp_path,
        monkeypatch,
    )
    policy.prepare_run(tmp_path, tmp_path)

    with ThreadPoolExecutor(max_workers=8) as executor:
        invocations = tuple(
            executor.map(lambda _: policy.prepare_run(tmp_path, tmp_path), range(32))
        )

    assert len(probes) == 1
    assert all(invocation == invocations[0] for invocation in invocations)


def test_run_refuses_when_the_malformed_profile_negative_control_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    boundary, probe_gate, launch_gate = prepare_probe_run(tmp_path, monkeypatch)
    monkeypatch.setenv("FACTORY_NEGATIVE_PROFILE_EXIT", "0")

    with pytest.raises(
        RunAdmissionError,
        match="malformed permission profile was accepted",
    ):
        run(boundary, io.BytesIO())

    assert probe_gate.is_file()
    assert (tmp_path / "prompt-probe-ran").is_file()
    assert (tmp_path / "negative-profile-ran").is_file()
    assert not launch_gate.exists()


@pytest.mark.parametrize(
    ("exit_status", "stdout", "stderr"),
    [
        ("2", "", MALFORMED_PROFILE_REJECTION.decode()),
        ("1", "unexpected output\n", MALFORMED_PROFILE_REJECTION.decode()),
        ("1", "", "unrelated failure\n"),
    ],
)
def test_run_requires_pinned_malformed_profile_rejection_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exit_status: str,
    stdout: str,
    stderr: str,
) -> None:
    boundary, probe_gate, launch_gate = prepare_probe_run(tmp_path, monkeypatch)
    monkeypatch.setenv("FACTORY_NEGATIVE_PROFILE_EXIT", exit_status)
    monkeypatch.setenv("FACTORY_NEGATIVE_PROFILE_STDOUT", stdout)
    monkeypatch.setenv("FACTORY_NEGATIVE_PROFILE_STDERR", stderr)

    with pytest.raises(
        RunAdmissionError,
        match="malformed permission profile rejection was not recognized",
    ):
        run(boundary, io.BytesIO())

    assert probe_gate.is_file()
    assert (tmp_path / "negative-profile-ran").is_file()
    assert not launch_gate.exists()


@pytest.mark.parametrize(
    ("prompt_input", "reason"),
    [
        (b"{", "unrecognized prompt-input output"),
        (b"[" * 2000 + b"]" * 2000, "unrecognized prompt-input output"),
        (b"{}", "unrecognized prompt-input output"),
        (
            json.dumps(
                [
                    {
                        "type": "unknown",
                        "role": "user",
                        "content": [
                            {
                                "type": "input_text",
                                "text": "factory-host-admission-project-guidance-v1",
                            }
                        ],
                    }
                ]
            ).encode(),
            "unrecognized prompt-input output",
        ),
        (
            (
                b'[{"type":"message","role":"user","content":'
                b'[{"type":"input_text","text":"other guidance"}]}]'
            ),
            "Project Guidance was not retained",
        ),
        (
            (
                b'[{"type":"message","role":"user","content":['
                b'{"type":"input_text","text":'
                b'"factory-host-admission-project-guidance-v1"},'
                b'{"type":"input_text","text":'
                b'"factory-host-admission-forbidden-project-guidance-v1"}]}]'
            ),
            "project instruction or extension was discovered",
        ),
    ],
)
def test_run_refuses_incomplete_or_unrecognized_prompt_input_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    prompt_input: bytes,
    reason: str,
) -> None:
    executable = tmp_path / "bin" / "codex"
    executable.parent.mkdir()
    make_executable(executable)
    monkeypatch.setenv("PATH", os.fspath(executable.parent))
    policy = install_compatibility_fixture(executable, monkeypatch)
    monkeypatch.setattr("_codex._match_compatibility", lambda path, version: policy)
    launched = False

    def inspect(
        command: list[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[bytes]:
        reply = version_reply(command)
        if reply is not None:
            return reply
        if command[1:3] == ["features", "list"]:
            output = safe_feature_output()
        elif command[1:3] == ["mcp", "list"]:
            output = b"[]\n"
        else:
            output = prompt_input
        return subprocess.CompletedProcess(command, 0, output, b"")

    def launch(*args: object, **kwargs: object) -> None:
        nonlocal launched
        launched = True

    monkeypatch.setattr("_codex.subprocess.run", inspect)
    monkeypatch.setattr("_codex.subprocess.Popen", launch)

    with pytest.raises(
        RunAdmissionError,
        match=reason,
    ):
        run(Project(tmp_path, tmp_path), io.BytesIO())

    assert not launched


@pytest.mark.parametrize(
    ("probe", "reason"),
    [
        (("", "", "0"), "unexpected output"),
        (("factory-host-admission-probe-v1", "", "0"), "unexpected output"),
        (
            (
                "factory-host-admission-probe-v1\nfactory-host-admission-probe-v1\n",
                "",
                "0",
            ),
            "unexpected output",
        ),
        (("unexpected\n", "", "0"), "unexpected output"),
        (
            ("factory-host-admission-probe-v1\n", "warning\n", "0"),
            "unexpected output",
        ),
        (("factory-host-admission-probe-v1\n", "", "7"), "exit status 7"),
    ],
)
def test_run_refuses_partial_ambiguous_or_failed_host_probe_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    probe: tuple[str, str, str],
    reason: str,
) -> None:
    stdout, stderr, exit_status = probe
    boundary, probe_gate, launch_gate = prepare_probe_run(tmp_path, monkeypatch)
    monkeypatch.setenv("FACTORY_PROBE_STDOUT", stdout)
    monkeypatch.setenv("FACTORY_PROBE_STDERR", stderr)
    monkeypatch.setenv("FACTORY_PROBE_EXIT", exit_status)

    with pytest.raises(RunAdmissionError, match=reason):
        run(boundary, io.BytesIO())

    assert probe_gate.is_file()
    assert not launch_gate.exists()


def test_run_refuses_a_timed_out_host_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    boundary, probe_gate, launch_gate = prepare_probe_run(
        tmp_path,
        monkeypatch,
        host_probe_timeout_seconds=0.5,
    )
    actual_run = subprocess.run

    def run_setup(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        """The short limit belongs to the sandbox probe, not the inspectors
        or network control that run before it.
        """
        kwargs["timeout"] = 10
        return actual_run(command, **kwargs)

    monkeypatch.setattr("_codex.subprocess.run", run_setup)
    descendant_survived = tmp_path / "probe-descendant-survived"
    monkeypatch.setenv("FACTORY_PROBE_DESCENDANT_SLEEP", "1.5")
    monkeypatch.setenv("FACTORY_PROBE_DESCENDANT_SURVIVED", str(descendant_survived))
    started = time.monotonic()
    with pytest.raises(
        RunAdmissionError, match="Host Admission Probe failed: timed out"
    ):
        run(boundary, io.BytesIO())

    assert time.monotonic() - started < 3
    time.sleep(1.6)
    assert probe_gate.is_file()
    assert not launch_gate.exists()
    assert not descendant_survived.exists()


def test_run_refuses_a_timed_out_network_denial_control(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    boundary, probe_gate, launch_gate = prepare_probe_run(tmp_path, monkeypatch)
    original_run = subprocess.run

    def run_subprocess(
        command: list[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[bytes]:
        if command[0] == "/bin/bash":
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        return original_run(command, **kwargs)

    monkeypatch.setattr("_codex.subprocess.run", run_subprocess)

    with pytest.raises(
        RunAdmissionError,
        match="network denial control timed out",
    ):
        run(boundary, io.BytesIO())

    assert not probe_gate.exists()
    assert not launch_gate.exists()


def test_host_probe_ignores_bash_startup_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    boundary, probe_gate, launch_gate = prepare_probe_run(tmp_path, monkeypatch)
    startup_file = tmp_path / "bash-env"
    startup_file.write_text("exit 99\n")
    monkeypatch.setenv("BASH_ENV", os.fspath(startup_file))
    monkeypatch.setenv("BASH_FUNC_exit%%", "() { return 0; }")
    for name in _codex._CODEX_MANAGED_MARKERS:
        monkeypatch.setenv(name, "inherited")
    original_popen = subprocess.Popen

    def launch(
        command: list[str],
        **kwargs: object,
    ) -> subprocess.Popen[bytes]:
        if command[1] == "sandbox" and "factory-malformed" not in command:
            assert command[9:12] == ["/bin/bash", "-p", "-c"]
            environment = kwargs["env"]
            assert isinstance(environment, dict)
            assert "BASH_ENV" not in environment
            assert not any(name.startswith("BASH_FUNC_") for name in environment)
            assert not any(
                name in environment for name in _codex._CODEX_MANAGED_MARKERS
            )
        return original_popen(command, **kwargs)

    monkeypatch.setattr("_codex.subprocess.Popen", launch)

    spool = tmp_path / "run-request"
    spool.write_bytes(b"# Run Request\n")
    with spool.open("rb") as request:
        assert run(boundary, request) == 23
    assert probe_gate.is_file()
    assert launch_gate.is_file()


def test_run_refuses_when_synthetic_probe_data_cannot_be_created(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    boundary, probe_gate, launch_gate = prepare_probe_run(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "_codex.tempfile.TemporaryDirectory",
        lambda **kwargs: (_ for _ in ()).throw(OSError(errno.ENOSPC, "No space left")),
    )
    with pytest.raises(
        RunAdmissionError,
        match="Host Admission Probe failed: No space left",
    ):
        run(boundary, io.BytesIO())

    assert not probe_gate.exists()
    assert not launch_gate.exists()


def test_run_requires_complete_strict_configuration_inspection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "bin" / "codex"
    executable.parent.mkdir()
    make_executable(executable, version="0.158.0")
    boundary = tmp_path / "project"
    boundary.mkdir()
    git_root = tmp_path / "git-root"
    git_root.mkdir()
    monkeypatch.setenv("PATH", os.fspath(executable.parent))
    policy = install_compatibility_fixture(executable, monkeypatch)
    monkeypatch.setattr("_codex._match_compatibility", lambda path, version: policy)

    inspected: list[list[str]] = []

    def inspect(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        inspected.append(command)
        reply = version_reply(command)
        if reply is not None:
            return reply
        if command[1:3] == ["features", "list"]:
            return subprocess.CompletedProcess(command, 0, OFFICIAL_FEATURE_OUTPUT, b"")
        if command[1:3] == ["mcp", "list"]:
            return subprocess.CompletedProcess(command, 0, b"[]\n", b"")
        if command[1:3] == ["debug", "prompt-input"]:
            return successful_inspection(command)
        if command[1] == "sandbox":
            return subprocess.CompletedProcess(
                command,
                1,
                b"",
                MALFORMED_PROFILE_REJECTION,
            )
        return subprocess.CompletedProcess(command, 0, b"", b"")

    class Process(SuccessfulProbeProcess):
        def wait(self) -> int:
            return 0

    monkeypatch.setattr("_codex.subprocess.run", inspect)
    monkeypatch.setattr("_codex.subprocess.Popen", lambda *args, **kwargs: Process())

    assert run(Project(boundary, git_root), io.BytesIO()) == 0
    configuration = expected_configuration(executable, git_root)
    assert inspected[0][1] == "--version"
    assert inspected[1:3] == [
        [os.fspath(executable), "features", "list", *configuration],
        [os.fspath(executable), "mcp", "list", "--json", *configuration],
    ]
    assert any(command[1:3] == ["debug", "prompt-input"] for command in inspected)
    assert any(
        command[1] == "sandbox" and "factory-malformed" in command
        for command in inspected
    )


def test_run_refuses_a_timed_out_strict_configuration_inspector(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "bin" / "codex"
    executable.parent.mkdir()
    make_executable(executable)
    monkeypatch.setenv("PATH", os.fspath(executable.parent))
    policy = install_compatibility_fixture(
        executable,
        monkeypatch,
        host_probe_timeout_seconds=0.25,
    )
    monkeypatch.setattr("_codex._match_compatibility", lambda path, version: policy)
    launched = False

    def inspect(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[bytes] | None:
        """The short limit belongs to the strict-configuration inspectors; the
        version read that precedes them completes immediately.
        """
        reply = version_reply(command)
        if reply is not None:
            return reply
        assert kwargs["timeout"] == 0.25
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    def launch(*args: object, **kwargs: object) -> None:
        nonlocal launched
        launched = True

    monkeypatch.setattr("_codex.subprocess.run", inspect)
    monkeypatch.setattr("_codex.subprocess.Popen", launch)

    with pytest.raises(
        RunAdmissionError,
        match="strict configuration inspection failed: timed out",
    ):
        run(Project(tmp_path, tmp_path), io.BytesIO())

    assert not launched


def test_run_refuses_stderr_from_a_successful_strict_configuration_inspector(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "bin" / "codex"
    executable.parent.mkdir()
    make_executable(executable)
    monkeypatch.setenv("PATH", os.fspath(executable.parent))
    policy = install_compatibility_fixture(executable, monkeypatch)
    monkeypatch.setattr("_codex._match_compatibility", lambda path, version: policy)
    launched = False

    def inspect(
        command: list[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[bytes]:
        reply = version_reply(command)
        if reply is not None:
            return reply
        if command[1:3] == ["features", "list"]:
            return subprocess.CompletedProcess(
                command,
                0,
                safe_feature_output(),
                b"unknown warning\n",
            )
        return subprocess.CompletedProcess(command, 0, b"[]\n", b"")

    def launch(*args: object, **kwargs: object) -> None:
        nonlocal launched
        launched = True

    monkeypatch.setattr("_codex.subprocess.run", inspect)
    monkeypatch.setattr("_codex.subprocess.Popen", launch)

    with pytest.raises(
        RunAdmissionError,
        match="strict configuration inspection failed: unexpected stderr",
    ):
        run(Project(tmp_path, tmp_path), io.BytesIO())

    assert not launched


@pytest.mark.parametrize(
    "features, mcp",
    [
        (b"apps stable false\n", b"[]\n"),
        (b"apps                         stable             true\n", b"[]\n"),
        (
            b"apps                         stable             false unexpected\n",
            b"[]\n",
        ),
        (b"apps                         stable             false\n" * 2, b"[]\n"),
        (b"apps                         stable             false\n", b"{}\n"),
        (
            b"apps                         stable             false\n",
            b'[{"name": "unexpected"}]\n',
        ),
    ],
)
def test_run_refuses_incomplete_or_unrecognized_inspector_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    features: bytes,
    mcp: bytes,
) -> None:
    executable = tmp_path / "bin" / "codex"
    executable.parent.mkdir()
    make_executable(executable, version="0.158.0")
    monkeypatch.setenv("PATH", os.fspath(executable.parent))
    policy = install_compatibility_fixture(executable, monkeypatch)
    monkeypatch.setattr("_codex._match_compatibility", lambda path, version: policy)
    launched = False

    def inspect(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        reply = version_reply(command)
        if reply is not None:
            return reply
        if command[1:3] == ["features", "list"]:
            output = features
        else:
            output = mcp
        return subprocess.CompletedProcess(command, 0, output, b"")

    def launch(*args: object, **kwargs: object) -> None:
        nonlocal launched
        launched = True

    monkeypatch.setattr("_codex.subprocess.run", inspect)
    monkeypatch.setattr("_codex.subprocess.Popen", launch)

    with pytest.raises(RunAdmissionError, match="strict configuration"):
        run(Project(tmp_path, tmp_path), io.BytesIO())

    assert not launched


@pytest.mark.parametrize(
    "forbidden_feature",
    [
        "apps",
        "hooks",
        "plugins",
        "remote_plugin",
        "browser_use",
        "browser_use_external",
        "browser_use_full_cdp_access",
        "in_app_browser",
        "computer_use",
        "multi_agent",
        "memories",
    ],
)
def test_run_refuses_each_active_effective_capability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    forbidden_feature: str,
) -> None:
    executable = tmp_path / "bin" / "codex"
    executable.parent.mkdir()
    make_executable(executable, version="0.158.0")
    monkeypatch.setenv("PATH", os.fspath(executable.parent))
    policy = install_compatibility_fixture(executable, monkeypatch)
    monkeypatch.setattr("_codex._match_compatibility", lambda path, version: policy)
    launched = False
    features = safe_feature_output(forbidden_feature)

    def inspect(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        reply = version_reply(command)
        if reply is not None:
            return reply
        if command[1:3] == ["features", "list"]:
            output = features
        else:
            output = b"[]\n"
        return subprocess.CompletedProcess(command, 0, output, b"")

    def launch(*args: object, **kwargs: object) -> None:
        nonlocal launched
        launched = True

    monkeypatch.setattr("_codex.subprocess.run", inspect)
    monkeypatch.setattr("_codex.subprocess.Popen", launch)

    with pytest.raises(
        RunAdmissionError,
        match=f"forbidden capability {forbidden_feature}",
    ):
        run(Project(tmp_path, tmp_path), io.BytesIO())

    assert not launched


@pytest.mark.parametrize(
    "features, mcp, expected_reason",
    [
        (
            safe_feature_output(),
            b'[{"name": "managed-server"}]\n',
            "active MCP servers",
        ),
        (safe_feature_output("hooks"), b"[]\n", "forbidden capability hooks"),
    ],
)
def test_run_refuses_active_or_ambiguous_extension_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    features: bytes,
    mcp: bytes,
    expected_reason: str,
) -> None:
    executable = tmp_path / "bin" / "codex"
    executable.parent.mkdir()
    make_executable(executable, version="0.158.0")
    monkeypatch.setenv("PATH", os.fspath(executable.parent))
    policy = install_compatibility_fixture(executable, monkeypatch)
    monkeypatch.setattr("_codex._match_compatibility", lambda path, version: policy)

    def inspect(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        reply = version_reply(command)
        if reply is not None:
            return reply
        if command[1:3] == ["features", "list"]:
            output = features
        else:
            output = mcp
        return subprocess.CompletedProcess(command, 0, output, b"")

    monkeypatch.setattr("_codex.subprocess.run", inspect)

    with pytest.raises(RunAdmissionError, match=expected_reason):
        run(Project(tmp_path, tmp_path), io.BytesIO())


def test_force_and_hidden_environment_state_cannot_bypass_capability_rejection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The executable's effective result contradicts Factory's runtime
    disable, modelling a higher-precedence hidden or environment policy.
    """
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    executable = tmp_path / "bin" / "codex"
    executable.parent.mkdir()
    make_inspector_executable(executable)
    monkeypatch.setenv(
        "PATH", os.pathsep.join((str(executable.parent), os.environ["PATH"]))
    )
    monkeypatch.setenv("FACTORY_FEATURES", safe_feature_output("apps").decode("ascii"))
    monkeypatch.setenv("FACTORY_MCP", "[]\n")
    monkeypatch.setenv("FACTORY_CONTEXT_WARN_BYTES", "0")
    policy = install_compatibility_fixture(executable, monkeypatch)
    monkeypatch.setattr("_codex._match_compatibility", lambda path, version: policy)
    (tmp_path / "context").write_text("context")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        ["factory", "--path", str(tmp_path), "--file", "context", "--force", "Fix"],
    )
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"")))

    assert factory.main() == 1
    assert "forbidden capability apps is active" in capsys.readouterr().err


@pytest.mark.parametrize(
    "features, mcp, environment",
    [
        (
            safe_feature_output(),
            b"[]\n",
            {},
        ),
        (
            safe_feature_output()
            + b"future_feature               stable             true\n",
            b"[]\n",
            {},
        ),
        (b"apps                         stable             true\n", b"[]\n", {}),
        (
            b"apps                         stable             false\n",
            b"[]\n",
            {"FACTORY_FEATURE_EXIT": "1"},
        ),
        (b"apps                         stable             false\n", b"not json\n", {}),
    ],
)
def test_run_uses_executable_fakes_for_strict_inspector_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    features: bytes,
    mcp: bytes,
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
    policy = install_compatibility_fixture(executable, monkeypatch)
    monkeypatch.setattr("_codex._match_compatibility", lambda path, version: policy)

    if environment:
        with pytest.raises(RunAdmissionError, match="strict configuration"):
            run(Project(tmp_path, tmp_path), io.BytesIO())
    elif features == safe_feature_output() and mcp == b"[]\n":
        spool = tmp_path / "request"
        spool.write_bytes(b"# Run Request\n")
        with spool.open("rb") as request:
            assert run(Project(tmp_path, tmp_path), request) == 0
    else:
        with pytest.raises(RunAdmissionError, match="strict configuration"):
            run(Project(tmp_path, tmp_path), io.BytesIO())


def test_run_refuses_an_installed_codex_older_than_newest_stable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    publisher_responses: PublisherResponses,
    live_newest_stable_verification: None,
) -> None:
    executable = (
        tmp_path / ".codex" / "packages" / "standalone" / "releases" / "bin" / "codex"
    )
    executable.parent.mkdir(parents=True)
    make_executable(executable, version="0.158.0")
    monkeypatch.setenv("PATH", os.fspath(executable.parent))
    publisher_responses.install(
        {
            CODEX_GITHUB_RELEASES: github_release("rust-v0.159.1"),
            CODEX_NPM_DIST_TAGS: npm_dist_tags(latest="0.159.1"),
        }
    )

    with pytest.raises(RunAdmissionError) as refused:
        run(Project(tmp_path, tmp_path), io.BytesIO())

    message = str(refused.value)
    assert "0.158.0" in message and "0.159.1" in message
    assert "older than the newest stable release" in message
    assert "curl -fsSL https://chatgpt.com/codex/install.sh | sh" in message


def test_run_refuses_a_local_version_failure_before_any_publisher_lookup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    publisher_responses: PublisherResponses,
    live_newest_stable_verification: None,
) -> None:
    """The default harness fake is silent, so the local version read refuses
    on its own; the offline guard turns any publisher GET into a failure.
    """

    with pytest.raises(RunAdmissionError, match="reported an unrecognized version"):
        run(Project(tmp_path, tmp_path), io.BytesIO())

    assert publisher_responses.calls == []


def test_run_refuses_an_uncertified_newest_release_and_names_the_factory_update(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    publisher_responses: PublisherResponses,
    live_newest_stable_verification: None,
) -> None:
    executable = tmp_path / "bin" / "codex"
    executable.parent.mkdir()
    make_executable(executable, version="0.156.1")
    monkeypatch.setenv("PATH", os.fspath(executable.parent))
    publisher_responses.install(
        {
            CODEX_GITHUB_RELEASES: github_release("rust-v0.156.1"),
            CODEX_NPM_DIST_TAGS: npm_dist_tags(latest="0.156.1"),
        }
    )

    with pytest.raises(RunAdmissionError) as refused:
        run(Project(tmp_path, tmp_path), io.BytesIO())

    message = str(refused.value)
    assert "version is not in the Codex Compatibility Set" in message
    assert "update Factory to a release that certifies Codex 0.156.1" in message


def test_identity_matches_an_exact_private_compatibility_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "codex"
    make_executable(executable)
    install_compatibility_fixture(executable, monkeypatch)

    assert _match_compatibility(executable, "0.158.0") == certify(executable)


_NPM_PLATFORM_LAYOUT_CASES = [
    pytest.param(system, machine, id=f"{system}-{machine}")
    for system, machine in sorted(_codex._NPM_PLATFORM_LAYOUT)
]


@pytest.mark.parametrize(("system", "machine"), _NPM_PLATFORM_LAYOUT_CASES)
def test_npm_launcher_resolves_to_the_sibling_platform_binary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    system: str,
    machine: str,
) -> None:
    package, triple = _codex._NPM_PLATFORM_LAYOUT[(system, machine)]
    monkeypatch.setattr(platform, "system", lambda: system)
    monkeypatch.setattr(platform, "machine", lambda: machine)
    package_root = tmp_path / "lib" / "node_modules" / "@openai" / "codex"
    launcher = package_root / "bin" / "codex.js"
    launcher.parent.mkdir(parents=True)
    make_executable(launcher)
    platform_package = package_root.parent / package
    platform_package.mkdir()
    (platform_package / "package.json").write_text("{}")
    binary = platform_package / "vendor" / triple / "bin" / "codex"
    binary.parent.mkdir(parents=True)
    make_executable(binary)
    path_directory = tmp_path / "bin"
    path_directory.mkdir()
    (path_directory / "codex").symlink_to(launcher)
    monkeypatch.setenv("PATH", os.fspath(path_directory))

    assert _codex._resolve_executable() == binary
    assert _codex._observed_install_channel(binary) == "npm"


@pytest.mark.parametrize(("system", "machine"), _NPM_PLATFORM_LAYOUT_CASES)
def test_npm_launcher_resolves_to_the_nested_platform_binary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    system: str,
    machine: str,
) -> None:
    package, triple = _codex._NPM_PLATFORM_LAYOUT[(system, machine)]
    monkeypatch.setattr(platform, "system", lambda: system)
    monkeypatch.setattr(platform, "machine", lambda: machine)
    package_root = tmp_path / "lib" / "node_modules" / "@openai" / "codex"
    launcher = package_root / "bin" / "codex.js"
    launcher.parent.mkdir(parents=True)
    make_executable(launcher)
    platform_package = package_root / "node_modules" / "@openai" / package
    platform_package.mkdir(parents=True)
    (platform_package / "package.json").write_text("{}")
    binary = platform_package / "vendor" / triple / "bin" / "codex"
    binary.parent.mkdir(parents=True)
    make_executable(binary)
    sibling_package = package_root.parent / package
    sibling_package.mkdir()
    (sibling_package / "package.json").write_text("{}")
    sibling_binary = sibling_package / "vendor" / triple / "bin" / "codex"
    sibling_binary.parent.mkdir(parents=True)
    make_executable(sibling_binary)
    path_directory = tmp_path / "bin"
    path_directory.mkdir()
    (path_directory / "codex").symlink_to(launcher)
    monkeypatch.setenv("PATH", os.fspath(path_directory))

    assert _codex._resolve_executable() == binary
    assert _codex._observed_install_channel(binary) == "npm"


def test_npm_launcher_refuses_a_missing_platform_binary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = tmp_path / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
    launcher.parent.mkdir(parents=True)
    make_executable(launcher)
    path_directory = tmp_path / "bin"
    path_directory.mkdir()
    (path_directory / "codex").symlink_to(launcher)
    monkeypatch.setenv("PATH", os.fspath(path_directory))

    with pytest.raises(
        RunAdmissionError, match="cannot resolve its vendored platform binary"
    ):
        _codex._resolve_executable()


@pytest.mark.parametrize(("system", "machine"), _NPM_PLATFORM_LAYOUT_CASES)
def test_npm_launcher_uses_package_local_vendor_when_platform_package_is_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    system: str,
    machine: str,
) -> None:
    _, triple = _codex._NPM_PLATFORM_LAYOUT[(system, machine)]
    monkeypatch.setattr(platform, "system", lambda: system)
    monkeypatch.setattr(platform, "machine", lambda: machine)
    package_root = tmp_path / "node_modules" / "@openai" / "codex"
    launcher = package_root / "bin" / "codex.js"
    launcher.parent.mkdir(parents=True)
    make_executable(launcher)
    binary = package_root / "vendor" / triple / "bin" / "codex"
    binary.parent.mkdir(parents=True)
    make_executable(binary)
    path_directory = tmp_path / "bin"
    path_directory.mkdir()
    (path_directory / "codex").symlink_to(launcher)
    monkeypatch.setenv("PATH", os.fspath(path_directory))

    assert _codex._resolve_executable() == binary
    assert _codex._observed_install_channel(binary) == "npm"


def test_codex_compatibility_refuses_the_wrong_install_channel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binary = (
        tmp_path
        / "node_modules"
        / "@openai"
        / "codex-linux-x64"
        / "vendor"
        / "x86_64-unknown-linux-musl"
        / "bin"
        / "codex"
    )
    binary.parent.mkdir(parents=True)
    make_executable(binary)
    policy = certify(binary)
    monkeypatch.setattr("_codex._COMPATIBILITY_SET", (policy,))

    with pytest.raises(RunAdmissionError, match="install channel does not match"):
        _match_compatibility(binary, "0.158.0")


def test_identity_refuses_multiple_policies_for_one_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "codex"
    make_executable(executable)
    first = certify(executable)
    second = certify(executable, host_probe_timeout_seconds=11)
    monkeypatch.setattr("_codex._COMPATIBILITY_SET", (first, second))

    with pytest.raises(RunAdmissionError, match="matches multiple policies"):
        _match_compatibility(executable, "0.158.0")


@pytest.mark.parametrize(
    "output",
    [
        b"0.158.0\n",
        b"codex 0.158.0\n",
        b"codex-cli 0.158.0 extra\n",
        b"codex-cli 0.158.0\r\n",
        b"codex-cli 0.158.0\nmore\n",
    ],
)
def test_version_report_refuses_malformed_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    output: bytes,
) -> None:
    executable = tmp_path / "codex"
    make_executable(executable)
    monkeypatch.setattr(
        "_codex.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, output, b""),
    )

    with pytest.raises(RunAdmissionError, match="reported an unrecognized version"):
        _reported_version(executable)


def test_identity_refuses_a_changed_digest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "codex"
    make_executable(executable)
    install_compatibility_fixture(executable, monkeypatch)
    executable.write_text(executable.read_text() + "# changed\n")
    executable.chmod(0o755)

    with pytest.raises(RunAdmissionError, match="SHA-256 does not match"):
        _match_compatibility(executable, "0.158.0")


def test_identity_refuses_another_platform(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "codex"
    make_executable(executable)
    entry = certify(executable)
    monkeypatch.setattr(
        "_codex._COMPATIBILITY_SET",
        (
            _Codex01580Policy(
                sha256=entry.sha256,
                operating_system="other-os",
                architecture=entry.architecture,
                canonical_path=entry.canonical_path,
                filesystem_identity=entry.filesystem_identity,
            ),
        ),
    )

    with pytest.raises(RunAdmissionError, match="platform does not match"):
        _match_compatibility(executable, "0.158.0")


def test_identity_refuses_another_architecture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "codex"
    make_executable(executable)
    entry = certify(executable)
    monkeypatch.setattr(
        "_codex._COMPATIBILITY_SET",
        (
            _Codex01580Policy(
                sha256=entry.sha256,
                operating_system=entry.operating_system,
                architecture="other-architecture",
                canonical_path=entry.canonical_path,
                filesystem_identity=entry.filesystem_identity,
            ),
        ),
    )

    with pytest.raises(RunAdmissionError, match="platform does not match"):
        _match_compatibility(executable, "0.158.0")


def test_identity_uses_the_canonical_target_of_a_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "installation" / "codex"
    executable.parent.mkdir()
    make_executable(executable)
    symlink = tmp_path / "bin" / "codex"
    symlink.parent.mkdir()
    symlink.symlink_to(executable)
    install_compatibility_fixture(executable, monkeypatch)

    assert _match_compatibility(symlink.resolve(), "0.158.0") == certify(executable)


def test_identity_refuses_a_replaced_filesystem_object(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "codex"
    make_executable(executable)
    install_compatibility_fixture(executable, monkeypatch)
    replacement = tmp_path / "replacement"
    shutil.copyfile(executable, replacement)
    replacement.chmod(0o755)
    replacement.replace(executable)

    with pytest.raises(RunAdmissionError, match="filesystem identity does not match"):
        _match_compatibility(executable, "0.158.0")


def test_identity_refuses_replacement_between_version_and_artifact_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
        _match_compatibility(executable, "0.158.0")


def test_run_refuses_when_codex_is_missing_from_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    empty_path = tmp_path / "empty"
    empty_path.mkdir()
    monkeypatch.setenv("PATH", str(empty_path))

    with pytest.raises(
        RunAdmissionError,
        match=r'^Codex executable "codex" was not found on PATH; install Codex or add it to PATH$',
    ):
        run(Project(tmp_path, tmp_path), io.BytesIO())


def test_run_canonicalizes_a_direct_codex_executable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("_codex._COMPATIBILITY_SET", ())
    executable = tmp_path / "bin" / "codex"
    executable.parent.mkdir()
    make_executable(executable)
    monkeypatch.setenv("PATH", os.fspath(executable.parent))

    with pytest.raises(RunAdmissionError) as raised:
        run(Project(tmp_path, tmp_path), io.BytesIO())

    assert str(raised.value) == "Codex Compatibility Set is empty; no Run is supported"
    assert raised.value.executable == executable


def test_run_canonicalizes_a_symlinked_codex_executable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("_codex._COMPATIBILITY_SET", ())
    executable = tmp_path / "installation" / "codex-real"
    executable.parent.mkdir()
    make_executable(executable)
    path_directory = tmp_path / "bin"
    path_directory.mkdir()
    (path_directory / "codex").symlink_to(executable)
    monkeypatch.setenv("PATH", os.fspath(path_directory))

    with pytest.raises(RunAdmissionError) as raised:
        run(Project(tmp_path, tmp_path), io.BytesIO())

    assert str(raised.value) == "Codex Compatibility Set is empty; no Run is supported"
    assert raised.value.executable == executable


def test_run_refuses_a_dangling_codex_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path_directory = tmp_path / "bin"
    path_directory.mkdir()
    candidate = path_directory / "codex"
    candidate.symlink_to(tmp_path / "missing-codex")
    monkeypatch.setenv("PATH", os.fspath(path_directory))

    with pytest.raises(RunAdmissionError) as raised:
        run(Project(tmp_path, tmp_path), io.BytesIO())

    assert str(raised.value) == (
        f'Codex executable "{candidate}" is a dangling symlink; '
        "repair or remove it from PATH"
    )


def test_run_refuses_a_codex_symlink_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path_directory = tmp_path / "bin"
    path_directory.mkdir()
    candidate = path_directory / "codex"
    candidate.symlink_to(candidate)
    monkeypatch.setenv("PATH", os.fspath(path_directory))

    with pytest.raises(RunAdmissionError) as raised:
        run(Project(tmp_path, tmp_path), io.BytesIO())

    assert str(raised.value) == (
        f'Codex executable "{candidate}" contains a symlink loop; '
        "repair or remove it from PATH"
    )


def test_run_refuses_a_codex_symlink_loop_reported_as_os_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path_directory = tmp_path / "bin"
    path_directory.mkdir()
    candidate = path_directory / "codex"
    make_executable(candidate)
    original_resolve = Path.resolve

    def looped_resolve(path: Path, *, strict: bool = False) -> Path:
        if path == candidate and strict:
            raise OSError(
                errno.ELOOP, "Too many levels of symbolic links", os.fspath(path)
            )
        return original_resolve(path, strict=strict)

    monkeypatch.setenv("PATH", os.fspath(path_directory))
    monkeypatch.setattr(Path, "resolve", looped_resolve)

    with pytest.raises(RunAdmissionError) as raised:
        run(Project(tmp_path, tmp_path), io.BytesIO())

    assert str(raised.value) == (
        f'Codex executable "{candidate}" contains a symlink loop; '
        "repair or remove it from PATH"
    )


@pytest.mark.parametrize("kind", ["non-regular", "non-executable"])
def test_run_refuses_an_unusable_codex_path_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
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
        run(Project(tmp_path, tmp_path), io.BytesIO())

    assert str(raised.value) == expected


def test_run_reports_an_inaccessible_codex_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
        run(Project(tmp_path, tmp_path), io.BytesIO())

    assert str(raised.value) == (
        f'Codex executable "{candidate}" could not be inspected: '
        "Permission denied; check its permissions"
    )


def test_run_reports_an_inaccessible_resolved_codex_executable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
        run(Project(tmp_path, tmp_path), io.BytesIO())

    assert str(raised.value) == (
        f'Codex executable "{candidate}" could not be inspected: '
        "Permission denied; check its permissions"
    )


def test_factory_reports_an_admission_failure_without_a_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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


def test_an_empty_compatibility_set_refuses_every_run_before_launching_codex(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Uncertified releases stay installable while refusing every Run."""

    executable = tmp_path / "bin" / "codex"
    executable.parent.mkdir()
    make_executable(executable, version="0.158.0")
    monkeypatch.setenv("PATH", os.fspath(executable.parent))
    monkeypatch.setattr("_codex._COMPATIBILITY_SET", ())

    request = b"# Run Request\n\n## Instruction\n\nDiagnose this failure\n"
    spool = io.BytesIO(request)
    with pytest.raises(RunAdmissionError) as raised:
        run(Project(tmp_path, tmp_path), spool)

    assert str(raised.value) == "Codex Compatibility Set is empty; no Run is supported"
    assert raised.value.executable == executable
    assert spool.read() == request
