"""Command-line interface for JobSearcher."""

import click

from jobsearcher import __version__


@click.command()
@click.version_option(version=__version__, prog_name="jobsearcher")
def main() -> None:
    """JobSearcher: collect, store, score, and export job postings."""


if __name__ == "__main__":
    main()
