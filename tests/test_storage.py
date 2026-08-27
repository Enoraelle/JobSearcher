"""Tests for the SQLite-backed job posting storage."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from jobsearcher.models import ApplicationStatus, JobPosting, WorkMode
from jobsearcher.storage.sqlite import _MIGRATIONS, CURRENT_SCHEMA_VERSION, SqliteStorage


def _make_posting(**overrides: Any) -> JobPosting:
    """Build a minimal valid `JobPosting`, overriding any field as needed."""
    fields: dict[str, Any] = {
        "url": "https://example.com/jobs/123",
        "title": "Backend Engineer",
        "company": "Acme",
        "source": "example",
        "description_raw": "Build things.",
        "fetched_at": datetime(2026, 1, 1, tzinfo=UTC),
    }
    fields.update(overrides)
    return JobPosting(**fields)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """A path to a SQLite database file inside pytest's temporary directory."""
    return tmp_path / "jobsearcher.db"


@pytest.fixture
def storage(db_path: Path) -> Iterator[SqliteStorage]:
    with SqliteStorage(db_path) as store:
        yield store


def test_save_many_inserts_and_reports_count(storage: SqliteStorage) -> None:
    postings = [
        _make_posting(url="https://example.com/jobs/1"),
        _make_posting(url="https://example.com/jobs/2"),
    ]

    inserted = storage.save_many(postings)

    assert inserted == 2
    assert storage.count() == 2


def test_get_by_url_roundtrips_the_model(storage: SqliteStorage) -> None:
    posting = _make_posting(
        url="https://example.com/jobs/roundtrip",
        location="Paris",
        work_mode=WorkMode.HYBRID,
        contract_type="full-time",
        salary_text="60-70k EUR",
        published_at=datetime(2025, 12, 1, tzinfo=UTC),
        eligible_locations=["France", "Remote - EU"],
        score=80,
        summary="Good match",
        matched_skills=["python"],
        unmatched_profile_skills=["rust"],
        missing_requirements=["kubernetes"],
        penalized_skills=["php"],
        location_match=True,
        work_mode_match=False,
        status=ApplicationStatus.SHORTLISTED,
        applied_at=datetime(2025, 12, 5, tzinfo=UTC),
    )

    storage.save_many([posting])
    fetched = storage.get_by_url(posting.url)

    assert fetched == posting


def test_get_by_url_normalizes_before_lookup(storage: SqliteStorage) -> None:
    posting = _make_posting(url="https://Example.com/jobs/123/?utm_source=x")
    storage.save_many([posting])

    fetched = storage.get_by_url("http://example.com/jobs/123")

    assert fetched is not None
    assert fetched.url == posting.url


def test_reinsertion_of_same_url_does_not_duplicate_or_overwrite_score(
    storage: SqliteStorage,
) -> None:
    original = _make_posting(url="https://example.com/jobs/dup", score=42, title="Original title")
    storage.save_many([original])
    storage.update_score(original.url, score=42)

    inserted_again = storage.save_many(
        [_make_posting(url="https://example.com/jobs/dup", score=None, title="Rescraped title")]
    )

    assert inserted_again == 0
    assert storage.count() == 1
    stored = storage.get_by_url(original.url)
    assert stored is not None
    assert stored.title == "Original title"
    assert stored.score == 42


def test_list_filters_by_source_status_and_score(storage: SqliteStorage) -> None:
    storage.save_many(
        [
            _make_posting(
                url="https://example.com/jobs/a",
                source="linkedin",
                status=ApplicationStatus.NEW,
                score=90,
            ),
            _make_posting(
                url="https://example.com/jobs/b",
                source="indeed",
                status=ApplicationStatus.APPLIED,
                score=50,
            ),
            _make_posting(
                url="https://example.com/jobs/c",
                source="linkedin",
                status=ApplicationStatus.APPLIED,
                score=None,
            ),
        ]
    )

    assert {p.url for p in storage.list(source="linkedin")} == {
        "https://example.com/jobs/a",
        "https://example.com/jobs/c",
    }
    assert {p.url for p in storage.list(status=ApplicationStatus.APPLIED)} == {
        "https://example.com/jobs/b",
        "https://example.com/jobs/c",
    }
    assert {p.url for p in storage.list(min_score=60)} == {"https://example.com/jobs/a"}
    assert {p.url for p in storage.list(max_score=60)} == {"https://example.com/jobs/b"}
    assert storage.count(source="linkedin") == 2
    assert storage.count(status=ApplicationStatus.APPLIED) == 2


def test_list_limit_and_offset(storage: SqliteStorage) -> None:
    storage.save_many([_make_posting(url=f"https://example.com/jobs/{i}") for i in range(5)])

    assert len(storage.list(limit=2)) == 2
    assert len(storage.list(limit=2, offset=4)) == 1


def test_update_score_changes_only_scoring_fields(storage: SqliteStorage) -> None:
    posting = _make_posting(url="https://example.com/jobs/score-me")
    storage.save_many([posting])

    updated = storage.update_score(
        posting.url,
        score=77,
        summary="Solid fit",
        matched_skills=["python", "sql"],
        unmatched_profile_skills=["go"],
        penalized_skills=["php"],
        location_match=True,
        work_mode_match=None,
    )

    assert updated is True
    stored = storage.get_by_url(posting.url)
    assert stored is not None
    assert stored.score == 77
    assert stored.summary == "Solid fit"
    assert stored.matched_skills == ["python", "sql"]
    assert stored.unmatched_profile_skills == ["go"]
    assert stored.missing_requirements == []
    assert stored.penalized_skills == ["php"]
    assert stored.location_match is True
    assert stored.work_mode_match is None
    assert stored.title == posting.title


def test_update_score_on_unknown_url_returns_false(storage: SqliteStorage) -> None:
    assert storage.update_score("https://example.com/jobs/missing", score=10) is False


def test_update_status_changes_status_and_applied_at(storage: SqliteStorage) -> None:
    posting = _make_posting(url="https://example.com/jobs/apply-me")
    storage.save_many([posting])
    applied_at = datetime(2026, 2, 1, tzinfo=UTC)

    updated = storage.update_status(posting.url, ApplicationStatus.APPLIED, applied_at=applied_at)

    assert updated is True
    stored = storage.get_by_url(posting.url)
    assert stored is not None
    assert stored.status == ApplicationStatus.APPLIED
    assert stored.applied_at == applied_at


def test_update_status_on_unknown_url_returns_false(storage: SqliteStorage) -> None:
    assert (
        storage.update_status("https://example.com/jobs/missing", ApplicationStatus.APPLIED)
        is False
    )


def test_fetched_at_roundtrips_as_aware_utc_not_naive(storage: SqliteStorage) -> None:
    # SQLite has no native datetime type, so a naive datetime stored back as a
    # naive one would still compare "equal" in a test that only checks the
    # displayed value. Assert tzinfo explicitly, not just `==`.
    fetched_at = datetime(2026, 3, 14, 9, 30, 15, tzinfo=UTC)
    posting = _make_posting(url="https://example.com/jobs/tz", fetched_at=fetched_at)

    storage.save_many([posting])
    stored = storage.get_by_url(posting.url)

    assert stored is not None
    assert stored.fetched_at.tzinfo is not None
    assert stored.fetched_at.utcoffset() == timedelta(0)
    assert stored.fetched_at.tzinfo is UTC
    assert stored.fetched_at == fetched_at


def test_list_fields_roundtrip_empty_single_and_special_characters(
    storage: SqliteStorage, db_path: Path
) -> None:
    scenarios: list[list[str]] = [
        [],
        ["Remote - EU"],
        ['C++, "senior" level, 5+ years'],
    ]

    for index, values in enumerate(scenarios):
        posting = _make_posting(
            url=f"https://example.com/jobs/list-{index}",
            eligible_locations=list(values),
            matched_skills=list(values),
            unmatched_profile_skills=list(values),
            missing_requirements=list(values),
            penalized_skills=list(values),
        )
        storage.save_many([posting])
        stored = storage.get_by_url(posting.url)

        assert stored is not None
        assert stored.eligible_locations == values
        assert stored.matched_skills == values
        assert stored.unmatched_profile_skills == values
        assert stored.missing_requirements == values
        assert stored.penalized_skills == values
        assert isinstance(stored.eligible_locations, list)
        assert isinstance(stored.matched_skills, list)
        assert isinstance(stored.unmatched_profile_skills, list)

    # An empty list must be stored as an empty JSON array, not SQL NULL (and a
    # non-empty list must not collapse into NULL either): check the raw column.
    connection = sqlite3.connect(db_path)
    try:
        row = connection.execute(
            "SELECT eligible_locations, matched_skills, unmatched_profile_skills, "
            "missing_requirements, penalized_skills FROM postings WHERE url = ?",
            ("https://example.com/jobs/list-0",),
        ).fetchone()
    finally:
        connection.close()
    assert row == ("[]", "[]", "[]", "[]", "[]")
    assert None not in row


def test_raw_url_is_stored_in_schema_and_preserved_verbatim(
    storage: SqliteStorage, db_path: Path
) -> None:
    raw_url = "https://EXAMPLE.com/jobs/456?utm_source=newsletter&ref=abc"
    posting = _make_posting(url="https://example.com/jobs/456", raw_url=raw_url)

    storage.save_many([posting])
    stored = storage.get_by_url(posting.url)

    assert stored is not None
    assert stored.raw_url == raw_url
    assert stored.raw_url != stored.url

    connection = sqlite3.connect(db_path)
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(postings)").fetchall()}
    finally:
        connection.close()
    assert "raw_url" in columns


def test_migration_from_v1_to_v2_preserves_data_and_adds_indexes(db_path: Path) -> None:
    # Build a v1-only database by hand: schema statements for version 1 only,
    # with the meta table manually pinned at version 1.
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        for statement in _MIGRATIONS[1]:
            connection.execute(statement)
        connection.execute("INSERT INTO meta (key, value) VALUES ('schema_version', '1')")
        connection.execute(
            f"INSERT INTO postings ({', '.join(_v1_columns())}) "
            f"VALUES ({', '.join('?' for _ in _v1_columns())})",
            _v1_row(),
        )
        connection.commit()
    finally:
        connection.close()

    # No index should exist yet at v1.
    pre_check = sqlite3.connect(db_path)
    try:
        index_names = {
            row[0]
            for row in pre_check.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            ).fetchall()
        }
    finally:
        pre_check.close()
    assert "idx_postings_source" not in index_names

    with SqliteStorage(db_path) as storage:
        stored = storage.get_by_url("https://example.com/jobs/legacy")
        assert stored is not None
        assert stored.title == "Legacy posting"

        post_check = sqlite3.connect(db_path)
        try:
            version_row = post_check.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()
            index_names = {
                row[0]
                for row in post_check.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'index'"
                ).fetchall()
            }
        finally:
            post_check.close()

    assert version_row is not None
    assert int(version_row[0]) == CURRENT_SCHEMA_VERSION
    assert {"idx_postings_source", "idx_postings_score", "idx_postings_status"} <= index_names


def test_migration_v3_renames_missing_skills_and_adds_scoring_columns(db_path: Path) -> None:
    # Stand up a v1 database with one row, then let SqliteStorage migrate it.
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        for statement in _MIGRATIONS[1]:
            connection.execute(statement)
        connection.execute("INSERT INTO meta (key, value) VALUES ('schema_version', '1')")
        row = list(_v1_row())
        row[_v1_columns().index("missing_skills")] = '["python"]'
        connection.execute(
            f"INSERT INTO postings ({', '.join(_v1_columns())}) "
            f"VALUES ({', '.join('?' for _ in _v1_columns())})",
            row,
        )
        connection.commit()
    finally:
        connection.close()

    with SqliteStorage(db_path) as storage:
        stored = storage.get_by_url("https://example.com/jobs/legacy")
        assert stored is not None
        # The old missing_skills column carried "profile skills the posting
        # doesn't mention", so its data survives the rename verbatim.
        assert stored.unmatched_profile_skills == ["python"]
        # New lists default to empty, new flags to "unknown".
        assert stored.missing_requirements == []
        assert stored.penalized_skills == []
        assert stored.location_match is None
        assert stored.work_mode_match is None

    check = sqlite3.connect(db_path)
    try:
        columns = {row[1] for row in check.execute("PRAGMA table_info(postings)").fetchall()}
    finally:
        check.close()
    assert "missing_skills" not in columns
    assert {
        "unmatched_profile_skills",
        "missing_requirements",
        "penalized_skills",
        "location_match",
        "work_mode_match",
    } <= columns


def _v1_columns() -> tuple[str, ...]:
    return (
        "url",
        "raw_url",
        "title",
        "company",
        "source",
        "description_raw",
        "description_clean",
        "location",
        "work_mode",
        "contract_type",
        "salary_text",
        "published_at",
        "fetched_at",
        "eligible_locations",
        "score",
        "summary",
        "matched_skills",
        "missing_skills",
        "status",
        "applied_at",
    )


def _v1_row() -> tuple[Any, ...]:
    return (
        "https://example.com/jobs/legacy",
        "https://example.com/jobs/legacy",
        "Legacy posting",
        "Acme",
        "example",
        "Pre-migration data.",
        None,
        None,
        "unknown",
        None,
        None,
        None,
        "2025-01-01T00:00:00+00:00",
        "[]",
        None,
        None,
        "[]",
        "[]",
        "new",
        None,
    )
