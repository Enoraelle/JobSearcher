"""Tests for the fetch / score / export pipeline, using in-memory fakes.

No network I/O: the fake sources below yield postings straight from their
configuration block (see CLAUDE.md's "tests never touch the network").
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

import pytest

from jobsearcher.config import AppConfig
from jobsearcher.models import JobPosting
from jobsearcher.pipeline import Pipeline, PipelineError, PostingFilters
from jobsearcher.scoring import (
    KeywordScorer,
    ScorerBudgetExhaustedError,
    ScoringError,
)
from jobsearcher.sources.base import (
    _REGISTRY,
    DEFAULT_BACKOFF_BASE,
    DEFAULT_MAX_RETRIES,
    DEFAULT_TIMEOUT,
    Source,
    register_source,
)
from jobsearcher.storage.sqlite import SqliteStorage

# The network-free "fake" source is registered by tests/conftest.py.


def _posting(url: str, title: str, **extra: Any) -> dict[str, Any]:
    """A raw posting dict for the fake source's ``postings`` config key."""
    return {"url": url, "title": title, **extra}


def _make_config(**overrides: Any) -> AppConfig:
    base: dict[str, Any] = {
        "profile": {
            "role": "Backend Developer",
            "skills": ["python", "django"],
            "absent_skills": ["php"],
        },
        "search": {},
        "sources": {},
        "storage": {"backend": "sqlite", "path": ":memory:"},
        "scoring": {"backend": "keyword_match"},
        "exporters": {},
    }
    base.update(overrides)
    return AppConfig.model_validate(base)


@pytest.fixture
def storage() -> Iterator[SqliteStorage]:
    with SqliteStorage(":memory:") as store:
        yield store


# -- fetch ---------------------------------------------------------------


def test_fetch_stores_and_filters_by_title_keywords(storage: SqliteStorage) -> None:
    config = _make_config(
        search={"title_keywords_include": ["engineer"], "title_keywords_exclude": ["senior"]},
        sources={
            "fake": {
                "enabled": True,
                "postings": [
                    _posting("https://e.test/1", "Backend Engineer"),
                    _posting("https://e.test/2", "Senior Backend Engineer"),
                    _posting("https://e.test/3", "Marketing Manager"),
                ],
            }
        },
    )
    pipeline = Pipeline(config, storage)

    [result] = list(pipeline.fetch())

    assert result.kept == 1
    assert result.filtered_out == 2
    assert result.stored == 1
    assert [p.title for p in storage.list()] == ["Backend Engineer"]


def test_fetch_is_idempotent_and_never_loses_data(storage: SqliteStorage) -> None:
    config = _make_config(
        sources={"fake": {"enabled": True, "postings": [_posting("https://e.test/1", "Dev")]}}
    )
    pipeline = Pipeline(config, storage)

    (first,) = list(pipeline.fetch())
    (second,) = list(pipeline.fetch())

    assert first.stored == 1
    assert second.stored == 0
    assert second.duplicates == 1
    assert storage.count() == 1


def test_fetch_isolates_a_failing_source(storage: SqliteStorage) -> None:
    config = _make_config(
        sources={
            "fake": {
                "enabled": True,
                "postings": [_posting("https://e.test/ok", "Dev")],
                "fail": True,
            }
        }
    )
    pipeline = Pipeline(config, storage)

    [result] = list(pipeline.fetch())

    # The source raised after yielding one item: that item is still stored,
    # and the failure is recorded rather than propagated.
    assert result.stored == 1
    assert result.errors
    assert storage.count() == 1


def test_fetch_dry_run_writes_nothing(storage: SqliteStorage) -> None:
    config = _make_config(
        sources={"fake": {"enabled": True, "postings": [_posting("https://e.test/1", "Dev")]}}
    )
    pipeline = Pipeline(config, storage, dry_run=True)

    [result] = list(pipeline.fetch())

    assert result.kept == 1
    assert result.stored == 0
    assert storage.count() == 0


def test_fetch_unknown_source_name_raises(storage: SqliteStorage) -> None:
    pipeline = Pipeline(_make_config(), storage)
    with pytest.raises(PipelineError, match="nope"):
        list(pipeline.fetch(only=["nope"]))


def test_fetch_configured_source_without_implementation_is_not_fatal(
    storage: SqliteStorage,
) -> None:
    config = _make_config(sources={"linkedin": {"enabled": True}})
    pipeline = Pipeline(config, storage)

    [result] = list(pipeline.fetch())

    assert result.failed is True
    assert result.errors


# -- score -------------------------------------------------------------


def _fetch_three(storage: SqliteStorage) -> Pipeline:
    config = _make_config(
        sources={
            "fake": {
                "enabled": True,
                "postings": [_posting(f"https://e.test/{i}", f"Backend Dev {i}") for i in range(3)],
            }
        }
    )
    pipeline = Pipeline(config, storage)
    list(pipeline.fetch())
    return pipeline


def test_score_only_touches_unscored_postings(storage: SqliteStorage) -> None:
    pipeline = _fetch_three(storage)

    first_pass = list(pipeline.score())
    second_pass = list(pipeline.score())

    assert len(first_pass) == 3
    assert all(row.result is not None for row in first_pass)
    assert second_pass == []
    assert storage.count(scored=False) == 0


def test_score_resumes_after_a_limited_run(storage: SqliteStorage) -> None:
    pipeline = _fetch_three(storage)

    list(pipeline.score(limit=2))
    assert storage.count(scored=False) == 1

    list(pipeline.score())
    assert storage.count(scored=False) == 0


def test_score_writes_the_keyword_score(storage: SqliteStorage) -> None:
    pipeline = _fetch_three(storage)

    list(pipeline.score())

    stored = storage.list()
    assert stored
    # Every fake posting mentions both profile skills.
    assert all(p.score == 100 for p in stored)


# -- export ----------------------------------------------------------


def test_export_writes_a_file(storage: SqliteStorage, tmp_path: Path) -> None:
    out = tmp_path / "digest.md"
    config = _make_config(
        sources={
            "fake": {"enabled": True, "postings": [_posting("https://e.test/1", "Backend Dev")]}
        },
        exporters={"markdown": {"enabled": True, "output_path": str(out)}},
    )
    pipeline = Pipeline(config, storage)
    list(pipeline.fetch())
    list(pipeline.score())

    [outcome] = list(pipeline.export())

    assert outcome.error is None
    assert outcome.result is not None
    assert out.exists()
    assert "Backend Dev" in out.read_text(encoding="utf-8")


def test_export_dry_run_reports_without_writing(storage: SqliteStorage, tmp_path: Path) -> None:
    out = tmp_path / "digest.md"
    config = _make_config(
        sources={
            "fake": {"enabled": True, "postings": [_posting("https://e.test/1", "Backend Dev")]}
        },
        exporters={"markdown": {"enabled": True, "output_path": str(out)}},
    )
    pipeline = Pipeline(config, storage)
    list(pipeline.fetch())
    dry = Pipeline(config, storage, dry_run=True)

    [outcome] = list(dry.export())

    assert outcome.would_export == 1
    assert not out.exists()


def test_export_unknown_format_block_is_reported(storage: SqliteStorage) -> None:
    pipeline = Pipeline(_make_config(), storage)

    [outcome] = list(pipeline.export(formats=["markdown"]))

    assert outcome.result is None
    assert outcome.error is not None
    assert "markdown" in outcome.error


# -- run -------------------------------------------------------------


def test_run_executes_every_phase(storage: SqliteStorage, tmp_path: Path) -> None:
    out = tmp_path / "digest.md"
    config = _make_config(
        sources={
            "fake": {
                "enabled": True,
                "postings": [_posting("https://e.test/1", "Backend Engineer")],
            }
        },
        exporters={"markdown": {"enabled": True, "output_path": str(out)}},
    )
    pipeline = Pipeline(config, storage)

    report = pipeline.run()

    assert report.ok
    assert report.fetch[0].stored == 1
    assert report.score is not None and report.score.scored == 1
    assert report.export[0].error is None
    assert out.exists()


def test_run_report_not_ok_when_only_source_fails(storage: SqliteStorage) -> None:
    config = _make_config(
        sources={"fake": {"enabled": True, "postings": [], "fail": True}},
    )
    pipeline = Pipeline(config, storage)

    report = pipeline.run(skip_export=True)

    assert report.fetch[0].failed is True
    assert report.ok is False


def test_posting_filters_select_respects_remote(storage: SqliteStorage) -> None:
    config = _make_config(
        sources={
            "fake": {
                "enabled": True,
                "postings": [
                    _posting("https://e.test/1", "Remote Dev", work_mode="remote"),
                    _posting("https://e.test/2", "Onsite Dev", work_mode="onsite"),
                ],
            }
        }
    )
    pipeline = Pipeline(config, storage)
    list(pipeline.fetch())

    remote_only = PostingFilters(remote=True).select(storage)

    assert [p.title for p in remote_only] == ["Remote Dev"]


# --------------------------------------------------------------------------
# The pipeline must hold the error policy sources/base.py declares
# --------------------------------------------------------------------------


class _ExplodingSource(Source):
    """A source that raises something other than a SourceError mid-stream.

    This is what selector drift actually looks like: bs4 hands back None and
    the next attribute access raises AttributeError. `Source.fetch` isolates
    `SourceError` only, so nothing below it catches this.
    """

    def fetch_raw(self) -> Iterator[dict[str, Any]]:
        yield {"url": "https://jobs.test/one", "title": "Backend Engineer"}
        raise AttributeError("NoneType object has no attribute get_text")

    def normalize(self, raw: dict[str, Any]) -> JobPosting:
        return JobPosting(
            url=raw["url"],
            title=raw["title"],
            company="Co",
            source=self.name,
            description_raw="python django",
            fetched_at=datetime.now(UTC),
        )


class _ImmediatelyExplodingSource(_ExplodingSource):
    """Breaks before yielding anything at all."""

    def fetch_raw(self) -> Iterator[dict[str, Any]]:
        raise AttributeError("NoneType object has no attribute select_one")
        yield {}  # pragma: no cover - unreachable, keeps this a generator


@pytest.fixture
def exploding_sources() -> Iterator[None]:
    register_source("exploding")(_ExplodingSource)
    register_source("exploding-at-once")(_ImmediatelyExplodingSource)
    try:
        yield
    finally:
        _REGISTRY.pop("exploding", None)
        _REGISTRY.pop("exploding-at-once", None)


def test_an_unexpected_exception_in_one_source_does_not_abort_the_others(
    storage: SqliteStorage, exploding_sources: None
) -> None:
    """The policy in sources/base.py is only real if the pipeline enforces it.

    `Source.fetch` catches SourceError. Anything else - a TypeError, a bs4
    AttributeError - walks straight out of the fetch phase and costs every
    source that had not run yet.
    """
    config = _make_config(
        sources={
            "exploding": {"enabled": True},
            "fake": {
                "enabled": True,
                "postings": [_posting("https://jobs.test/two", "Django Developer")],
            },
        }
    )

    results = list(Pipeline(config, storage).fetch())

    by_name = {result.source: result for result in results}
    assert set(by_name) == {"exploding", "fake"}
    # The healthy source still ran and stored its posting.
    assert by_name["fake"].stored == 1
    # The broken one is reported, not raised.
    assert by_name["exploding"].errors
    assert "AttributeError" in by_name["exploding"].errors[0]
    # It had produced one posting before breaking, so it is not a total loss.
    assert by_name["exploding"].failed is False
    assert storage.count() == 2


def test_a_source_that_explodes_before_producing_anything_is_failed(
    storage: SqliteStorage, exploding_sources: None
) -> None:
    config = _make_config(sources={"exploding-at-once": {"enabled": True}})

    results = list(Pipeline(config, storage).fetch())

    assert len(results) == 1
    assert results[0].failed is True
    assert results[0].kept == 0
    assert "AttributeError" in results[0].errors[0]


def test_fetch_limit_keeps_the_source_stats_readable(storage: SqliteStorage) -> None:
    """Stopping early must not leave the stats of the run half-written."""
    config = _make_config(
        sources={
            "fake": {
                "enabled": True,
                "postings": [
                    _posting(f"https://jobs.test/{index}", "Backend Engineer") for index in range(5)
                ],
            }
        }
    )

    results = list(Pipeline(config, storage).fetch(limit=2))

    assert results[0].kept == 2
    assert results[0].stored == 2
    assert results[0].failed is False
    assert storage.count() == 2


def test_score_does_not_count_a_posting_whose_write_was_lost(
    storage: SqliteStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`update_score` returning False means nothing was written.

    Reporting the posting as scored anyway makes the run summary disagree
    with the database, and hides that the row is still unscored.
    """
    config = _make_config(
        sources={
            "fake": {
                "enabled": True,
                "postings": [_posting("https://jobs.test/1", "Backend Engineer")],
            }
        }
    )
    pipeline = Pipeline(config, storage)
    list(pipeline.fetch())

    monkeypatch.setattr(storage, "update_score", lambda *args, **kwargs: False)
    rows = list(pipeline.score())

    assert len(rows) == 1
    assert rows[0].result is None
    assert rows[0].reason is not None
    assert "not written" in rows[0].reason


def test_run_is_not_ok_when_every_posting_fails_to_score(
    storage: SqliteStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`run` is sold as cron-friendly; a wholly failed phase must exit non-zero."""
    config = _make_config(
        sources={
            "fake": {
                "enabled": True,
                "postings": [_posting("https://jobs.test/1", "Backend Engineer")],
            }
        }
    )

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise ScoringError("scorer is down")

    monkeypatch.setattr(KeywordScorer, "score", _boom)
    report = Pipeline(config, storage).run()

    assert report.score is not None
    assert report.score.scored == 0
    assert report.score.failed == 1
    assert report.ok is False


def test_run_is_not_ok_when_one_source_among_several_fails(
    storage: SqliteStorage, exploding_sources: None
) -> None:
    """Working sources do not make a broken one a success."""
    config = _make_config(
        sources={
            "fake": {
                "enabled": True,
                "postings": [_posting("https://jobs.test/1", "Backend Engineer")],
            },
            "exploding-at-once": {"enabled": True},
        },
    )

    report = Pipeline(config, storage).run(skip_export=True)

    assert {result.source for result in report.fetch} == {"fake", "exploding-at-once"}
    assert any(result.failed for result in report.fetch)
    assert report.ok is False


def test_score_stops_cleanly_when_a_metered_scorer_runs_out_of_budget(
    storage: SqliteStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Budget exhaustion ends the phase without counting as a failure."""
    config = _make_config(
        sources={
            "fake": {
                "enabled": True,
                "postings": [
                    _posting(f"https://jobs.test/{index}", "Backend Engineer") for index in range(3)
                ],
            }
        }
    )
    pipeline = Pipeline(config, storage)
    list(pipeline.fetch())

    calls = {"n": 0}
    real_score = KeywordScorer.score

    def _budgeted(self: KeywordScorer, posting: Any, profile: Any) -> Any:
        calls["n"] += 1
        if calls["n"] > 1:
            raise ScorerBudgetExhaustedError("budget of 1 posting(s) per run is spent")
        return real_score(self, posting, profile)

    monkeypatch.setattr(KeywordScorer, "score", _budgeted)
    rows = list(pipeline.score())

    assert len(rows) == 2
    assert rows[0].result is not None
    assert rows[1].result is None
    assert rows[1].stopped_early is True
    # The ones it never reached are still unscored, ready for the next run.
    assert storage.count(scored=False) == 2

    monkeypatch.undo()
    report = Pipeline(config, storage).run(skip_fetch=True, skip_export=True)
    assert report.score is not None
    # A clean stop is not a failure.
    assert report.ok is True


# --------------------------------------------------------------------------
# `run` is the unattended command, so it is where a silent loss costs most
# --------------------------------------------------------------------------


def _break_one_row(storage: SqliteStorage, column: str = "work_mode", value: str = "nope") -> str:
    """Corrupt one stored row in place and return its url."""
    connection = storage._connection
    (url,) = connection.execute("SELECT url FROM postings ORDER BY url LIMIT 1").fetchone()
    connection.execute(f"UPDATE postings SET {column} = ? WHERE url = ?", (value, url))
    connection.commit()
    return str(url)


def _two_posting_config(tmp_path: Path) -> AppConfig:
    return _make_config(
        sources={
            "fake": {
                "enabled": True,
                "postings": [
                    _posting("https://jobs.test/1", "Backend Engineer"),
                    _posting("https://jobs.test/2", "Django Developer"),
                ],
            }
        },
        exporters={
            "json": {"enabled": True, "output_path": str(tmp_path / "out.json")},
        },
    )


def test_the_export_phase_counts_the_rows_it_could_not_read(
    storage: SqliteStorage, tmp_path: Path
) -> None:
    config = _two_posting_config(tmp_path)
    pipeline = Pipeline(config, storage)
    list(pipeline.fetch())
    _break_one_row(storage)

    outcomes = list(pipeline.export())

    assert len(outcomes) == 1
    assert outcomes[0].unreadable == 1
    assert outcomes[0].result is not None
    assert outcomes[0].result.exported == 1


def test_a_dry_run_export_counts_them_too(storage: SqliteStorage, tmp_path: Path) -> None:
    config = _two_posting_config(tmp_path)
    pipeline = Pipeline(config, storage, dry_run=True)
    list(Pipeline(config, storage).fetch())
    _break_one_row(storage)

    outcomes = list(pipeline.export())

    assert outcomes[0].would_export == 1
    assert outcomes[0].unreadable == 1


def test_an_export_that_reads_everything_reports_zero(
    storage: SqliteStorage, tmp_path: Path
) -> None:
    config = _two_posting_config(tmp_path)
    pipeline = Pipeline(config, storage)
    list(pipeline.fetch())

    outcomes = list(pipeline.export())

    assert outcomes[0].unreadable == 0


def test_run_carries_the_export_phase_count_into_its_report(
    storage: SqliteStorage, tmp_path: Path
) -> None:
    """Nobody watches a cron run; the report is the only thing that speaks."""
    config = _two_posting_config(tmp_path)
    list(Pipeline(config, storage).fetch())
    _break_one_row(storage)

    report = Pipeline(config, storage).run(skip_fetch=True)

    assert [outcome.unreadable for outcome in report.export] == [1]


def test_the_score_phase_counts_the_rows_it_could_not_read(
    storage: SqliteStorage, tmp_path: Path
) -> None:
    """`run --skip-export` is a real cron shape, and skips the export report."""
    config = _two_posting_config(tmp_path)
    list(Pipeline(config, storage).fetch())
    _break_one_row(storage)

    report = Pipeline(config, storage).run(skip_fetch=True, skip_export=True)

    assert report.score is not None
    assert report.score.scored == 1
    assert report.score.unreadable == 1


def test_the_score_phase_reports_zero_when_everything_reads(
    storage: SqliteStorage, tmp_path: Path
) -> None:
    config = _two_posting_config(tmp_path)
    list(Pipeline(config, storage).fetch())

    report = Pipeline(config, storage).run(skip_fetch=True, skip_export=True)

    assert report.score is not None
    assert report.score.unreadable == 0


def test_unreadable_rows_do_not_by_themselves_make_a_run_fail(
    storage: SqliteStorage, tmp_path: Path
) -> None:
    """A skipped row is reported, not treated as a phase failure.

    The readable postings were collected, scored and exported; failing the
    run would be as wrong as staying quiet about the skipped one.
    """
    config = _two_posting_config(tmp_path)
    list(Pipeline(config, storage).fetch())
    _break_one_row(storage)

    report = Pipeline(config, storage).run(skip_fetch=True)

    assert report.ok is True
    assert any(outcome.unreadable for outcome in report.export)


# -- the scorer exception contract ---------------------------------------


def test_score_reports_a_missing_api_key_as_a_pipeline_error(
    storage: SqliteStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The most expected LLM failure must not reach the caller as a traceback.

    The pipeline promises `PipelineError` when the backend cannot be built.
    An `except` clause that happens to cover the errors one scorer raises
    breaks as soon as another scorer raises something else, so the contract
    is what is asserted here, not the concrete class.
    """
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    config = _make_config(scoring={"backend": "llm"})

    with pytest.raises(PipelineError, match="OPENAI_API_KEY"):
        list(Pipeline(config, storage).score())


def test_score_reports_an_unknown_backend_as_a_pipeline_error(storage: SqliteStorage) -> None:
    config = _make_config(scoring={"backend": "nope"})

    with pytest.raises(PipelineError, match="nope"):
        list(Pipeline(config, storage).score())


def test_score_reports_an_invalid_backend_option_as_a_pipeline_error(
    storage: SqliteStorage,
) -> None:
    config = _make_config(scoring={"backend": "keyword_match", "synonynms": []})

    with pytest.raises(PipelineError, match="synonynms"):
        list(Pipeline(config, storage).score())


def test_a_scoring_error_leaves_the_posting_unscored_and_the_phase_running(
    storage: SqliteStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A scorer failing on one posting is a per-posting failure, not a crash."""
    config = _make_config(
        sources={
            "fake": {
                "enabled": True,
                "postings": [
                    _posting("https://jobs.test/1", "Backend Engineer"),
                    _posting("https://jobs.test/2", "Django Developer"),
                ],
            }
        }
    )
    pipeline = Pipeline(config, storage)
    list(pipeline.fetch())

    calls = {"n": 0}
    real_score = KeywordScorer.score

    def _flaky(self: KeywordScorer, posting: Any, profile: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise ScoringError("the model refused this posting")
        return real_score(self, posting, profile)

    monkeypatch.setattr(KeywordScorer, "score", _flaky)
    rows = list(pipeline.score())

    assert [row.result is None for row in rows] == [True, False]
    assert rows[0].reason == "the model refused this posting"
    assert storage.count(scored=False) == 1


# -- the shared HTTP policy is configuration, not a constructor secret ----


class _ProbeSource(Source):
    """Records the instances the pipeline builds, and fetches nothing."""

    built: ClassVar[list[_ProbeSource]] = []

    def __init__(self, name: str, config: Any, **kwargs: Any) -> None:
        super().__init__(name, config, **kwargs)
        type(self).built.append(self)

    def fetch_raw(self) -> Iterator[dict[str, Any]]:
        return iter(())

    def normalize(self, raw: dict[str, Any]) -> JobPosting:  # pragma: no cover - never reached
        raise AssertionError("the probe source yields no item")


@pytest.fixture
def probe_source() -> Iterator[list[_ProbeSource]]:
    _ProbeSource.built = []
    register_source("probe")(_ProbeSource)
    try:
        yield _ProbeSource.built
    finally:
        _REGISTRY.pop("probe", None)


def test_fetch_passes_the_configured_http_policy_to_the_source(
    storage: SqliteStorage, probe_source: list[_ProbeSource]
) -> None:
    """Rate limiting is unreachable if the pipeline never passes it on.

    The We Work Remotely source tells callers to set `min_request_interval`
    before enabling its one-request-per-posting fan-out. That advice is only
    real if the value can travel from config.yaml to the source.
    """
    config = _make_config(
        sources={
            "probe": {
                "enabled": True,
                "min_request_interval": 1.5,
                "max_retries": 7,
                "backoff_base": 0.25,
                "timeout": 3.5,
            }
        }
    )

    list(Pipeline(config, storage).fetch())

    assert len(probe_source) == 1
    source = probe_source[0]
    assert source._min_request_interval == 1.5
    assert source._max_retries == 7
    assert source._backoff_base == 0.25
    assert source._client.timeout.read == 3.5


def test_fetch_uses_the_shared_defaults_when_the_block_says_nothing(
    storage: SqliteStorage, probe_source: list[_ProbeSource]
) -> None:
    config = _make_config(sources={"probe": {"enabled": True}})

    list(Pipeline(config, storage).fetch())

    source = probe_source[0]
    assert source._min_request_interval == 0.0
    assert source._max_retries == DEFAULT_MAX_RETRIES
    assert source._backoff_base == DEFAULT_BACKOFF_BASE
    assert source._client.timeout.read == DEFAULT_TIMEOUT


def test_an_invalid_http_policy_value_fails_that_source_cleanly(
    storage: SqliteStorage, probe_source: list[_ProbeSource]
) -> None:
    """A bad value is a config error for one source, not a traceback."""
    config = _make_config(sources={"probe": {"enabled": True, "min_request_interval": -1}})

    results = list(Pipeline(config, storage).fetch())

    assert len(results) == 1
    assert results[0].failed is True
    assert "min_request_interval" in results[0].errors[0]
