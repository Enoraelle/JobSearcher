"""Tests for the SQLite-backed job posting storage."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from jobsearcher.config import BackendSectionConfig
from jobsearcher.models import ApplicationStatus, JobPosting, WorkMode, posting_id
from jobsearcher.storage import StorageConfigError, open_storage
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


def test_id_is_populated_on_insert_and_matches_posting_id(
    storage: SqliteStorage, db_path: Path
) -> None:
    posting = _make_posting(url="https://example.com/jobs/ident")
    storage.save_many([posting])

    connection = sqlite3.connect(db_path)
    try:
        row = connection.execute("SELECT id FROM postings WHERE url = ?", (posting.url,)).fetchone()
    finally:
        connection.close()
    assert row[0] == posting_id(posting.url) == posting.id
    assert len(row[0]) == 12


def test_find_by_id_prefix_resolves_git_style(storage: SqliteStorage) -> None:
    postings = [_make_posting(url=f"https://example.com/jobs/{i}") for i in range(5)]
    storage.save_many(postings)

    target = storage.list()[0]
    matches = storage.find_by_id_prefix(target.id[:6])

    assert [p.url for p in matches] == [target.url]
    assert storage.find_by_id_prefix("zzzz") == []


def test_list_scored_filter(storage: SqliteStorage) -> None:
    storage.save_many(
        [
            _make_posting(url="https://example.com/jobs/scored", score=80),
            _make_posting(url="https://example.com/jobs/unscored", score=None),
        ]
    )

    assert {p.url for p in storage.list(scored=True)} == {"https://example.com/jobs/scored"}
    assert {p.url for p in storage.list(scored=False)} == {"https://example.com/jobs/unscored"}
    assert storage.count(scored=False) == 1


def test_list_language_filter_keeps_undetermined_postings(storage: SqliteStorage) -> None:
    storage.save_many(
        [
            _make_posting(url="https://example.com/jobs/fr", detected_language="fr"),
            _make_posting(url="https://example.com/jobs/en", detected_language="en"),
            _make_posting(url="https://example.com/jobs/unknown", detected_language=None),
        ]
    )

    urls = {p.url for p in storage.list(languages=["fr"])}

    # The French posting matches; the undetermined one is never hidden; the
    # English one is filtered out.
    assert urls == {"https://example.com/jobs/fr", "https://example.com/jobs/unknown"}


def test_migration_v1_to_v4_backfills_ids(db_path: Path) -> None:

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

    with SqliteStorage(db_path) as storage:
        stored = storage.get_by_url("https://example.com/jobs/legacy")
        assert stored is not None
        assert stored.detected_language is None

    check = sqlite3.connect(db_path)
    try:
        row = check.execute(
            "SELECT id FROM postings WHERE url = ?", ("https://example.com/jobs/legacy",)
        ).fetchone()
    finally:
        check.close()
    assert row[0] == posting_id("https://example.com/jobs/legacy")


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


# --------------------------------------------------------------------------
# The three ways the database can become unusable
# --------------------------------------------------------------------------


def test_opening_a_path_in_a_missing_directory_is_a_config_error(tmp_path: Path) -> None:
    """`storage.path: ./data/jobs.db` with no ./data must not be a traceback.

    sqlite3 raises OperationalError here; every command opens storage, so an
    unhandled one takes the whole CLI down on an ordinary config.
    """
    config = BackendSectionConfig(backend="sqlite", path=str(tmp_path / "missing" / "jobs.db"))

    with pytest.raises(StorageConfigError) as excinfo:
        open_storage(config)

    message = str(excinfo.value)
    assert "storage.path" in message
    assert "missing" in message


def test_opening_a_file_that_is_not_a_database_is_a_config_error(tmp_path: Path) -> None:
    not_a_db = tmp_path / "jobs.db"
    not_a_db.write_bytes(b"this is definitely not a SQLite file" * 64)
    config = BackendSectionConfig(backend="sqlite", path=str(not_a_db))

    with pytest.raises(StorageConfigError):
        open_storage(config)


def test_an_interrupted_migration_leaves_the_database_openable(db_path: Path) -> None:
    """A migration that dies halfway must roll back, not half-apply.

    Without a transaction per version, sqlite3 auto-commits each DDL
    statement while `schema_version` stays behind, so the next open replays
    the batch against a schema that has already moved and fails forever.
    """
    with SqliteStorage(db_path) as storage:
        storage.save_many([_make_posting(url="https://example.com/jobs/keep-me")])

    doomed = (
        "ALTER TABLE postings ADD COLUMN experiment TEXT",
        "ALTER TABLE postings ADD COLUMN experiment TEXT",  # duplicate: fails
    )
    original = dict(_MIGRATIONS)
    _MIGRATIONS[CURRENT_SCHEMA_VERSION + 1] = doomed
    try:
        with pytest.raises(sqlite3.OperationalError):
            SqliteStorage(db_path)
    finally:
        _MIGRATIONS.clear()
        _MIGRATIONS.update(original)

    # The failed batch left nothing behind, so the store still opens.
    with SqliteStorage(db_path) as storage:
        stored = storage.get_by_url("https://example.com/jobs/keep-me")
        assert stored is not None

    connection = sqlite3.connect(db_path)
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(postings)")}
        version = connection.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
    finally:
        connection.close()

    assert "experiment" not in columns
    assert int(version[0]) == CURRENT_SCHEMA_VERSION


def test_a_legacy_row_with_an_unusable_url_does_not_block_opening(db_path: Path) -> None:
    """The id backfill must skip a row it cannot hash, not refuse to open.

    `posting_id` normalizes the URL first and raises on a relative one. A
    single such legacy row used to make every command fail with no way to
    reach the database and repair it.
    """
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        for statement in _MIGRATIONS[1]:
            connection.execute(statement)
        connection.execute("INSERT INTO meta (key, value) VALUES ('schema_version', '1')")
        for url in ("/relative/path", "https://example.com/jobs/good"):
            row = list(_v1_row())
            row[_v1_columns().index("url")] = url
            row[_v1_columns().index("raw_url")] = url
            connection.execute(
                f"INSERT INTO postings ({', '.join(_v1_columns())}) "
                f"VALUES ({', '.join('?' for _ in _v1_columns())})",
                tuple(row),
            )
        connection.commit()
    finally:
        connection.close()

    with SqliteStorage(db_path) as storage:
        good = storage.get_by_url("https://example.com/jobs/good")
        assert good is not None
        assert good.id == posting_id("https://example.com/jobs/good")

    # The unusable row keeps a NULL id rather than blocking the backfill.
    check = sqlite3.connect(db_path)
    try:
        ids = dict(check.execute("SELECT url, id FROM postings").fetchall())
    finally:
        check.close()
    assert ids["/relative/path"] is None
    assert ids["https://example.com/jobs/good"] is not None


# --------------------------------------------------------------------------
# Paging, and what idempotent insertion actually preserves
# --------------------------------------------------------------------------


def test_offset_without_limit_still_skips(storage: SqliteStorage) -> None:
    """`offset` is a public parameter; ignoring it silently is worse than failing."""
    storage.save_many([_make_posting(url=f"https://example.com/jobs/{i}") for i in range(5)])

    assert len(storage.list(offset=3)) == 2
    assert len(storage.list(offset=5)) == 0
    assert len(storage.list(offset=99)) == 0
    assert len(storage.list()) == 5


def test_offset_without_limit_pages_consistently(storage: SqliteStorage) -> None:
    storage.save_many(
        [
            _make_posting(
                url=f"https://example.com/jobs/{i}",
                fetched_at=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(hours=i),
            )
            for i in range(5)
        ]
    )

    everything = [posting.url for posting in storage.list()]
    tail = [posting.url for posting in storage.list(offset=2)]

    assert tail == everything[2:]


def test_the_same_job_under_two_tracking_urls_is_stored_once(storage: SqliteStorage) -> None:
    """Deduplication is by *normalized* URL, which is the point of normalizing.

    Two sources linking the same posting with different campaign parameters
    must not produce two rows the user has to read twice.
    """
    inserted_first = storage.save_many(
        [_make_posting(url="https://example.com/jobs/42?utm_source=newsletter")]
    )
    inserted_second = storage.save_many(
        [_make_posting(url="https://example.com/jobs/42?utm_source=twitter&gclid=abc")]
    )

    assert inserted_first == 1
    assert inserted_second == 0
    assert storage.count() == 1
    assert storage.get_by_url("https://example.com/jobs/42") is not None


def test_reinsertion_preserves_application_tracking_and_score_detail(
    storage: SqliteStorage,
) -> None:
    """A rescrape must not roll back where a posting stands in the pipeline.

    `save_many` protects the score; status, applied_at and the score's
    supporting detail are just as much the user's own work, and a source
    that re-emits the posting knows nothing about any of them.
    """
    url = "https://example.com/jobs/tracked"
    storage.save_many([_make_posting(url=url, title="Original title")])
    storage.update_score(
        url,
        score=77,
        summary="matched 2/2 skills",
        matched_skills=["python", "django"],
        unmatched_profile_skills=["celery"],
        missing_requirements=["kubernetes"],
        penalized_skills=["php"],
        location_match=True,
        work_mode_match=False,
    )
    applied_at = datetime(2026, 3, 4, tzinfo=UTC)
    storage.update_status(url, ApplicationStatus.APPLIED, applied_at=applied_at)

    reinserted = storage.save_many(
        [
            _make_posting(
                url=url,
                title="Rescraped title",
                status=ApplicationStatus.NEW,
                score=None,
            )
        ]
    )

    assert reinserted == 0
    stored = storage.get_by_url(url)
    assert stored is not None
    assert stored.title == "Original title"
    assert stored.score == 77
    assert stored.summary == "matched 2/2 skills"
    assert stored.matched_skills == ["python", "django"]
    assert stored.unmatched_profile_skills == ["celery"]
    assert stored.missing_requirements == ["kubernetes"]
    assert stored.penalized_skills == ["php"]
    assert stored.location_match is True
    assert stored.work_mode_match is False
    assert stored.status is ApplicationStatus.APPLIED
    assert stored.applied_at == applied_at


def test_save_many_counts_only_the_new_rows_in_a_mixed_batch(storage: SqliteStorage) -> None:
    """The count drives the CLI's "new" and "dupes" columns."""
    storage.save_many([_make_posting(url="https://example.com/jobs/known")])

    inserted = storage.save_many(
        [
            _make_posting(url="https://example.com/jobs/new-1"),
            _make_posting(url="https://example.com/jobs/known"),
            _make_posting(url="https://example.com/jobs/new-2"),
        ]
    )

    assert inserted == 2
    assert storage.count() == 3


def test_a_batch_containing_the_same_url_twice_inserts_it_once(storage: SqliteStorage) -> None:
    inserted = storage.save_many(
        [
            _make_posting(url="https://example.com/jobs/same"),
            _make_posting(url="https://example.com/jobs/same?utm_medium=email"),
        ]
    )

    assert inserted == 1
    assert storage.count() == 1


def test_reopening_an_already_migrated_database_is_a_no_op(db_path: Path) -> None:
    """Opening is on the path of every command; it must be idempotent."""
    with SqliteStorage(db_path) as storage:
        storage.save_many([_make_posting(url="https://example.com/jobs/persisted")])
        first_id = storage.get_by_url("https://example.com/jobs/persisted")

    for _ in range(3):
        with SqliteStorage(db_path) as storage:
            again = storage.get_by_url("https://example.com/jobs/persisted")
            assert again == first_id
            assert storage.count() == 1

    connection = sqlite3.connect(db_path)
    try:
        versions = connection.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchall()
    finally:
        connection.close()

    assert len(versions) == 1
    assert int(versions[0][0]) == CURRENT_SCHEMA_VERSION


# --------------------------------------------------------------------------
# An unreadable stored row must cost that row, and nothing else
# --------------------------------------------------------------------------
#
# A row can stop decoding for reasons no command can fix from the outside: an
# enum value written by a future version, a URL some earlier version stored
# relative, hand-editing. Letting it raise takes down `list` - the command the
# whole tool is used through. Dropping it quietly is worse still, so every
# read counts what it had to skip.


def _corrupt_column(db_path: Path, url: str, column: str, value: Any) -> None:
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(f"UPDATE postings SET {column} = ? WHERE url = ?", (value, url))
        connection.commit()
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("url", "/relative/path"),  # fails the model's URL validator
        ("work_mode", "teleportation"),  # not a WorkMode
        ("status", "ghosted"),  # not an ApplicationStatus
        ("fetched_at", "not a date"),
        ("published_at", "the third of never"),
        ("eligible_locations", "{not json"),
        ("matched_skills", "{not json"),
        ("title", ""),  # blank identity field
    ],
)
def test_an_unreadable_row_is_skipped_rather_than_raising(
    db_path: Path, column: str, value: Any
) -> None:
    with SqliteStorage(db_path) as storage:
        storage.save_many(
            [
                _make_posting(url="https://example.com/jobs/broken", title="Broken"),
                _make_posting(url="https://example.com/jobs/fine", title="Fine"),
            ]
        )
    _corrupt_column(db_path, "https://example.com/jobs/broken", column, value)

    with SqliteStorage(db_path) as storage:
        postings = storage.list()

        assert [posting.title for posting in postings] == ["Fine"]
        assert storage.unreadable_rows == 1


def test_the_number_of_unreadable_rows_is_reported_per_read(db_path: Path) -> None:
    with SqliteStorage(db_path) as storage:
        storage.save_many(
            [_make_posting(url=f"https://example.com/jobs/{index}") for index in range(4)]
        )
    for index in (0, 1, 2):
        _corrupt_column(db_path, f"https://example.com/jobs/{index}", "work_mode", "nope")

    with SqliteStorage(db_path) as storage:
        assert len(storage.list()) == 1
        assert storage.unreadable_rows == 3

        # The counter describes the latest read, not the lifetime of the handle.
        assert len(storage.list(source="nobody")) == 0
        assert storage.unreadable_rows == 0

        assert len(storage.list()) == 1
        assert storage.unreadable_rows == 3


def test_an_unreadable_row_is_logged_with_its_id_and_url(
    db_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The log is the only place the lost row can be identified from."""
    url = "https://example.com/jobs/broken"
    with SqliteStorage(db_path) as storage:
        storage.save_many([_make_posting(url=url)])
    expected_id = posting_id(url)
    _corrupt_column(db_path, url, "work_mode", "teleportation")

    with caplog.at_level(logging.WARNING), SqliteStorage(db_path) as storage:
        storage.list()

    assert len(caplog.records) == 1
    message = caplog.records[0].getMessage()
    assert expected_id in message
    assert url in message
    assert "teleportation" in message


def test_get_by_url_on_an_unreadable_row_reports_rather_than_raising(db_path: Path) -> None:
    url = "https://example.com/jobs/broken"
    with SqliteStorage(db_path) as storage:
        storage.save_many([_make_posting(url=url)])
    _corrupt_column(db_path, url, "status", "ghosted")

    with SqliteStorage(db_path) as storage:
        assert storage.get_by_url(url) is None
        assert storage.unreadable_rows == 1


def test_find_by_id_prefix_on_an_unreadable_row_reports_rather_than_raising(
    db_path: Path,
) -> None:
    url = "https://example.com/jobs/broken"
    with SqliteStorage(db_path) as storage:
        storage.save_many([_make_posting(url=url)])
    prefix = posting_id(url)[:8]
    _corrupt_column(db_path, url, "fetched_at", "not a date")

    with SqliteStorage(db_path) as storage:
        assert storage.find_by_id_prefix(prefix) == []
        assert storage.unreadable_rows == 1


def test_count_still_sees_a_row_that_cannot_be_decoded(db_path: Path) -> None:
    """`count` is pure SQL: it counts what is stored, not what is readable.

    The gap between `count` and `len(list())` is exactly what the reported
    number of unreadable rows explains.
    """
    with SqliteStorage(db_path) as storage:
        storage.save_many(
            [
                _make_posting(url="https://example.com/jobs/broken"),
                _make_posting(url="https://example.com/jobs/fine"),
            ]
        )
    _corrupt_column(db_path, "https://example.com/jobs/broken", "work_mode", "nope")

    with SqliteStorage(db_path) as storage:
        assert storage.count() == 2
        assert len(storage.list()) == 1
        assert storage.unreadable_rows == 1


def test_a_legacy_row_with_an_unusable_url_is_skipped_by_list_too(db_path: Path) -> None:
    """The other half of the backfill fix: opening works, and so does reading.

    A row the id backfill had to skip is exactly a row `list` cannot decode.
    Fixing only the open would move the crash to the most-used command.
    """
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        for statement in _MIGRATIONS[1]:
            connection.execute(statement)
        connection.execute("INSERT INTO meta (key, value) VALUES ('schema_version', '1')")
        for url in ("/relative/path", "https://example.com/jobs/good"):
            row = list(_v1_row())
            row[_v1_columns().index("url")] = url
            row[_v1_columns().index("raw_url")] = url
            connection.execute(
                f"INSERT INTO postings ({', '.join(_v1_columns())}) "
                f"VALUES ({', '.join('?' for _ in _v1_columns())})",
                tuple(row),
            )
        connection.commit()
    finally:
        connection.close()

    with SqliteStorage(db_path) as storage:
        postings = storage.list()

        assert [posting.url for posting in postings] == ["https://example.com/jobs/good"]
        assert storage.unreadable_rows == 1
        assert storage.count() == 2
