"""Enforce that release-facing support claims stay inside the Compatibility Set.

A Factory release may advertise support only for exact Codex tuples that
completed real-host certification and that production Run Admission embeds.
These tests keep the advertised claims and the embedded set from drifting
apart in either direction.
"""

import json
from pathlib import Path
import re

import _codex


PROJECT = Path(__file__).resolve().parents[1]
SUPPORT_DOCUMENT = PROJECT / "SUPPORT.md"
RESULTS = PROJECT / "certification" / "results"
REPORT_LINE = re.compile(r"FACTORY_REAL_HOST_CERTIFICATION=(\{.*\})")
TUPLE_FIELDS = ("codex_version", "official_sha256", "operating_system", "architecture")


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
            "codex_version": policy.version,
            "official_sha256": policy.sha256,
            "operating_system": policy.operating_system,
            "architecture": policy.architecture,
        }
        for policy in _codex._COMPATIBILITY_SET
    ]


def _reported_tuple(report: dict[str, object]) -> tuple[object, ...]:
    """Reduce one report or claim to its exact platform tuple."""

    return tuple(report[field] for field in TUPLE_FIELDS)


def _recorded_reports() -> list[dict[str, object]]:
    """Read every emitted certification report under certification/results."""

    reports = []
    for record in sorted(RESULTS.glob("*.md")):
        for match in REPORT_LINE.finditer(record.read_text(encoding="utf-8")):
            reports.append(json.loads(match.group(1)))
    return reports


def test_support_document_advertises_exactly_the_embedded_compatibility_set() -> None:
    """A support claim without an embedded policy overstates certification."""

    assert _advertised_tuples() == _compatibility_claims()


def test_every_embedded_tuple_has_a_certified_real_host_record() -> None:
    """No tuple enters the embedded set without recorded certification evidence."""

    certified = tuple(
        _reported_tuple(report)
        for report in _recorded_reports()
        if report["result"] == "certified"
    )
    for claim in _compatibility_claims():
        assert _reported_tuple(claim) in certified, claim
