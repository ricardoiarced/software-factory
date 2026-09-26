"""Explicit selection and immutable inputs for real-host certification."""

import pytest

from certification._selection import (
    CertificationSelection,
    SelectionError,
    load_committed_selection,
)


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("real-host-certification")
    group.addoption(
        "--real-host-certification",
        action="store_true",
        help="run the explicitly selected real-host certification suite",
    )
    group.addoption(
        "--harness-candidate",
        help="committed candidate manifest beneath certification/candidates",
    )


def pytest_collection_modifyitems(
    config: pytest.Config,
    items: list[pytest.Item],
) -> None:
    if config.getoption("real_host_certification"):
        return
    reason = "real-host certification was not explicitly selected"
    for item in items:
        if item.get_closest_marker("real_host_certification") is not None:
            item.add_marker(pytest.mark.skip(reason=reason))


@pytest.fixture(scope="session")
def certification_selection(
    pytestconfig: pytest.Config,
) -> CertificationSelection:
    if not pytestconfig.getoption("real_host_certification"):
        pytest.skip("real-host certification was not explicitly selected")

    candidate = pytestconfig.getoption("harness_candidate")
    if not isinstance(candidate, str) or not candidate:
        raise pytest.UsageError(
            "--harness-candidate is required for real-host certification",
        )
    try:
        return load_committed_selection(pytestconfig.rootpath, candidate)
    except SelectionError as error:
        raise pytest.UsageError(str(error)) from error
