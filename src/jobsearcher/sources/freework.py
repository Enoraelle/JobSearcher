"""free-work.com search-results scraper.

Fetches French freelance ("mission") postings from
https://www.free-work.com's tech/IT job search, once per configured keyword
under `sources.freework.keywords` (e.g. ``"python django"``).

Unlike `GreenhouseSource` (a stable JSON API) or `WeWorkRemotelySource` (a
stable RSS feed), this source scrapes an HTML page free-work.com does not
publish as a contract — its markup can change at any time without notice.
That fragility is handled two ways:

- Every selector this module depends on is a module-level constant in the
  "CSS selectors" section below, so a markup change is a one-line fix to a
  constant instead of a re-read of the extraction logic that uses it.
- A selector that no longer matches must never surface as an
  `AttributeError` on `None` deep inside BeautifulSoup traversal code. Two
  helpers enforce this: `_select_text` returns `None` instead of raising
  when a selector finds nothing, and every place that needs a match to
  proceed checks for `None` explicitly and raises `SourceConfigError` (unit
  level, in `_parse_search_results`) or `KeyError` (item level, in
  `normalize`) with a message that says outright that free-work.com's page
  layout has probably changed — see `_LAYOUT_CHANGED_HINT`.

Each configured keyword is an independent unit of collection, exactly like
Greenhouse's company slugs (see `GreenhouseSource.fetch_raw`): `fetch_raw`
searches one keyword at a time and, if that keyword's request fails or its
results page doesn't parse, records the failure with `Source._record_error`
and moves on to the next keyword rather than aborting the rest of the list.

Only the first page of results is collected for a keyword. `fetch_raw`
issues exactly one request per keyword and never follows free-work.com's
pagination, so everything past the first results page is simply not seen.
`max_postings_per_keyword` is a cap *below* that ceiling, not a window onto
a larger stream: it can reduce what a keyword contributes, but it can never
raise it above one page, and a value larger than a page holds changes
nothing. Widening coverage today means configuring more (or more specific)
keywords; following pagination would be a change to this module.

A results page with zero job cards is ambiguous on its own: it could mean
"no postings currently match this keyword" (not a failure) or "the card
selector is stale" (a failure). `_parse_search_results` disambiguates by
also checking for `_NO_RESULTS_SELECTOR`, the page's own "no results" state
element — present, it's a genuine empty result; absent, neither selector
matched anything on a page that came back with a 200, which is the layout
having changed.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any, Final
from urllib.parse import urlencode, urljoin

from bs4 import BeautifulSoup, Tag
from pydantic import Field, ValidationError

from jobsearcher.config import PluginConfig
from jobsearcher.models import JobPosting
from jobsearcher.sources._work_mode import infer_work_mode
from jobsearcher.sources.base import (
    Source,
    SourceConfigError,
    SourceHttpConfig,
    SourceUnavailableError,
    register_source,
)

logger = logging.getLogger(__name__)

_SEARCH_URL: Final[str] = "https://www.free-work.com/fr/tech-it/jobs"

_LAYOUT_CHANGED_HINT: Final[str] = "the free-work.com page layout has probably changed"

# --- CSS selectors ---------------------------------------------------------
# Isolated here, and nowhere else in this module, precisely because
# free-work.com's markup is not a stable contract. If scraping starts
# failing, this is the section to update; the parsing logic below should not
# need to change along with it.
_JOB_CARD_SELECTOR: Final[str] = "article[data-testid='search-tile']"
_NO_RESULTS_SELECTOR: Final[str] = "[data-testid='search-no-results']"
_TITLE_LINK_SELECTOR: Final[str] = "a[data-testid='search-tile-title']"
_COMPANY_SELECTOR: Final[str] = "[data-testid='search-tile-company']"
_LOCATION_SELECTOR: Final[str] = "[data-testid='search-tile-location']"
_CONTRACT_SELECTOR: Final[str] = "[data-testid='search-tile-contract']"
_SALARY_SELECTOR: Final[str] = "[data-testid='search-tile-salary']"
_EXCERPT_SELECTOR: Final[str] = "[data-testid='search-tile-excerpt']"

# Free-text markers used to infer `WorkMode` from a card's location/contract
# text, which on free-work.com carries phrasing like "Télétravail total"
# rather than a structured remote flag. Only the vocabulary lives here: the
# reading of it — whole tokens, negations — is shared with every other
# source in `jobsearcher.sources._work_mode`, because a second copy of that
# is how this source came to file "This role is not remote" under REMOTE.
_REMOTE_MARKERS: Final[frozenset[str]] = frozenset(
    {"télétravail total", "100% télétravail", "full remote", "remote"}
)
_HYBRID_MARKERS: Final[frozenset[str]] = frozenset({"télétravail partiel", "hybride", "hybrid"})


class FreeworkSourceConfig(SourceHttpConfig):
    """Typed, validated configuration for `FreeworkSource`.

    Re-validated from the permissive `PluginConfig` once we know we're
    dealing with free-work.com specifically, the same way
    `GreenhouseSourceConfig` is — see that class's docstring for why
    `extra="forbid"` matters here, and `SourceHttpConfig` for the shared
    HTTP-policy options this inherits.
    """

    keywords: list[str] = Field(min_length=1)
    max_postings_per_keyword: int | None = Field(default=None, ge=1)


def _select_text(scope: Tag, selector: str) -> str | None:
    """Return the stripped text of the first match of `selector`, or `None`.

    Never raises for a selector that finds nothing — that is the one job of
    this helper, so callers can treat a stale selector as "missing data" to
    handle explicitly, instead of an `AttributeError` surfacing from `None`
    deep inside BeautifulSoup traversal code.
    """
    element = scope.select_one(selector)
    if element is None:
        return None
    text = element.get_text(strip=True)
    return text or None


def _parse_search_results(html_content: str, base_url: str) -> list[dict[str, str]]:
    """Parse a free-work.com search-results page into raw job-card dicts.

    Each dict holds whatever of `title`, `url`, `company`, `location`,
    `contract_type`, `salary_text`, and `description` could be extracted
    from its card — a selector that didn't match is simply absent from the
    dict, and it is `normalize`'s job to decide which of these are required.

    Args:
        html_content: The response body of a search-results page.
        base_url: The URL the page was fetched from, used to resolve a job
            card's link if it's relative.

    Returns:
        One dict per job card found, in page order. An empty list means the
        page's own "no results" marker was present — a genuine empty
        result, not a parsing failure.

    Raises:
        SourceConfigError: If neither a job card nor the "no results" marker
            matched anything, which means the page's markup no longer
            matches any selector this module knows about.
    """
    soup = BeautifulSoup(html_content, "html.parser")
    cards = soup.select(_JOB_CARD_SELECTOR)

    if not cards:
        if soup.select_one(_NO_RESULTS_SELECTOR) is not None:
            return []
        raise SourceConfigError(
            f"No element matched {_JOB_CARD_SELECTOR!r}, and no "
            f"{_NO_RESULTS_SELECTOR!r} 'no results' marker was present either: "
            f"{_LAYOUT_CHANGED_HINT}."
        )

    raw_items: list[dict[str, str]] = []
    for card in cards:
        raw: dict[str, str] = {}

        title_link = card.select_one(_TITLE_LINK_SELECTOR)
        if title_link is not None:
            title_text = title_link.get_text(strip=True)
            if title_text:
                raw["title"] = title_text
            href = title_link.get("href")
            if isinstance(href, str) and href:
                raw["url"] = urljoin(base_url, href)

        for field_name, selector in (
            ("company", _COMPANY_SELECTOR),
            ("location", _LOCATION_SELECTOR),
            ("contract_type", _CONTRACT_SELECTOR),
            ("salary_text", _SALARY_SELECTOR),
            ("description", _EXCERPT_SELECTOR),
        ):
            text = _select_text(card, selector)
            if text is not None:
                raw[field_name] = text

        raw_items.append(raw)

    return raw_items


@register_source("freework")
class FreeworkSource(Source):
    """Job source backed by scraping free-work.com's tech/IT job search."""

    def __init__(self, name: str, config: PluginConfig, **kwargs: Any) -> None:
        """Initialize the source, validating free-work-specific config.

        Args:
            name: The source's registered name (see `Source.__init__`).
            config: This source's configuration block; re-validated into
                `FreeworkSourceConfig`.
            **kwargs: Passed through to `Source.__init__` (`client`,
                `timeout`, `max_retries`, `backoff_base`,
                `min_request_interval`); the pipeline fills these from the
                same config block.

        Raises:
            SourceConfigError: If `config` doesn't validate as
                `FreeworkSourceConfig` (e.g. missing `keywords`, or an
                unknown key).
        """
        super().__init__(name, config, **kwargs)
        try:
            self._freework_config = FreeworkSourceConfig.model_validate(config.model_dump())
        except ValidationError as exc:
            raise SourceConfigError(f"Invalid configuration for source {name!r}: {exc}") from exc

    def fetch_raw(self) -> Iterator[dict[str, Any]]:
        """Yield raw job-card dicts for every configured keyword, in order.

        Each configured keyword is an independent unit (see the module
        docstring): a keyword whose request fails, or whose results page
        doesn't parse (`_parse_search_results` raising `SourceConfigError`),
        is recorded via `Source._record_error` and skipped — it does not
        raise, and does not stop the remaining keywords from being tried.
        """
        for keyword in self._freework_config.keywords:
            url = f"{_SEARCH_URL}?{urlencode({'query': keyword})}"
            try:
                response = self._request("GET", url)
            except SourceConfigError as exc:
                self._record_error(
                    f"free-work.com search keyword {keyword!r} "
                    f"(from sources.freework.keywords) looks wrong: {exc}"
                )
                continue
            except SourceUnavailableError as exc:
                self._record_error(
                    f"free-work.com search keyword {keyword!r} "
                    f"(from sources.freework.keywords) is unavailable: {exc}"
                )
                continue

            try:
                raw_items = _parse_search_results(response.text, str(response.url))
            except SourceConfigError as exc:
                self._record_error(
                    f"free-work.com search keyword {keyword!r} "
                    f"(from sources.freework.keywords): {exc}"
                )
                continue

            limit = self._freework_config.max_postings_per_keyword
            for raw in raw_items[:limit] if limit is not None else raw_items:
                yield {**raw, "_keyword": keyword}

    def normalize(self, raw: dict[str, Any]) -> JobPosting:
        """Convert one raw job-card dict into a `JobPosting`.

        Args:
            raw: One dict as yielded by `fetch_raw`.

        Returns:
            The normalized posting.

        Raises:
            KeyError: If `title` or `url` is missing — the two fields no
                card can be built without, and whose absence on an
                individual card most plausibly means that card's markup no
                longer matches `_TITLE_LINK_SELECTOR`.
        """
        missing = [key for key in ("title", "url") if not raw.get(key)]
        if missing:
            raise KeyError(
                f"free-work.com job card is missing required field(s) {missing}: "
                f"{_LAYOUT_CHANGED_HINT}."
            )

        location = raw.get("location")
        contract_type = raw.get("contract_type")
        description = raw.get("description") or ""

        return JobPosting(
            url=raw["url"],
            raw_url=raw["url"],
            title=raw["title"],
            # Freelance mission boards commonly list postings without a
            # disclosed client name; that is expected data, not a parsing
            # failure, so it falls back rather than raising.
            company=raw.get("company") or "Unknown",
            source=self.name,
            description_raw=description,
            description_clean=description or None,
            location=location,
            work_mode=infer_work_mode(
                location,
                contract_type,
                remote_markers=_REMOTE_MARKERS,
                hybrid_markers=_HYBRID_MARKERS,
            ),
            contract_type=contract_type,
            salary_text=raw.get("salary_text"),
            fetched_at=datetime.now(UTC),
        )
