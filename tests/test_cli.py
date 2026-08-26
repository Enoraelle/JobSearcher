"""Smoke tests for the jobsearcher package and its CLI entry point."""

from click.testing import CliRunner

import jobsearcher
from jobsearcher.cli import main


def test_package_imports() -> None:
    """The jobsearcher package imports and exposes a version string."""
    assert isinstance(jobsearcher.__version__, str)
    assert jobsearcher.__version__


def test_cli_version() -> None:
    """`jobsearcher --version` exits successfully and reports the package version."""
    runner = CliRunner()
    result = runner.invoke(main, ["--version"])

    assert result.exit_code == 0
    assert jobsearcher.__version__ in result.output
