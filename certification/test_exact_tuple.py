"""Real-host certification for one independently selected Codex tuple."""

import json
from pathlib import Path
from typing import Literal, NotRequired, TypedDict

import pytest

import _codex
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
)
PRODUCTION_CHECKS = (
    "identity",
    "strict-invocation-builder",
    "strict-configuration-inspectors",
    "permission-profile",
    "host-admission-probe",
)
EvidenceStatus = Literal["not-established", "established"]


class FilesystemIdentity(TypedDict):
    device: int
    inode: int


class CertificationReport(TypedDict):
    schema: int
    result: Literal["rejected", "certified"]
    factory_version: str
    codex_version: str
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


def _rejected_report(
    selection: CertificationSelection,
) -> CertificationReport:
    return {
        "schema": 1,
        "result": "rejected",
        "factory_version": _codex._factory_version(),
        "codex_version": selection.version,
        "host_reported_version": None,
        "official_sha256": selection.official_sha256,
        "official_source": selection.official_source,
        "operating_system": selection.operating_system,
        "architecture": selection.architecture,
        "host_identity": _codex._host_identity(),
        "selection_manifest": str(selection.manifest),
        "selection_commit": selection.selection_commit,
        "canonical_executable": None,
        "filesystem_identity": None,
        "evidence": {
            name: "not-established" for name in EVIDENCE_PROPERTIES
        },
        "production_checks": {
            name: "not-established" for name in PRODUCTION_CHECKS
        },
    }


def _report_line(report: CertificationReport) -> str:
    return "FACTORY_REAL_HOST_CERTIFICATION=" + json.dumps(
        report, sort_keys=True, separators=(",", ":"),
    )


def test_selected_artifact_and_host_pass_production_run_admission(
    certification_selection: CertificationSelection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Certify only evidence established by the complete production path."""

    report = _rejected_report(certification_selection)
    project_root = Path(__file__).resolve().parent.parent
    try:
        executable = _codex._resolve_executable()
        report["canonical_executable"] = str(executable)
        _, filesystem_identity = _codex._artifact_identity(executable)
        report["filesystem_identity"] = {
            "device": filesystem_identity[0],
            "inode": filesystem_identity[1],
        }
        report["host_reported_version"] = _codex._reported_version(executable)
        policy = _codex._Codex01551Policy(
            sha256=certification_selection.official_sha256,
            operating_system=certification_selection.operating_system,
            architecture=certification_selection.architecture,
            canonical_path=executable,
            filesystem_identity=filesystem_identity,
        )
        if certification_selection.version != policy.version:
            raise _codex.RunAdmissionError(
                f"selected Codex version {certification_selection.version!r} "
                f"has no production certification policy; expected {policy.version!r}",
                executable=executable,
            )
        monkeypatch.setattr(_codex, "_COMPATIBILITY_SET", (policy,))
        monkeypatch.setattr(
            _codex, "_cache_directory", lambda: tmp_path / "fresh-probe-cache",
        )
        admitted_policy, _ = _codex._admit_run(Project(project_root, project_root))
        if admitted_policy is not policy:
            raise _codex.RunAdmissionError(
                "production admission returned a different policy",
                executable=executable,
            )
    except _codex.RunAdmissionError as error:
        report["failure"] = str(error)
        pytest.fail(_report_line(report))

    report["result"] = "certified"
    report["evidence"] = {
        name: "established" for name in EVIDENCE_PROPERTIES
    }
    report["production_checks"] = {
        name: "established" for name in PRODUCTION_CHECKS
    }
    print(_report_line(report))
