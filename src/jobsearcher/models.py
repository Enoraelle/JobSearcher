"""Data models for JobSearcher (job postings, scoring, and tracking state).

This module has a single implementation, so it stays a flat file rather than
a package — see the "File vs. package" convention in CLAUDE.md.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Final
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import BaseModel, Field, computed_field, field_validator, model_validator

# Number of hex characters kept from the SHA-256 digest of a posting's
# normalized URL to form its short identifier. Twelve keeps the collision
# probability negligible well past any realistic database size; consumers
# that display the id (the CLI) truncate further and resolve by prefix.
_POSTING_ID_LENGTH: Final[int] = 12

# Query parameters stripped during URL normalization because they identify a
# traffic source rather than the job posting itself. Two URLs that only differ
# by one of these params refer to the same posting and must normalize to the
# same key. Edit this list, not the normalization logic, to add new ones.
_TRACKING_PARAM_PREFIXES: Final[tuple[str, ...]] = ("utm_",)
_TRACKING_PARAM_NAMES: Final[frozenset[str]] = frozenset(
    {
        "fbclid",
        "gclid",
        "msclkid",
        "mc_cid",
        "mc_eid",
        "ref",
        "referrer",
        "source",
        "src",
    }
)


class WorkMode(StrEnum):
    """Work arrangement offered by a job posting."""

    REMOTE = "remote"
    HYBRID = "hybrid"
    ONSITE = "onsite"
    UNKNOWN = "unknown"


class ApplicationStatus(StrEnum):
    """Where a job posting stands in the user's application pipeline."""

    NEW = "new"
    SHORTLISTED = "shortlisted"
    APPLIED = "applied"
    INTERVIEWING = "interviewing"
    REJECTED = "rejected"
    CLOSED = "closed"


def _is_tracking_param(name: str) -> bool:
    """Return whether a query parameter name is a traffic-tracking param."""
    lowered = name.lower()
    return lowered in _TRACKING_PARAM_NAMES or lowered.startswith(_TRACKING_PARAM_PREFIXES)


def normalize_job_url(value: str) -> str:
    """Normalize a job posting URL into a stable, deduplication-safe key.

    Applies, in order: lowercase scheme/host with http forced to https,
    fragment removal, trailing-slash removal (except for the root path),
    tracking-parameter removal, empty-value parameter removal, and
    alphabetical sorting of the remaining query parameters.

    Args:
        value: The URL as received from a source.

    Returns:
        The normalized URL, suitable as a stable identity key.

    Raises:
        ValueError: If the value is not an absolute URL (missing scheme or host).
    """
    parts = urlsplit(value.strip())
    if not parts.scheme or not parts.netloc:
        raise ValueError(f"'{value}' is not an absolute URL")

    scheme = "https" if parts.scheme.lower() == "http" else parts.scheme.lower()
    netloc = parts.netloc.lower()
    path = parts.path.rstrip("/") or "/"

    query_pairs = sorted(
        (key, val)
        for key, val in parse_qsl(parts.query, keep_blank_values=True)
        if val and not _is_tracking_param(key)
    )
    query = urlencode(query_pairs)

    return urlunsplit((scheme, netloc, path, query, ""))


def posting_id(url: str) -> str:
    """Return the stable short identifier for a posting URL.

    The identifier is the first :data:`_POSTING_ID_LENGTH` hex characters of
    the SHA-256 digest of the *normalized* URL. Because it derives only from
    the URL, it is identical on every machine and can be recomputed outside
    the database — which is what makes it safe to cite in a bug report or
    paste into a tracker, unlike an auto-incrementing row counter that would
    not survive an export, a rebuilt database, or a comparison between two
    machines.

    Args:
        url: A posting URL, normalized or not.

    Returns:
        The lowercase hex identifier.

    Raises:
        ValueError: If ``url`` is not an absolute URL.
    """
    digest = hashlib.sha256(normalize_job_url(url).encode("utf-8")).hexdigest()
    return digest[:_POSTING_ID_LENGTH]


class JobPosting(BaseModel):
    """A single job posting collected from a source.

    ``url`` is the normalized, natural identity of a posting: two postings
    with the same normalized URL are considered the same job and must not be
    stored twice. ``raw_url`` keeps the URL exactly as the source produced
    it, for debugging sources that start emitting unexpected links.
    """

    # --- Identity ---
    url: str
    raw_url: str
    title: str
    company: str
    source: str

    # --- Content ---
    # `description_raw` is the posting body exactly as its source delivers
    # it, and what that *is* differs by source: HTML markup from Greenhouse
    # (unescaped once, see `_description_html`) and from We Work Remotely,
    # plain text from free-work.com, whose search cards carry no markup.
    # Nothing normalizes it, on purpose — it is the field to look at when a
    # source starts emitting something unexpected, so it must stay
    # untouched. A consumer that wants text must therefore read
    # `description_clean`, which every source fills with plain text (and
    # which is `None` only when there was no body at all); treating
    # `description_raw` as text is how `<p>` and `<li>` end up counted as
    # words by a scorer or shown to a reader in an export.
    description_raw: str
    description_clean: str | None = None

    # --- Metadata ---
    location: str | None = None
    work_mode: WorkMode = WorkMode.UNKNOWN
    contract_type: str | None = None
    salary_text: str | None = None
    published_at: datetime | None = None
    fetched_at: datetime

    # Best-effort language of the posting text, filled at collection time by
    # the heuristic in :mod:`jobsearcher.language`. ``None`` means
    # "undetermined" (text too short, or no language clearly won) — a state
    # kept distinct from "a language outside the supported set", and one that
    # a language filter must never hide.
    detected_language: str | None = None

    # --- Geography / eligibility ---
    # Captures restrictions such as "Only candidates in India" or
    # "Remote - US", which `location` and `work_mode` cannot express alone.
    eligible_locations: list[str] = Field(default_factory=list)

    # --- Scoring ---
    # The last scoring outcome merged into the posting. See ``ScoreResult``
    # for the exact meaning of each field; the three skill lists are kept
    # separate on purpose and never merged.
    score: int | None = Field(default=None, ge=0, le=100)
    summary: str | None = None
    matched_skills: list[str] = Field(default_factory=list)
    unmatched_profile_skills: list[str] = Field(default_factory=list)
    missing_requirements: list[str] = Field(default_factory=list)
    penalized_skills: list[str] = Field(default_factory=list)
    location_match: bool | None = None
    work_mode_match: bool | None = None

    # --- Application tracking ---
    status: ApplicationStatus = ApplicationStatus.NEW
    applied_at: datetime | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def id(self) -> str:
        """The posting's stable short identifier (see :func:`posting_id`)."""
        return posting_id(self.url)

    @model_validator(mode="before")
    @classmethod
    def _default_raw_url_from_url(cls, data: Any) -> Any:
        """Populate raw_url from url when a source only supplies one field."""
        if isinstance(data, dict) and data.get("raw_url") is None and "url" in data:
            data = {**data, "raw_url": data["url"]}
        return data

    @field_validator("url")
    @classmethod
    def normalize_url(cls, value: str) -> str:
        return normalize_job_url(value)

    @field_validator("title", "company", "source")
    @classmethod
    def not_blank(cls, value: str) -> str:
        """Reject empty or whitespace-only identity fields."""
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be empty")
        return stripped

    @field_validator("fetched_at", "published_at", "applied_at")
    @classmethod
    def ensure_utc(cls, value: datetime | None) -> datetime | None:
        """Require timezone-aware datetimes, normalized to UTC."""
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("datetime must be timezone-aware")
        return value.astimezone(UTC)


class ScoreResult(BaseModel):
    """The outcome of scoring one posting against a profile.

    ``score`` reflects **skill coverage only** — how much of the profile's
    skill set the posting mentions, minus a penalty for skills the profile
    explicitly rules out. It deliberately does not fold in location or
    work-mode fit: blending "I can do this job" and "I can take this job"
    into a single number produces ranking inversions (a perfect-stack
    posting in an unreachable location lands mid-table, too high to dismiss
    and too low to explain). Location and work-mode fit are reported next to
    the score, as ``location_match`` / ``work_mode_match``, and filtered on
    separately.

    The three skill lists carry three distinct meanings and are never merged
    into one, because a consumer (an exporter, a CLI filter) must be able to
    tell them apart without knowing which scorer ran:

    - ``matched_skills``: entries of ``profile.skills`` found in the posting.
    - ``unmatched_profile_skills``: entries of ``profile.skills`` *not* found
      in the posting. This is what lowers coverage. It is a **weak** signal
      and must never be shown as a value judgement on the posting ("this
      posting ignores my skills") — a terse posting produces many of these
      with nothing being wrong. It only ever means "coverage is partial,
      here is on what".
    - ``missing_requirements``: skills the posting *states it requires* that
      the profile does not cover. A **strong** signal — a reason not to
      apply. Only a scorer that reads requirements out of the posting text
      (the LLM scorer) fills this; the keyword scorer always leaves it
      empty.
    - ``penalized_skills``: entries of ``profile.absent_skills`` found in the
      posting. These trigger the score penalty. Both scorers fill this.

    An empty list is honest information ("this scorer found none"), never an
    ambiguous or unknown state.
    """

    score: int = Field(ge=0, le=100)
    summary: str
    matched_skills: list[str] = Field(default_factory=list)
    unmatched_profile_skills: list[str] = Field(default_factory=list)
    missing_requirements: list[str] = Field(default_factory=list)
    penalized_skills: list[str] = Field(default_factory=list)
    location_match: bool | None = None
    work_mode_match: bool | None = None
