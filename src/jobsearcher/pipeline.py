"""End-to-end orchestration: fetch, filter, store, score, export.

This module wires the other subpackages together and owns nothing of their
logic. It depends on all of them; nothing depends on it except
:mod:`jobsearcher.cli`.

Every phase is independent and re-runnable, and each one persists its work
as it goes so that interrupting the pipeline never loses data and never
forces a restart from the beginning:

- **fetch** stores each posting the moment it is normalized and passes the
  keyword filter. Storage is an ``INSERT OR IGNORE`` on the posting's URL
  (see :meth:`~jobsearcher.storage.sqlite.SqliteStorage.save_many`), so a
  re-run simply skips what is already there — and skips whole sources that
  already succeeded just as cheaply.
- **score** reads only unscored postings by default and writes each score
  immediately, so a re-run picks up exactly where the last one stopped. A
  metered scorer that exhausts its budget ends the phase cleanly, leaving
  the rest for next time.
- **export** is read-only against storage; re-running it just rewrites the
  output.

The phase methods are generators that yield one result per unit of work, so
a caller (the CLI) can render progress as it happens. :meth:`Pipeline.run`
consumes all three for the one-shot ``jobsearcher run``.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import jobsearcher.sources  # noqa: F401  (import registers the built-in sources)
from jobsearcher.config import AppConfig, PluginConfig, SearchConfig
from jobsearcher.exporters import ExporterConfigError, ExporterError, ExportResult, get_exporter
from jobsearcher.models import ApplicationStatus, JobPosting, ScoreResult, WorkMode
from jobsearcher.scoring import ScorerBudgetExhaustedError, ScoringConfigError, get_scorer
from jobsearcher.sources.base import SourceError, get_source_class
from jobsearcher.storage import Storage, StorageConfigError

logger = logging.getLogger(__name__)


class PipelineError(Exception):
    """A pipeline phase could not run at all (bad config, unknown source)."""


def _passes_keyword_filter(title: str, search: SearchConfig) -> bool:
    """Whether a posting title survives the ``search:`` keyword filter.

    An excluded keyword anywhere in the title drops it. If any include
    keywords are configured, at least one must appear; with none configured
    every non-excluded title passes. Matching is case-insensitive substring.
    """
    lowered = title.casefold()
    if any(term.casefold() in lowered for term in search.title_keywords_exclude):
        return False
    includes = search.title_keywords_include
    if includes:
        return any(term.casefold() in lowered for term in includes)
    return True


@dataclass(frozen=True)
class PostingFilters:
    """The read-side filters shared by ``list``, ``export`` and ``run``."""

    source: str | None = None
    status: ApplicationStatus | None = None
    min_score: int | None = None
    max_score: int | None = None
    scored: bool | None = None
    languages: tuple[str, ...] | None = None
    remote: bool = False

    def select(self, storage: Storage) -> list[JobPosting]:
        """Return the stored postings that match these filters."""
        postings = storage.list(
            source=self.source,
            status=self.status,
            min_score=self.min_score,
            max_score=self.max_score,
            scored=self.scored,
            languages=self.languages,
        )
        if self.remote:
            postings = [p for p in postings if p.work_mode is WorkMode.REMOTE]
        return postings


@dataclass(frozen=True)
class SourceFetchResult:
    """What one source contributed to a fetch phase."""

    source: str
    yielded: int
    kept: int
    filtered_out: int
    stored: int
    duplicates: int
    errors: tuple[str, ...] = ()
    failed: bool = False


@dataclass(frozen=True)
class ScoredPosting:
    """One posting handled by the score phase."""

    posting: JobPosting
    result: ScoreResult | None
    reason: str | None = None
    stopped_early: bool = False


@dataclass(frozen=True)
class ScoreSummary:
    """Totals for a completed score phase.

    Attributes:
        scored: Postings scored and written.
        failed: Postings the scorer could not score.
        stopped_early: Whether a metered scorer ended the phase on budget.
        unreadable: Stored rows the phase could not decode and therefore
            never offered to the scorer. They stay unscored, and no later
            run will pick them up.
    """

    scored: int = 0
    failed: int = 0
    stopped_early: bool = False
    unreadable: int = 0


@dataclass(frozen=True)
class ExportOutcome:
    """The result of running one exporter.

    Attributes:
        fmt: The format name that ran.
        result: What the exporter wrote, if it ran successfully.
        error: Why it did not run, if it did not.
        would_export: How many postings a dry run would have written.
        unreadable: Stored rows that could not be decoded and are therefore
            missing from the output. Counted here, on the phase result,
            rather than read back off the storage handle afterwards: `run`
            performs several reads in a row, and a count that depended on
            which one happened last would be right only by luck.
    """

    fmt: str
    result: ExportResult | None = None
    error: str | None = None
    would_export: int | None = None
    unreadable: int = 0


@dataclass
class RunReport:
    """Everything ``jobsearcher run`` did, for a final summary."""

    fetch: list[SourceFetchResult] = field(default_factory=list)
    score: ScoreSummary | None = None
    export: list[ExportOutcome] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Whether every phase that ran produced a usable outcome.

        Every phase counts, and within the fetch phase every source counts:
        ``run`` is the cron-friendly entry point, so its exit code is the
        only signal an unattended run has. Four working sources do not
        excuse a fifth that collected nothing, and a scoring phase where
        every posting failed is not a success either.

        A metered scorer that cleanly exhausts its budget
        (``stopped_early``) is not a failure: the phase did what it was
        configured to do and left the rest for the next run.
        """
        fetch_ok = all(not result.failed for result in self.fetch)
        score_ok = self.score is None or self.score.failed == 0
        export_ok = all(outcome.error is None for outcome in self.export)
        return fetch_ok and score_ok and export_ok


class Pipeline:
    """Runs the fetch / score / export phases against one config and store."""

    def __init__(self, config: AppConfig, storage: Storage, *, dry_run: bool = False) -> None:
        """Bind the pipeline to its inputs.

        Args:
            config: The loaded application configuration.
            storage: An open storage handle. The pipeline reads and writes it
                but does not own its lifecycle.
            dry_run: When true, every phase computes its work and reports it
                but performs no writes (no inserts, no score updates, no
                files, no outbound API calls).
        """
        self._config = config
        self._storage = storage
        self._dry_run = dry_run
        self.last_score_unreadable = 0

    # -- fetch ---------------------------------------------------------------

    def fetch(
        self,
        *,
        only: Sequence[str] | None = None,
        limit: int | None = None,
    ) -> Iterator[SourceFetchResult]:
        """Collect, keyword-filter, and store postings, one source at a time.

        Args:
            only: Restrict to these source names; ``None`` runs every source
                marked ``enabled`` in config.
            limit: Stop each source after this many postings have passed the
                keyword filter.

        Yields:
            One :class:`SourceFetchResult` per source, after that source is
            fully stored.

        Raises:
            PipelineError: If ``only`` names a source absent from config.
        """
        for name, block in self._selected_sources(only):
            yield self._fetch_one(name, block, limit)

    def planned_sources(self, only: Sequence[str] | None = None) -> list[str]:
        """Return the source names :meth:`fetch` would run, in order.

        Lets a caller size a progress display before collection starts.

        Raises:
            PipelineError: If ``only`` names a source absent from config.
        """
        return [name for name, _ in self._selected_sources(only)]

    def _selected_sources(self, only: Sequence[str] | None) -> Iterator[tuple[str, PluginConfig]]:
        configured = self._config.sources
        if only is not None:
            unknown = sorted(set(only) - set(configured))
            if unknown:
                raise PipelineError(
                    f"no source named {', '.join(repr(u) for u in unknown)} in config"
                )
            requested = set(only)
            for name in configured:
                if name in requested:
                    yield name, configured[name]
            return
        for name, block in configured.items():
            if block.enabled:
                yield name, block

    def _fetch_one(self, name: str, block: PluginConfig, limit: int | None) -> SourceFetchResult:
        source_cls = get_source_class(name)
        if source_cls is None:
            message = f"source {name!r} is configured but no implementation is registered; skipping"
            logger.warning("%s", message)
            return SourceFetchResult(name, 0, 0, 0, 0, 0, (message,), failed=True)

        try:
            source = source_cls(name, block)
        except SourceError as exc:
            logger.error("source %r could not be initialized: %s", name, exc)
            return SourceFetchResult(name, 0, 0, 0, 0, 0, (str(exc),), failed=True)

        kept = 0
        filtered_out = 0
        stored = 0
        unexpected: tuple[str, ...] = ()
        with source:
            postings = source.fetch()
            try:
                for posting in postings:
                    if not _passes_keyword_filter(posting.title, self._config.search):
                        filtered_out += 1
                        continue
                    kept += 1
                    if not self._dry_run:
                        stored += self._storage.save_many([posting])
                    if limit is not None and kept >= limit:
                        break
            except Exception as exc:
                # `Source.fetch` isolates `SourceError`; everything else — a
                # bs4 `AttributeError` on drifted markup, a `TypeError` on an
                # unexpected payload shape — would otherwise walk out of this
                # method and cost every source that has not run yet. The
                # policy stated in sources/base.py only holds if the layer
                # that runs the sources enforces it.
                logger.exception("source %r raised an unexpected error", name)
                unexpected = (f"unexpected {type(exc).__name__}: {exc}",)
            finally:
                # Run the generator's own cleanup instead of leaving it
                # suspended mid-request when `limit` cut the loop short.
                postings.close()
            stats = source.last_run

        errors = (*stats.errors, *unexpected)
        return SourceFetchResult(
            source=name,
            yielded=stats.yielded,
            kept=kept,
            filtered_out=filtered_out,
            stored=stored,
            duplicates=0 if self._dry_run else kept - stored,
            errors=errors,
            # Same rule as `SourceRunStats.failed`, extended to cover an
            # unexpected error: the source failed only if it produced nothing.
            failed=stats.yielded == 0 and bool(errors),
        )

    # -- score --------------------------------------------------------------

    def score(
        self,
        *,
        limit: int | None = None,
        rescore: bool = False,
    ) -> Iterator[ScoredPosting]:
        """Score postings with the configured backend, writing each result.

        Args:
            limit: Score at most this many postings.
            rescore: Also re-score postings that already have a score;
                by default only unscored postings are considered.

        Yields:
            One :class:`ScoredPosting` per posting handled. ``result`` is
            ``None`` when the posting could not be scored; ``stopped_early``
            marks the posting whose attempt exhausted a metered scorer's
            budget (iteration stops right after it).

        Raises:
            PipelineError: If the scoring backend cannot be built.
        """
        try:
            scorer = get_scorer(self._config.scoring)
        except (ScoringConfigError, ValueError) as exc:
            raise PipelineError(str(exc)) from exc

        profile = self._config.profile
        close = getattr(scorer, "close", None)
        try:
            postings = self._storage.list(
                scored=None if rescore else False,
                limit=limit,
            )
            # Captured at the read, not after the loop: a row that cannot be
            # decoded is never offered to the scorer and stays unscored
            # forever, which `run --skip-export` would otherwise never
            # mention to anyone.
            self.last_score_unreadable = self._storage.unreadable_rows
            for posting in postings:
                try:
                    result = scorer.score(posting, profile)
                except ScorerBudgetExhaustedError as exc:
                    yield ScoredPosting(posting, None, str(exc), stopped_early=True)
                    return
                except RuntimeError as exc:
                    logger.warning("could not score %s: %s", posting.url, exc)
                    yield ScoredPosting(posting, None, str(exc))
                    continue
                if not self._dry_run:
                    written = self._storage.update_score(
                        posting.url,
                        score=result.score,
                        summary=result.summary,
                        matched_skills=result.matched_skills,
                        unmatched_profile_skills=result.unmatched_profile_skills,
                        missing_requirements=result.missing_requirements,
                        penalized_skills=result.penalized_skills,
                        location_match=result.location_match,
                        work_mode_match=result.work_mode_match,
                    )
                    if not written:
                        # The row went away between the list and the update.
                        # Counting this posting as scored would make the run
                        # summary disagree with the database and hide that it
                        # is still unscored.
                        reason = "the posting is no longer in storage; its score was not written"
                        logger.warning("could not store the score for %s: %s", posting.url, reason)
                        yield ScoredPosting(posting, None, reason)
                        continue
                yield ScoredPosting(posting, result)
        finally:
            if callable(close):
                close()

    # -- export -----------------------------------------------------------

    def export(
        self,
        *,
        formats: Sequence[str] | None = None,
        filters: PostingFilters | None = None,
        output_override: Path | None = None,
    ) -> Iterator[ExportOutcome]:
        """Run one or more exporters over the filtered stored postings.

        Args:
            formats: Format names to run; ``None`` runs every exporter block
                marked ``enabled`` in config.
            filters: Which stored postings to export; defaults to all.
            output_override: Replace each file exporter's ``output_path``
                with this path (ignored by the Notion exporter).

        Yields:
            One :class:`ExportOutcome` per format.
        """
        active_filters = filters or PostingFilters()
        blocks = self._config.exporters
        if formats is not None:
            names = list(formats)
        else:
            names = [name for name, block in blocks.items() if block.enabled]
        for name in names:
            yield self._export_one(name, blocks.get(name), active_filters, output_override)

    def _export_one(
        self,
        name: str,
        block: PluginConfig | None,
        filters: PostingFilters,
        output_override: Path | None,
    ) -> ExportOutcome:
        if block is None:
            return ExportOutcome(name, error=f"no `exporters.{name}` block in config")

        try:
            exporter = get_exporter(name)
        except ExporterConfigError as exc:
            return ExportOutcome(name, error=str(exc))

        options = block
        if output_override is not None and name != "notion":
            options = PluginConfig(**{**block.model_dump(), "output_path": str(output_override)})

        postings = filters.select(self._storage)
        # Read immediately after the select: the counter describes the most
        # recent read, and anything else touching storage would reset it.
        unreadable = self._storage.unreadable_rows
        if self._dry_run:
            return ExportOutcome(name, would_export=len(postings), unreadable=unreadable)
        try:
            result = exporter.export(postings, options)
        except ExporterError as exc:
            return ExportOutcome(name, error=str(exc), unreadable=unreadable)
        return ExportOutcome(name, result=result, unreadable=unreadable)

    # -- run ---------------------------------------------------------------

    def run(
        self,
        *,
        only_sources: Sequence[str] | None = None,
        formats: Sequence[str] | None = None,
        score_limit: int | None = None,
        filters: PostingFilters | None = None,
        skip_fetch: bool = False,
        skip_score: bool = False,
        skip_export: bool = False,
    ) -> RunReport:
        """Run fetch, then score, then export, and return a combined report.

        A phase that fails does not roll back the phases before it: their
        writes are already persisted, and re-running ``run`` resumes from
        where this one stopped.
        """
        report = RunReport()

        if not skip_fetch:
            report.fetch = list(self.fetch(only=only_sources))

        if not skip_score:
            scored = 0
            failed = 0
            stopped_early = False
            for row in self.score(limit=score_limit):
                if row.result is not None:
                    scored += 1
                elif row.stopped_early:
                    stopped_early = True
                else:
                    failed += 1
            report.score = ScoreSummary(scored, failed, stopped_early, self.last_score_unreadable)

        if not skip_export:
            report.export = list(self.export(formats=formats, filters=filters))

        return report


__all__ = [
    "ExportOutcome",
    "Pipeline",
    "PipelineError",
    "PostingFilters",
    "RunReport",
    "ScoreSummary",
    "ScoredPosting",
    "SourceFetchResult",
    "StorageConfigError",
]
