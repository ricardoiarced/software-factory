"""Real-host certification for one independently selected Codex tuple."""

import json
from pathlib import Path
from typing import Any

import pytest

import _codex
from _project import Project
from conftest import CertificationSelection


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


def _report(
    selection: CertificationSelection,
    executable: Path,
    filesystem_identity: tuple[int, int],
) -> dict[str, Any]:
    return {
        "schema": 1,
        "result": "rejected",
        "factory_version": _codex._factory_version(),
        "codex_version": selection.version,
        "official_sha256": selection.official_sha256,
        "operating_system": selection.operating_system,
        "architecture": selection.architecture,
        "host_identity": _codex._host_identity(),
        "canonical_executable": str(executable),
        "filesystem_identity": {
            "device": filesystem_identity[0],
            "inode": filesystem_identity[1],
        },
        "evidence": {
            name: "not-established" for name in EVIDENCE_PROPERTIES
        },
        "production_checks": {
            name: "not-established" for name in PRODUCTION_CHECKS
        },
    }


def test_selected_artifact_and_host_pass_production_run_admission(
    certification_selection: CertificationSelection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Certify only evidence established by the complete production path."""

    executable = _codex._resolve_executable()
    _, filesystem_identity = _codex._artifact_identity(executable)
    report = _report(
        certification_selection, executable, filesystem_identity,
    )
    policy = _codex._Codex01551Policy(
        sha256=certification_selection.official_sha256,
        operating_system=certification_selection.operating_system,
        architecture=certification_selection.architecture,
        canonical_path=executable,
        filesystem_identity=filesystem_identity,
    )

    try:
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
        admitted_policy, _ = _codex._admit_run(
            Project(
                Path(__file__).resolve().parent.parent,
                Path(__file__).resolve().parent.parent,
            ),
        )
        if admitted_policy is not policy:
            raise _codex.RunAdmissionError(
                "production admission returned a different policy",
                executable=executable,
            )
    except _codex.RunAdmissionError as error:
        report["failure"] = str(error)
        pytest.fail(
            "FACTORY_REAL_HOST_CERTIFICATION="
            + json.dumps(report, sort_keys=True, separators=(",", ":")),
        )

    report["result"] = "certified"
    report["evidence"] = {
        name: "established" for name in EVIDENCE_PROPERTIES
    }
    report["production_checks"] = {
        name: "established" for name in PRODUCTION_CHECKS
    }
    print(
        "FACTORY_REAL_HOST_CERTIFICATION="
        + json.dumps(report, sort_keys=True, separators=(",", ":")),
    )
