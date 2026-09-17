"""Entry point for the Factory command."""

from importlib.metadata import version
import sys


def main() -> int:
    """Print the installed Factory version."""
    if sys.argv[1:] == ["--version"]:
        print(f"factory {version('software-factory')}")
        return 0

    print("factory: error: expected --version", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
