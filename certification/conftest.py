"""Explicit selection and immutable inputs for real-host certification."""

from dataclasses import dataclass
import re

import pytest


@dataclass(frozen=True)
class CertificationSelection:
    """The independently named Codex artifact and platform tuple."""

    version: str
    official_sha256: str
    operating_system: str
    architecture: str


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("real-host-certification")
    group.addoption(
        "--real-host-certification",
        action="store_true",
        help="run the explicitly selected real-host certification suite",
    )
    group.addoption("--codex-version", help="exact selected Codex version")
    group.addoption(
        "--codex-official-sha256",
        help="official SHA-256 digest of the selected Codex executable",
    )
    group.addoption(
        "--codex-operating-system",
        help="exact selected platform.system() value",
    )
    group.addoption(
        "--codex-architecture",
        help="exact selected platform.machine() value",
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item],
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

    option_names = (
        "codex_version",
        "codex_official_sha256",
        "codex_operating_system",
        "codex_architecture",
    )
    values: dict[str, str] = {}
    for name in option_names:
        value = pytestconfig.getoption(name)
        if not isinstance(value, str) or not value:
            option = "--" + name.replace("_", "-")
            raise pytest.UsageError(
                f"{option} is required for real-host certification",
            )
        values[name] = value

    digest = values["codex_official_sha256"]
    if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise pytest.UsageError(
            "--codex-official-sha256 must be 64 lowercase hexadecimal characters",
        )

    return CertificationSelection(
        version=values["codex_version"],
        official_sha256=digest,
        operating_system=values["codex_operating_system"],
        architecture=values["codex_architecture"],
    )
