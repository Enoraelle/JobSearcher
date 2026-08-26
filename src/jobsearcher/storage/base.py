"""Storage contract for collected job postings.

This module defines only the interface. It has no knowledge of SQLite (or any
other backend) and performs no I/O itself.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from jobsearcher.models import ApplicationStatus, JobPosting


class Storage(Protocol):
    """Persistence contract for job postings.

    Implementations own all read/write access to stored postings. A posting's
    identity is its (normalized) URL: saving a posting whose URL already
    exists must not duplicate or overwrite the stored row, including its
    score.
    """

    def save_many(self, postings: Sequence[JobPosting]) -> int:
        """Persist postings, skipping any whose URL is already stored.

        Args:
            postings: The postings to persist.

        Returns:
            The number of postings that were newly inserted.
        """
        ...

    def get_by_url(self, url: str) -> JobPosting | None:
        """Fetch a single posting by URL.

        Args:
            url: The posting's URL (need not be pre-normalized).

        Returns:
            The matching posting, or ``None`` if no posting has that URL.
        """
        ...

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
        """List stored postings, optionally filtered.

        Args:
            source: Restrict to postings from this source, if given.
            status: Restrict to postings with this application status, if given.
            min_score: Restrict to postings with a score >= this value, if given.
            max_score: Restrict to postings with a score <= this value, if given.
            limit: Maximum number of postings to return, if given.
            offset: Number of matching postings to skip before returning results.

        Returns:
            The matching postings.
        """
        ...

    def update_score(
        self,
        url: str,
        *,
        score: int,
        summary: str | None = None,
        matched_skills: Sequence[str] = (),
        missing_skills: Sequence[str] = (),
    ) -> bool:
        """Set the score and related scoring fields of a stored posting.

        Args:
            url: The posting's URL (need not be pre-normalized).
            score: The new score.
            summary: A short human-readable rationale for the score, if any.
            matched_skills: Skills from the posting that matched the user's criteria.
            missing_skills: Skills the user's criteria call for but the posting lacks.

        Returns:
            ``True`` if a posting with that URL was found and updated,
            ``False`` otherwise.
        """
        ...

    def update_status(
        self,
        url: str,
        status: ApplicationStatus,
        *,
        applied_at: datetime | None = None,
    ) -> bool:
        """Set the application status of a stored posting.

        Args:
            url: The posting's URL (need not be pre-normalized).
            status: The new application status.
            applied_at: When the user applied, if applicable.

        Returns:
            ``True`` if a posting with that URL was found and updated,
            ``False`` otherwise.
        """
        ...

    def count(
        self,
        *,
        source: str | None = None,
        status: ApplicationStatus | None = None,
    ) -> int:
        """Count stored postings, optionally filtered.

        Args:
            source: Restrict to postings from this source, if given.
            status: Restrict to postings with this application status, if given.

        Returns:
            The number of matching postings.
        """
        ...
