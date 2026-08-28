"""Greenhouse Job Board API source.

Fetches postings from the public, unauthenticated Job Board JSON API
(https://developers.greenhouse.io/job-board.html) for each company slug
configured under `sources.greenhouse.companies`:

    https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true

That endpoint does not return a company display name, only the postings for
the board identified by `slug` — so the company name attached to each
posting is derived from the configured slug itself (see `_humanize_slug`).

Two quirks of that response shape are handled here rather than downstream:
the `content` field arrives HTML-*escaped* and has to be unescaped before it
can be parsed as markup (see `_description_html`), and there is no
structured remote/hybrid flag, so the work mode is inferred (see
`_infer_work_mode`).

A realistic configuration lists ten to seventy company slugs. Each is an
independent unit of collection: `fetch_raw` fetches and yields one company's
jobs at a time and, if that company's request fails (a 404 for a misspelled
slug via `SourceConfigError`, or a persistently unreachable board via
`SourceUnavailableError`), records the failure with `Source._record_error`
and moves on to the next company rather than aborting the rest of the list.
A run with one bad slug among fifty still returns the other forty-nine
companies' postings; `last_run.errors` will have one entry naming the
offending slug and the `sources.greenhouse.companies` field, and
`last_run.failed` only becomes `True` if every configured company failed.
"""

from __future__ import annotations

import html
import json
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from jobsearcher.config import PluginConfig
from jobsearcher.models import JobPosting, WorkMode
from jobsearcher.sources._html import strip_html
from jobsearcher.sources.base import (
    Source,
    SourceConfigError,
    SourceUnavailableError,
    register_source,
)

_JOB_BOARD_URL: Final[str] = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"

# Heuristics for pulling a geographic restriction out of free text. These are
# best-effort: Greenhouse gives no structured eligibility field, so this
# scans `location.name` and the description for common phrasings. False
# negatives (a restriction stated in an unrecognized phrasing) are expected;
# `eligible_locations` staying empty just means "not detected", not "none".
_REMOTE_LOCATION_RE: Final[re.Pattern[str]] = re.compile(
    r"remote\s*[-–—:]\s*(.+)",  # noqa: RUF001 (en/em dash used by real listings)
    re.IGNORECASE,
)
# Work-mode inference (see `_infer_work_mode`). A marker preceded by one of
# these words within `_NEGATION_WINDOW` tokens is a mention of the
# arrangement being ruled *out*, not offered. The set is kept small and
# unambiguous on purpose: a missed negation only restores the previous
# behaviour, while a word like "can" would negate the sentences that grant
# remote work.
_WORD_RE: Final[re.Pattern[str]] = re.compile(r"[a-z]+")
_WORK_MODE_NEGATORS: Final[frozenset[str]] = frozenset(
    {"no", "non", "not", "never", "neither", "nor", "without", "rarely"}
)
_NEGATION_WINDOW: Final[int] = 3

_RESTRICTION_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"only candidates (?:based |located )?in ([A-Za-z ,]+)", re.IGNORECASE),
    re.compile(r"must be (?:based|located) in ([A-Za-z ,]+)", re.IGNORECASE),
    re.compile(r"open to candidates in ([A-Za-z ,]+)", re.IGNORECASE),
)


class GreenhouseSourceConfig(BaseModel):
    """Typed, validated configuration for `GreenhouseSource`.

    Re-validated from the permissive `PluginConfig` once we know we're
    dealing with Greenhouse specifically. `PluginConfig` allows unknown keys
    because it's shared by every source and doesn't know which one it is;
    this model forbids them, so a typo like `companys:` raises immediately
    instead of silently producing a source that never collects anything.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    companies: list[str] = Field(min_length=1)
    max_postings_per_company: int | None = Field(default=None, ge=1)


def _description_html(raw: dict[str, Any]) -> str:
    """Return a job's description as real HTML, ready to be parsed.

    The Job Board API delivers `content` **HTML-escaped**: the wire value of
    a paragraph is the literal text ``&lt;p&gt;``, not ``<p>``. Feeding that
    straight to an HTML parser is a no-op — the parser sees text, decodes
    the entities, and hands back a string still full of ``<p>`` tags, which
    then travel into the score haystack (where `p`, `li` and `strong` count
    as words), the language heuristic, and every export.

    One `html.unescape` turns the wire value into the markup it represents.
    Entities inside the prose itself (`&amp;amp;` for a literal `&`) are
    left one level escaped by that same pass and are decoded by the HTML
    parser afterwards, so each is decoded exactly once.

    Args:
        raw: One job dict as returned by the API.

    Returns:
        The description as HTML, or an empty string if the job carries none.
    """
    content = raw.get("content") or ""
    return html.unescape(content)


def _humanize_slug(slug: str) -> str:
    """Turn a company slug like `some-company` into `Some Company`."""
    return " ".join(word.capitalize() for word in slug.replace("_", "-").split("-") if word)


def _extract_location_name(raw: dict[str, Any]) -> str | None:
    location = raw.get("location")
    if isinstance(location, dict):
        name = location.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    return None


def _mode_is_asserted(tokens: list[str], marker: str) -> bool:
    """Whether ``marker`` appears as a whole token that is not negated.

    Two things go wrong with a plain ``marker in text`` over a description:
    it fires inside longer words, and — far more often — it fires on the
    sentence that rules the arrangement *out*. "This role is not remote" is
    the single most common way a posting mentions remote work, and reading
    it as REMOTE puts the posting straight into `--remote`.

    Args:
        tokens: The lowercased word tokens of the text to search.
        marker: The whole token to look for.

    Returns:
        Whether at least one occurrence of ``marker`` stands unnegated.
    """
    return any(
        token == marker
        and not _WORK_MODE_NEGATORS.intersection(tokens[max(0, index - _NEGATION_WINDOW) : index])
        for index, token in enumerate(tokens)
    )


def _infer_work_mode(location_name: str | None, description_clean: str) -> WorkMode:
    """Best-effort work arrangement, since Greenhouse has no field for it.

    `location.name` is checked first and on its own: it is the board's own
    assertion about the job, while the description is prose in which the
    word "remote" turns up in negations, asides about other teams, and
    boilerplate. The description is only consulted when the location says
    nothing about the arrangement, and then only for unnegated whole-token
    mentions (see `_mode_is_asserted`).

    `--remote` filters on the result, so this errs toward ONSITE/UNKNOWN
    rather than claiming an arrangement the posting did not assert.
    """
    if location_name:
        location_tokens = _WORD_RE.findall(location_name.casefold())
        if _mode_is_asserted(location_tokens, "remote"):
            return WorkMode.REMOTE
        if _mode_is_asserted(location_tokens, "hybrid"):
            return WorkMode.HYBRID

    description_tokens = _WORD_RE.findall(description_clean.casefold())
    if _mode_is_asserted(description_tokens, "remote"):
        return WorkMode.REMOTE
    if _mode_is_asserted(description_tokens, "hybrid"):
        return WorkMode.HYBRID

    if location_name:
        return WorkMode.ONSITE
    return WorkMode.UNKNOWN


def _extract_eligible_locations(location_name: str | None, description_clean: str) -> list[str]:
    """Best-effort extraction of geographic eligibility restrictions.

    Looks at the `Remote - <region>` convention in `location.name` and a
    handful of common restriction phrasings in the description. See the
    module docstring's note on `_RESTRICTION_PATTERNS` for the trade-off.
    """
    found: list[str] = []

    if location_name:
        match = _REMOTE_LOCATION_RE.match(location_name.strip())
        if match:
            tail = match.group(1).strip(" -–—()")  # noqa: RUF001 (en/em dash used by real listings)
            if tail:
                found.append(tail)

    for pattern in _RESTRICTION_PATTERNS:
        for match in pattern.finditer(description_clean):
            candidate = match.group(1).strip().rstrip(".").strip()
            if candidate and candidate not in found:
                found.append(candidate)

    return found


def _parse_datetime(value: Any) -> datetime | None:
    """Parse a Greenhouse timestamp into a UTC-aware datetime.

    `JobPosting` requires timezone-aware datetimes, and a naive one raises —
    which `Source.fetch` would count as a malformed item and skip, losing
    the whole posting over an optional metadata field. A value that has no
    offset is therefore read as UTC (what the API documents its timestamps
    to be) rather than propagated as-is, and one that cannot be parsed at
    all becomes `None`, which the model accepts.

    Args:
        value: The raw `updated_at` field, of whatever type the API sent.

    Returns:
        The timestamp in UTC, or `None` if there is none to read.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


@register_source("greenhouse")
class GreenhouseSource(Source):
    """Job source backed by the public Greenhouse Job Board API."""

    def __init__(self, name: str, config: PluginConfig, **kwargs: Any) -> None:
        """Initialize the source, validating Greenhouse-specific config.

        Args:
            name: The source's registered name (see `Source.__init__`).
            config: This source's configuration block; re-validated into
                `GreenhouseSourceConfig`.
            **kwargs: Passed through to `Source.__init__` (`client`,
                `max_retries`, `backoff_base`, `min_request_interval`).

        Raises:
            SourceConfigError: If `config` doesn't validate as
                `GreenhouseSourceConfig` (e.g. missing `companies`, or an
                unknown key). This happens at construction time, before
                `fetch()` is ever called — a caller that wants one bad
                config block to not abort a multi-source run should
                construct sources inside its own error boundary, the same
                way `fetch()` isolates failures during collection.
        """
        super().__init__(name, config, **kwargs)
        try:
            self._greenhouse_config = GreenhouseSourceConfig.model_validate(config.model_dump())
        except ValidationError as exc:
            raise SourceConfigError(f"Invalid configuration for source {name!r}: {exc}") from exc

    def fetch_raw(self) -> Iterator[dict[str, Any]]:
        """Yield raw job dicts for every configured company, in order.

        Each yielded dict is the job as returned by the API, plus an
        internal `_company_slug` key so `normalize` can derive a company
        name without a second request.

        Each company is an independent unit (see the module docstring): a
        company whose request fails, or whose response doesn't have the
        expected `{"jobs": [...]}` shape, is recorded via
        `Source._record_error` and skipped — it does not raise, and does not
        stop the remaining companies from being tried.
        """
        for slug in self._greenhouse_config.companies:
            url = _JOB_BOARD_URL.format(slug=slug)
            try:
                response = self._request("GET", url, params={"content": "true"})
            except SourceConfigError as exc:
                self._record_error(
                    f"Greenhouse company slug {slug!r} "
                    f"(from sources.greenhouse.companies) looks wrong: {exc}"
                )
                continue
            except SourceUnavailableError as exc:
                self._record_error(
                    f"Greenhouse company slug {slug!r} "
                    f"(from sources.greenhouse.companies) is unavailable: {exc}"
                )
                continue

            try:
                payload = response.json()
                jobs = payload["jobs"]
                if not isinstance(jobs, list):
                    raise TypeError(f"'jobs' is a {type(jobs).__name__}, expected a list")
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                self._record_error(
                    f"Greenhouse returned an unexpected response shape for company "
                    f"slug {slug!r} (from sources.greenhouse.companies): {exc}"
                )
                continue

            limit = self._greenhouse_config.max_postings_per_company
            for job in jobs[:limit] if limit is not None else jobs:
                yield {**job, "_company_slug": slug}

    def normalize(self, raw: dict[str, Any]) -> JobPosting:
        """Convert one raw Greenhouse job dict into a `JobPosting`.

        Args:
            raw: One job dict as yielded by `fetch_raw`.

        Returns:
            The normalized posting.

        Raises:
            KeyError: If `absolute_url`, `title`, or the internal
                `_company_slug` is missing.
        """
        missing = [key for key in ("absolute_url", "title", "_company_slug") if key not in raw]
        if missing:
            raise KeyError(f"Greenhouse job is missing required field(s): {missing}")

        # `description_raw` keeps the markup as sent (unescaped once, so it
        # is HTML rather than text-that-looks-like-HTML); `description_clean`
        # is the plain text everything downstream actually reads.
        description_raw = _description_html(raw)
        description_clean = strip_html(description_raw)
        location_name = _extract_location_name(raw)

        return JobPosting(
            url=raw["absolute_url"],
            raw_url=raw["absolute_url"],
            title=raw["title"],
            company=_humanize_slug(raw["_company_slug"]),
            source=self.name,
            description_raw=description_raw,
            description_clean=description_clean,
            location=location_name,
            work_mode=_infer_work_mode(location_name, description_clean),
            published_at=_parse_datetime(raw.get("updated_at")),
            fetched_at=datetime.now(UTC),
            eligible_locations=_extract_eligible_locations(location_name, description_clean),
        )
