"""The ``jobsearcher`` command-line interface.

This module wires the other layers together and holds no business logic of
its own: :mod:`jobsearcher.pipeline` orchestrates fetch/score/export, and
the storage, scoring and exporter packages do the work. Everything here is
about parsing arguments, rendering results, and choosing exit codes.

Everything a posting carries - title, company, description, and any
error message quoting them - reaches this module as scraped text, and
Rich reads ``[...]`` as markup. Such values are therefore never
interpolated into a markup string: they go through :func:`_plain`, which
wraps them in a :class:`~rich.text.Text` that Rich renders literally.
Skipping that turns a posting title into either a ``MarkupError``
traceback or, worse, silently deleted text.

Output is written with :mod:`rich`: progress bars on the long phases,
aligned tables, and a small palette (``cyan`` / ``green`` / ``yellow`` /
``red`` / ``dim``) chosen to stay readable on both light and dark
terminals. Errors and progress go to stderr so ``jobsearcher list --json``
and friends stay pipeable. Exit codes: ``0`` success, ``1`` a runtime
failure (bad config, a phase failed, nothing matched), ``2`` a usage error
(Click's default).
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import click
from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskID,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table
from rich.text import Text

from jobsearcher import __version__
from jobsearcher.config import (
    DEFAULT_CONFIG_PATH,
    AppConfig,
    ConfigError,
    example_config_text,
    load_config,
)
from jobsearcher.exporters import EXPORTER_FORMATS
from jobsearcher.models import ApplicationStatus, JobPosting
from jobsearcher.pipeline import (
    ExportOutcome,
    Pipeline,
    PipelineError,
    PostingFilters,
    ScoredPosting,
    SourceFetchResult,
)
from jobsearcher.storage import StorageConfigError, open_storage
from jobsearcher.storage.sqlite import SqliteStorage

_STATUS_VALUES = tuple(status.value for status in ApplicationStatus)
_SORT_VALUES = ("score", "date", "company")
# `list`, `export` and `run` read the same postings, so they share the wording
# of the filter that decides which ones.
_LANGUAGE_HELP = (
    "Detected-language filter (repeatable). 'all' shows every language. "
    "Default: search.languages from config."
)
_ID_PREFIX_RE = re.compile(r"[0-9a-fA-F]{4,12}")

_out = Console()
_err = Console(stderr=True)


# --------------------------------------------------------------------------
# Shared context
# --------------------------------------------------------------------------


@dataclass
class AppContext:
    """Global options, plus lazily-loaded config, shared by every command."""

    config_path: Path | None
    verbose: int
    dry_run: bool
    _config: AppConfig | None = None

    def load_config(self) -> AppConfig:
        """Load (once) and return the application configuration."""
        if self._config is None:
            try:
                self._config = load_config(self.config_path)
            except ConfigError as exc:
                raise click.ClickException(str(exc)) from exc
        return self._config

    def open_storage(self) -> SqliteStorage:
        """Open the storage backend named in the configuration."""
        try:
            return open_storage(self.load_config().storage)
        except StorageConfigError as exc:
            raise click.ClickException(str(exc)) from exc

    def pipeline(self, storage: SqliteStorage) -> Pipeline:
        """Build a pipeline bound to this context's config and ``storage``."""
        return Pipeline(self.load_config(), storage, dry_run=self.dry_run)


def _make_output_encodable() -> None:
    """Stop a character the terminal cannot encode from crashing the CLI.

    Job postings routinely carry accented text, and a redirected or legacy
    Windows console encodes stdout as cp1252 with ``errors="strict"`` - one
    unrepresentable character would otherwise raise ``UnicodeEncodeError``
    mid-render. Downgrading to ``errors="replace"`` turns it into ``?``.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            # Fails only if the stream is already detached / not reconfigurable.
            with contextlib.suppress(ValueError, OSError):
                reconfigure(errors="replace")


def _configure_logging(verbose: int) -> None:
    level = logging.WARNING
    if verbose == 1:
        level = logging.INFO
    elif verbose >= 2:
        level = logging.DEBUG
    # force=True re-binds to the current stderr on every invocation, which
    # keeps repeated in-process runs (the test suite, a REPL) from logging
    # into a stale, closed stream.
    logging.basicConfig(
        level=level,
        format="%(levelname)s %(name)s: %(message)s",
        force=True,
    )


pass_app = click.make_pass_decorator(AppContext)


# --------------------------------------------------------------------------
# Root group
# --------------------------------------------------------------------------


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.option(
    "--config",
    "config_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help=f"Config file to load (default: {DEFAULT_CONFIG_PATH}).",
)
@click.option("-v", "--verbose", count=True, help="Log more (-v info, -vv debug).")
@click.option(
    "--dry-run",
    is_flag=True,
    help="Compute everything but write nothing (no DB, no files, no API calls).",
)
@click.version_option(version=__version__, prog_name="jobsearcher")
@click.pass_context
def main(ctx: click.Context, config_path: Path | None, verbose: int, dry_run: bool) -> None:
    """Collect, store, score, and export job postings from multiple sources."""
    _make_output_encodable()
    _configure_logging(verbose)
    ctx.obj = AppContext(config_path=config_path, verbose=verbose, dry_run=dry_run)


# --------------------------------------------------------------------------
# init
# --------------------------------------------------------------------------


@main.command()
@click.option("--force", is_flag=True, help="Overwrite an existing config.yaml.")
def init(force: bool) -> None:
    """Create config.yaml from the bundled example and explain what is next."""
    if DEFAULT_CONFIG_PATH.exists() and not force:
        raise click.ClickException(
            f"{DEFAULT_CONFIG_PATH} already exists. Edit it, or pass --force to overwrite it."
        )
    DEFAULT_CONFIG_PATH.write_text(example_config_text(), encoding="utf-8")
    _out.print(f"[green]Created[/green] {DEFAULT_CONFIG_PATH} from the bundled example.")
    _out.print(
        "\nNext:\n"
        f"  1. Edit [cyan]{DEFAULT_CONFIG_PATH}[/cyan] - set your [cyan]profile:[/cyan] "
        "(role, skills, absent_skills) and pick your [cyan]sources:[/cyan].\n"
        "  2. [cyan]jobsearcher fetch[/cyan]   - collect and store postings.\n"
        "  3. [cyan]jobsearcher score[/cyan]   - score the ones you collected.\n"
        "  4. [cyan]jobsearcher list[/cyan]    - see the ranked results.\n"
        "  5. [cyan]jobsearcher export markdown[/cyan] - write them out."
    )


# --------------------------------------------------------------------------
# fetch
# --------------------------------------------------------------------------


@main.command()
@click.option("--source", "sources", multiple=True, help="Only this source (repeatable).")
@click.option(
    "--limit",
    type=click.IntRange(min=1),
    default=None,
    help="Stop after N kept postings per source.",
)
@pass_app
def fetch(ctx: AppContext, sources: tuple[str, ...], limit: int | None) -> None:
    """Collect postings from the configured sources and store them (no scoring)."""
    with ctx.open_storage() as storage:
        pipeline = ctx.pipeline(storage)
        only = list(sources) or None
        try:
            planned = pipeline.planned_sources(only)
        except PipelineError as exc:
            raise click.ClickException(str(exc)) from exc

        if not planned:
            _err.print(
                "[yellow]No sources selected.[/yellow] Enable one under `sources:` in config."
            )
            return

        results: list[SourceFetchResult] = []
        with _phase_progress("Fetching", total=len(planned)) as (progress, task):
            for result in pipeline.fetch(only=only, limit=limit):
                results.append(result)
                progress.update(
                    task, advance=1, description=f"Fetched [cyan]{result.source}[/cyan]"
                )

    _render_fetch_table(results, dry_run=ctx.dry_run)
    if any(result.failed for result in results):
        # Same rule as `run` (see `RunReport.ok`): one dead source fails the
        # command. `fetch` is just as likely to be the thing a cron job
        # calls, and an exit code that only turns non-zero once *every*
        # source is down is an exit code that ignores a source which has
        # been broken for three weeks.
        raise click.ClickException("one or more sources failed; see the errors above")


# --------------------------------------------------------------------------
# score
# --------------------------------------------------------------------------


@main.command()
@click.option("--limit", type=click.IntRange(min=1), default=None, help="Score at most N postings.")
@click.option("--rescore", is_flag=True, help="Also re-score postings that already have a score.")
@pass_app
def score(ctx: AppContext, limit: int | None, rescore: bool) -> None:
    """Score postings that have no score yet with the configured backend."""
    with ctx.open_storage() as storage:
        pipeline = ctx.pipeline(storage)
        pending = storage.count(scored=None if rescore else False)
        total = min(pending, limit) if limit is not None else pending
        if total == 0:
            _out.print("[green]Nothing to score.[/green] Every posting already has a score.")
            return

        rows: list[ScoredPosting] = []
        try:
            with _phase_progress("Scoring", total=total) as (progress, task):
                for row in pipeline.score(limit=limit, rescore=rescore):
                    rows.append(row)
                    progress.update(task, advance=1)
        except PipelineError as exc:
            raise click.ClickException(str(exc)) from exc

    scored = sum(1 for row in rows if row.result is not None)
    failed = [row for row in rows if row.result is None and not row.stopped_early]
    stopped = any(row.stopped_early for row in rows)

    verb = "would score" if ctx.dry_run else "scored"
    _out.print(f"[green]{verb.capitalize()} {scored}[/green] posting(s).")
    if failed:
        _err.print(f"[yellow]{len(failed)} posting(s) could not be scored:[/yellow]")
        for row in failed:
            _err.print(
                Text.assemble(
                    "  ",
                    (row.posting.id[:8], "dim"),
                    " ",
                    row.posting.title,
                    " - ",
                    row.reason or "",
                )
            )
    if stopped:
        _out.print("[yellow]Scorer budget reached[/yellow]; run again to score the rest.")
    _print_unreadable(pipeline.last_score_unreadable, what="the scoring phase")


# --------------------------------------------------------------------------
# list
# --------------------------------------------------------------------------


@main.command(name="list")
@click.option("--min-score", type=click.IntRange(0, 100), default=None)
@click.option("--max-score", type=click.IntRange(0, 100), default=None)
@click.option("--source", default=None, help="Only postings from this source.")
@click.option(
    "--status",
    type=click.Choice(_STATUS_VALUES),
    default=None,
    help="Only postings with this application status.",
)
@click.option("--remote", is_flag=True, help="Only fully-remote postings.")
@click.option("--unscored", is_flag=True, help="Only postings with no score.")
@click.option("--language", "languages", multiple=True, help=_LANGUAGE_HELP)
@click.option("--limit", type=click.IntRange(min=0), default=50, help="Max rows (0 = all).")
@click.option("--sort", type=click.Choice(_SORT_VALUES), default="score")
@click.option("--json", "as_json", is_flag=True, help="Emit a JSON array instead of a table.")
@pass_app
def list_postings(
    ctx: AppContext,
    min_score: int | None,
    max_score: int | None,
    source: str | None,
    status: str | None,
    remote: bool,
    unscored: bool,
    languages: tuple[str, ...],
    limit: int,
    sort: str,
    as_json: bool,
) -> None:
    """Show stored postings as a table (or JSON)."""
    filters = _posting_filters(
        ctx,
        source=source,
        status=status,
        min_score=min_score,
        max_score=max_score,
        remote=remote,
        unscored=unscored,
        languages=languages,
    )

    with ctx.open_storage() as storage:
        postings = filters.select(storage)
        _report_unreadable(storage)

    postings = _sorted_postings(postings, sort)
    if limit:
        postings = postings[:limit]

    if as_json:
        click.echo(json.dumps([_posting_summary(p) for p in postings], indent=2))
        return
    if not postings:
        _out.print("[dim]No postings match.[/dim]")
        return
    _render_list_table(postings)


# --------------------------------------------------------------------------
# show
# --------------------------------------------------------------------------


@main.command()
@click.argument("ref")
@pass_app
def show(ctx: AppContext, ref: str) -> None:
    """Show one posting in full, by id prefix or URL."""
    with ctx.open_storage() as storage:
        posting = _resolve_ref(storage, ref)
    _render_detail(posting)


# --------------------------------------------------------------------------
# status
# --------------------------------------------------------------------------


@main.command(name="status")
@click.argument("ref")
@click.argument("new_status", type=click.Choice(_STATUS_VALUES))
@click.option("--at", "applied_at", type=click.DateTime(), default=None, help="Set applied_at.")
@click.option("--clear-date", is_flag=True, help="Clear applied_at instead of setting it.")
@pass_app
def set_status(
    ctx: AppContext,
    ref: str,
    new_status: str,
    applied_at: datetime | None,
    clear_date: bool,
) -> None:
    """Update the application-tracking status of one posting."""
    if applied_at is not None and clear_date:
        raise click.UsageError("--at and --clear-date are mutually exclusive.")

    target = ApplicationStatus(new_status)
    with ctx.open_storage() as storage:
        posting = _resolve_ref(storage, ref)
        when = _resolve_applied_at(posting, target, applied_at, clear_date)
        previous = posting.status
        if not ctx.dry_run:
            storage.update_status(posting.url, target, applied_at=when)

    prefix = "Would set" if ctx.dry_run else "Set"
    _out.print(
        f"{prefix} [cyan]{posting.id[:8]}[/cyan] "
        f"[dim]{previous.value}[/dim] -> [green]{target.value}[/green]"
        + (f" (applied {when:%Y-%m-%d})" if when else "")
    )


# --------------------------------------------------------------------------
# export
# --------------------------------------------------------------------------


@main.command()
@click.argument("fmt", type=click.Choice(EXPORTER_FORMATS))
@click.option("--output", type=click.Path(dir_okay=False, path_type=Path), default=None)
@click.option("--min-score", type=click.IntRange(0, 100), default=None)
@click.option("--max-score", type=click.IntRange(0, 100), default=None)
@click.option("--source", default=None)
@click.option("--status", type=click.Choice(_STATUS_VALUES), default=None)
@click.option("--remote", is_flag=True)
@click.option("--unscored", is_flag=True, help="Only postings with no score.")
@click.option("--language", "languages", multiple=True, help=_LANGUAGE_HELP)
@pass_app
def export(
    ctx: AppContext,
    fmt: str,
    output: Path | None,
    min_score: int | None,
    max_score: int | None,
    source: str | None,
    status: str | None,
    remote: bool,
    unscored: bool,
    languages: tuple[str, ...],
) -> None:
    """Write stored postings to a file format or to Notion.

    Covers the same postings `list` shows, and takes the same options to
    change that: an export that quietly carried more than the table did
    would make the table a lie.
    """
    if output is not None and fmt == "notion":
        _err.print("[yellow]--output is ignored for the notion exporter.[/yellow]")

    filters = _posting_filters(
        ctx,
        source=source,
        status=status,
        min_score=min_score,
        max_score=max_score,
        remote=remote,
        unscored=unscored,
        languages=languages,
    )
    with ctx.open_storage() as storage:
        pipeline = ctx.pipeline(storage)
        outcomes = list(pipeline.export(formats=[fmt], filters=filters, output_override=output))

    _render_export(outcomes, dry_run=ctx.dry_run)
    if any(outcome.error for outcome in outcomes):
        raise click.ClickException("export failed; see the errors above")


# --------------------------------------------------------------------------
# run
# --------------------------------------------------------------------------


@main.command()
@click.option("--source", "sources", multiple=True, help="Fetch only these sources (repeatable).")
@click.option("--format", "formats", multiple=True, help="Export only these formats (repeatable).")
@click.option("--score-limit", type=click.IntRange(min=1), default=None)
@click.option("--language", "languages", multiple=True, help=_LANGUAGE_HELP)
@click.option("--skip-fetch", is_flag=True)
@click.option("--skip-score", is_flag=True)
@click.option("--skip-export", is_flag=True)
@pass_app
def run(
    ctx: AppContext,
    sources: tuple[str, ...],
    formats: tuple[str, ...],
    score_limit: int | None,
    languages: tuple[str, ...],
    skip_fetch: bool,
    skip_score: bool,
    skip_export: bool,
) -> None:
    """Fetch, then score, then export - one command, cron-friendly."""
    filters = _posting_filters(ctx, languages=languages)
    with ctx.open_storage() as storage:
        pipeline = ctx.pipeline(storage)
        try:
            report = pipeline.run(
                only_sources=list(sources) or None,
                formats=list(formats) or None,
                score_limit=score_limit,
                filters=filters,
                skip_fetch=skip_fetch,
                skip_score=skip_score,
                skip_export=skip_export,
            )
        except PipelineError as exc:
            raise click.ClickException(str(exc)) from exc

    if report.fetch:
        _out.rule("fetch")
        _render_fetch_table(report.fetch, dry_run=ctx.dry_run)
    if report.score is not None:
        _out.rule("score")
        verb = "would score" if ctx.dry_run else "scored"
        _out.print(f"{verb.capitalize()} {report.score.scored}, failed {report.score.failed}.")
        if report.score.stopped_early:
            _out.print("[yellow]Scorer budget reached[/yellow]; run again to finish.")
        _print_unreadable(report.score.unreadable, what="the scoring phase")
    if report.export:
        _out.rule("export")
        _render_export(report.export, dry_run=ctx.dry_run)

    if not report.ok:
        raise click.ClickException("one or more phases failed; see above")


# --------------------------------------------------------------------------
# Rendering helpers
# --------------------------------------------------------------------------


@contextlib.contextmanager
def _phase_progress(label: str, *, total: int) -> Iterator[tuple[Progress, TaskID]]:
    """A progress display for one phase, yielding it and its single task id."""
    progress = Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=_err,
        transient=True,
        # No live bar when output is not a terminal (a pipe, a cron log):
        # the phase result tables still print afterwards.
        disable=not _err.is_terminal,
    )
    with progress:
        task = progress.add_task(label, total=total)
        yield progress, task


def _print_unreadable(count: int, *, what: str) -> None:
    """Say how many stored rows a read had to skip, if any.

    Storage skips a row it cannot decode rather than letting one bad row
    take down the command (see
    :meth:`~jobsearcher.storage.sqlite.SqliteStorage.unreadable_rows`). That
    is only acceptable while it is said out loud: a row missing with nothing
    to show for it is worse than the crash it replaced, because nobody goes
    looking for what they were not told is gone. It matters most in `run`,
    which is built to be put in a cron and therefore the one command nobody
    is reading while it works.

    Written to stderr so `list --json` stays pipeable.

    Args:
        count: How many rows were skipped; nothing is printed for zero.
        what: What the skipped rows are missing from, e.g. ``"these
            results"`` or ``"the export"``.
    """
    if count:
        _err.print(
            f"[yellow]{count} stored row(s) could not be read[/yellow] and are missing "
            f"from {what}; see the log above for which, and the README's "
            f"Limitations for what can be done about it."
        )


def _report_unreadable(storage: SqliteStorage) -> None:
    """Report on the storage handle's most recent read (see `_print_unreadable`)."""
    _print_unreadable(storage.unreadable_rows, what="these results")


def _plain(value: str, style: str = "") -> Text:
    """Wrap posting-derived text so Rich renders it literally.

    Titles, companies, locations and descriptions come from scraped pages,
    where ``[`` is an ordinary character. Rich reads it as the start of a
    style tag, which either raises ``MarkupError`` on an unmatched closing
    tag or - the quieter failure - swallows a well-formed one and drops the
    text from the output with nothing to show that anything went missing.

    Args:
        value: The untrusted text.
        style: An optional Rich style applied to the whole run, replacing
            the inline markup that would otherwise have wrapped it.

    Returns:
        A ``Text`` carrying ``value`` verbatim.
    """
    return Text(value, style=style)


def _score_style(value: int | None) -> str:
    if value is None:
        return "dim"
    if value >= 70:
        return "green"
    if value >= 40:
        return "yellow"
    return "dim"


def _render_fetch_table(results: list[SourceFetchResult], *, dry_run: bool) -> None:
    table = Table(title="Fetch" + (" (dry run)" if dry_run else ""), title_justify="left")
    table.add_column("source", style="cyan")
    table.add_column("yielded", justify="right")
    table.add_column("kept", justify="right")
    table.add_column("filtered", justify="right")
    table.add_column("new" if not dry_run else "would store", justify="right")
    table.add_column("dupes", justify="right")
    table.add_column("errors", justify="right")
    for result in results:
        stored = "-" if dry_run else str(result.stored)
        dupes = "-" if dry_run else str(result.duplicates)
        table.add_row(
            _plain(result.source, "red" if result.failed else ""),
            str(result.yielded),
            str(result.kept),
            str(result.filtered_out),
            stored,
            dupes,
            f"[red]{len(result.errors)}[/red]" if result.errors else "0",
        )
    _out.print(table)
    for result in results:
        for message in result.errors:
            _err.print(Text.assemble((f"{result.source}:", "red"), " ", message))


def _sorted_postings(postings: list[JobPosting], sort: str) -> list[JobPosting]:
    if sort == "company":
        return sorted(postings, key=lambda p: (p.company.casefold(), p.title.casefold()))
    if sort == "date":
        return sorted(postings, key=lambda p: p.fetched_at, reverse=True)
    return sorted(
        postings,
        key=lambda p: (p.score is None, -(p.score or 0), p.title.casefold()),
    )


def _render_list_table(postings: list[JobPosting]) -> None:
    table = Table()
    table.add_column("id", style="dim", no_wrap=True)
    table.add_column("score", justify="right")
    table.add_column("title")
    table.add_column("company", style="cyan")
    table.add_column("source")
    table.add_column("mode")
    table.add_column("location")
    table.add_column("status")
    for posting in postings:
        score = "-" if posting.score is None else str(posting.score)
        table.add_row(
            posting.id[:8],
            _plain(score, _score_style(posting.score)),
            _plain(posting.title),
            _plain(posting.company),
            _plain(posting.source),
            posting.work_mode.value,
            _plain(posting.location) if posting.location else _plain("-", "dim"),
            posting.status.value,
        )
    _out.print(table)


def _render_detail(posting: JobPosting) -> None:
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="dim", justify="right")
    grid.add_column()

    def row(label: str, value: str | Text) -> None:
        grid.add_row(label, value)

    row("id", posting.id)
    row("title", _plain(posting.title, "bold"))
    row("company", _plain(posting.company))
    row("source", _plain(posting.source))
    row("url", _plain(posting.url))
    if posting.raw_url != posting.url:
        row("raw url", _plain(posting.raw_url))
    row("location", _plain(posting.location or "-"))
    row("work mode", posting.work_mode.value)
    if posting.contract_type:
        row("contract", _plain(posting.contract_type))
    if posting.salary_text:
        row("salary", _plain(posting.salary_text))
    if posting.eligible_locations:
        row("eligible", _plain(", ".join(posting.eligible_locations)))
    row("language", _plain(posting.detected_language or "undetermined"))
    if posting.published_at:
        row("published", f"{posting.published_at:%Y-%m-%d}")
    row("fetched", f"{posting.fetched_at:%Y-%m-%d}")
    applied = f" ({posting.applied_at:%Y-%m-%d})" if posting.applied_at else ""
    row("status", f"{posting.status.value}{applied}")

    score = "not scored yet" if posting.score is None else str(posting.score)
    row("score", _plain(score, _score_style(posting.score)))
    if posting.summary:
        row("summary", _plain(posting.summary))
    _detail_skill_row(grid, "matched", posting.matched_skills)
    _detail_skill_row(grid, "not mentioned", posting.unmatched_profile_skills)
    _detail_skill_row(grid, "missing reqs", posting.missing_requirements)
    _detail_skill_row(grid, "penalized", posting.penalized_skills)

    _out.print(grid)
    if posting.description_clean or posting.description_raw:
        _out.rule("description")
        _out.print(_plain(posting.description_clean or posting.description_raw))


def _detail_skill_row(grid: Table, label: str, skills: list[str]) -> None:
    if skills:
        grid.add_row(label, _plain(", ".join(skills)))


def _render_export(outcomes: list[ExportOutcome], *, dry_run: bool) -> None:
    for outcome in outcomes:
        _print_unreadable(outcome.unreadable, what=f"the {outcome.fmt} export")
        if outcome.error is not None:
            _err.print(Text.assemble((f"{outcome.fmt}:", "red"), " ", outcome.error))
        elif dry_run:
            _out.print(
                f"[cyan]{outcome.fmt}[/cyan]: would export {outcome.would_export} posting(s)."
            )
        elif outcome.result is not None:
            _out.print(Text.assemble((outcome.fmt, "green"), ": ", outcome.result.detail))


# --------------------------------------------------------------------------
# Small pure helpers
# --------------------------------------------------------------------------


def _posting_filters(
    ctx: AppContext,
    *,
    source: str | None = None,
    status: str | None = None,
    min_score: int | None = None,
    max_score: int | None = None,
    remote: bool = False,
    unscored: bool = False,
    languages: tuple[str, ...] = (),
) -> PostingFilters:
    """Build the read scope shared by `list`, `export` and `run`.

    One builder rather than one per command, so the set of postings a
    command shows and the set another one writes out cannot drift apart.

    Raises:
        click.UsageError: If ``unscored`` is combined with a score bound,
            which can only ever match nothing.
    """
    if unscored and (min_score is not None or max_score is not None):
        raise click.UsageError("--unscored cannot be combined with --min-score/--max-score.")

    configured = ctx.load_config().search.languages
    return PostingFilters(
        source=source,
        status=ApplicationStatus(status) if status else None,
        min_score=min_score,
        max_score=max_score,
        scored=False if unscored else None,
        languages=_resolve_languages(languages, configured),
        remote=remote,
    )


def _resolve_languages(requested: tuple[str, ...], configured: list[str]) -> tuple[str, ...] | None:
    """Turn ``--language`` options into a filter list (``None`` = no filter)."""
    if not requested:
        return tuple(configured) or None
    if any(value.lower() == "all" for value in requested):
        return None
    return tuple(requested)


def _resolve_applied_at(
    posting: JobPosting,
    target: ApplicationStatus,
    explicit: datetime | None,
    clear: bool,
) -> datetime | None:
    if clear:
        return None
    if explicit is not None:
        return explicit if explicit.tzinfo else explicit.replace(tzinfo=UTC)
    if target is ApplicationStatus.APPLIED and posting.applied_at is None:
        return datetime.now(UTC)
    return posting.applied_at


def _posting_summary(posting: JobPosting) -> dict[str, Any]:
    return {
        "id": posting.id,
        "score": posting.score,
        "title": posting.title,
        "company": posting.company,
        "source": posting.source,
        "work_mode": posting.work_mode.value,
        "location": posting.location,
        "status": posting.status.value,
        "detected_language": posting.detected_language,
        "url": posting.url,
    }


# Reads as a continuation of the sentence naming the reference, so it must
# not carry its own subject.
_UNREADABLE_HINT = (
    "is stored but could not be read; the reason is in the log above, and the "
    "README's Limitations cover what can be done about it"
)


def _resolve_ref(storage: SqliteStorage, ref: str) -> JobPosting:
    """Resolve a CLI ``<ref>`` (id prefix or URL) to exactly one posting.

    A row storage had to skip looks exactly like a row that is not there.
    Reporting it as absent would be false, and would hide the same broken
    row the skip was meant to survive — so the two cases are told apart.
    """
    if "://" in ref:
        try:
            posting = storage.get_by_url(ref)
        except ValueError as exc:
            raise click.ClickException(f"{ref!r} is not a usable URL: {exc}") from exc
        if posting is None:
            if storage.unreadable_rows:
                raise click.ClickException(f"The posting at {ref!r} {_UNREADABLE_HINT}.")
            raise click.ClickException(f"No posting has the URL {ref!r}.")
        return posting

    if not _ID_PREFIX_RE.fullmatch(ref):
        raise click.ClickException(
            f"{ref!r} is neither a full URL nor an id prefix (at least 4 hexadecimal characters)."
        )
    matches = storage.find_by_id_prefix(ref.lower())
    if len(matches) == 1:
        return matches[0]
    if not matches:
        if storage.unreadable_rows:
            raise click.ClickException(
                f"The only posting matching the id prefix {ref!r} {_UNREADABLE_HINT}."
            )
        raise click.ClickException(f"No posting matches the id prefix {ref!r}.")
    listed = "\n".join(f"  {p.id[:8]}  {p.title} ({p.company})" for p in matches)
    raise click.ClickException(f"The id prefix {ref!r} matches several postings:\n{listed}")


if __name__ == "__main__":
    main()
