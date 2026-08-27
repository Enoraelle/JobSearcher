"""SQLite implementation of the :class:`~jobsearcher.storage.base.Storage` protocol."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import Any

from jobsearcher.models import (
    ApplicationStatus,
    JobPosting,
    WorkMode,
    normalize_job_url,
    posting_id,
)

_COLUMNS: tuple[str, ...] = (
    "url",
    "id",
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
    "detected_language",
    "eligible_locations",
    "score",
    "summary",
    "matched_skills",
    "unmatched_profile_skills",
    "missing_requirements",
    "penalized_skills",
    "location_match",
    "work_mode_match",
    "status",
    "applied_at",
)

_INSERT_SQL = (
    f"INSERT OR IGNORE INTO postings ({', '.join(_COLUMNS)}) "
    f"VALUES ({', '.join('?' for _ in _COLUMNS)})"
)

# Migrations are numbered SQL batches applied in order, starting right after
# whatever version is currently recorded in the `meta` table. There is no
# migration framework here on purpose: each entry is a short, reviewable list
# of statements, and the schema can still evolve without the user losing
# their existing database.
_MIGRATIONS: dict[int, tuple[str, ...]] = {
    1: (
        """
        CREATE TABLE IF NOT EXISTS postings (
            url                 TEXT PRIMARY KEY,
            raw_url             TEXT NOT NULL,
            title               TEXT NOT NULL,
            company             TEXT NOT NULL,
            source              TEXT NOT NULL,
            description_raw     TEXT NOT NULL,
            description_clean   TEXT,
            location            TEXT,
            work_mode           TEXT NOT NULL,
            contract_type       TEXT,
            salary_text         TEXT,
            published_at        TEXT,
            fetched_at          TEXT NOT NULL,
            eligible_locations  TEXT NOT NULL,
            score               INTEGER,
            summary             TEXT,
            matched_skills      TEXT NOT NULL,
            missing_skills      TEXT NOT NULL,
            status              TEXT NOT NULL,
            applied_at          TEXT
        )
        """,
    ),
    2: (
        "CREATE INDEX IF NOT EXISTS idx_postings_source ON postings(source)",
        "CREATE INDEX IF NOT EXISTS idx_postings_score ON postings(score)",
        "CREATE INDEX IF NOT EXISTS idx_postings_status ON postings(status)",
    ),
    # Scoring split into three separate skill lists plus two eligibility
    # flags (see jobsearcher.models.ScoreResult). The old `missing_skills`
    # column already held exactly "profile skills the posting doesn't
    # mention", so it is renamed in place rather than dropped and refilled.
    # `location_match` / `work_mode_match` are nullable: NULL == "not known"
    # (the posting says nothing, or the profile has no constraint), which is
    # distinct from 0 == "does not match".
    3: (
        "ALTER TABLE postings RENAME COLUMN missing_skills TO unmatched_profile_skills",
        "ALTER TABLE postings ADD COLUMN missing_requirements TEXT NOT NULL DEFAULT '[]'",
        "ALTER TABLE postings ADD COLUMN penalized_skills TEXT NOT NULL DEFAULT '[]'",
        "ALTER TABLE postings ADD COLUMN location_match INTEGER",
        "ALTER TABLE postings ADD COLUMN work_mode_match INTEGER",
    ),
    # A short, git-style identifier per posting (12 hex chars of
    # sha256(url); see jobsearcher.models.posting_id) and a best-effort
    # detected language. Both columns are nullable here: the id is
    # backfilled for existing rows right after migration by
    # `_backfill_posting_ids` (SQLite has no sha256 to do it in pure SQL),
    # and a legacy row's language stays NULL, which is the honest "not
    # known". The UNIQUE index tolerates the transient NULLs before the
    # backfill runs (SQLite allows multiple NULLs in a UNIQUE index) and
    # then enforces uniqueness and powers prefix lookups.
    4: (
        "ALTER TABLE postings ADD COLUMN id TEXT",
        "ALTER TABLE postings ADD COLUMN detected_language TEXT",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_postings_id ON postings(id)",
    ),
}

CURRENT_SCHEMA_VERSION = max(_MIGRATIONS)


def _schema_version(connection: sqlite3.Connection) -> int:
    """Return the schema version recorded in `meta`, or 0 if unset."""
    table_exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'meta'"
    ).fetchone()
    if table_exists is None:
        return 0
    row = connection.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    return int(row[0]) if row is not None else 0


def _migrate(connection: sqlite3.Connection) -> None:
    """Bring the database schema up to `CURRENT_SCHEMA_VERSION` in place."""
    connection.execute(
        "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    current = _schema_version(connection)
    for version in sorted(v for v in _MIGRATIONS if v > current):
        for statement in _MIGRATIONS[version]:
            connection.execute(statement)
        connection.execute(
            "INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(version),),
        )
    connection.commit()


def _bool_to_db(value: bool | None) -> int | None:
    """Encode an optional bool as SQLite's 1 / 0 / NULL."""
    return None if value is None else int(value)


def _db_to_bool(value: Any) -> bool | None:
    """Decode SQLite's 1 / 0 / NULL back into an optional bool."""
    return None if value is None else bool(value)


def _load_list(value: Any) -> list[str]:
    """Decode a JSON string list, tolerating the NULL left by an old row."""
    return list(json.loads(value)) if value else []


def _filter_clauses(
    *,
    source: str | None,
    status: ApplicationStatus | None,
    min_score: int | None,
    max_score: int | None,
    scored: bool | None,
    languages: Sequence[str] | None,
) -> tuple[list[str], list[Any]]:
    """Build the WHERE clauses and bind parameters shared by `list` and `count`."""
    clauses: list[str] = []
    params: list[Any] = []
    if source is not None:
        clauses.append("source = ?")
        params.append(source)
    if status is not None:
        clauses.append("status = ?")
        params.append(status.value)
    if min_score is not None:
        clauses.append("score >= ?")
        params.append(min_score)
    if max_score is not None:
        clauses.append("score <= ?")
        params.append(max_score)
    if scored is True:
        clauses.append("score IS NOT NULL")
    elif scored is False:
        clauses.append("score IS NULL")
    if languages:
        placeholders = ", ".join("?" for _ in languages)
        # A NULL (undetermined) language is never filtered out — see the
        # module docstring in jobsearcher.language.
        clauses.append(f"(detected_language IS NULL OR detected_language IN ({placeholders}))")
        params.extend(languages)
    return clauses, params


def _to_row(posting: JobPosting) -> tuple[Any, ...]:
    """Convert a `JobPosting` into a tuple matching `_COLUMNS` order."""
    return (
        posting.url,
        posting.id,
        posting.raw_url,
        posting.title,
        posting.company,
        posting.source,
        posting.description_raw,
        posting.description_clean,
        posting.location,
        posting.work_mode.value,
        posting.contract_type,
        posting.salary_text,
        posting.published_at.isoformat() if posting.published_at else None,
        posting.fetched_at.isoformat(),
        posting.detected_language,
        json.dumps(posting.eligible_locations),
        posting.score,
        posting.summary,
        json.dumps(posting.matched_skills),
        json.dumps(posting.unmatched_profile_skills),
        json.dumps(posting.missing_requirements),
        json.dumps(posting.penalized_skills),
        _bool_to_db(posting.location_match),
        _bool_to_db(posting.work_mode_match),
        posting.status.value,
        posting.applied_at.isoformat() if posting.applied_at else None,
    )


def _from_row(row: sqlite3.Row) -> JobPosting:
    """Convert a `postings` row back into a `JobPosting`."""
    return JobPosting(
        url=row["url"],
        raw_url=row["raw_url"],
        title=row["title"],
        company=row["company"],
        source=row["source"],
        description_raw=row["description_raw"],
        description_clean=row["description_clean"],
        location=row["location"],
        work_mode=WorkMode(row["work_mode"]),
        contract_type=row["contract_type"],
        salary_text=row["salary_text"],
        published_at=datetime.fromisoformat(row["published_at"]) if row["published_at"] else None,
        fetched_at=datetime.fromisoformat(row["fetched_at"]),
        detected_language=row["detected_language"],
        eligible_locations=json.loads(row["eligible_locations"]),
        score=row["score"],
        summary=row["summary"],
        matched_skills=json.loads(row["matched_skills"]),
        unmatched_profile_skills=_load_list(row["unmatched_profile_skills"]),
        missing_requirements=_load_list(row["missing_requirements"]),
        penalized_skills=_load_list(row["penalized_skills"]),
        location_match=_db_to_bool(row["location_match"]),
        work_mode_match=_db_to_bool(row["work_mode_match"]),
        status=ApplicationStatus(row["status"]),
        applied_at=datetime.fromisoformat(row["applied_at"]) if row["applied_at"] else None,
    )


class SqliteStorage:
    """SQLite-backed implementation of the `Storage` protocol.

    Each instance owns a single connection. Use it as a context manager to
    ensure the connection is closed:

        with SqliteStorage(db_path) as storage:
            storage.save_many(postings)
    """

    def __init__(self, database: str | Path, *, timeout: float = 5.0) -> None:
        """Open (creating and migrating if needed) a SQLite-backed store.

        Args:
            database: Path to the SQLite database file, or ``":memory:"``.
            timeout: Seconds to wait for the database lock before failing.
        """
        self._connection = sqlite3.connect(str(database), timeout=timeout)
        self._connection.row_factory = sqlite3.Row
        _migrate(self._connection)
        self._backfill_posting_ids()

    def _backfill_posting_ids(self) -> None:
        """Fill the `id` column for rows that predate schema version 4.

        SQLite has no SHA-256, so the identifier cannot be computed in the
        migration's SQL. This runs once per legacy row (``WHERE id IS
        NULL``) and no-ops thereafter, so opening an already-migrated
        database costs a single cheap query.
        """
        rows = self._connection.execute("SELECT url FROM postings WHERE id IS NULL").fetchall()
        if not rows:
            return
        with self._connection:
            self._connection.executemany(
                "UPDATE postings SET id = ? WHERE url = ?",
                [(posting_id(row["url"]), row["url"]) for row in rows],
            )

    def __enter__(self) -> SqliteStorage:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        """Close the underlying connection."""
        self._connection.close()

    def save_many(self, postings: Sequence[JobPosting]) -> int:
        """Persist postings, skipping any whose URL is already stored.

        Design decision: this is a plain ``INSERT OR IGNORE`` keyed on the
        (normalized) URL. A posting that is already in the database is left
        completely untouched by a later call — not just its score, but also
        its title, description, and every other field — even if the source
        has since changed them. This is intentional for v1: the point of
        idempotent insertion is that a score, once assigned, must never be
        silently clobbered by a rescrape, and there is no single-statement
        way to express "update everything except score/status" without a
        separate, explicit refresh path. Whether and how to refresh
        non-scoring content on an already-seen posting is an open question
        for a later version; see "Open questions" in CLAUDE.md.

        Args:
            postings: The postings to persist.

        Returns:
            The number of postings that were newly inserted.
        """
        inserted = 0
        with self._connection:
            for posting in postings:
                cursor = self._connection.execute(_INSERT_SQL, _to_row(posting))
                if cursor.rowcount > 0:
                    inserted += 1
        return inserted

    def get_by_url(self, url: str) -> JobPosting | None:
        row = self._connection.execute(
            "SELECT * FROM postings WHERE url = ?", (normalize_job_url(url),)
        ).fetchone()
        return _from_row(row) if row is not None else None

    def find_by_id_prefix(self, prefix: str) -> list[JobPosting]:
        """Return every posting whose identifier starts with ``prefix``.

        The caller (the CLI) is responsible for validating that ``prefix``
        is a plausible identifier fragment; this method treats it literally.

        Args:
            prefix: The leading hex characters of a posting identifier.

        Returns:
            All matching postings, most recently fetched first. Empty if
            none match.
        """
        rows = self._connection.execute(
            "SELECT * FROM postings WHERE id LIKE ? ESCAPE '\\' ORDER BY fetched_at DESC",
            (prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%",),
        ).fetchall()
        return [_from_row(row) for row in rows]

    def list(
        self,
        *,
        source: str | None = None,
        status: ApplicationStatus | None = None,
        min_score: int | None = None,
        max_score: int | None = None,
        scored: bool | None = None,
        languages: Sequence[str] | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[JobPosting]:
        clauses, params = _filter_clauses(
            source=source,
            status=status,
            min_score=min_score,
            max_score=max_score,
            scored=scored,
            languages=languages,
        )

        sql = "SELECT * FROM postings"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY fetched_at DESC"
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params.extend([limit, offset])

        rows = self._connection.execute(sql, params).fetchall()
        return [_from_row(row) for row in rows]

    def update_score(
        self,
        url: str,
        *,
        score: int,
        summary: str | None = None,
        matched_skills: Sequence[str] = (),
        unmatched_profile_skills: Sequence[str] = (),
        missing_requirements: Sequence[str] = (),
        penalized_skills: Sequence[str] = (),
        location_match: bool | None = None,
        work_mode_match: bool | None = None,
    ) -> bool:
        with self._connection:
            cursor = self._connection.execute(
                "UPDATE postings SET score = ?, summary = ?, matched_skills = ?, "
                "unmatched_profile_skills = ?, missing_requirements = ?, "
                "penalized_skills = ?, location_match = ?, work_mode_match = ? "
                "WHERE url = ?",
                (
                    score,
                    summary,
                    json.dumps(list(matched_skills)),
                    json.dumps(list(unmatched_profile_skills)),
                    json.dumps(list(missing_requirements)),
                    json.dumps(list(penalized_skills)),
                    _bool_to_db(location_match),
                    _bool_to_db(work_mode_match),
                    normalize_job_url(url),
                ),
            )
        return cursor.rowcount > 0

    def update_status(
        self,
        url: str,
        status: ApplicationStatus,
        *,
        applied_at: datetime | None = None,
    ) -> bool:
        with self._connection:
            cursor = self._connection.execute(
                "UPDATE postings SET status = ?, applied_at = ? WHERE url = ?",
                (
                    status.value,
                    applied_at.isoformat() if applied_at else None,
                    normalize_job_url(url),
                ),
            )
        return cursor.rowcount > 0

    def count(
        self,
        *,
        source: str | None = None,
        status: ApplicationStatus | None = None,
        min_score: int | None = None,
        max_score: int | None = None,
        scored: bool | None = None,
        languages: Sequence[str] | None = None,
    ) -> int:
        clauses, params = _filter_clauses(
            source=source,
            status=status,
            min_score=min_score,
            max_score=max_score,
            scored=scored,
            languages=languages,
        )

        sql = "SELECT COUNT(*) FROM postings"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)

        row = self._connection.execute(sql, params).fetchone()
        return int(row[0])
