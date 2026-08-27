"""Tests for the fetch / score / export pipeline, using in-memory fakes.

No network I/O: the fake sources below yield postings straight from their
configuration block (see CLAUDE.md's "tests never touch the network").
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from jobsearcher.config import AppConfig
from jobsearcher.pipeline import Pipeline, PipelineError, PostingFilters
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
