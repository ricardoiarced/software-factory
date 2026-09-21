"""Codex Run Admission and lifecycle boundary."""

from collections.abc import Callable
from dataclasses import dataclass
import errno
import hashlib
from importlib.metadata import version
import json
import os
from pathlib import Path
import platform
import re
import signal
import socket
import stat
import subprocess
import tempfile
from types import FrameType
from typing import Any, BinaryIO, ClassVar

from _project import Project


class RunAdmissionError(Exception):
    """Codex cannot be launched under the certified compatibility contract."""

    def __init__(self, message: str, *, executable: Path | None = None) -> None:
        super().__init__(message)
        self.executable = executable


def _cache_directory() -> Path:
    from platformdirs import user_cache_path

    return user_cache_path("factory", appauthor=False)


def _factory_version() -> str:
    return version("software-factory")


def _host_identity() -> dict[str, str]:
    """Return the OS/kernel facts whose change invalidates probe evidence."""

    return {
        "release": platform.release(),
        "version": platform.version(),
    }


@dataclass(frozen=True)
class _Codex01551Policy:
    """The complete Run Admission policy certified for one Codex release."""

    sha256: str
    operating_system: str
    architecture: str
    canonical_path: Path
    filesystem_identity: tuple[int, int]
    host_probe_timeout_seconds: float = 10

    version: ClassVar[str] = "0.155.1"
    _feature_output: ClassVar[re.Pattern[bytes]] = re.compile(
        rb"([a-z][a-z0-9_]*) {2,}"
        rb"(stable|under development|experimental|deprecated|removed)"
        rb" {2,}(true|false)\n"
    )
    _required_feature_states: ClassVar[tuple[tuple[str, bool], ...]] = (
        ("apps", False),
        ("hooks", False),
        ("plugins", False),
        ("remote_plugin", False),
        ("browser_use", False),
        ("browser_use_external", False),
        ("browser_use_full_cdp", False),
        ("computer_use", False),
        ("multi_agent", False),
        ("memories", False),
        ("skip_host_skill_discovery", True),
    )
    _host_probe_success: ClassVar[bytes] = b"factory-host-admission-probe-v1\n"
    _malformed_profile_rejection: ClassVar[bytes] = (
        b'Error: invalid type: string "{", expected struct PermissionProfileToml\n'
        b'in `permissions`\n'
    )
    _host_probe_project_guidance: ClassVar[str] = (
        "factory-host-admission-project-guidance-v1"
    )
    _host_probe_forbidden_guidance: ClassVar[str] = (
        "factory-host-admission-forbidden-project-guidance-v1"
    )
    _host_probe_prompt: ClassVar[str] = (
        "# Project Guidance\n\n"
        + _host_probe_project_guidance
        + "\n\n# Run Request\n\n"
        "## Instruction\n\n"
        "factory-host-admission-run-request-v1"
    )
    _host_probe_script: ClassVar[str] = r"""
set -u

boundary=$1
temporary_write=$2
slash_tmp_write=$3
executable=$4
network_port=$5
root=${boundary%/*}
inside_read=$boundary/inside-read
inside_write=$boundary/inside-write
parent_read=$root/parent-read
parent_write=$root/parent-write
sibling_read=$root/sibling/outside-read
sibling_write=$root/sibling/outside-write

read_exact() {
  IFS= read -r value < "$1" && [ "$value" = "$2" ]
}
can_read() {
  ( : < "$1" ) 2>/dev/null
}
can_write() {
  ( umask 077; printf '%s\n' 'probe-write' > "$1" ) 2>/dev/null
}
can_network() {
  ( : > "/dev/tcp/127.0.0.1/$1" ) 2>/dev/null
}

read_exact "$inside_read" 'inside-boundary' || exit 10
can_write "$inside_write" || exit 11
read_exact "$inside_write" 'probe-write' || exit 12
can_read "$parent_read" && exit 13
can_write "$parent_write" && exit 14
can_read "$sibling_read" && exit 15
can_write "$sibling_write" && exit 16
can_write "$temporary_write" && exit 17
can_write "$slash_tmp_write" && exit 18
can_read "/bin/sh" || exit 19
can_read "$executable" || exit 20
can_network "$network_port" && exit 21
printf '%s\n' 'factory-host-admission-probe-v1'
""".strip()

    def _permission_profile(self) -> str:
        return (
            '{extends = ":workspace", filesystem = {":root" = "deny", '
            '":minimal" = "read", ":tmpdir" = "deny", ":slash_tmp" = "deny", '
            f'{_quoted_absolute_path(self.canonical_path.parent)} = "read", '
            '":workspace_roots" = {"." = "write"}}, network = {enabled = false}}'
        )

    def _configuration(self, enclosing_git_root: Path) -> list[str]:
        return [
            "-c", "approval_policy=\"never\"",
            "-c", "project_doc_max_bytes=0",
            "-c", "project_root_markers=[]",
            "-c", "allow_login_shell=false",
            "-c", "shell_environment_policy.inherit=\"core\"",
            "-c", "shell_environment_policy.ignore_default_excludes=false",
            "-c", "features.apps=false",
            "-c", "features.hooks=false",
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
            "-c", "permissions.factory-isolated=" + self._permission_profile(),
            "-c", (
                "projects."
                + json.dumps(os.fspath(enclosing_git_root), ensure_ascii=False)
                + ".trust_level=\"untrusted\""
            ),
        ]

    def _invocation(
        self, project_boundary: Path, enclosing_git_root: Path,
    ) -> list[str]:
        return [
            os.fspath(self.canonical_path),
            "exec",
            "--ephemeral",
            "--strict-config",
            "--ignore-user-config",
            "--ignore-rules",
            "-C",
            os.fspath(project_boundary),
            *self._configuration(enclosing_git_root),
            "-",
        ]

    def _strict_configuration_failure(
        self, reason: str,
    ) -> RunAdmissionError:
        return RunAdmissionError(
            f"Codex executable {_quoted_absolute_path(self.canonical_path)} "
            "strict configuration "
            f"inspection failed: {reason}; install the certified artifact",
            executable=self.canonical_path,
        )

    def _run_inspector(self, command: list[str], *, cwd: Path) -> bytes:
        try:
            result = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                cwd=cwd,
                timeout=self.host_probe_timeout_seconds,
            )
        except subprocess.TimeoutExpired as error:
            raise self._strict_configuration_failure("timed out") from error
        except OSError as error:
            raise self._strict_configuration_failure(
                error.strerror or str(error),
            ) from error
        if result.returncode:
            raise self._strict_configuration_failure(
                f"exit status {result.returncode}",
            )
        if result.stderr:
            raise self._strict_configuration_failure("unexpected stderr")
        return result.stdout

    def _inspect_strict_configuration(
        self, project_boundary: Path, enclosing_git_root: Path,
    ) -> None:
        configuration = self._configuration(enclosing_git_root)
        features = self._run_inspector(
            [os.fspath(self.canonical_path), "features", "list", *configuration],
            cwd=project_boundary,
        )
        required_states = dict(self._required_feature_states)
        states: dict[str, bool] = {}
        for line in features.splitlines(keepends=True):
            match = self._feature_output.fullmatch(line)
            if match is None:
                raise self._strict_configuration_failure(
                    "unrecognized feature output",
                )
            name = match.group(1).decode("ascii")
            if name not in required_states:
                raise self._strict_configuration_failure(
                    "unrecognized feature output",
                )
            if name in states:
                raise self._strict_configuration_failure(
                    "contradictory feature output",
                )
            states[name] = match.group(3) == b"true"
        if states != required_states:
            for name, required_state in required_states.items():
                if name in states and states[name] != required_state:
                    raise self._strict_configuration_failure(
                        f"forbidden capability {name} is active",
                    )
            raise self._strict_configuration_failure(
                "incomplete feature output",
            )

        mcp = self._run_inspector(
            [os.fspath(self.canonical_path), "mcp", "list", "--json", *configuration],
            cwd=project_boundary,
        )
        try:
            configured_mcp = json.loads(mcp)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise self._strict_configuration_failure(
                "unrecognized MCP output",
            ) from error
        if configured_mcp != []:
            raise self._strict_configuration_failure("active MCP servers")

    @staticmethod
    def _prompt_input_strings(value: object) -> tuple[str, ...] | None:
        if not isinstance(value, list):
            return None
        strings: list[str] = []
        for message in value:
            if (
                not isinstance(message, dict)
                or message.get("type") != "message"
                or message.get("role") not in ("developer", "user")
            ):
                return None
            content = message.get("content")
            if not isinstance(content, list):
                return None
            for item in content:
                if (
                    not isinstance(item, dict)
                    or item.get("type") != "input_text"
                    or not isinstance(item.get("text"), str)
                ):
                    return None
                strings.append(item["text"])
        return tuple(strings)

    def _inspect_project_guidance(
        self, boundary: Path, configuration: list[str],
    ) -> None:
        output = self._run_inspector([
            os.fspath(self.canonical_path),
            "debug", "prompt-input",
            *configuration,
            self._host_probe_prompt,
        ], cwd=boundary)
        try:
            prompt_input = json.loads(output)
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
            raise self._host_probe_failure(
                "unrecognized prompt-input output",
            ) from error
        strings = self._prompt_input_strings(prompt_input)
        if strings is None:
            raise self._host_probe_failure("unrecognized prompt-input output")
        if not any(
            self._host_probe_project_guidance in value
            for value in strings
        ):
            raise self._host_probe_failure("Project Guidance was not retained")
        if any(
            self._host_probe_forbidden_guidance in value
            for value in strings
        ):
            raise self._host_probe_failure(
                "project instruction or extension was discovered",
            )

    def _inspect_malformed_profile_rejection(self, boundary: Path) -> None:
        command = [
            os.fspath(self.canonical_path),
            "sandbox",
            "-C", os.fspath(boundary),
            "-c", "permissions.factory-malformed={",
            "-P", "factory-malformed",
            "--",
            "/bin/sh", "-c", "exit 0",
        ]
        try:
            result = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                cwd=boundary,
                timeout=self.host_probe_timeout_seconds,
            )
        except subprocess.TimeoutExpired as error:
            raise self._host_probe_failure(
                "malformed permission profile check timed out",
            ) from error
        except OSError as error:
            raise self._host_probe_failure(
                error.strerror or str(error),
            ) from error
        if result.returncode == 0:
            raise self._host_probe_failure(
                "malformed permission profile was accepted",
            )
        if (
            result.returncode != 1
            or result.stdout
            or not result.stderr.endswith(self._malformed_profile_rejection)
        ):
            raise self._host_probe_failure(
                "malformed permission profile rejection was not recognized",
            )

    def _host_probe_failure(self, reason: str) -> RunAdmissionError:
        return RunAdmissionError(
            f"Codex executable {_quoted_absolute_path(self.canonical_path)} "
            "Host Admission Probe "
            f"failed: {reason}; this host cannot enforce Project Data Isolation",
            executable=self.canonical_path,
        )

    def _host_probe_cache_payload(self) -> bytes:
        document = {
            "schema": 1,
            "success": True,
            "factory_version": _factory_version(),
            "codex_version": self.version,
            "executable_sha256": self.sha256,
            "operating_system": self.operating_system,
            "architecture": self.architecture,
            "host_identity": _host_identity(),
        }
        return (
            json.dumps(document, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode("ascii")

    def _host_probe_cache_path(self, payload: bytes) -> Path:
        key = hashlib.sha256(payload).hexdigest()
        return _cache_directory() / "host-admission-probe" / f"{key}.json"

    def _has_cached_host_probe_success(self, payload: bytes) -> bool:
        try:
            return self._host_probe_cache_path(payload).read_bytes() == payload
        except OSError:
            return False

    def _store_host_probe_success(self, payload: bytes) -> None:
        path = self._host_probe_cache_path(payload)
        temporary: Path | None = None
        try:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=path.parent,
                prefix=".host-admission-probe-", delete=False,
            ) as target:
                temporary = Path(target.name)
                os.fchmod(target.fileno(), 0o600)
                target.write(payload)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, path)
            temporary = None
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError:
            pass
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def _run_host_admission_probe(self) -> None:
        try:
            temporary_root = Path(tempfile.gettempdir()).resolve()
            with tempfile.TemporaryDirectory(
                prefix="factory-host-admission-probe-",
            ) as raw_root:
                root = Path(raw_root)
                boundary = root / "project"
                sibling = root / "sibling"
                boundary.mkdir()
                sibling.mkdir()
                (boundary / ".git").mkdir()
                project_skills = boundary / ".agents" / "skills" / "factory-probe"
                project_skills.mkdir(parents=True)
                project_config = boundary / ".codex"
                project_config.mkdir()
                inside_read = boundary / "inside-read"
                parent_read = root / "parent-read"
                sibling_read = sibling / "outside-read"
                temporary_write = temporary_root / f".{root.name}-temporary-write"
                slash_tmp_write = Path("/tmp") / f".{root.name}-slash-tmp-write"
                inside_read.write_text("inside-boundary\n")
                parent_read.write_text("outside-boundary\n")
                sibling_read.write_text("outside-boundary\n")
                (root / "AGENTS.md").write_text(
                    self._host_probe_forbidden_guidance + "\n",
                )
                (boundary / "AGENTS.md").write_text(
                    self._host_probe_forbidden_guidance + "\n",
                )
                (project_skills / "SKILL.md").write_text(
                    "---\nname: factory-probe\ndescription: "
                    + self._host_probe_forbidden_guidance
                    + "\n---\n\n"
                    + self._host_probe_forbidden_guidance
                    + "\n",
                )
                (project_config / "config.toml").write_text(
                    "[features]\napps = true\n\n"
                    "[mcp_servers.factory_probe]\ncommand = \"/bin/false\"\n",
                )

                configuration = self._configuration(boundary)
                self._inspect_strict_configuration(boundary, boundary)
                self._inspect_project_guidance(boundary, configuration)
                unsafe_bash_environment = {
                    "BASH_ENV", "BASHOPTS", "CDPATH", "ENV", "GLOBIGNORE",
                    "SHELLOPTS",
                }
                probe_environment = {
                    name: value for name, value in os.environ.items()
                    if (
                        name not in unsafe_bash_environment
                        and not name.startswith("BASH_FUNC_")
                    )
                }

                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
                    listener.bind(("127.0.0.1", 0))
                    listener.listen(2)
                    network_port = listener.getsockname()[1]
                    try:
                        control = subprocess.run(
                            [
                                "/bin/bash", "-p", "-c",
                                ': > "/dev/tcp/127.0.0.1/$1"',
                                "--", str(network_port),
                            ],
                            stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            check=False,
                            timeout=self.host_probe_timeout_seconds,
                            env=probe_environment,
                        )
                    except subprocess.TimeoutExpired as error:
                        raise self._host_probe_failure(
                            "network denial control timed out",
                        ) from error
                    if control.returncode:
                        raise self._host_probe_failure(
                            "network denial control could not connect",
                        )

                    invocation = [
                        os.fspath(self.canonical_path),
                        "sandbox",
                        "-C", os.fspath(boundary),
                        "-c", "permissions.factory-isolated="
                        + self._permission_profile(),
                        "-P", "factory-isolated",
                        "--",
                        "/bin/bash", "-p", "-c", self._host_probe_script,
                        "factory-host-admission-probe",
                        os.fspath(boundary),
                        os.fspath(temporary_write),
                        os.fspath(slash_tmp_write),
                        os.fspath(self.canonical_path),
                        str(network_port),
                    ]
                    process = subprocess.Popen(
                        invocation,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        start_new_session=True,
                        env=probe_environment,
                    )
                    try:
                        stdout, stderr = process.communicate(
                            timeout=self.host_probe_timeout_seconds,
                        )
                    except subprocess.TimeoutExpired as error:
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        process.communicate()
                        raise self._host_probe_failure("timed out") from error
                    finally:
                        temporary_write.unlink(missing_ok=True)
                        if slash_tmp_write != temporary_write:
                            slash_tmp_write.unlink(missing_ok=True)

                if process.returncode:
                    raise self._host_probe_failure(
                        f"exit status {process.returncode}",
                    )
                if stdout != self._host_probe_success or stderr:
                    raise self._host_probe_failure("unexpected output")
                self._inspect_malformed_profile_rejection(boundary)
        except OSError as error:
            raise self._host_probe_failure(
                error.strerror or str(error),
            ) from error

    def prepare_run(
        self, project_boundary: Path, enclosing_git_root: Path,
    ) -> list[str]:
        """Apply all certified checks and build this policy's launch invocation."""

        self._inspect_strict_configuration(project_boundary, enclosing_git_root)
        cache_payload = self._host_probe_cache_payload()
        if not self._has_cached_host_probe_success(cache_payload):
            self._run_host_admission_probe()
            self._store_host_probe_success(cache_payload)
        return self._invocation(project_boundary, enclosing_git_root)


# Certification adds complete exact policies here. Keeping production empty is
# fail-closed until an artifact has passed the real-host certification suite.
_COMPATIBILITY_SET: tuple[_Codex01551Policy, ...] = ()
_VERSION_OUTPUT = re.compile(rb"codex ([0-9]+(?:\.[0-9]+)*(?:[-+][0-9A-Za-z.-]+)?)\n\Z")
_CHILD_RUNTIME_SIGNALS = (
    signal.SIGHUP,
    signal.SIGINT,
    signal.SIGQUIT,
    signal.SIGTERM,
    signal.SIGALRM,
    signal.SIGUSR1,
    signal.SIGUSR2,
    signal.SIGPIPE,
    signal.SIGPROF,
    signal.SIGVTALRM,
    signal.SIGXCPU,
    signal.SIGXFSZ,
)


def _defer_runtime_signal_to_child(
    _signal_number: int, _frame: FrameType | None,
) -> None:
    """Let the foreground child status decide the Run's signal outcome."""


def _quoted_absolute_path(path: Path) -> str:
    return json.dumps(str(path.absolute()), ensure_ascii=False)


def _inspection_failure(path: Path, error: OSError) -> RunAdmissionError:
    cause = error.strerror or str(error)
    return RunAdmissionError(
        f"Codex executable {_quoted_absolute_path(path)} could not be inspected: "
        f"{cause}; check its permissions"
    )


def _resolve_executable() -> Path:
    first_failure: RunAdmissionError | None = None
    for directory in os.get_exec_path():
        candidate = Path(directory or os.curdir) / "codex"
        try:
            candidate.lstat()
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError as error:
            if first_failure is None:
                first_failure = _inspection_failure(candidate, error)
            continue

        try:
            executable = candidate.resolve(strict=True)
            metadata = executable.stat()
        except FileNotFoundError:
            failure = RunAdmissionError(
                f"Codex executable {_quoted_absolute_path(candidate)} is a dangling symlink; "
                "repair or remove it from PATH"
            )
        except RuntimeError:
            failure = RunAdmissionError(
                f"Codex executable {_quoted_absolute_path(candidate)} contains a symlink loop; "
                "repair or remove it from PATH"
            )
        except OSError as error:
            if error.errno == errno.ELOOP:
                failure = RunAdmissionError(
                    f"Codex executable {_quoted_absolute_path(candidate)} contains a symlink loop; "
                    "repair or remove it from PATH"
                )
            else:
                failure = _inspection_failure(candidate, error)
        else:
            if not stat.S_ISREG(metadata.st_mode):
                failure = RunAdmissionError(
                    f"Codex executable {_quoted_absolute_path(executable)} "
                    "is not a regular file; "
                    "install Codex or remove it from PATH"
                )
            elif not os.access(executable, os.X_OK):
                failure = RunAdmissionError(
                    f"Codex executable {_quoted_absolute_path(executable)} is not executable; "
                    "grant execute permission or replace it"
                )
            else:
                return executable
        if first_failure is None:
            first_failure = failure

    if first_failure is not None:
        raise first_failure
    raise RunAdmissionError(
        'Codex executable "codex" was not found on PATH; '
        "install Codex or add it to PATH"
    )


def _reported_version(executable: Path) -> str:
    try:
        result = subprocess.run(
            [os.fspath(executable), "--version"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError as error:
        raise RunAdmissionError(
            f"Codex executable {_quoted_absolute_path(executable)} could not report its version: "
            f"{error.strerror or error}; check its permissions",
            executable=executable,
        ) from error
    if result.returncode:
        raise RunAdmissionError(
            f"Codex executable {_quoted_absolute_path(executable)} could not report its version: "
            f"exit status {result.returncode}",
            executable=executable,
        )
    match = _VERSION_OUTPUT.fullmatch(result.stdout)
    if match is None:
        raise RunAdmissionError(
            f"Codex executable {_quoted_absolute_path(executable)} reported an unrecognized version; "
            "install a certified Codex release",
            executable=executable,
        )
    return match.group(1).decode("ascii")


def _artifact_identity(executable: Path) -> tuple[str, tuple[int, int]]:
    """Read digest and filesystem identity from the same opened artifact."""

    try:
        with executable.open("rb") as artifact:
            metadata = os.fstat(artifact.fileno())
            digest = hashlib.file_digest(artifact, "sha256").hexdigest()
    except OSError as error:
        raise _inspection_failure(executable, error) from error
    return digest, (metadata.st_dev, metadata.st_ino)


def _match_compatibility(executable: Path) -> _Codex01551Policy:
    """Return the sole policy whose complete immutable identity matches."""

    version = _reported_version(executable)
    digest, filesystem_identity = _artifact_identity(executable)
    operating_system = platform.system()
    architecture = platform.machine()

    matches = tuple(
        policy for policy in _COMPATIBILITY_SET
        if (
            policy.version == version
            and policy.sha256 == digest
            and policy.operating_system == operating_system
            and policy.architecture == architecture
            and policy.canonical_path == executable
            and policy.filesystem_identity == filesystem_identity
        )
    )
    if len(matches) == 1:
        return matches[0]
    if matches:
        raise RunAdmissionError(
            f"Codex executable {_quoted_absolute_path(executable)} matches multiple policies "
            "in the Codex Compatibility Set; install the certified artifact",
            executable=executable,
        )

    candidates = tuple(policy for policy in _COMPATIBILITY_SET if policy.version == version)
    if not candidates:
        reason = "version is not in the Codex Compatibility Set"
    elif not any(
        policy.operating_system == operating_system and policy.architecture == architecture
        for policy in candidates
    ):
        reason = "platform does not match the Codex Compatibility Set"
    elif not any(policy.sha256 == digest for policy in candidates):
        reason = "SHA-256 does not match the Codex Compatibility Set"
    elif not any(policy.canonical_path == executable for policy in candidates):
        reason = "canonical path does not match the Codex Compatibility Set"
    else:
        reason = "filesystem identity does not match the Codex Compatibility Set"
    raise RunAdmissionError(
        f"Codex executable {_quoted_absolute_path(executable)} {reason}; "
        "install the certified artifact",
        executable=executable,
    )


def _changed_artifact_failure(policy: _Codex01551Policy) -> RunAdmissionError:
    executable = policy.canonical_path
    return RunAdmissionError(
        f"Codex executable {_quoted_absolute_path(executable)} changed "
        "during Run Admission; refusing to evaluate its replacement",
        executable=executable,
    )


def _recheck_artifact(policy: _Codex01551Policy) -> None:
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
        raise _inspection_failure(executable, error) from error

    if (
        digest != policy.sha256
        or (
            path_metadata_after_hash.st_dev,
            path_metadata_after_hash.st_ino,
        ) != filesystem_identity
    ):
        raise _changed_artifact_failure(policy)


def run(project: Project, run_request_spool: BinaryIO) -> int:
    """Refuse every Run until a Codex artifact and host tuple is certified."""
    executable = _resolve_executable()
    if not _COMPATIBILITY_SET:
        raise RunAdmissionError(
            "Codex Compatibility Set is empty; no Run is supported",
            executable=executable,
        )
    policy = _match_compatibility(executable)
    invocation = policy.prepare_run(
        project.boundary, project.enclosing_git_root,
    )
    _recheck_artifact(policy)
    previous_handlers: dict[
        signal.Signals,
        Callable[[int, FrameType | None], Any] | int | None,
    ] = {}
    try:
        for runtime_signal in _CHILD_RUNTIME_SIGNALS:
            previous_handler = signal.getsignal(runtime_signal)
            previous_handlers[runtime_signal] = previous_handler
            if previous_handler != signal.SIG_IGN:
                signal.signal(runtime_signal, _defer_runtime_signal_to_child)
        try:
            process = subprocess.Popen(
                invocation,
                cwd=project.boundary,
                stdin=run_request_spool,
                stdout=None,
                stderr=None,
                env=None,
                shell=False,
                start_new_session=False,
            )
        except OSError as error:
            raise RunAdmissionError(
                f"Codex executable {_quoted_absolute_path(executable)} could not start: "
                f"{error.strerror or error}",
                executable=executable,
            ) from error
        status = process.wait()
    finally:
        for handled_signal, handler in previous_handlers.items():
            signal.signal(handled_signal, handler)
    if status < 0:
        termination_signal = -status
        if termination_signal != signal.SIGKILL:
            signal.signal(termination_signal, signal.SIG_DFL)
            signal.pthread_sigmask(signal.SIG_UNBLOCK, {termination_signal})
        os.kill(os.getpid(), termination_signal)
    return status
