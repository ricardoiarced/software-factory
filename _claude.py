"""Claude Code Run Admission and the certified Claude Code Runner."""

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
from typing import BinaryIO, ClassVar

from _harness import (
    PreparedRun,
    RunAdmissionError,
    _quoted_absolute_path,
    inspection_failure,
    launch,
    reported_version,
    resolve_executable,
)
from _project import Project
from _publisher import verify_claude_code_newest_stable


_VERSION_OUTPUT = re.compile(
    rb"([0-9]+(?:\.[0-9]+)*(?:[-+][0-9A-Za-z.-]+)?) \(Claude Code\)\n\Z",
)

# No Claude Code release has completed real-host certification; the
# certification suite and reviewed candidate manifests extend this set.
_CLAUDE_COMPATIBILITY_SET: tuple["_ClaudePolicy", ...] = ()

# The certified first-scope install channels; a tuple names the exact
# artifact family whose official surface anchored its digest.
_NATIVE_INSTALLER = "native-installer"
_NPM = "npm"

# Managed-policy sources that Run Admission refuses on detection. Factory
# cannot prove Claude Code's effective managed policy before submitting a
# Run Request, so any detected source refuses instead of attempting
# per-policy proof. The gate is read-only, uncached, and runs on every Run.
_MANAGED_SETTINGS = Path("/etc/claude-code/managed-settings.json")
_MANAGED_SETTINGS_DROP_INS = Path("/etc/claude-code/managed-settings.d")
_MANAGED_MCP = Path("/etc/claude-code/managed-mcp.json")
_DOCTOR_POLICY_MARKERS = ("Managed settings (remote)", "Organization policy")
_DOCTOR_TIMEOUT_SECONDS = 10.0


def _remote_settings_cache() -> Path:
    """The user's remote server-policy cache, resolved from the live HOME."""

    return Path.home() / ".claude" / "remote-settings.json"

# Linux sandbox dependencies of the certified boundary; missing
# dependencies refuse instead of accepting an unsandboxed fallback.
_SANDBOX_DEPENDENCIES = ("bwrap", "socat")

# Certified nonessential-traffic pins: the Run and the probe set exactly
# these values over the inherited host environment, so certification
# evidence covers production behavior.
_ENVIRONMENT_PINS: tuple[tuple[str, str], ...] = (
    ("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "1"),
    ("DISABLE_TELEMETRY", "1"),
    ("DISABLE_ERROR_REPORTING", "1"),
)


def _resolve_executable() -> Path:
    return resolve_executable("claude", "Claude Code")


def _reported_version(executable: Path) -> str:
    return reported_version(
        executable,
        pattern=_VERSION_OUTPUT,
        label="Claude Code",
        remedy="install a stable Claude Code release",
    )


def _managed_policy_failure(source: str) -> RunAdmissionError:
    return RunAdmissionError(
        f"{source} is present; Factory cannot verify Project Data Isolation "
        "under organization policy, so the Run is refused",
    )


def _doctor_failure(cause: str) -> RunAdmissionError:
    return RunAdmissionError(
        "Factory could not inspect the effective Claude Code managed policy "
        f"through a read-only claude doctor ({cause}); refusing instead of "
        "assuming no organization policy",
    )


def _managed_doctor_lines(output: str) -> tuple[str, ...]:
    """Return the doctor lines that report a configured policy source.

    The exact line shapes of the installed release are pinned by the
    real-host probe; an unrecognized shape refuses fail-closed upstream
    because the doctor evidence cannot be inspected at all.
    """

    return tuple(
        line.strip()
        for line in output.splitlines()
        if any(marker in line for marker in _DOCTOR_POLICY_MARKERS)
    )


def _require_no_managed_policy(executable: Path) -> None:
    """Refuse whenever any managed-policy source is detected."""

    if _MANAGED_SETTINGS.exists():
        raise _managed_policy_failure(
            f"The managed settings file {_MANAGED_SETTINGS}",
        )
    try:
        drop_ins = sorted(entry.name for entry in _MANAGED_SETTINGS_DROP_INS.iterdir())
    except OSError:
        drop_ins = []
    if drop_ins:
        raise _managed_policy_failure(
            f"The managed settings drop-in directory {_MANAGED_SETTINGS_DROP_INS} "
            f"contains {drop_ins[0]!r}",
        )
    if _MANAGED_MCP.exists():
        raise _managed_policy_failure(
            f"The managed MCP configuration {_MANAGED_MCP}",
        )
    remote_settings_cache = _remote_settings_cache()
    if remote_settings_cache.exists():
        raise _managed_policy_failure(
            f"The remote server-policy cache {remote_settings_cache}",
        )
    try:
        result = subprocess.run(
            [os.fspath(executable), "doctor"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=_DOCTOR_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        raise _doctor_failure("timed out") from error
    except OSError as error:
        raise _doctor_failure(error.strerror or str(error)) from error
    if result.returncode:
        raise _doctor_failure(f"exit status {result.returncode}")
    output = result.stdout.decode("utf-8", errors="replace") + result.stderr.decode(
        "utf-8", errors="replace",
    )
    if not output.strip():
        raise _doctor_failure("the doctor reported nothing to inspect")
    lines = _managed_doctor_lines(output)
    if lines:
        raise _managed_policy_failure(
            f"claude doctor reports a managed policy source ({lines[0]!r})",
        )


def _sandbox_dependency_failure(dependency: str) -> RunAdmissionError:
    return RunAdmissionError(
        f"this host is missing the {dependency} sandbox dependency required "
        "for Project Data Isolation; install it instead of accepting an "
        "unsandboxed fallback",
    )


def _require_sandbox_dependencies() -> None:
    """Preflight the sandbox dependencies of the certified boundary."""

    if platform.system() != "Linux":
        return
    for dependency in _SANDBOX_DEPENDENCIES:
        if shutil.which(dependency) is None:
            raise _sandbox_dependency_failure(dependency)


def _observed_install_channel(executable: Path) -> str | None:
    """Name the install channel this executable's canonical path was installed
    through, or None when the path matches no certified channel."""

    path = os.fspath(executable)
    if "/.local/share/claude/versions/" in path or "/.claude/local/" in path:
        return _NATIVE_INSTALLER
    if "/node_modules/@anthropic-ai/claude-code" in path:
        return _NPM
    return None


@dataclass(frozen=True)
class _ClaudePolicy:
    """The complete Run Admission policy certified for one Claude release."""

    version: str
    sha256: str
    operating_system: str
    architecture: str
    install_channel: str
    canonical_path: Path
    filesystem_identity: tuple[int, int]
    probe_timeout_seconds: float = 10
    _admitted_tools: ClassVar[str] = "Bash,Read,Edit,Write"

    def _settings_document(self) -> str:
        """The inline certified settings: sandbox fail-closed with a read fence."""

        return json.dumps(
            {
                "permissions": {
                    "blockReadsOutsideWorkingDirectories": True,
                },
                "sandbox": {
                    "allowUnsandboxedCommands": False,
                    "enabled": True,
                    "excludedCommands": [],
                    "failIfUnavailable": True,
                    "filesystem": {"disabled": False},
                    "network": {"allow": [], "strictAllowlist": True},
                },
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )

    def _invocation(self) -> list[str]:
        """The exact certified candidate recipe as one launch command.

        The Project Boundary is the sole working directory: the shared
        lifecycle launches with it as the child's cwd, and --restricted
        confines the native file tools to those working directories.
        """

        return [
            os.fspath(self.canonical_path),
            "-p",
            "--restricted",
            "--safe-mode",
            "--strict-mcp-config",
            "--tools", self._admitted_tools,
            "--allowedTools", self._admitted_tools,
            "--permission-mode", "dontAsk",
            "--settings", self._settings_document(),
        ]

    def prepare_run(self, project_boundary: Path) -> list[str]:
        """Run the cached Host Admission Probe, then build this invocation."""

        raise RunAdmissionError(
            "Claude Code executable "
            f"{_quoted_absolute_path(self.canonical_path)} has no implemented "
            "Host Admission Probe; the real-host probe must establish the "
            "sandbox fixture before any Claude Code Run; update Factory to a "
            "release that certifies Claude Code",
            executable=self.canonical_path,
        )


def _artifact_identity(executable: Path) -> tuple[str, tuple[int, int]]:
    """Read digest and filesystem identity from the same opened artifact."""

    try:
        with executable.open("rb") as artifact:
            metadata = os.fstat(artifact.fileno())
            digest = hashlib.file_digest(artifact, "sha256").hexdigest()
    except OSError as error:
        raise inspection_failure("Claude Code", executable, error) from error
    return digest, (metadata.st_dev, metadata.st_ino)


def _match_compatibility(executable: Path, version: str) -> _ClaudePolicy:
    """Return the sole policy whose complete immutable identity matches."""

    digest, filesystem_identity = _artifact_identity(executable)
    operating_system = platform.system()
    architecture = platform.machine()
    install_channel = _observed_install_channel(executable)

    matches = tuple(
        policy for policy in _CLAUDE_COMPATIBILITY_SET
        if (
            policy.version == version
            and policy.sha256 == digest
            and policy.operating_system == operating_system
            and policy.architecture == architecture
            and policy.install_channel == install_channel
            and policy.canonical_path == executable
            and policy.filesystem_identity == filesystem_identity
        )
    )
    if len(matches) == 1:
        return matches[0]
    if matches:
        raise RunAdmissionError(
            f"Claude Code executable {_quoted_absolute_path(executable)} "
            "matches multiple policies in the Claude Compatibility Set; "
            "update Factory to ship a consistent Claude Compatibility Set",
            executable=executable,
        )

    candidates = tuple(policy for policy in _CLAUDE_COMPATIBILITY_SET if policy.version == version)
    if not candidates:
        reason = "version is not in the Claude Compatibility Set"
    elif not any(
        policy.operating_system == operating_system and policy.architecture == architecture
        for policy in candidates
    ):
        reason = "platform does not match the Claude Compatibility Set"
    elif not any(policy.sha256 == digest for policy in candidates):
        reason = "SHA-256 does not match the Claude Compatibility Set"
    elif not any(policy.install_channel == install_channel for policy in candidates):
        reason = "install channel does not match the Claude Compatibility Set"
    elif not any(policy.canonical_path == executable for policy in candidates):
        reason = "canonical path does not match the Claude Compatibility Set"
    else:
        reason = "filesystem identity does not match the Claude Compatibility Set"
    raise RunAdmissionError(
        f"Claude Code executable {_quoted_absolute_path(executable)} {reason}; "
        f"update Factory to a release that certifies Claude Code {version}",
        executable=executable,
    )


def _changed_artifact_failure(policy: _ClaudePolicy) -> RunAdmissionError:
    executable = policy.canonical_path
    return RunAdmissionError(
        f"Claude Code executable {_quoted_absolute_path(executable)} changed "
        "during Run Admission; refusing to evaluate its replacement",
        executable=executable,
    )


def _recheck_artifact(policy: _ClaudePolicy) -> None:
    """Refuse a changed artifact without executing it as a new candidate."""

    executable = policy.canonical_path
    try:
        with executable.open("rb") as artifact:
            metadata = os.fstat(artifact.fileno())
            filesystem_identity = (metadata.st_dev, metadata.st_ino)
            if filesystem_identity != policy.filesystem_identity:
                raise _changed_artifact_failure(policy)
            digest = hashlib.file_digest(artifact, "sha256").hexdigest()
        path_metadata_after_hash = executable.stat()
    except OSError as error:
        raise inspection_failure("Claude Code", executable, error) from error

    if (
        digest != policy.sha256
        or (
            path_metadata_after_hash.st_dev,
            path_metadata_after_hash.st_ino,
        ) != filesystem_identity
    ):
        raise _changed_artifact_failure(policy)


def _admit_run(project: Project) -> tuple[_ClaudePolicy, list[str]]:
    """Establish the complete production evidence and strict invocation.

    The order is fixed: local executable resolution and installed-version
    read, then the live newest-stable lookup and its refusal classification,
    then the empty-set and exact-tuple Compatibility Set match, the uncached
    managed-policy gate, the sandbox dependency preflight, the Host Admission
    Probe, and the final artifact recheck.
    """

    executable = _resolve_executable()
    version = _reported_version(executable)
    verify_claude_code_newest_stable(executable, version)
    if not _CLAUDE_COMPATIBILITY_SET:
        raise RunAdmissionError(
            "Claude Compatibility Set is empty; update Factory to a release "
            "that certifies Claude Code",
            executable=executable,
        )
    policy = _match_compatibility(executable, version)
    _require_no_managed_policy(executable)
    _require_sandbox_dependencies()
    invocation = policy.prepare_run(project.boundary)
    _recheck_artifact(policy)
    return policy, invocation


class ClaudeCodeRunner:
    """The certified Claude Code Runner: fail-closed until certification."""

    def admit(self, project: Project) -> PreparedRun:
        """Establish all production evidence and freeze the launch plan."""

        policy, invocation = _admit_run(project)
        return PreparedRun(
            harness="Claude Code",
            executable=policy.canonical_path,
            invocation=tuple(invocation),
            environment=_ENVIRONMENT_PINS,
        )

    def run(self, project: Project, run_request_spool: BinaryIO) -> int:
        """Admit, then launch one Run and return Claude Code's exit status."""

        return launch(project, self.admit(project), run_request_spool)
