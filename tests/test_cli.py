"""Smoke test the published artifact through its installed console script."""

from email.parser import Parser
from pathlib import Path
import subprocess
import sys
import venv
from zipfile import ZipFile


PROJECT = Path(__file__).resolve().parents[1]


def test_built_wheel_installs_and_reports_distribution_version(tmp_path):
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
    assert help_result.stdout.startswith(b"Usage:\n  factory [--project NAME")
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
