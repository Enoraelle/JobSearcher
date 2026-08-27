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

    def find_by_id_prefix(self, prefix: str) -> list[JobPosting]:
        """Return every posting whose identifier starts with ``prefix``.

        Args:
            prefix: The leading hex characters of a posting identifier (see
                :func:`jobsearcher.models.posting_id`). Treated literally;
                the caller validates that it is a plausible fragment.

        Returns:
            All matching postings, most recently fetched first.
        """
        ...

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
        """List stored postings, optionally filtered.

        Args:
            source: Restrict to postings from this source, if given.
            status: Restrict to postings with this application status, if given.
            min_score: Restrict to postings with a score >= this value, if given.
            max_score: Restrict to postings with a score <= this value, if given.
            scored: ``True`` for only scored postings, ``False`` for only
                unscored ones, ``None`` (default) for both.
            languages: Restrict to postings whose detected language is one of
                these, if given. Postings with an undetermined language
                (``None``) always pass this filter.
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
        unmatched_profile_skills: Sequence[str] = (),
        missing_requirements: Sequence[str] = (),
        penalized_skills: Sequence[str] = (),
        location_match: bool | None = None,
        work_mode_match: bool | None = None,
    ) -> bool:
        """Set the score and related scoring fields of a stored posting.

        The three skill lists have three distinct meanings and are stored
        separately — see :class:`~jobsearcher.models.ScoreResult`.

        Args:
            url: The posting's URL (need not be pre-normalized).
            score: The new score (skill coverage only).
            summary: A short human-readable rationale for the score, if any.
            matched_skills: Profile skills the posting mentions.
            unmatched_profile_skills: Profile skills the posting does not
                mention (a weak, coverage-only signal).
            missing_requirements: Skills the posting states it requires that
                the profile does not cover (a strong signal; only an
                extracting scorer fills this).
            penalized_skills: Profile ``absent_skills`` the posting mentions,
                i.e. what triggered the score penalty.
            location_match: Whether the posting's location fits the profile,
                or ``None`` if unknown.
            work_mode_match: Whether the posting's work mode fits the
                profile, or ``None`` if unknown.

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
        min_score: int | None = None,
        max_score: int | None = None,
        scored: bool | None = None,
        languages: Sequence[str] | None = None,
    ) -> int:
        """Count stored postings, optionally filtered.

        Args:
            source: Restrict to postings from this source, if given.
            status: Restrict to postings with this application status, if given.
            min_score: Restrict to postings with a score >= this value, if given.
            max_score: Restrict to postings with a score <= this value, if given.
            scored: ``True`` for only scored, ``False`` for only unscored,
                ``None`` for both.
            languages: Restrict to these detected languages (``None``-language
                postings always pass), if given.

        Returns:
            The number of matching postings.
        """
        ...
