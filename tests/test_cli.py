"""Smoke test the published artifact through its installed console script."""

from collections.abc import Callable
from email.parser import Parser
import os
from pathlib import Path
import shutil
import subprocess
import sys
import venv
from zipfile import ZipFile

import platformdirs
import pytest


PROJECT = Path(__file__).resolve().parents[1]


def test_built_wheel_installs_and_reports_distribution_version(
    tmp_path, monkeypatch,
):
    # Build isolation installs its backend through pip; the offline guard's
    # dead proxy is for publisher lookups, not this local packaging step.
    for variable in ("HTTPS_PROXY", "https_proxy"):
        monkeypatch.delenv(variable, raising=False)
    artifacts = tmp_path / "dist"
    subprocess.run(
        [sys.executable, "-m", "build", "--sdist", "--wheel", "--outdir", str(artifacts)],
        cwd=PROJECT,
        check=True,
        capture_output=True,
        text=True,
    )

    assert [path.name for path in artifacts.glob("*.tar.gz")] == [
        "software_factory-0.1.0.tar.gz"
    ]
    (wheel,) = artifacts.glob("*.whl")
    assert wheel.name == "software_factory-0.1.0-py3-none-any.whl"

    with ZipFile(wheel) as archive:
        metadata_path = next(name for name in archive.namelist() if name.endswith(".dist-info/METADATA"))
        metadata = Parser().parsestr(archive.read(metadata_path).decode("utf-8"))
        assert metadata["Name"] == "software-factory"
        assert metadata["Version"] == "0.1.0"
        assert metadata["Requires-Python"] == ">=3.11"
        assert "platformdirs>=4" in metadata.get_all("Requires-Dist")

    environment = tmp_path / "venv"
    venv.EnvBuilder(with_pip=True).create(environment)
    scripts = environment / ("Scripts" if sys.platform == "win32" else "bin")
    subprocess.run(
        [str(scripts / "python"), "-m", "pip", "install", "--no-deps", str(wheel)],
        check=True,
        capture_output=True,
        text=True,
    )
    result = subprocess.run(
        [str(scripts / "factory"), "--version"],
        cwd=tmp_path,
        capture_output=True,
    )
    assert result.returncode == 0
    assert result.stdout == b"factory 0.1.0\n"
    assert result.stderr == b""

    help_result = subprocess.run(
        [str(scripts / "factory"), "--help"],
        cwd=tmp_path,
        capture_output=True,
    )
    assert help_result.returncode == 0
    assert help_result.stdout.startswith(
        b"Usage:\n  factory [--harness codex|claude] [--project NAME",
    )
    assert help_result.stderr == b""

    malformed = subprocess.run(
        [str(scripts / "factory"), "--version", "--help"],
        cwd=tmp_path,
        capture_output=True,
    )
    assert malformed.returncode == 2
    assert malformed.stdout == b""
    assert malformed.stderr.startswith(b"factory: error: ")
    assert b"Try 'factory --help' for more information.\n" in malformed.stderr


@pytest.fixture(scope="module")
def installed_factory(
    tmp_path_factory: pytest.TempPathFactory,
) -> Callable[..., subprocess.CompletedProcess[bytes]]:
    """Build the wheel once and serve the installed entry point from a venv.

    The installed ``platformdirs`` dependency is served from the host through
    ``PYTHONPATH`` so the registry works without network access; dependency
    isolation itself stays with the strict smoke test above.
    """

    artifacts = tmp_path_factory.mktemp("dist")
    subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(artifacts), str(PROJECT)],
        check=True,
        capture_output=True,
        text=True,
    )
    (wheel,) = artifacts.glob("*.whl")
    environment = tmp_path_factory.mktemp("venv")
    venv.EnvBuilder(with_pip=True).create(environment)
    scripts = environment / ("Scripts" if sys.platform == "win32" else "bin")
    subprocess.run(
        [str(scripts / "python"), "-m", "pip", "install", "--no-deps", str(wheel)],
        check=True,
        capture_output=True,
        text=True,
    )
    factory_script = str(scripts / "factory")
    platformdirs_package = Path(platformdirs.__file__).resolve().parent.parent
    working_directory = tmp_path_factory.mktemp("cwd")

    def invoke(
        *arguments: str, env: dict[str, str], cwd: Path | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        path_entries = [str(platformdirs_package)]
        if env.get("PYTHONPATH"):
            path_entries.append(env["PYTHONPATH"])
        # Run away from the checkout so its modules can never shadow the
        # installed distribution under test.
        return subprocess.run(
            [factory_script, *arguments],
            capture_output=True,
            cwd=cwd or working_directory,
            env={**env, "PYTHONPATH": os.pathsep.join(path_entries)},
        )

    return invoke


def test_installed_cli_serves_administration_and_fails_closed_before_codex(
    tmp_path: Path,
    installed_factory: Callable[..., subprocess.CompletedProcess[bytes]],
) -> None:
    """An uncertified release still installs and administers, but never runs."""

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, capture_output=True)
    boundary = tmp_path / "boundary"
    boundary.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    git_binary = shutil.which("git")
    assert git_binary is not None
    environment = {
        **os.environ,
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "PATH": os.pathsep.join((
            os.fspath(Path(git_binary).parent), os.defpath,
        )),
    }

    added = installed_factory("project", "add", "api", str(boundary), env=environment)
    assert added.returncode == 0
    assert added.stdout == f'Added project "api" at "{boundary.resolve()}".\n'.encode()
    assert added.stderr == b""

    listed = installed_factory("project", "list", env=environment)
    assert listed.returncode == 0
    assert listed.stdout == (
        f'NAME\tSTATUS\tPATH\n"api"\tvalid\t"{boundary.resolve()}"\n'
    ).encode()
    assert listed.stderr == b""

    run = installed_factory(
        "--project", "api", "Diagnose this failure", env=environment,
    )
    assert run.returncode == 1
    assert run.stdout == b""
    assert run.stderr == (
        b'factory: error: Codex executable "codex" was not found on PATH; '
        b"install Codex or add it to PATH.\n"
    )
