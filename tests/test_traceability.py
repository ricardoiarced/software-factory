"""Enforce specification traceability for every named Factory test.

The final specification (issue #4) requires every normative statement to map
to at least one named test or certification assertion, and admits no coverage
percentage as a substitute. `docs/traceability.md` is that mapping; this
module keeps it honest in both directions: every row names tests that exist,
and no test exists without a normative statement that needs it.
"""

import ast
import re
from pathlib import Path

from certification.test_exact_tuple import EVIDENCE_PROPERTIES, PRODUCTION_CHECKS

PROJECT = Path(__file__).resolve().parents[1]
MATRIX = PROJECT / "docs" / "traceability.md"
ACCEPTANCE_IDS = tuple(f"AC-{number:02d}" for number in range(1, 13))
SECTION_STATEMENT_COUNTS = (6, 3, 14, 9, 12, 9, 24, 5, 11, 4, 10, 0, 16, 8, 15)
STATEMENT_IDS = {
    f"S{section}.{number}"
    for section, count in enumerate(SECTION_STATEMENT_COUNTS, start=1)
    for number in range(1, count + 1)
}


def _test_functions(relative_path: Path) -> set[str]:
    """Collect the test function names defined in one suite file."""

    tree = ast.parse(
        (PROJECT / relative_path).read_text(encoding="utf-8"),
        filename=str(relative_path),
    )
    return {
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")
    }


def _suite_inventory() -> set[str]:
    """Every test function the deterministic suite and certification define."""

    suites = set()
    for directory in (PROJECT / "tests", PROJECT / "certification"):
        for path in sorted(directory.glob("test_*.py")):
            suites |= _test_functions(path.relative_to(PROJECT))
    return suites


def _certification_assertions() -> set[str]:
    """The named assertions the real-host certification suite establishes."""

    return (
        _test_functions(Path("certification/test_exact_tuple.py"))
        | set(EVIDENCE_PROPERTIES)
        | set(PRODUCTION_CHECKS)
    )


def _matrix_rows() -> list[tuple[str, set[str], set[str]]]:
    """Parse (requirement id, named tests, certification assertions) rows."""

    rows = []
    for line in MATRIX.read_text(encoding="utf-8").splitlines():
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) != 4 or not re.fullmatch(
            r"[A-Z]{1,3}-?[0-9]+(\.[0-9]+)?", cells[0]
        ):
            continue
        rows.append(
            (
                cells[0],
                set(re.findall(r"`([^`]+)`", cells[2])),
                set(re.findall(r"`([^`]+)`", cells[3])),
            )
        )
    return rows


def test_traceability_matrix_exists_and_covers_the_final_acceptance_criteria() -> None:
    rows = _matrix_rows()
    assert rows, f"{MATRIX} must map normative statements to named tests"
    identifiers = [identifier for identifier, _, _ in rows]
    expected = STATEMENT_IDS | set(ACCEPTANCE_IDS)
    assert len(identifiers) == len(set(identifiers)), (
        "duplicate normative statement IDs"
    )
    assert set(identifiers) == expected, (
        f"missing IDs: {sorted(expected - set(identifiers))}; "
        f"unexpected IDs: {sorted(set(identifiers) - expected)}"
    )
    empty = [
        identifier
        for identifier, tests, assertions in rows
        if not (tests or assertions)
    ]
    assert not empty, f"normative statements without evidence: {empty}"


def test_every_referenced_test_exists() -> None:
    inventory = _suite_inventory()
    dangling = sorted(
        name
        for _, tests, _ in _matrix_rows()
        for name in tests
        if name not in inventory
    )
    assert not dangling, f"matrix rows name tests that do not exist: {dangling}"


def test_every_referenced_certification_assertion_exists() -> None:
    assertions = _certification_assertions()
    dangling = sorted(
        name
        for _, _, certifications in _matrix_rows()
        for name in certifications
        if name not in assertions
    )
    assert not dangling, (
        f"matrix rows name unknown certification assertions: {dangling}"
    )


def test_every_named_test_traces_to_a_normative_statement() -> None:
    """No test exists without a normative statement that requires it."""

    referenced = {name for _, tests, _ in _matrix_rows() for name in tests}
    orphans = sorted(_suite_inventory() - referenced)
    assert not orphans, f"tests no normative statement requires: {orphans}"
