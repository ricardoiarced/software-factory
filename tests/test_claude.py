"""Claude Code Run Admission: the live newest-stable gate, fail-closed launch."""

import contextlib
import errno
import io
import json
import os
import platform
import socket
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import BinaryIO, Self

import pytest
from conftest import (
    CLAUDE_GITHUB_RELEASES,
    CLAUDE_LATEST_POINTER,
    CLAUDE_NPM_DIST_TAGS,
    PublisherResponses,
    github_release,
    npm_dist_tags,
)

import _claude
import _publisher
from _claude import ClaudeCodeRunner
from _harness import PreparedRun, RunAdmissionError, launch
from _project import Project


def make_claude_executable(path: Path, version: str) -> None:
    path.write_text(f"#!/bin/sh\nprintf '{version} (Claude Code)\\n'\n")
    path.chmod(0o755)


def make_doctor_aware_claude(
    path: Path,
    version: str,
    doctor_output: str = "claude doctor: no managed settings found\\n",
    doctor_status: int = 0,
) -> None:
    path.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "doctor" ]; then\n'
        f"  printf '{doctor_output}'\n"
        f"  exit {doctor_status}\n"
        "fi\n"
        f"printf '{version} (Claude Code)\\n'\n",
    )
    path.chmod(0o755)


def install_native_claude(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    version: str,
) -> Path:
    executable_directory = tmp_path / ".local" / "share" / "claude" / "versions"
    executable_directory.mkdir(parents=True)
    executable = executable_directory / "claude"
    make_claude_executable(executable, version)
    monkeypatch.setenv("PATH", os.fspath(executable_directory))
    return executable


def install_policy(
    executable: Path,
    monkeypatch: pytest.MonkeyPatch,
    **overrides: object,
) -> _claude._ClaudePolicy:
    """Inject the exact policy the installed fake artifact identifies as."""

    digest, filesystem_identity = _claude._artifact_identity(executable)
    values: dict[str, object] = {
        "version": "9.9.9",
        "sha256": digest,
        "operating_system": platform.system(),
        "architecture": platform.machine(),
        "install_channel": _claude._observed_install_channel(executable),
        "canonical_path": executable,
        "filesystem_identity": filesystem_identity,
    }
    values.update(overrides)
    policy = _claude._ClaudePolicy(**values)
    monkeypatch.setattr(_claude, "_CLAUDE_COMPATIBILITY_SET", (policy,))
    return policy


def run_claude(project: Project) -> io.BytesIO:
    spool = io.BytesIO(b"# Run Request\n\n## Instruction\n\nFix")
    ClaudeCodeRunner().run(project, spool)
    return spool


def agreeing_claude_publisher(responses: PublisherResponses, newest: str) -> None:
    responses.install(
        {
            CLAUDE_NPM_DIST_TAGS: npm_dist_tags(latest=newest, stable="9.7.3"),
            CLAUDE_GITHUB_RELEASES: github_release(f"v{newest}"),
            CLAUDE_LATEST_POINTER: f"{newest}\n".encode(),
        }
    )


def test_claude_run_refuses_an_installed_release_older_than_newest_stable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    publisher_responses: PublisherResponses,
    live_newest_stable_verification: None,
) -> None:
    install_native_claude(tmp_path, monkeypatch, version="9.8.0")
    agreeing_claude_publisher(publisher_responses, "9.9.9")

    with pytest.raises(RunAdmissionError) as refused:
        run_claude(Project(tmp_path, tmp_path))

    message = str(refused.value)
    assert "9.8.0" in message and "9.9.9" in message
    assert "older than the newest stable release" in message
    assert "update Claude Code with claude update and retry" in message


def test_claude_run_refuses_a_local_version_failure_before_any_publisher_lookup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    publisher_responses: PublisherResponses,
    live_newest_stable_verification: None,
) -> None:
    """The default harness fake is silent, so the local version read refuses
    on its own; the offline guard turns any publisher GET into a failure.
    """

    with pytest.raises(RunAdmissionError, match="reported an unrecognized version"):
        run_claude(Project(tmp_path, tmp_path))

    assert publisher_responses.calls == []


def test_claude_run_reaches_the_empty_compatibility_set_when_installed_is_newest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    publisher_responses: PublisherResponses,
    live_newest_stable_verification: None,
) -> None:
    install_native_claude(tmp_path, monkeypatch, version="9.9.9")
    agreeing_claude_publisher(publisher_responses, "9.9.9")

    with pytest.raises(
        RunAdmissionError,
        match=(
            "Claude Compatibility Set is empty; "
            "update Factory to a release that certifies Claude Code"
        ),
    ):
        run_claude(Project(tmp_path, tmp_path))


def test_claude_run_refuses_when_the_publisher_lookup_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    publisher_responses: PublisherResponses,
    live_newest_stable_verification: None,
) -> None:
    install_native_claude(tmp_path, monkeypatch, version="9.9.9")
    publisher_responses.install(
        {
            CLAUDE_NPM_DIST_TAGS: _publisher._TransportError("connection refused"),
            CLAUDE_GITHUB_RELEASES: _publisher._TransportError("connection refused"),
            CLAUDE_LATEST_POINTER: _publisher._TransportError("connection refused"),
        }
    )

    with pytest.raises(
        RunAdmissionError,
        match="could not reach the Claude Code publisher",
    ):
        run_claude(Project(tmp_path, tmp_path))


@pytest.mark.parametrize(
    ("source", "marker"),
    [
        ("managed-settings.json", "managed settings file"),
        ("managed-settings.d/drop-in.json", "drop-in directory"),
        ("managed-mcp.json", "managed MCP configuration"),
        (".claude/remote-settings.json", "remote server-policy cache"),
    ],
)
def test_claude_run_refuses_whenever_any_managed_policy_source_is_detected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
    marker: str,
) -> None:
    executable = install_native_claude(tmp_path, monkeypatch, version="9.9.9")
    system_sources = tmp_path / "etc" / "claude-code"
    if source == ".claude/remote-settings.json":
        monkeypatch.setenv("HOME", str(tmp_path))
        planted = tmp_path / source
    elif source == "managed-settings.d/drop-in.json":
        drop_ins = system_sources / "managed-settings.d"
        monkeypatch.setattr(_claude, "_MANAGED_SETTINGS_DROP_INS", drop_ins)
        planted = drop_ins / "drop-in.json"
    elif source == "managed-settings.json":
        monkeypatch.setattr(_claude, "_MANAGED_SETTINGS", system_sources / source)
        planted = system_sources / source
    else:
        monkeypatch.setattr(_claude, "_MANAGED_MCP", system_sources / source)
        planted = system_sources / source
    planted.parent.mkdir(parents=True, exist_ok=True)
    planted.write_text("{}\n", encoding="utf-8")

    with pytest.raises(RunAdmissionError, match=marker):
        _claude._require_no_managed_policy(executable)


@pytest.mark.parametrize(
    "doctor_output",
    [
        "Managed settings (remote): enabled for example.com\\n",
        "Organization policy: configured\\n",
    ],
)
def test_claude_run_refuses_a_doctor_reported_policy_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    doctor_output: str,
) -> None:
    executable_directory = tmp_path / "claude-fake"
    executable_directory.mkdir()
    executable = executable_directory / "claude"
    make_doctor_aware_claude(
        executable,
        "9.9.9",
        doctor_output=doctor_output,
    )
    monkeypatch.setenv("PATH", os.fspath(executable_directory))

    with pytest.raises(
        RunAdmissionError,
        match="claude doctor reports a managed policy source",
    ):
        _claude._require_no_managed_policy(executable)


@pytest.mark.parametrize(
    ("line", "verdict"),
    [
        (
            (
                "Managed settings (remote): not fetched — not available with a "
                "custom ANTHROPIC_BASE_URL"
            ),
            "unavailable",
        ),
        (
            "Organization policy: not fetched with a custom ANTHROPIC_BASE_URL",
            "unavailable",
        ),
        ("Organization policy: not fetched", "refuse"),
        ("Organization policy: not fetched (network unreachable)", "refuse"),
        ("Managed settings (remote): not fetched by the proxy", "refuse"),
        ("Managed settings (remote): enabled for example.com", "refuse"),
        ("Organization policy: configured", "refuse"),
        ("Managed settings (remote): absent", "benign"),
        ("Organization policy: skipped", "benign"),
        ("Managed settings (remote): something new", "refuse"),
        ("Organization policy", "refuse"),
    ],
)
def test_the_doctor_policy_line_classifier_pins_the_strict_shapes(
    line: str,
    verdict: str,
) -> None:
    assert _claude._classify_doctor_policy_line(line) == verdict


@pytest.mark.parametrize(
    "doctor_output",
    [
        (
            "Managed settings (remote): not fetched — not available with a "
            "custom ANTHROPIC_BASE_URL\\n"
            "Organization policy: not fetched with a custom ANTHROPIC_BASE_URL\\n"
        ),
        "Managed settings (remote): absent\\nOrganization policy: skipped\\n",
    ],
    ids=["unavailable", "benign"],
)
def test_the_managed_policy_gate_allows_unavailable_and_benign_surfaces(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
    doctor_output: str,
) -> None:
    executable_directory = tmp_path / "claude-fake"
    executable_directory.mkdir()
    executable = executable_directory / "claude"
    make_doctor_aware_claude(
        executable,
        "9.9.9",
        doctor_output=doctor_output,
    )
    monkeypatch.setenv("PATH", os.fspath(executable_directory))

    _claude._require_no_managed_policy(executable)

    stderr = capfd.readouterr().err
    if "not fetched" in doctor_output:
        assert (
            "remote managed-policy surfaces unavailable — custom endpoint; "
            "residual risk accepted" in stderr
        )
    else:
        assert stderr == ""


def test_the_managed_policy_gate_refuses_a_configured_line_after_an_unavailable_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    executable_directory = tmp_path / "claude-fake"
    executable_directory.mkdir()
    executable = executable_directory / "claude"
    make_doctor_aware_claude(
        executable,
        "9.9.9",
        doctor_output=(
            "Organization policy: not fetched with a custom "
            "ANTHROPIC_BASE_URL\\n"
            "Managed settings (remote): configured\\n"
        ),
    )
    monkeypatch.setenv("PATH", os.fspath(executable_directory))

    with pytest.raises(
        RunAdmissionError,
        match="claude doctor reports a managed policy source",
    ):
        _claude._require_no_managed_policy(executable)

    assert capfd.readouterr().err == ""


@pytest.mark.parametrize("doctor_status", [1, 2])
def test_the_managed_policy_gate_fails_closed_when_doctor_cannot_be_inspected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    doctor_status: int,
) -> None:
    executable_directory = tmp_path / "claude-fake"
    executable_directory.mkdir()
    executable = executable_directory / "claude"
    make_doctor_aware_claude(
        executable,
        "9.9.9",
        doctor_status=doctor_status,
    )
    monkeypatch.setenv("PATH", os.fspath(executable_directory))

    with pytest.raises(
        RunAdmissionError,
        match="could not inspect the effective Claude Code managed policy",
    ):
        _claude._require_no_managed_policy(executable)


def test_the_managed_policy_gate_fails_closed_on_silent_doctor_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable_directory = tmp_path / "claude-fake"
    executable_directory.mkdir()
    executable = executable_directory / "claude"
    make_doctor_aware_claude(executable, "9.9.9", doctor_output="")
    monkeypatch.setenv("PATH", os.fspath(executable_directory))

    with pytest.raises(
        RunAdmissionError,
        match="could not inspect the effective Claude Code managed policy",
    ):
        _claude._require_no_managed_policy(executable)


def test_the_managed_policy_gate_passes_an_unmanaged_host(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable_directory = tmp_path / "claude-fake"
    executable_directory.mkdir()
    executable = executable_directory / "claude"
    make_doctor_aware_claude(executable, "9.9.9")
    monkeypatch.setenv("PATH", os.fspath(executable_directory))

    _claude._require_no_managed_policy(executable)


def test_run_admission_refuses_a_detected_managed_policy_after_the_tuple_match(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = install_native_claude(tmp_path, monkeypatch, version="9.9.9")
    install_policy(executable, monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "remote-settings.json").write_text("{}\n")

    with pytest.raises(RunAdmissionError, match="remote server-policy cache"):
        run_claude(Project(tmp_path, tmp_path))


def test_a_version_mismatch_refuses_before_the_managed_policy_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = install_native_claude(tmp_path, monkeypatch, version="9.9.9")
    install_policy(executable, monkeypatch, version="9.9.10")
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "remote-settings.json").write_text("{}\n")

    with pytest.raises(
        RunAdmissionError,
        match="version is not in the Claude Compatibility Set",
    ):
        run_claude(Project(tmp_path, tmp_path))


@pytest.mark.parametrize("missing", ["bwrap", "socat"])
def test_claude_admission_preflights_the_linux_sandbox_dependencies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    missing: str,
) -> None:
    dependencies = tmp_path / "dependencies"
    dependencies.mkdir()
    for name in ("bwrap", "socat"):
        if name == missing:
            continue
        executable = dependencies / name
        executable.write_text("#!/bin/sh\nexit 0\n")
        executable.chmod(0o755)
    monkeypatch.setenv("PATH", os.fspath(dependencies))
    monkeypatch.setattr(_claude.platform, "system", lambda: "Linux")

    with pytest.raises(RunAdmissionError) as refused:
        _claude._require_sandbox_dependencies()

    assert missing in str(refused.value)


def test_claude_admission_preflight_passes_with_both_dependencies_present(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dependencies = tmp_path / "dependencies"
    dependencies.mkdir()
    for name in ("bwrap", "socat"):
        executable = dependencies / name
        executable.write_text("#!/bin/sh\nexit 0\n")
        executable.chmod(0o755)
    monkeypatch.setenv("PATH", os.fspath(dependencies))
    monkeypatch.setattr(_claude.platform, "system", lambda: "Linux")

    _claude._require_sandbox_dependencies()


def test_claude_admission_skips_the_linux_sandbox_preflight_off_linux(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    empty = tmp_path / "no-dependencies"
    empty.mkdir()
    monkeypatch.setenv("PATH", os.fspath(empty))
    monkeypatch.setattr(_claude.platform, "system", lambda: "Darwin")

    _claude._require_sandbox_dependencies()


def test_the_certified_claude_invocation_has_the_exact_candidate_recipe_shape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    boundary = tmp_path / "project"
    executable = tmp_path / "claude"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    monkeypatch.setenv("HOME", str(home))
    policy = _claude._ClaudePolicy(
        version="9.9.9",
        sha256="a" * 64,
        operating_system="Linux",
        architecture="x86_64",
        install_channel="native-installer",
        canonical_path=executable,
        filesystem_identity=(2096, 1),
    )
    settings = {
        "permissions": {"blockReadsOutsideWorkingDirectories": True},
        "sandbox": {
            "allowUnsandboxedCommands": False,
            "credentials": {
                "deny": [
                    os.fspath(home / ".aws"),
                    os.fspath(home / ".ssh"),
                ],
            },
            "enabled": True,
            "excludedCommands": [],
            "failIfUnavailable": True,
            "filesystem": {
                "allowRead": [os.fspath(boundary), os.fspath(executable)],
                "denyRead": sorted(
                    [os.fspath(boundary.parent), os.fspath(home)],
                ),
                "disabled": False,
            },
            "network": {"allow": [], "strictAllowlist": True},
        },
    }

    invocation = policy._invocation(boundary)

    assert invocation == [
        os.fspath(executable),
        "-p",
        "--restricted",
        "--safe-mode",
        "--strict-mcp-config",
        "--tools",
        "Bash,Read,Edit,Write",
        "--allowedTools",
        "Bash,Read,Edit,Write",
        "--permission-mode",
        "dontAsk",
        "--settings",
        json.dumps(
            settings,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ),
    ]
    assert json.loads(invocation[invocation.index("--settings") + 1]) == settings


def test_the_certified_read_fence_deduplicates_a_boundary_directly_under_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    boundary = home / "project"
    executable = tmp_path / "claude"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    monkeypatch.setenv("HOME", str(home))
    policy = _claude._ClaudePolicy(
        version="9.9.9",
        sha256="a" * 64,
        operating_system="Linux",
        architecture="x86_64",
        install_channel="native-installer",
        canonical_path=executable,
        filesystem_identity=(2096, 1),
    )

    fence = policy._read_fence(boundary)

    assert fence == {
        "denyRead": [os.fspath(home)],
        "allowRead": [os.fspath(boundary), os.fspath(executable)],
    }


def test_the_certified_claude_launch_plan_pins_the_nonessential_traffic_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = install_native_claude(tmp_path, monkeypatch, version="9.9.9")
    install_policy(executable, monkeypatch)

    policy = _claude._ClaudePolicy(
        version="9.9.9",
        sha256="a" * 64,
        operating_system=platform.system(),
        architecture=platform.machine(),
        install_channel="native-installer",
        canonical_path=executable,
        filesystem_identity=(2096, 1),
    )
    prepared = PreparedRun(
        harness="Claude Code",
        executable=executable,
        invocation=tuple(policy._invocation(tmp_path)),
        environment=_claude._ENVIRONMENT_PINS,
    )
    assert prepared.environment == (
        ("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "1"),
        ("DISABLE_TELEMETRY", "1"),
        ("DISABLE_ERROR_REPORTING", "1"),
    )


def test_the_shared_lifecycle_applies_the_certified_environment_pins(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    executable_directory = tmp_path / "claude-fake"
    executable_directory.mkdir()
    executable = executable_directory / "claude"
    executable.write_text(
        "#!/bin/sh\n"
        "printf '%s|%s|%s' \"$CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC\" "
        '"$DISABLE_TELEMETRY" "$DISABLE_ERROR_REPORTING"\n',
    )
    executable.chmod(0o755)
    monkeypatch.setenv("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "unset-by-host")

    descriptor, _name = tempfile.mkstemp(dir=tmp_path)
    with os.fdopen(descriptor, "r+b") as spool:
        spool.write(b"# Run Request\n\n## Instruction\n\nFix")
        spool.flush()
        spool.seek(0)
        status = launch(
            Project(tmp_path, tmp_path),
            PreparedRun(
                harness="Claude Code",
                executable=executable,
                invocation=(os.fspath(executable),),
                environment=_claude._ENVIRONMENT_PINS,
            ),
            spool,
        )

    assert status == 0
    assert capfd.readouterr().out == "1|1|1"


def test_the_shared_lifecycle_inherits_the_host_environment_without_pins(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    executable = tmp_path / "pass-through"
    executable.write_text(
        "#!/bin/sh\nprintf '%s' \"$FACTORY_PASS_THROUGH_VARIABLE\"\n",
    )
    executable.chmod(0o755)
    monkeypatch.setenv("FACTORY_PASS_THROUGH_VARIABLE", "inherited")

    descriptor, _name = tempfile.mkstemp(dir=tmp_path)
    with os.fdopen(descriptor, "r+b") as spool:
        spool.write(b"# Run Request\n\n## Instruction\n\nFix")
        spool.flush()
        spool.seek(0)
        status = launch(
            Project(tmp_path, tmp_path),
            PreparedRun(
                harness="Codex",
                executable=executable,
                invocation=(os.fspath(executable),),
            ),
            spool,
        )

    assert status == 0
    assert capfd.readouterr().out == "inherited"


def test_run_requires_a_successful_host_admission_probe_before_launching_claude(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = install_native_claude(tmp_path, monkeypatch, version="9.9.9")
    install_policy(executable, monkeypatch)
    for dependency in ("bwrap", "socat"):
        fake = executable.parent / dependency
        fake.write_text("#!/bin/sh\nexit 0\n")
        fake.chmod(0o755)
    monkeypatch.setenv("HOME", str(tmp_path))
    probe_gate = tmp_path / "probe-gate"
    launch_gate = tmp_path / "launch-gate"

    def probe(self: _claude._ClaudePolicy) -> None:
        probe_gate.write_text("ran")

    def gate_launch(
        project: Project,
        prepared: PreparedRun,
        spool: BinaryIO,
    ) -> int:
        launch_gate.write_text("launched")
        return 0

    monkeypatch.setattr(_claude._ClaudePolicy, "_run_host_admission_probe", probe)
    monkeypatch.setattr(_claude, "_cache_directory", lambda: tmp_path / "cache")
    monkeypatch.setattr(_claude, "launch", gate_launch)

    run_claude(Project(tmp_path, tmp_path))

    assert probe_gate.is_file()
    assert launch_gate.is_file()


def test_claude_admission_matches_exactly_one_channel_aware_tuple(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = install_native_claude(tmp_path, monkeypatch, version="9.9.9")
    policy = install_policy(executable, monkeypatch)

    assert _claude._match_compatibility(executable, "9.9.9") is policy


@pytest.mark.parametrize(
    ("override", "reason"),
    [
        ({"sha256": "b" * 64}, "SHA-256 does not match"),
        ({"operating_system": "SunOS"}, "platform does not match"),
        ({"install_channel": "npm"}, "install channel does not match"),
        (
            {"canonical_path": Path("/elsewhere/claude")},
            "canonical path does not match",
        ),
    ],
)
def test_claude_admission_refuses_each_wrong_tuple_dimension(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    override: dict[str, object],
    reason: str,
) -> None:
    executable = install_native_claude(tmp_path, monkeypatch, version="9.9.9")
    install_policy(executable, monkeypatch, **override)

    with pytest.raises(RunAdmissionError, match=reason):
        _claude._match_compatibility(executable, "9.9.9")


def test_claude_admission_refuses_an_unrecognized_install_channel_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable_directory = tmp_path / "claude-fake"
    executable_directory.mkdir()
    executable = executable_directory / "claude"
    make_claude_executable(executable, "9.9.9")
    monkeypatch.setenv("PATH", os.fspath(executable_directory))
    install_policy(executable, monkeypatch, install_channel="native-installer")

    with pytest.raises(
        RunAdmissionError,
        match="install channel does not match",
    ):
        _claude._match_compatibility(executable, "9.9.9")


def test_claude_admission_refuses_a_changed_artifact_at_the_recheck(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = install_native_claude(tmp_path, monkeypatch, version="9.9.9")
    policy = install_policy(executable, monkeypatch)
    _claude._recheck_artifact(policy)
    make_claude_executable(executable, "9.9.10")

    with pytest.raises(
        RunAdmissionError,
        match="changed during Run Admission",
    ):
        _claude._recheck_artifact(policy)


def make_probe_policy(tmp_path: Path) -> _claude._ClaudePolicy:
    """A policy whose identity is irrelevant to the probe behavior under test."""

    executable = tmp_path / "probe-claude"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    return _claude._ClaudePolicy(
        version="9.9.9",
        sha256="a" * 64,
        operating_system="Linux",
        architecture="x86_64",
        install_channel="native-installer",
        canonical_path=executable,
        filesystem_identity=(2096, 1),
    )


def policy_with_recorded_host_admission_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, _claude._ClaudePolicy, list[str]]:
    """A policy whose Host Admission Probe records each run and succeeds
    without inference."""

    executable = install_native_claude(tmp_path, monkeypatch, version="9.9.9")
    policy = install_policy(executable, monkeypatch)
    probes: list[str] = []
    monkeypatch.setattr(
        _claude._ClaudePolicy,
        "_run_host_admission_probe",
        lambda self: probes.append("ran"),
    )
    monkeypatch.setattr(_claude, "_cache_directory", lambda: tmp_path / "cache")
    return executable, policy, probes


def probe_fixture(tmp_path: Path) -> _claude._ProbeFixture:
    return _claude._build_probe_fixture(
        tmp_path / "root",
        tmp_path / "tmp",
        home=tmp_path / "home",
    )


def plant_credential_canaries(
    policy: _claude._ClaudePolicy,
    fixture: _claude._ProbeFixture,
) -> None:
    """Plant the credential canaries exactly as the real probe does."""

    for canary in (fixture.ssh_canary, fixture.aws_canary):
        canary.parent.mkdir(parents=True, exist_ok=True)
        canary.write_text(
            f"{policy._probe_credential_canary} {canary.parent.name}\n",
            encoding="utf-8",
        )


def simulated_probe_success(
    policy: _claude._ClaudePolicy,
    fixture: _claude._ProbeFixture,
) -> bytes:
    """Realize the inside-boundary effects and record the correct report."""

    fixture.bash_write_inside.write_text("probe-write\n", encoding="utf-8")
    fixture.tool_write_inside.write_text("probe-write\n", encoding="utf-8")
    fixture.edit_target.write_text("beta\n", encoding="utf-8")
    plant_credential_canaries(policy, fixture)
    report_bytes = (
        json.dumps(
            {
                "operations": {
                    identifier: outcome
                    for identifier, outcome, _ in policy._probe_steps
                },
                "discoveries": {name: [] for name in policy._probe_discovery_names},
            }
        )
        + "\n"
    ).encode("utf-8")
    fixture.report.write_bytes(report_bytes)
    return report_bytes


def test_run_reuses_a_complete_host_admission_probe_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, policy, probes = policy_with_recorded_host_admission_probe(
        tmp_path,
        monkeypatch,
    )

    first = policy.prepare_run(tmp_path)
    second = policy.prepare_run(tmp_path)

    assert probes == ["ran"]
    assert first == second == policy._invocation(tmp_path)


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
    policy.prepare_run(tmp_path)
    cached = next((tmp_path / "cache" / "host-admission-probe").iterdir())
    cached.write_bytes(replacement)

    policy.prepare_run(tmp_path)

    assert len(probes) == 2


def test_run_reprobes_after_factory_or_host_upgrade(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, policy, probes = policy_with_recorded_host_admission_probe(
        tmp_path,
        monkeypatch,
    )
    factory = "1.0"
    host_release = "kernel-1"

    monkeypatch.setattr("_claude._factory_version", lambda: factory)
    monkeypatch.setattr(
        "_claude._host_identity",
        lambda: {"release": host_release, "version": "host-version"},
    )
    policy.prepare_run(tmp_path)
    factory = "1.1"
    policy.prepare_run(tmp_path)
    host_release = "kernel-2"
    policy.prepare_run(tmp_path)

    assert len(probes) == 3


def test_run_reprobes_after_claude_artifact_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable, policy, probes = policy_with_recorded_host_admission_probe(
        tmp_path,
        monkeypatch,
    )

    policy.prepare_run(tmp_path)
    make_claude_executable(executable, "9.9.10")
    changed = install_policy(executable, monkeypatch)
    changed.prepare_run(tmp_path)

    assert len(probes) == 2


def test_failed_host_admission_probe_is_not_cached(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, policy, _probes = policy_with_recorded_host_admission_probe(
        tmp_path,
        monkeypatch,
    )
    attempts = 0

    def flaky(self: _claude._ClaudePolicy) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise self._host_probe_failure("exit status 7")

    monkeypatch.setattr(_claude._ClaudePolicy, "_run_host_admission_probe", flaky)

    with pytest.raises(RunAdmissionError, match="exit status 7"):
        policy.prepare_run(tmp_path)
    assert not tuple((tmp_path / "cache").rglob("*.json"))
    policy.prepare_run(tmp_path)

    assert attempts == 2


def test_host_admission_probe_success_is_atomically_stored_with_its_complete_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, policy, _ = policy_with_recorded_host_admission_probe(
        tmp_path,
        monkeypatch,
    )
    replaced: list[tuple[Path, Path, bytes]] = []
    actual_replace = os.replace

    def replace(source: Path, destination: Path) -> None:
        replaced.append((source, destination, source.read_bytes()))
        assert source.parent == destination.parent
        assert not destination.exists()
        actual_replace(source, destination)

    monkeypatch.setattr("_claude._factory_version", lambda: "factory-version")
    monkeypatch.setattr(
        "_claude._host_identity",
        lambda: {"release": "host-release", "version": "host-version"},
    )
    monkeypatch.setattr("_claude.os.replace", replace)

    policy.prepare_run(tmp_path)

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
        "claude_version": "9.9.9",
        "executable_sha256": policy.sha256,
        "operating_system": policy.operating_system,
        "architecture": policy.architecture,
        "host_identity": {"release": "host-release", "version": "host-version"},
    }


def test_cache_write_failure_leaves_admission_uncached(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, policy, probes = policy_with_recorded_host_admission_probe(
        tmp_path,
        monkeypatch,
    )
    monkeypatch.setattr(
        "_claude.os.replace",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            OSError(errno.ENOSPC, "full"),
        ),
    )

    policy.prepare_run(tmp_path)
    policy.prepare_run(tmp_path)

    assert len(probes) == 2
    assert not tuple((tmp_path / "cache").rglob("*.json"))


def test_concurrent_cache_readers_observe_a_complete_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, policy, _ = policy_with_recorded_host_admission_probe(
        tmp_path,
        monkeypatch,
    )
    policy.prepare_run(tmp_path)
    payload = policy._host_probe_cache_payload()
    problems: list[str] = []
    stop = threading.Event()

    def reader() -> None:
        while not stop.is_set():
            if not policy._has_cached_host_probe_success(payload):
                problems.append("a reader observed missing or mismatched state")
                return

    def writer() -> None:
        for _ in range(50):
            policy._store_host_probe_success(payload)

    readers = [threading.Thread(target=reader) for _ in range(4)]
    for thread in readers:
        thread.start()
    writer = threading.Thread(target=writer)
    writer.start()
    writer.join()
    stop.set()
    for thread in readers:
        thread.join()

    assert not problems


def test_the_claude_probe_fixture_plants_the_forbidden_guidance_and_boundaries(
    tmp_path: Path,
) -> None:
    fixture = probe_fixture(tmp_path)
    sentinel = _claude._ClaudePolicy._probe_forbidden_guidance

    planted = (
        fixture.root / "CLAUDE.md",
        fixture.root / "AGENTS.md",
        fixture.boundary / "CLAUDE.md",
        fixture.boundary / "CLAUDE.local.md",
        fixture.boundary / "AGENTS.md",
        fixture.boundary / ".claude" / "rules" / "factory-probe.md",
        fixture.boundary / ".claude" / "skills" / "factory-probe" / "SKILL.md",
        fixture.boundary / ".claude" / "agents" / "factory-probe.md",
    )
    for source in planted:
        assert sentinel in source.read_text(encoding="utf-8")
    assert sentinel in (fixture.boundary / ".claude-plugin" / "plugin.json").read_text(
        encoding="utf-8"
    )
    assert json.loads(
        (fixture.boundary / ".mcp.json").read_text(encoding="utf-8"),
    ) == {"mcpServers": {"factory-probe-mcp": {"command": "/bin/false"}}}
    settings = json.loads(
        (fixture.boundary / ".claude" / "settings.json").read_text(
            encoding="utf-8",
        ),
    )
    command = settings["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    assert os.fspath(fixture.tmp_hook_canary) in command
    assert fixture.escape_read_symlink.resolve() == fixture.outside_read.resolve()
    assert (
        fixture.escape_write_symlink.resolve()
        == (fixture.root / "outside-write3.txt").resolve()
    )
    for read_target in (
        fixture.outside_read,
        fixture.sibling_read,
        fixture.tmp_read,
    ):
        assert read_target.read_text(encoding="utf-8") == "outside-boundary\n"
    assert fixture.ssh_canary == (
        tmp_path / "home" / ".ssh" / "factory-probe-credential-canary.txt"
    )
    assert fixture.aws_canary == (
        tmp_path / "home" / ".aws" / "factory-probe-credential-canary.txt"
    )
    assert not fixture.ssh_canary.exists()
    assert not fixture.aws_canary.exists()
    assert not fixture.report.exists()


def test_the_claude_probe_steps_read_the_planted_credential_canaries(
    tmp_path: Path,
) -> None:
    policy = make_probe_policy(tmp_path)
    fixture = probe_fixture(tmp_path)

    prompt = policy._probe_prompt(fixture)
    expected = {identifier: outcome for identifier, outcome, _ in policy._probe_steps}

    assert os.fspath(fixture.ssh_canary) in prompt
    assert os.fspath(fixture.aws_canary) in prompt
    assert expected["bash-read-ssh"] == "failed"
    assert expected["bash-read-aws"] == "failed"


def test_every_claude_probe_bash_step_parses_cleanly(tmp_path: Path) -> None:
    """A command the shell parser cannot analyze asks the person, and under
    --permission-mode dontAsk in a non-interactive session that ask is
    denied — a forbidden step could then fail for the wrong reason."""

    policy = make_probe_policy(tmp_path)
    fixture = probe_fixture(tmp_path)
    substitutions = policy._probe_substitutions(fixture)
    bash_steps = [
        (identifier, directive[len("Bash: `") : directive.index("`", len("Bash: `"))])
        for identifier, _, directive in policy._probe_steps
        if directive.startswith("Bash: `")
    ]

    assert len(bash_steps) >= 10

    for identifier, command in bash_steps:
        parsed = subprocess.run(
            ["bash", "-n", "-c", command.format(**substitutions)],
            capture_output=True,
            check=False,
        )
        assert parsed.returncode == 0, (
            f"{identifier} does not parse: {parsed.stderr.decode()}"
        )


def test_the_probe_plants_and_cleans_the_credential_canaries(
    tmp_path: Path,
) -> None:
    policy = make_probe_policy(tmp_path)
    fixture = probe_fixture(tmp_path)

    with policy._planted_credential_canaries(fixture):
        for canary in (fixture.ssh_canary, fixture.aws_canary):
            assert canary.read_text(encoding="utf-8") == (
                f"{policy._probe_credential_canary} {canary.parent.name}\n"
            )
            assert canary.stat().st_mode & 0o777 == 0o600

    for canary in (fixture.ssh_canary, fixture.aws_canary):
        assert not canary.exists()
    assert not fixture.ssh_canary.parent.exists()
    assert not fixture.aws_canary.parent.exists()


def test_the_probe_canary_planting_keeps_pre_existing_directories(
    tmp_path: Path,
) -> None:
    policy = make_probe_policy(tmp_path)
    fixture = probe_fixture(tmp_path)
    fixture.ssh_canary.parent.mkdir(parents=True)

    with policy._planted_credential_canaries(fixture):
        pass

    assert fixture.ssh_canary.parent.is_dir()
    assert not fixture.ssh_canary.exists()
    assert not fixture.aws_canary.parent.exists()


def test_the_probe_refuses_when_credential_canaries_cannot_be_planted(
    tmp_path: Path,
) -> None:
    policy = make_probe_policy(tmp_path)
    fixture = probe_fixture(tmp_path)
    fixture.ssh_canary.parent.mkdir(parents=True)
    fixture.ssh_canary.mkdir()

    with (
        pytest.raises(
            RunAdmissionError,
            match="the credential canaries could not be planted",
        ),
        policy._planted_credential_canaries(fixture),
    ):
        pass

    assert not fixture.aws_canary.exists()


def test_the_claude_probe_refuses_unplanted_modified_or_leaked_canaries(
    tmp_path: Path,
) -> None:
    policy = make_probe_policy(tmp_path)
    fixture = probe_fixture(tmp_path)
    sentinel = policy._probe_credential_canary.encode("ascii")
    report_bytes = simulated_probe_success(policy, fixture)

    fixture.ssh_canary.unlink()
    fixture.aws_canary.unlink()
    with pytest.raises(RunAdmissionError, match="credential canary was missing"):
        policy._assert_probe_outcomes(
            fixture=fixture,
            report_bytes=report_bytes,
            stdout=b"",
            stderr=b"",
        )

    plant_credential_canaries(policy, fixture)
    fixture.ssh_canary.write_text("tampered\n", encoding="utf-8")
    with pytest.raises(RunAdmissionError, match="credential canary was modified"):
        policy._assert_probe_outcomes(
            fixture=fixture,
            report_bytes=report_bytes,
            stdout=b"",
            stderr=b"",
        )

    plant_credential_canaries(policy, fixture)
    with pytest.raises(
        RunAdmissionError,
        match="credential canary reached the probe session",
    ):
        policy._assert_probe_outcomes(
            fixture=fixture,
            report_bytes=report_bytes,
            stdout=sentinel,
            stderr=b"",
        )
    with pytest.raises(
        RunAdmissionError,
        match="credential canary reached the probe session",
    ):
        policy._assert_probe_outcomes(
            fixture=fixture,
            report_bytes=report_bytes + sentinel,
            stdout=b"",
            stderr=b"",
        )


def test_the_claude_probe_accepts_the_recorded_success_outcomes(
    tmp_path: Path,
) -> None:
    policy = make_probe_policy(tmp_path)
    fixture = probe_fixture(tmp_path)
    simulated_probe_success(policy, fixture)

    policy._assert_probe_outcomes(
        fixture=fixture,
        report_bytes=fixture.report.read_bytes(),
        stdout=b"The probe completed.",
        stderr=b"",
    )


@pytest.mark.parametrize(
    "identifier",
    [identifier for identifier, _, _ in _claude._ClaudePolicy._probe_steps],
)
def test_the_claude_probe_refuses_each_false_recorded_outcome(
    tmp_path: Path,
    identifier: str,
) -> None:
    policy = make_probe_policy(tmp_path)
    fixture = probe_fixture(tmp_path)
    simulated_probe_success(policy, fixture)
    operations = {name: outcome for name, outcome, _ in policy._probe_steps}
    operations[identifier] = (
        "failed" if operations[identifier] == "succeeded" else "succeeded"
    )
    flipped = (
        json.dumps(
            {
                "operations": operations,
                "discoveries": {name: [] for name in policy._probe_discovery_names},
            }
        )
        + "\n"
    ).encode("utf-8")

    with pytest.raises(RunAdmissionError, match=identifier):
        policy._assert_probe_outcomes(
            fixture=fixture,
            report_bytes=flipped,
            stdout=b"",
            stderr=b"",
        )


def test_the_claude_probe_refuses_discovered_guidance_and_configuration(
    tmp_path: Path,
) -> None:
    policy = make_probe_policy(tmp_path)
    fixture = probe_fixture(tmp_path)
    sentinel = _claude._ClaudePolicy._probe_forbidden_guidance.encode("ascii")
    operations = {name: outcome for name, outcome, _ in policy._probe_steps}

    def report(discoveries: dict[str, list[str]]) -> bytes:
        return (
            json.dumps(
                {
                    "operations": operations,
                    "discoveries": discoveries,
                }
            )
            + "\n"
        ).encode("utf-8")

    empty = {name: [] for name in policy._probe_discovery_names}
    discovered = dict(empty)
    discovered["skills"] = ["factory-probe"]
    with pytest.raises(
        RunAdmissionError,
        match="discovered suppressed configuration",
    ):
        policy._assert_probe_outcomes(
            fixture=fixture,
            report_bytes=report(discovered),
            stdout=b"",
            stderr=b"",
        )
    with pytest.raises(
        RunAdmissionError,
        match="forbidden guidance reached the probe session",
    ):
        policy._assert_probe_outcomes(
            fixture=fixture,
            report_bytes=report(empty),
            stdout=sentinel,
            stderr=b"",
        )
    with pytest.raises(
        RunAdmissionError,
        match="forbidden guidance reached the probe session",
    ):
        policy._assert_probe_outcomes(
            fixture=fixture,
            report_bytes=report(empty) + sentinel,
            stdout=b"",
            stderr=b"",
        )

    simulated_probe_success(policy, fixture)
    fixture.guidance_canary.write_text("obeyed\n", encoding="utf-8")
    with pytest.raises(
        RunAdmissionError,
        match="forbidden-guidance canary was created",
    ):
        policy._assert_probe_outcomes(
            fixture=fixture,
            report_bytes=report(empty),
            stdout=b"",
            stderr=b"",
        )
    fixture.guidance_canary.unlink()
    fixture.tmp_hook_canary.write_text("", encoding="utf-8")
    with pytest.raises(
        RunAdmissionError,
        match="planted settings hook executed",
    ):
        policy._assert_probe_outcomes(
            fixture=fixture,
            report_bytes=report(empty),
            stdout=b"",
            stderr=b"",
        )


@pytest.mark.parametrize(
    ("case", "phrase"),
    [
        ("missing-report", "the probe report was not written"),
        ("malformed-report", "the probe report was not valid JSON"),
        ("extra-operation", "does not cover exactly the probe steps"),
        ("missing-operation", "does not cover exactly the probe steps"),
        ("unrealized-bash-write", "inside-boundary effect was not realized"),
        ("unrealized-edit", "inside-boundary effect was not realized"),
        ("landed-parent-write", "outside-boundary write landed"),
        ("landed-tmp-write", "outside-boundary write landed"),
        ("replaced-symlink", "escape symlink was replaced"),
        ("modified-outside-read", "planted outside file was modified"),
    ],
)
def test_the_claude_probe_refuses_unproven_or_ambiguous_fixture_state(
    tmp_path: Path,
    case: str,
    phrase: str,
) -> None:
    policy = make_probe_policy(tmp_path)
    fixture = probe_fixture(tmp_path)
    report_bytes = simulated_probe_success(policy, fixture)
    operations = {name: outcome for name, outcome, _ in policy._probe_steps}
    discoveries = {name: [] for name in policy._probe_discovery_names}

    if case == "missing-report":
        report_bytes = None
    elif case == "malformed-report":
        report_bytes = b"{"
    elif case == "extra-operation":
        operations["extra-step"] = "succeeded"
    elif case == "missing-operation":
        del operations["bash-read-inside"]
    elif case == "unrealized-bash-write":
        fixture.bash_write_inside.unlink()
    elif case == "unrealized-edit":
        fixture.edit_target.write_text("alpha\n", encoding="utf-8")
    elif case == "landed-parent-write":
        fixture.outside_writes[0].write_text("probe-write\n", encoding="utf-8")
    elif case == "landed-tmp-write":
        fixture.tmp_write.write_text("probe-write\n", encoding="utf-8")
    elif case == "replaced-symlink":
        fixture.escape_write_symlink.unlink()
        fixture.escape_write_symlink.write_text(
            "probe-write\n",
            encoding="utf-8",
        )
    elif case == "modified-outside-read":
        fixture.outside_read.write_text("changed\n", encoding="utf-8")

    if case not in ("missing-report", "malformed-report"):
        report_bytes = (
            json.dumps(
                {
                    "operations": operations,
                    "discoveries": discoveries,
                }
            )
            + "\n"
        ).encode("utf-8")

    with pytest.raises(RunAdmissionError, match=phrase):
        policy._assert_probe_outcomes(
            fixture=fixture,
            report_bytes=report_bytes,
            stdout=b"",
            stderr=b"",
        )


def test_the_claude_probe_asserts_a_quiet_network_listener(
    tmp_path: Path,
) -> None:
    policy = make_probe_policy(tmp_path)

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(2)
        port = listener.getsockname()[1]

        policy._assert_network_control(listener, port)
        policy._assert_no_sandboxed_connection(listener)

        intrusion = socket.create_connection(("127.0.0.1", port))
        with pytest.raises(
            RunAdmissionError,
            match="reached the network listener",
        ):
            policy._assert_no_sandboxed_connection(listener)
        intrusion.close()


class _TimedOutProbeProcess:
    pid: int = 2**28

    def __init__(self) -> None:
        self._timed_out = False

    def communicate(
        self,
        timeout: float | None = None,
    ) -> tuple[bytes, bytes]:
        if not self._timed_out:
            self._timed_out = True
            raise subprocess.TimeoutExpired(cmd="claude", timeout=timeout)
        return b"", b""


class _QuietListener:
    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        pass

    def bind(self, address: object) -> None:
        pass

    def listen(self, backlog: int) -> None:
        pass

    def getsockname(self) -> tuple[str, int]:
        return "127.0.0.1", 43123

    def setblocking(self, flag: bool) -> None:
        pass

    def accept(self) -> tuple[object, object]:
        raise BlockingIOError()


def test_run_refuses_a_timed_out_claude_host_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = make_probe_policy(tmp_path)
    monkeypatch.setattr(_claude, "_cache_directory", lambda: tmp_path / "cache")
    monkeypatch.setattr(
        _claude._ClaudePolicy,
        "_planted_credential_canaries",
        lambda self, fixture: contextlib.nullcontext(),
    )
    monkeypatch.setattr(
        _claude._ClaudePolicy,
        "_assert_network_control",
        lambda self, listener, port: None,
    )
    monkeypatch.setattr("_claude.socket.socket", lambda *a, **k: _QuietListener())
    monkeypatch.setattr(
        "_claude.subprocess.Popen",
        lambda *a, **k: _TimedOutProbeProcess(),
    )

    with pytest.raises(RunAdmissionError, match="timed out"):
        policy.prepare_run(tmp_path)

    assert not tuple((tmp_path / "cache").rglob("*.json"))


def test_run_refuses_when_claude_probe_data_cannot_be_created(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = make_probe_policy(tmp_path)
    monkeypatch.setattr(_claude, "_cache_directory", lambda: tmp_path / "cache")

    def refuse_creation(*args: object, **kwargs: object) -> None:
        raise OSError(errno.ENOSPC, "no space")

    monkeypatch.setattr(
        "_claude.tempfile.TemporaryDirectory",
        refuse_creation,
    )

    with pytest.raises(RunAdmissionError, match="no space"):
        policy.prepare_run(tmp_path)
