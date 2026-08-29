"""We Work Remotely RSS source.

Fetches postings from We Work Remotely's public RSS feeds: the site-wide
feed (``https://weworkremotely.com/remote-jobs.rss``) and the per-category
feeds (``https://weworkremotely.com/categories/{category}.rss``), for
whichever feed names are configured under `sources.weworkremotely.feeds`.

Every posting on We Work Remotely is remote by definition, so `work_mode` is
always `WorkMode.REMOTE` — unlike Greenhouse, there is no location field to
infer it from.

Each configured feed is an independent unit of collection, exactly like
Greenhouse's company slugs (see `GreenhouseSource.fetch_raw`): `fetch_raw`
fetches one feed at a time and, if a feed's request fails or its XML doesn't
parse, records the failure with `Source._record_error` and moves on to the
next feed rather than aborting the rest of the list.

The RSS `<description>` is truncated by We Work Remotely itself, not by this
source. `fetch_full_description` (off by default) makes normalize() issue
one extra GET per posting, to the posting's own page, and scrape the full
description out of it. This is opt-in because it turns O(feeds) requests
into O(postings) requests — a full category page lists dozens of jobs — and
because it depends on the page's HTML structure rather than a stable API, so
it is expected to degrade rather than fail: if the detail page is
unreachable or its markup doesn't match `_DETAIL_DESCRIPTION_CONTAINER_CLASS`
anymore, `_fetch_full_description` logs a warning and returns `None`, and
`normalize` simply keeps the truncated RSS description instead of raising.
Anyone enabling `fetch_full_description` should also set a non-zero
`min_request_interval` in the same `sources.weworkremotely` block — one of
the shared options in `SourceHttpConfig`, which the pipeline passes on —
since that is what paces the resulting one-request-per-posting fan-out;
this source does not add a second, separate rate limiter on top of it.
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any, Final

from pydantic import Field, ValidationError

from jobsearcher.config import PluginConfig
from jobsearcher.models import JobPosting, WorkMode
from jobsearcher.sources._html import strip_html
from jobsearcher.sources.base import (
    Source,
    SourceConfigError,
    SourceError,
    SourceHttpConfig,
    SourceUnavailableError,
    register_source,
)

logger = logging.getLogger(__name__)

_MAIN_FEED_NAME: Final[str] = "remote-jobs"
_MAIN_FEED_URL: Final[str] = "https://weworkremotely.com/remote-jobs.rss"
_CATEGORY_FEED_URL: Final[str] = "https://weworkremotely.com/categories/{category}.rss"

# We Work Remotely's own item field for its "region" restriction (e.g. "USA
# Only", "Europe Only"). These values mean "no restriction" and should not be
# reported as an eligibility restriction in `eligible_locations`.
_UNRESTRICTED_REGION_MARKERS: Final[frozenset[str]] = frozenset(
    {"anywhere in the world", "anywhere", "worldwide"}
)

# We Work Remotely's job detail pages wrap the full posting description in a
# `<div class="listing-container">`. Isolated as a constant so a future
# markup change is a one-line fix instead of a re-read of the extraction
# logic below it.
_DETAIL_DESCRIPTION_CONTAINER_CLASS: Final[str] = "listing-container"


class WeWorkRemotelySourceConfig(SourceHttpConfig):
    """Typed, validated configuration for `WeWorkRemotelySource`.

    Re-validated from the permissive `PluginConfig` once we know we're
    dealing with We Work Remotely specifically, the same way
    `GreenhouseSourceConfig` is — see that class's docstring for why
    `extra="forbid"` matters here.

    `min_request_interval`, the option this source's docstring tells you to
    set alongside `fetch_full_description`, is one of the shared HTTP-policy
    options inherited from `SourceHttpConfig`.
    """

    feeds: list[str] = Field(default_factory=lambda: [_MAIN_FEED_NAME], min_length=1)
    fetch_full_description: bool = False
    max_postings_per_feed: int | None = Field(default=None, ge=1)


class _ListingContainerExtractor(HTMLParser):
    """Pulls the raw HTML of the first `_DETAIL_DESCRIPTION_CONTAINER_CLASS` div.

    Tracks `<div>` nesting depth once inside the target container so nested
    divs in the real description don't end the capture early.
    """

    def __init__(self) -> None:
        super().__init__()
        self._depth = 0
        self._capturing = False
        self._chunks: list[str] = []

    def _has_target_class(self, attrs: list[tuple[str, str | None]]) -> bool:
        class_value = dict(attrs).get("class") or ""
        return _DETAIL_DESCRIPTION_CONTAINER_CLASS in class_value.split()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._capturing:
            if tag == "div":
                self._depth += 1
            self._chunks.append(self.get_starttag_text() or "")
        elif tag == "div" and self._has_target_class(attrs):
            self._capturing = True
            self._depth = 1

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._capturing:
            self._chunks.append(self.get_starttag_text() or "")

    def handle_endtag(self, tag: str) -> None:
        if not self._capturing:
            return
        if tag == "div":
            self._depth -= 1
            if self._depth == 0:
                self._capturing = False
                return
        self._chunks.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if self._capturing:
            self._chunks.append(data)

    def get_html(self) -> str | None:
        return "".join(self._chunks) if self._chunks else None


def _extract_listing_container_html(html_content: str) -> str | None:
    """Return the raw HTML inside the job detail page's description container.

    Returns `None` if no matching container is found, rather than raising —
    the caller treats that as "the page layout no longer matches" and falls
    back to the RSS-supplied truncated description instead of failing the
    posting outright.
    """
    extractor = _ListingContainerExtractor()
    extractor.feed(html_content)
    extractor.close()
    return extractor.get_html()


def _feed_url(feed_name: str) -> str:
    """Resolve a configured feed name to its RSS URL.

    Args:
        feed_name: `_MAIN_FEED_NAME` for the site-wide feed, or a We Work
            Remotely category slug (e.g. ``"remote-programming-jobs"``) for
            a category feed.
    """
    if feed_name == _MAIN_FEED_NAME:
        return _MAIN_FEED_URL
    return _CATEGORY_FEED_URL.format(category=feed_name)


def _parse_rss_items(xml_text: str) -> list[dict[str, str]]:
    """Parse an RSS document into one flat string dict per `<item>`.

    Each item's child tags become dict keys (namespace prefixes, if any, are
    stripped), keeping only the first occurrence of a repeated tag. Missing
    tags simply don't appear in the dict — `normalize` is responsible for
    deciding which keys are required.

    Raises:
        xml.etree.ElementTree.ParseError: If `xml_text` is not well-formed
            XML.
    """
    root = ET.fromstring(xml_text)
    items: list[dict[str, str]] = []
    for item_el in root.iterfind("./channel/item"):
        raw: dict[str, str] = {}
        for child in item_el:
            tag = child.tag.rsplit("}", 1)[-1]
            if tag not in raw:
                raw[tag] = (child.text or "").strip()
        items.append(raw)
    return items


def _split_company_and_title(title: str) -> tuple[str, str] | None:
    """Split a We Work Remotely RSS item title into (company, job title).

    We Work Remotely encodes both in one `<title>` as ``"Company: Job
    Title"``. Returns `None` if `title` doesn't contain that separator, or
    either side is blank once split.
    """
    if ":" not in title:
        return None
    company, _, job_title = title.partition(":")
    company = company.strip()
    job_title = job_title.strip()
    if not company or not job_title:
        return None
    return company, job_title


def _parse_rfc822(value: str | None) -> datetime | None:
    """Parse an RSS `pubDate` (RFC 822) into a UTC-aware datetime.

    Args:
        value: The item's `pubDate`, or `None` if it carries none.

    Returns:
        The timestamp in UTC, or `None` if there is none to read, or `None`
        with a logged warning if there was one and it could not be read —
        a posting silently losing its date leaves nothing to investigate
        with the day the feed changes format.
    """
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        logger.warning(
            "We Work Remotely: could not parse the pubDate %r; the posting is kept "
            "without a published date.",
            value,
        )
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _eligible_locations_from_region(region: str | None) -> list[str]:
    """Turn a We Work Remotely `<region>` value into an eligibility list.

    "Anywhere in the world"-style values mean no restriction and are
    reported as an empty list, matching `JobPosting.eligible_locations`'
    "not detected/none" convention.
    """
    if not region:
        return []
    stripped = region.strip()
    if stripped.lower() in _UNRESTRICTED_REGION_MARKERS:
        return []
    return [stripped]


@register_source("weworkremotely")
class WeWorkRemotelySource(Source):
    """Job source backed by We Work Remotely's public RSS feeds."""

    def __init__(self, name: str, config: PluginConfig, **kwargs: Any) -> None:
        """Initialize the source, validating We Work Remotely-specific config.

        Args:
            name: The source's registered name (see `Source.__init__`).
            config: This source's configuration block; re-validated into
                `WeWorkRemotelySourceConfig`.
            **kwargs: Passed through to `Source.__init__` (`client`,
                `timeout`, `max_retries`, `backoff_base`,
                `min_request_interval`); the pipeline fills these from the
                same config block. Set a non-zero `min_request_interval`
                when enabling `fetch_full_description` (see the module
                docstring).

        Raises:
            SourceConfigError: If `config` doesn't validate as
                `WeWorkRemotelySourceConfig` (e.g. an unknown key).
        """
        super().__init__(name, config, **kwargs)
        try:
            self._wwr_config = WeWorkRemotelySourceConfig.model_validate(config.model_dump())
        except ValidationError as exc:
            raise SourceConfigError(f"Invalid configuration for source {name!r}: {exc}") from exc

    def fetch_raw(self) -> Iterator[dict[str, Any]]:
        """Yield raw item dicts for every configured feed, in order.

        Each configured feed is an independent unit (see the module
        docstring): a feed whose request fails, or whose response isn't
        parsable XML, is recorded via `Source._record_error` and skipped —
        it does not raise, and does not stop the remaining feeds from being
        tried.
        """
        for feed_name in self._wwr_config.feeds:
            url = _feed_url(feed_name)
            try:
                response = self._request("GET", url)
            except SourceConfigError as exc:
                self._record_error(
                    f"We Work Remotely feed {feed_name!r} "
                    f"(from sources.weworkremotely.feeds) looks wrong: {exc}"
                )
                continue
            except SourceUnavailableError as exc:
                self._record_error(
                    f"We Work Remotely feed {feed_name!r} "
                    f"(from sources.weworkremotely.feeds) is unavailable: {exc}"
                )
                continue

            try:
                items = _parse_rss_items(response.text)
            except ET.ParseError as exc:
                self._record_error(
                    f"We Work Remotely feed {feed_name!r} "
                    f"(from sources.weworkremotely.feeds) returned unparsable XML: {exc}"
                )
                continue

            limit = self._wwr_config.max_postings_per_feed
            for item in items[:limit] if limit is not None else items:
                yield {**item, "_feed_name": feed_name}

    def normalize(self, raw: dict[str, Any]) -> JobPosting:
        """Convert one raw RSS item dict into a `JobPosting`.

        Args:
            raw: One item dict as yielded by `fetch_raw`.

        Returns:
            The normalized posting.

        Raises:
            KeyError: If `title` or `link` is missing.
            ValueError: If `title` isn't in We Work Remotely's
                ``"Company: Job Title"`` format.
        """
        missing = [key for key in ("title", "link") if not raw.get(key)]
        if missing:
            raise KeyError(f"We Work Remotely item is missing required field(s): {missing}")

        split = _split_company_and_title(raw["title"])
        if split is None:
            raise ValueError(
                f"We Work Remotely item title {raw['title']!r} is not in "
                "the expected 'Company: Job Title' format"
            )
        company, job_title = split

        description_raw = raw.get("description") or ""
        if self._wwr_config.fetch_full_description:
            full_description = self._fetch_full_description(raw["link"])
            if full_description is not None:
                description_raw = full_description
        description_clean = strip_html(description_raw)

        region = raw.get("region")

        return JobPosting(
            url=raw["link"],
            raw_url=raw["link"],
            title=job_title,
            company=company,
            source=self.name,
            description_raw=description_raw,
            description_clean=description_clean,
            location=region or None,
            work_mode=WorkMode.REMOTE,
            published_at=_parse_rfc822(raw.get("pubDate")),
            fetched_at=datetime.now(UTC),
            eligible_locations=_eligible_locations_from_region(region),
        )

    def _fetch_full_description(self, url: str) -> str | None:
        """Best-effort fetch of a posting's full description from its page.

        Returns `None` (rather than raising) on any failure — a transient
        network issue, or the page no longer having a
        `_DETAIL_DESCRIPTION_CONTAINER_CLASS` container — so `normalize` can
        fall back to the RSS-supplied truncated description instead of
        losing the posting entirely.
        """
        try:
            response = self._request("GET", url)
        except SourceError as exc:
            logger.warning(
                "We Work Remotely: could not fetch the full description from %s (%s); "
                "keeping the RSS summary",
                url,
                exc,
            )
            return None

        html_content = _extract_listing_container_html(response.text)
        if html_content is None:
            logger.warning(
                "We Work Remotely: could not find a %r container at %s "
                "(the page layout may have changed); keeping the RSS summary",
                _DETAIL_DESCRIPTION_CONTAINER_CLASS,
                url,
            )
            return None
        return html_content
