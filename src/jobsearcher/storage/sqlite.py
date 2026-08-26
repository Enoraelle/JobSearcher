"""SQLite implementation of the :class:`~jobsearcher.storage.base.Storage` protocol."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import Any

from jobsearcher.models import ApplicationStatus, JobPosting, WorkMode, normalize_job_url

_COLUMNS: tuple[str, ...] = (
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


def _to_row(posting: JobPosting) -> tuple[Any, ...]:
    """Convert a `JobPosting` into a tuple matching `_COLUMNS` order."""
    return (
        posting.url,
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
        json.dumps(posting.eligible_locations),
        posting.score,
        posting.summary,
        json.dumps(posting.matched_skills),
        json.dumps(posting.missing_skills),
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
        eligible_locations=json.loads(row["eligible_locations"]),
        score=row["score"],
        summary=row["summary"],
        matched_skills=json.loads(row["matched_skills"]),
        missing_skills=json.loads(row["missing_skills"]),
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

    def list(
        self,
        *,
        source: str | None = None,
        status: ApplicationStatus | None = None,
        min_score: int | None = None,
        max_score: int | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[JobPosting]:
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
        missing_skills: Sequence[str] = (),
    ) -> bool:
        with self._connection:
            cursor = self._connection.execute(
                "UPDATE postings SET score = ?, summary = ?, matched_skills = ?, "
                "missing_skills = ? WHERE url = ?",
                (
                    score,
                    summary,
                    json.dumps(list(matched_skills)),
                    json.dumps(list(missing_skills)),
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
    ) -> int:
        clauses: list[str] = []
        params: list[Any] = []
        if source is not None:
            clauses.append("source = ?")
            params.append(source)
        if status is not None:
            clauses.append("status = ?")
            params.append(status.value)

        sql = "SELECT COUNT(*) FROM postings"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)

        row = self._connection.execute(sql, params).fetchone()
        return int(row[0])
