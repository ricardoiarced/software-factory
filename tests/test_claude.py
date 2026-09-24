"""Claude Code Run Admission: the live newest-stable gate, fail-closed launch."""

import io
import json
import os
from pathlib import Path
import platform
import tempfile

import pytest

import _claude
import _publisher
from _claude import ClaudeCodeRunner
from _harness import PreparedRun, RunAdmissionError, launch
from _project import Project
from conftest import (
    CLAUDE_GITHUB_RELEASES,
    CLAUDE_LATEST_POINTER,
    CLAUDE_NPM_DIST_TAGS,
    PublisherResponses,
    github_release,
    npm_dist_tags,
)


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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: str,
) -> Path:
    executable_directory = (
        tmp_path / ".local" / "share" / "claude" / "versions"
    )
    executable_directory.mkdir(parents=True)
    executable = executable_directory / "claude"
    make_claude_executable(executable, version)
    monkeypatch.setenv("PATH", os.fspath(executable_directory))
    return executable


def install_policy(
    executable: Path, monkeypatch: pytest.MonkeyPatch, **overrides: object,
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
    responses.install({
        CLAUDE_NPM_DIST_TAGS: npm_dist_tags(**{"latest": newest, "stable": "9.7.3"}),
        CLAUDE_GITHUB_RELEASES: github_release(f"v{newest}"),
        CLAUDE_LATEST_POINTER: f"{newest}\n".encode(),
    })


def test_claude_run_refuses_an_installed_release_older_than_newest_stable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    publisher_responses: PublisherResponses,
    live_newest_stable_verification: None,
) -> None:
    # The default harness fake is silent, so the local version read refuses
    # on its own; the offline guard turns any publisher GET into a failure.

    with pytest.raises(RunAdmissionError, match="reported an unrecognized version"):
        run_claude(Project(tmp_path, tmp_path))

    assert publisher_responses.calls == []


def test_claude_run_reaches_the_empty_compatibility_set_when_installed_is_newest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    publisher_responses: PublisherResponses,
    live_newest_stable_verification: None,
) -> None:
    install_native_claude(tmp_path, monkeypatch, version="9.9.9")
    agreeing_claude_publisher(publisher_responses, "9.9.9")

    with pytest.raises(RunAdmissionError, match=(
        "Claude Compatibility Set is empty; "
        "update Factory to a release that certifies Claude Code"
    )):
        run_claude(Project(tmp_path, tmp_path))


def test_claude_run_refuses_when_the_publisher_lookup_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    publisher_responses: PublisherResponses,
    live_newest_stable_verification: None,
) -> None:
    install_native_claude(tmp_path, monkeypatch, version="9.9.9")
    publisher_responses.install({
        CLAUDE_NPM_DIST_TAGS: _publisher._TransportError("connection refused"),
        CLAUDE_GITHUB_RELEASES: _publisher._TransportError("connection refused"),
        CLAUDE_LATEST_POINTER: _publisher._TransportError("connection refused"),
    })

    with pytest.raises(
        RunAdmissionError, match="could not reach the Claude Code publisher",
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
        executable, "9.9.9", doctor_output=doctor_output,
    )
    monkeypatch.setenv("PATH", os.fspath(executable_directory))

    with pytest.raises(
        RunAdmissionError, match="claude doctor reports a managed policy source",
    ):
        _claude._require_no_managed_policy(executable)


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
        executable, "9.9.9", doctor_status=doctor_status,
    )
    monkeypatch.setenv("PATH", os.fspath(executable_directory))

    with pytest.raises(
        RunAdmissionError, match="could not inspect the effective Claude Code managed policy",
    ):
        _claude._require_no_managed_policy(executable)


def test_the_managed_policy_gate_fails_closed_on_silent_doctor_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable_directory = tmp_path / "claude-fake"
    executable_directory.mkdir()
    executable = executable_directory / "claude"
    make_doctor_aware_claude(executable, "9.9.9", doctor_output="")
    monkeypatch.setenv("PATH", os.fspath(executable_directory))

    with pytest.raises(
        RunAdmissionError, match="could not inspect the effective Claude Code managed policy",
    ):
        _claude._require_no_managed_policy(executable)


def test_the_managed_policy_gate_passes_an_unmanaged_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable_directory = tmp_path / "claude-fake"
    executable_directory.mkdir()
    executable = executable_directory / "claude"
    make_doctor_aware_claude(executable, "9.9.9")
    monkeypatch.setenv("PATH", os.fspath(executable_directory))

    _claude._require_no_managed_policy(executable)


def test_run_admission_refuses_a_detected_managed_policy_after_the_tuple_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = install_native_claude(tmp_path, monkeypatch, version="9.9.9")
    install_policy(executable, monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "remote-settings.json").write_text("{}\n")

    with pytest.raises(RunAdmissionError, match="remote server-policy cache"):
        run_claude(Project(tmp_path, tmp_path))


def test_a_version_mismatch_refuses_before_the_managed_policy_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    empty = tmp_path / "no-dependencies"
    empty.mkdir()
    monkeypatch.setenv("PATH", os.fspath(empty))
    monkeypatch.setattr(_claude.platform, "system", lambda: "Darwin")

    _claude._require_sandbox_dependencies()


def test_the_certified_claude_invocation_has_the_exact_candidate_recipe_shape(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "claude"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
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
            "enabled": True,
            "excludedCommands": [],
            "failIfUnavailable": True,
            "filesystem": {"disabled": False},
            "network": {"allow": [], "strictAllowlist": True},
        },
    }

    invocation = policy._invocation()

    assert invocation == [
        os.fspath(executable),
        "-p",
        "--restricted",
        "--safe-mode",
        "--strict-mcp-config",
        "--tools", "Bash,Read,Edit,Write",
        "--allowedTools", "Bash,Read,Edit,Write",
        "--permission-mode", "dontAsk",
        "--settings", json.dumps(
            settings, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
        ),
    ]
    assert json.loads(invocation[invocation.index("--settings") + 1]) == settings


def test_the_certified_claude_launch_plan_pins_the_nonessential_traffic_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
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
        invocation=tuple(policy._invocation()),
        environment=_claude._ENVIRONMENT_PINS,
    )
    assert prepared.environment == (
        ("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "1"),
        ("DISABLE_TELEMETRY", "1"),
        ("DISABLE_ERROR_REPORTING", "1"),
    )


def test_the_shared_lifecycle_applies_the_certified_environment_pins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str],
) -> None:
    executable_directory = tmp_path / "claude-fake"
    executable_directory.mkdir()
    executable = executable_directory / "claude"
    executable.write_text(
        "#!/bin/sh\n"
        'printf \'%s|%s|%s\' "$CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC" '
        '"$DISABLE_TELEMETRY" "$DISABLE_ERROR_REPORTING"\n',
    )
    executable.chmod(0o755)
    monkeypatch.setenv("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "unset-by-host")

    descriptor, name = tempfile.mkstemp(dir=tmp_path)
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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str],
) -> None:
    executable = tmp_path / "pass-through"
    executable.write_text(
        "#!/bin/sh\nprintf '%s' \"$FACTORY_PASS_THROUGH_VARIABLE\"\n",
    )
    executable.chmod(0o755)
    monkeypatch.setenv("FACTORY_PASS_THROUGH_VARIABLE", "inherited")

    descriptor, name = tempfile.mkstemp(dir=tmp_path)
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


def test_the_certified_claude_probe_stage_fails_closed_until_the_fixture_lands(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "claude"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    policy = _claude._ClaudePolicy(
        version="9.9.9",
        sha256="a" * 64,
        operating_system="Linux",
        architecture="x86_64",
        install_channel="native-installer",
        canonical_path=executable,
        filesystem_identity=(2096, 1),
    )

    with pytest.raises(
        RunAdmissionError,
        match="has no implemented Host Admission Probe",
    ):
        policy.prepare_run(tmp_path)


def test_claude_admission_matches_exactly_one_channel_aware_tuple(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
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
        ({"canonical_path": Path("/elsewhere/claude")}, "canonical path does not match"),
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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable_directory = tmp_path / "claude-fake"
    executable_directory.mkdir()
    executable = executable_directory / "claude"
    make_claude_executable(executable, "9.9.9")
    monkeypatch.setenv("PATH", os.fspath(executable_directory))
    install_policy(executable, monkeypatch, install_channel="native-installer")

    with pytest.raises(
        RunAdmissionError, match="install channel does not match",
    ):
        _claude._match_compatibility(executable, "9.9.9")


def test_claude_admission_refuses_a_changed_artifact_at_the_recheck(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = install_native_claude(tmp_path, monkeypatch, version="9.9.9")
    policy = install_policy(executable, monkeypatch)
    _claude._recheck_artifact(policy)
    make_claude_executable(executable, "9.9.10")

    with pytest.raises(
        RunAdmissionError, match="changed during Run Admission",
    ):
        _claude._recheck_artifact(policy)
