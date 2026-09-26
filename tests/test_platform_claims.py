"""Enforce that release-facing support claims stay inside the Compatibility Set.

A Factory release may advertise support only for exact Harness tuples that
completed real-host certification and that production Run Admission embeds.
These tests keep the advertised claims and the embedded sets from drifting
apart in either direction.
"""

import json
import re
from collections.abc import Mapping
from pathlib import Path

import _codex
from certification.test_exact_tuple import (
    CLAUDE_PRODUCTION_CHECKS,
    CODEX_EVIDENCE_PROPERTIES,
    CODEX_PRODUCTION_CHECKS,
    EVIDENCE_PROPERTIES,
)

PROJECT = Path(__file__).resolve().parents[1]
SUPPORT_DOCUMENT = PROJECT / "SUPPORT.md"
RESULTS = PROJECT / "certification" / "results"
REPORT_LINE = re.compile(r"FACTORY_REAL_HOST_CERTIFICATION=(\{.*\})")
TUPLE_FIELDS = (
    "harness",
    "harness_version",
    "install_channel",
    "official_sha256",
    "operating_system",
    "architecture",
)
_EVIDENCE_BY_HARNESS = {
    "codex": set(CODEX_EVIDENCE_PROPERTIES),
    "claude": set(EVIDENCE_PROPERTIES),
}
_CHECKS_BY_HARNESS = {
    "codex": set(CODEX_PRODUCTION_CHECKS),
    "claude": set(CLAUDE_PRODUCTION_CHECKS),
}
# The retired report schema 1 had exactly these required assertions; a
# historical record authorizes its tuple only when it is as complete as
# the gate that recorded it demanded.
_SCHEMA_ONE_EVIDENCE = frozenset({
    "filesystem",
    "temporary-root",
    "network",
    "runtime",
    "project-spec-exclusivity",
    "extension",
    "capability",
    "negative-control",
})
_SCHEMA_ONE_CHECKS = frozenset({
    "identity",
    "strict-invocation-builder",
    "strict-configuration-inspectors",
    "permission-profile",
    "host-admission-probe",
})


def _advertised_tuples() -> list[dict[str, str]]:
    """Read the exact tuples SUPPORT.md advertises as supported."""

    document = SUPPORT_DOCUMENT.read_text(encoding="utf-8")
    section = re.search(
        r"^## Advertised tuples\s*$\n(.*?)(?=^## |\Z)", document,
        re.MULTILINE | re.DOTALL,
    )
    assert section is not None, (
        "SUPPORT.md must state its exact support claims under '## Advertised tuples'"
    )
    fenced = re.search(r"```json\s*\n(.*?)```", section.group(1), re.DOTALL)
    assert fenced is not None, "the advertised tuples must be one JSON document"
    claims = json.loads(fenced.group(1))
    assert isinstance(claims, list), "the advertised tuples must be a JSON list"
    for claim in claims:
        assert isinstance(claim, dict), "each advertised tuple must be a JSON object"
        assert set(claim) == set(TUPLE_FIELDS), (
            f"each advertised tuple needs exactly {sorted(TUPLE_FIELDS)}"
        )
    return claims


def _compatibility_claims() -> list[dict[str, str]]:
    """Read the exact tuples production Run Admission embeds."""

    return [
        {
            "harness": "codex",
            "harness_version": policy.version,
            "install_channel": "github-asset",
            "official_sha256": policy.sha256,
            "operating_system": policy.operating_system,
            "architecture": policy.architecture,
        }
        for policy in _codex._COMPATIBILITY_SET
    ]


def _reported_tuple(report: Mapping[str, object]) -> tuple[object, ...]:
    """Reduce one report or claim to its exact platform tuple.

    Records emitted before report schema 2 remain valid evidence for their
    exact tuple; their GitHub-release official source names the artifact
    family the schema-2 vocabulary now calls the github-asset channel.
    """

    if report.get("schema") == 1:
        official_source = str(report.get("official_source", ""))
        if not official_source.startswith("https://github.com/openai/codex/releases/"):
            return ()
        return (
            "codex",
            report["codex_version"],
            "github-asset",
            report["official_sha256"],
            report["operating_system"],
            report["architecture"],
        )
    return tuple(report[field] for field in TUPLE_FIELDS)


def _recorded_reports() -> list[dict[str, object]]:
    """Read every emitted certification report under certification/results."""

    reports = []
    for record in sorted(RESULTS.glob("*.md")):
        for match in REPORT_LINE.finditer(record.read_text(encoding="utf-8")):
            reports.append(json.loads(match.group(1)))
    return reports


def _completed_certification(report: dict[str, object]) -> bool:
    """Accept only a complete successful report from the real-host gate."""

    if report.get("schema") == 1:
        evidence = report.get("evidence")
        checks = report.get("production_checks")
        return (
            report.get("result") == "certified"
            and "failure" not in report
            and report.get("host_reported_version") == report.get("codex_version")
            and isinstance(report.get("canonical_executable"), str)
            and isinstance(report.get("filesystem_identity"), dict)
            and isinstance(report.get("selection_commit"), str)
            and isinstance(evidence, dict)
            and set(evidence) == _SCHEMA_ONE_EVIDENCE
            and all(status == "established" for status in evidence.values())
            and isinstance(checks, dict)
            and set(checks) == _SCHEMA_ONE_CHECKS
            and all(status == "established" for status in checks.values())
        )
    harness = report.get("harness")
    evidence = report.get("evidence")
    checks = report.get("production_checks")
    if harness not in _EVIDENCE_BY_HARNESS:
        return False
    return (
        report.get("schema") == 2
        and report.get("result") == "certified"
        and "failure" not in report
        and report.get("host_reported_version") == report.get("harness_version")
        and isinstance(report.get("canonical_executable"), str)
        and isinstance(report.get("filesystem_identity"), dict)
        and isinstance(report.get("selection_commit"), str)
        and isinstance(evidence, dict)
        and set(evidence) == _EVIDENCE_BY_HARNESS[harness]
        and all(status == "established" for status in evidence.values())
        and isinstance(checks, dict)
        and set(checks) == _CHECKS_BY_HARNESS[harness]
        and all(status == "established" for status in checks.values())
    )


def test_support_document_advertises_exactly_the_embedded_compatibility_set() -> None:
    """A support claim without an embedded policy overstates certification."""

    assert _advertised_tuples() == _compatibility_claims()


def test_every_embedded_tuple_has_a_certified_real_host_record() -> None:
    """No tuple enters the embedded set without recorded certification evidence."""

    certified = tuple(
        _reported_tuple(report)
        for report in _recorded_reports()
        if _completed_certification(report)
    )
    for claim in _compatibility_claims():
        assert _reported_tuple(claim) in certified, claim


def test_incomplete_certification_cannot_authorize_a_support_claim() -> None:
    report: dict[str, object] = {
        "schema": 2,
        "result": "certified",
        "harness": "codex",
        "harness_version": "0.155.1",
        "install_channel": "github-asset",
        "host_reported_version": "0.155.1",
        "canonical_executable": "/bin/codex",
        "filesystem_identity": {"device": 1, "inode": 2},
        "selection_commit": "a" * 40,
        "evidence": {name: "not-established" for name in CODEX_EVIDENCE_PROPERTIES},
        "production_checks": {
            name: "not-established" for name in CODEX_PRODUCTION_CHECKS
        },
    }

    assert not _completed_certification(report)
    report["evidence"] = {name: "established" for name in CODEX_EVIDENCE_PROPERTIES}
    assert not _completed_certification(report)
    report["production_checks"] = {
        name: "established" for name in CODEX_PRODUCTION_CHECKS
    }
    assert _completed_certification(report)

    # The retired schema keeps its recorded strength: a historical record
    # authorizes only with its complete evidence and production checks.
    schema_one: dict[str, object] = {
        "schema": 1,
        "result": "certified",
        "codex_version": "0.155.1",
        "host_reported_version": "0.155.1",
        "official_source": "https://github.com/openai/codex/releases/tag/rust-v0.155.1",
        "official_sha256": "b" * 64,
        "operating_system": "Linux",
        "architecture": "x86_64",
        "canonical_executable": "/bin/codex",
        "filesystem_identity": {"device": 1, "inode": 2},
        "selection_commit": "a" * 40,
        "evidence": {name: "established" for name in _SCHEMA_ONE_EVIDENCE},
    }
    assert not _completed_certification(schema_one)
    schema_one["production_checks"] = {
        name: "established" for name in sorted(_SCHEMA_ONE_CHECKS)[:-1]
    }
    assert not _completed_certification(schema_one)
    schema_one["production_checks"] = {
        name: "established" for name in _SCHEMA_ONE_CHECKS
    }
    schema_one["evidence"] = {
        name: "established" for name in sorted(_SCHEMA_ONE_EVIDENCE)[:-1]
    }
    assert not _completed_certification(schema_one)
    schema_one["evidence"] = {
        name: "established" for name in _SCHEMA_ONE_EVIDENCE
    }
    assert _completed_certification(schema_one)
