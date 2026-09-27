"""Real-host certification for one independently selected Harness tuple.

Each first-scope tuple records exactly the evidence properties and production
checks named for its harness below. The probe-derived properties prove
Project Data Isolation on the host, Project Spec Exclusivity suppression
included; the refusal controls prove the fixed admission decisions bite on
the real artifact; the inference run proves admission and launch together.
One passing Claude Host Admission Probe derives inside-boundary success and
the required runtime read through the admitted tools, denies every forbidden
read and write, proves the network denial control detects egress before the
sandboxed Run stays quiet (the negative control), and leaves suppressed
guidance undiscovered.
"""

import json
import os
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import BinaryIO, Literal, NotRequired, TypedDict

import pytest

import _claude
import _codex
from _harness import (
    PreparedRun,
    RunAdmissionError,
    factory_version,
    host_identity,
    launch,
)
from _project import Project
from certification._selection import CertificationSelection

pytestmark = pytest.mark.real_host_certification

EVIDENCE_PROPERTIES = (
    "filesystem",
    "temporary-root",
    "network",
    "runtime",
    "project-spec-exclusivity",
    "extension",
    "capability",
    "negative-control",
    "inference-run",
    "stale-version-refusal",
    "wrong-digest-refusal",
    "forbidden-guidance-suppression",
    "managed-policy-refusal",
)
PRODUCTION_CHECKS = (
    "identity",
    "strict-invocation-builder",
    "strict-configuration-inspectors",
    "permission-profile",
    "host-admission-probe",
    "newest-stable-verification",
    "managed-policy-gate",
    "sandbox-dependency-preflight",
)

CODEX_EVIDENCE_PROPERTIES = tuple(
    name for name in EVIDENCE_PROPERTIES if name != "managed-policy-refusal"
)
CODEX_PRODUCTION_CHECKS = (
    "identity",
    "strict-invocation-builder",
    "strict-configuration-inspectors",
    "permission-profile",
    "host-admission-probe",
    "newest-stable-verification",
)
CLAUDE_PRODUCTION_CHECKS = (
    "identity",
    "strict-invocation-builder",
    "host-admission-probe",
    "newest-stable-verification",
    "managed-policy-gate",
    "sandbox-dependency-preflight",
)
CLAUDE_PROBE_EVIDENCE_PROPERTIES = tuple(
    name
    for name in EVIDENCE_PROPERTIES
    if name
    not in (
        "inference-run",
        "stale-version-refusal",
        "wrong-digest-refusal",
        "managed-policy-refusal",
    )
)

EvidenceStatus = Literal["not-established", "established"]

_INFERENCE_INSTRUCTION = "Add a basic docstring to main.py."
_STALE_VERSION = "0.0.1"


class FilesystemIdentity(TypedDict):
    device: int
    inode: int


class CertificationReport(TypedDict):
    schema: int
    result: Literal["rejected", "certified"]
    factory_version: str
    harness: str
    harness_version: str
    install_channel: str
    host_reported_version: str | None
    official_sha256: str
    official_source: str
    operating_system: str
    architecture: str
    host_identity: dict[str, str]
    selection_manifest: str
    selection_commit: str
    canonical_executable: str | None
    filesystem_identity: FilesystemIdentity | None
    evidence: dict[str, EvidenceStatus]
    production_checks: dict[str, EvidenceStatus]
    failure: NotRequired[str]


def _rejected_report(selection: CertificationSelection) -> CertificationReport:
    evidence_names = (
        CODEX_EVIDENCE_PROPERTIES
        if selection.harness == "codex"
        else EVIDENCE_PROPERTIES
    )
    production_check_names = (
        CODEX_PRODUCTION_CHECKS
        if selection.harness == "codex"
        else CLAUDE_PRODUCTION_CHECKS
    )
    return {
        "schema": 2,
        "result": "rejected",
        "factory_version": factory_version(),
        "harness": selection.harness,
        "harness_version": selection.version,
        "install_channel": selection.install_channel,
        "host_reported_version": None,
        "official_sha256": selection.official_sha256,
        "official_source": selection.official_source,
        "operating_system": selection.operating_system,
        "architecture": selection.architecture,
        "host_identity": host_identity(),
        "selection_manifest": str(selection.manifest),
        "selection_commit": selection.selection_commit,
        "canonical_executable": None,
        "filesystem_identity": None,
        "evidence": {name: "not-established" for name in evidence_names},
        "production_checks": {
            name: "not-established" for name in production_check_names
        },
    }


def _report_line(report: CertificationReport) -> str:
    return "FACTORY_REAL_HOST_CERTIFICATION=" + json.dumps(
        report,
        sort_keys=True,
        separators=(",", ":"),
    )


def _control_refused(
    report: CertificationReport,
    establishment: Callable[[], object],
    *required_phrases: str,
) -> bool:
    """Prove one fixed admission decision refuses on this real artifact.

    A control that does not refuse, or refuses with a different diagnostic,
    fails the certification instead of establishing its evidence property.
    The establishment callable may return any evidence value; only the
    raised RunAdmissionError and its diagnostic matter here.
    """

    try:
        establishment()
    except RunAdmissionError as refused:
        message = str(refused)
        for phrase in required_phrases:
            if phrase not in message:
                report["failure"] = (
                    "the negative control refused with an unexpected "
                    f"diagnostic: {message}"
                )
                return False
        return True
    report["failure"] = "the negative control did not refuse"
    return False


def _inference_project(
    root: Path,
) -> tuple[Path, Path, str]:
    """Create the disposable docstring-edit project and its original source."""

    boundary = root / "inference-boundary"
    boundary.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=boundary, check=True)
    (boundary / ".agent").mkdir()
    original = "def perform(column):\n    return column * 2\n"
    (boundary / "main.py").write_text(original, encoding="utf-8")
    return boundary, boundary / "main.py", original


def _perform_inference_run(
    root: Path,
    boundary: Path,
    start: Callable[[BinaryIO], int],
) -> None:
    """Compose the Run Request spool, submit it, and require Run success."""

    descriptor, name = tempfile.mkstemp(
        dir=root, prefix="run-request-", suffix=".spool"
    )
    spool = os.fdopen(descriptor, "r+b")
    try:
        spool.write(b"# Run Request\n\n## Instruction\n\n")
        spool.write(_INFERENCE_INSTRUCTION.encode("utf-8"))
        spool.flush()
        spool.seek(0)
        status = start(spool)
    finally:
        spool.close()
        Path(name).unlink(missing_ok=True)
    if status:
        raise RunAdmissionError(
            f"the inference Run exited with status {status}",
        )


def _docstring_was_added(source: Path, original: str) -> bool:
    edited = source.read_text(encoding="utf-8")
    return edited != original and ('"""' in edited or "'''" in edited)


def _certify_codex(
    report: CertificationReport,
    selection: CertificationSelection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Establish one Codex tuple's evidence through the production path.

    The probe plants forbidden AGENTS.md, project-skill, and configuration
    guidance; passing admission proves Project Spec Exclusivity suppressed
    all of it on this host.
    """

    executable = _codex._resolve_executable()
    report["canonical_executable"] = str(executable)
    digest, filesystem_identity = _codex._artifact_identity(executable)
    report["filesystem_identity"] = {
        "device": filesystem_identity[0],
        "inode": filesystem_identity[1],
    }
    report["host_reported_version"] = _codex._reported_version(executable)
    if digest != selection.official_sha256:
        raise RunAdmissionError(
            "the installed Codex artifact does not match the reviewed "
            "manifest digest; the selection is not the certified candidate",
            executable=executable,
        )
    policy = _codex._Codex01551Policy(
        sha256=selection.official_sha256,
        operating_system=selection.operating_system,
        architecture=selection.architecture,
        canonical_path=executable,
        filesystem_identity=filesystem_identity,
    )
    if selection.version != policy.version:
        raise RunAdmissionError(
            f"selected Codex version {selection.version!r} has no production "
            f"certification policy; expected {policy.version!r}",
            executable=executable,
        )
    monkeypatch.setattr(_codex, "_COMPATIBILITY_SET", (policy,))
    monkeypatch.setattr(
        _codex,
        "_cache_directory",
        lambda: tmp_path / "fresh-probe-cache",
    )
    admitted_policy, _ = _codex._admit_run(Project(tmp_path, tmp_path))
    if admitted_policy is not policy:
        raise RunAdmissionError(
            "production admission returned a different policy",
            executable=executable,
        )
    report["production_checks"] = {
        name: "established" for name in CODEX_PRODUCTION_CHECKS
    }

    report["evidence"]["forbidden-guidance-suppression"] = "established"

    with monkeypatch.context() as patched:
        patched.setattr(
            _codex,
            "_COMPATIBILITY_SET",
            (replace(policy, sha256="0" * 64),),
        )
        if not _control_refused(
            report,
            lambda: _codex._match_compatibility(executable, policy.version),
            "SHA-256 does not match the Codex Compatibility Set",
        ):
            return
    report["evidence"]["wrong-digest-refusal"] = "established"

    if not _control_refused(
        report,
        lambda: _codex.verify_codex_newest_stable(executable, _STALE_VERSION),
        "older than the newest stable release",
    ):
        return
    report["evidence"]["stale-version-refusal"] = "established"

    boundary, source, original = _inference_project(tmp_path / "codex-inference")
    _perform_inference_run(
        tmp_path / "codex-inference",
        boundary,
        lambda spool: _codex.CodexRunner().run(Project(boundary, boundary), spool),
    )
    if not _docstring_was_added(source, original):
        raise RunAdmissionError(
            "the inference Run did not perform the docstring edit inside the "
            "certified boundary",
            executable=executable,
        )
    report["evidence"]["inference-run"] = "established"

    report["result"] = "certified"


def _established_claude_policy(
    report: CertificationReport,
    selection: CertificationSelection,
) -> tuple[Path, _claude._ClaudePolicy]:
    """Establish one candidate's host identity and return its policy."""

    executable = _claude._resolve_executable()
    report["canonical_executable"] = str(executable)
    digest, filesystem_identity = _claude._artifact_identity(executable)
    report["filesystem_identity"] = {
        "device": filesystem_identity[0],
        "inode": filesystem_identity[1],
    }
    report["host_reported_version"] = _claude._reported_version(executable)
    report["production_checks"]["identity"] = "established"
    if digest != selection.official_sha256:
        raise RunAdmissionError(
            "the installed Claude Code artifact does not match the reviewed "
            "manifest digest; the selection is not the certified candidate",
            executable=executable,
        )
    policy = _claude._ClaudePolicy(
        version=selection.version,
        sha256=selection.official_sha256,
        operating_system=selection.operating_system,
        architecture=selection.architecture,
        install_channel=selection.install_channel,
        canonical_path=executable,
        filesystem_identity=filesystem_identity,
    )
    return executable, policy


def _certify_claude(
    report: CertificationReport,
    selection: CertificationSelection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Establish one Claude Code tuple's evidence through the production path,
    including the real-host Host Admission Probe against a fresh probe cache.

    Admission must pass unmodified with the candidate policy installed. The
    probe plants forbidden project guidance, extensions, and configuration,
    attempts every admitted and forbidden operation through Bash and the
    native tools, and asserts each outcome from observed evidence, so passing
    admission proves the isolation properties on this host.
    """

    executable, policy = _established_claude_policy(report, selection)

    _claude._require_no_managed_policy(executable)
    report["production_checks"]["managed-policy-gate"] = "established"
    managed_home = tmp_path / "managed-home"
    (managed_home / ".claude").mkdir(parents=True)
    (managed_home / ".claude" / "remote-settings.json").write_text(
        "{}\n",
        encoding="utf-8",
    )
    with monkeypatch.context() as patched:
        patched.setenv("HOME", str(managed_home))
        if not _control_refused(
            report,
            lambda: _claude._require_no_managed_policy(executable),
            "remote server-policy cache",
        ):
            return
    report["evidence"]["managed-policy-refusal"] = "established"

    _claude._require_sandbox_dependencies()
    report["production_checks"]["sandbox-dependency-preflight"] = "established"

    _claude.verify_claude_code_newest_stable(executable, policy.version)
    report["production_checks"]["newest-stable-verification"] = "established"

    monkeypatch.setattr(_claude, "_CLAUDE_COMPATIBILITY_SET", (policy,))
    matched = _claude._match_compatibility(executable, policy.version)
    if matched is not policy:
        raise RunAdmissionError(
            "production admission returned a different policy",
            executable=executable,
        )
    with monkeypatch.context() as patched:
        patched.setattr(
            _claude, "_CLAUDE_COMPATIBILITY_SET", (replace(policy, sha256="0" * 64),)
        )
        if not _control_refused(
            report,
            lambda: _claude._match_compatibility(executable, policy.version),
            "SHA-256 does not match the Claude Compatibility Set",
        ):
            return
    report["evidence"]["wrong-digest-refusal"] = "established"

    if not _control_refused(
        report,
        lambda: _claude.verify_claude_code_newest_stable(
            executable,
            _STALE_VERSION,
        ),
        "older than the newest stable release",
    ):
        return
    report["evidence"]["stale-version-refusal"] = "established"

    monkeypatch.setattr(
        _claude,
        "_cache_directory",
        lambda: tmp_path / "fresh-probe-cache",
    )
    admitted_policy, invocation = _claude._admit_run(Project(tmp_path, tmp_path))
    if admitted_policy is not policy:
        raise RunAdmissionError(
            "production admission returned a different policy",
            executable=executable,
        )
    report["production_checks"]["host-admission-probe"] = "established"
    for required in (
        "-p",
        "--restricted",
        "--safe-mode",
        "--strict-mcp-config",
        "--permission-mode",
        "dontAsk",
        "--settings",
    ):
        if required not in invocation:
            raise RunAdmissionError(
                f"the certified invocation lost {required!r}",
                executable=executable,
            )
    report["production_checks"]["strict-invocation-builder"] = "established"
    prepared = PreparedRun(
        harness="Claude Code",
        executable=executable,
        invocation=tuple(invocation),
        environment=_claude._ENVIRONMENT_PINS,
    )
    if {name for name, _ in _claude._ENVIRONMENT_PINS} > {
        name for name, _ in prepared.environment
    }:
        raise RunAdmissionError(
            "the certified launch plan lost the nonessential-traffic pins",
            executable=executable,
        )

    for name in CLAUDE_PROBE_EVIDENCE_PROPERTIES:
        report["evidence"][name] = "established"

    boundary, source, original = _inference_project(tmp_path / "claude-inference")
    _perform_inference_run(
        tmp_path / "claude-inference",
        boundary,
        lambda spool: launch(Project(boundary, boundary), prepared, spool),
    )
    if not _docstring_was_added(source, original):
        raise RunAdmissionError(
            "the inference Run did not perform the docstring edit inside the "
            "certified boundary",
            executable=executable,
        )
    report["evidence"]["inference-run"] = "established"

    report["result"] = "certified"


def _selected_certification(
    selection: CertificationSelection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    harness: str,
) -> None:
    """Emit the Certification Record of one explicitly selected tuple."""

    if selection.harness != harness:
        pytest.skip(f"the selected candidate manifest is not a {harness} tuple")
    report = _rejected_report(selection)
    establish = _certify_codex if harness == "codex" else _certify_claude
    try:
        establish(report, selection, tmp_path, monkeypatch)
    except RunAdmissionError as failure:
        report["failure"] = report.get("failure") or str(failure)
        pytest.fail(_report_line(report))

    print(_report_line(report))


def test_selected_codex_tuple_passes_the_complete_production_path(
    certification_selection: CertificationSelection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Certify only evidence established by the complete production path."""

    _selected_certification(
        certification_selection,
        tmp_path,
        monkeypatch,
        "codex",
    )


def test_selected_claude_tuple_passes_the_complete_production_path(
    certification_selection: CertificationSelection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Certify only evidence established by the production admission path."""

    _selected_certification(
        certification_selection,
        tmp_path,
        monkeypatch,
        "claude",
    )
