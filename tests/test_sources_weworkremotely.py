"""Tests for jobsearcher.sources.weworkremotely. No real HTTP call is made:
httpx is driven entirely through httpx.MockTransport."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import httpx
import pytest

from jobsearcher.config import PluginConfig
from jobsearcher.models import WorkMode
from jobsearcher.sources.base import SourceConfigError
from jobsearcher.sources.weworkremotely import WeWorkRemotelySource

FIXTURES_DIR = Path(__file__).parent / "fixtures"
MAIN_FEED_XML = (FIXTURES_DIR / "weworkremotely_rss.xml").read_text(encoding="utf-8")
CATEGORY_FEED_XML = (FIXTURES_DIR / "weworkremotely_category_rss.xml").read_text(encoding="utf-8")
DETAIL_HTML = (FIXTURES_DIR / "weworkremotely_detail.html").read_text(encoding="utf-8")


def _source(
    handler: Any, *, feeds: list[str] | None = None, **extra_config: Any
) -> WeWorkRemotelySource:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    config = PluginConfig(**({"feeds": feeds} if feeds is not None else {}), **extra_config)
    return WeWorkRemotelySource("weworkremotely", config, client=client, backoff_base=0.01)


def _main_feed_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, text=MAIN_FEED_XML)


def _rss_with_description(description: str) -> str:
    """A one-item feed whose <description> carries `description` verbatim."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0"><channel><title>WWR</title>'
        "<item>"
        "<title>ExampleCorp: Backend Engineer</title>"
        "<link>https://weworkremotely.com/remote-jobs/malformed-html</link>"
        "<pubDate>Wed, 15 Jul 2026 10:00:00 +0000</pubDate>"
        f"<description><![CDATA[{description}]]></description>"
        "<region>Anywhere in the World</region>"
        "</item></channel></rss>"
    )


def _feed_handler_for(xml_text: str) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=xml_text)

    return handler


def _feed_with_items(count: int, *, pubdate: str = "Wed, 15 Jul 2026 10:00:00 +0000") -> str:
    """A well-formed feed of `count` distinct, valid items."""
    items = "".join(
        f"<item><title>Corp{i}: Engineer {i}</title>"
        f"<link>https://weworkremotely.com/remote-jobs/job-{i}</link>"
        f"<pubDate>{pubdate}</pubDate>"
        "<description><![CDATA[short summary]]></description>"
        "<region>Anywhere in the World</region></item>"
        for i in range(count)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<rss version="2.0"><channel><title>WWR</title>{items}</channel></rss>'
    )


def test_fetch_yields_valid_items_and_skips_the_malformed_ones() -> None:
    source = _source(_main_feed_handler)

    postings = list(source.fetch())

    assert len(postings) == 2
    assert source.last_run.yielded == 2
    assert source.last_run.skipped == 2
    assert source.last_run.failed is False
    assert source.last_run.errors == []


def test_normalize_splits_company_and_title_and_sets_remote_work_mode() -> None:
    source = _source(_main_feed_handler)
    raw = {
        "title": "ExampleCorp: Senior Backend Engineer",
        "link": "https://weworkremotely.com/remote-jobs/examplecorp-senior-backend-engineer",
        "pubDate": "Wed, 15 Jul 2026 10:00:00 +0000",
        "description": "<p>We build the platform that powers our product.</p> ...",
        "region": "USA Only",
        "_feed_name": "remote-jobs",
    }

    posting = source.normalize(raw)

    assert posting.company == "ExampleCorp"
    assert posting.title == "Senior Backend Engineer"
    assert posting.work_mode == WorkMode.REMOTE
    assert posting.location == "USA Only"
    assert posting.eligible_locations == ["USA Only"]
    assert "We build the platform" in posting.description_clean
    assert "<p>" not in posting.description_clean
    assert posting.published_at is not None


def test_normalize_treats_anywhere_in_the_world_as_unrestricted() -> None:
    source = _source(_main_feed_handler)
    raw = {
        "title": "RemoteFirst Inc: Product Designer",
        "link": "https://weworkremotely.com/remote-jobs/remotefirst-product-designer",
        "region": "Anywhere in the World",
        "description": "<p>Design our product end to end.</p>",
    }

    posting = source.normalize(raw)

    assert posting.location == "Anywhere in the World"
    assert posting.eligible_locations == []


def test_normalize_raises_value_error_when_title_has_no_company_separator() -> None:
    source = _source(_main_feed_handler)
    raw = {
        "title": "This title has no colon separator",
        "link": "https://weworkremotely.com/remote-jobs/malformed-title",
    }

    with pytest.raises(ValueError, match="Company: Job Title"):
        source.normalize(raw)


def test_normalize_raises_key_error_on_missing_link() -> None:
    source = _source(_main_feed_handler)
    raw = {"title": "AnotherCo: Missing Link On Purpose"}

    with pytest.raises(KeyError):
        source.normalize(raw)


def test_default_feeds_is_the_main_site_wide_feed() -> None:
    requested_paths = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_paths.append(str(request.url))
        return httpx.Response(200, text=MAIN_FEED_XML)

    source = _source(handler)
    list(source.fetch())

    assert requested_paths == ["https://weworkremotely.com/remote-jobs.rss"]


def test_category_feed_name_resolves_to_the_category_feed_url() -> None:
    requested_paths = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_paths.append(str(request.url))
        return httpx.Response(200, text=CATEGORY_FEED_XML)

    source = _source(handler, feeds=["remote-programming-jobs"])
    postings = list(source.fetch())

    assert requested_paths == ["https://weworkremotely.com/categories/remote-programming-jobs.rss"]
    assert len(postings) == 1
    assert postings[0].company == "CategoryCo"


def test_one_bad_feed_among_several_does_not_abort_the_others() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "bad-category" in str(request.url):
            return httpx.Response(404)
        return httpx.Response(200, text=MAIN_FEED_XML)

    source = _source(handler, feeds=["remote-jobs", "bad-category"])

    postings = list(source.fetch())

    assert len(postings) == 2
    assert source.last_run.failed is False
    assert len(source.last_run.errors) == 1
    assert "bad-category" in source.last_run.errors[0]
    assert "sources.weworkremotely.feeds" in source.last_run.errors[0]


def test_every_feed_failing_marks_the_source_failed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    source = _source(handler, feeds=["bad-one", "bad-two"])

    postings = list(source.fetch())

    assert postings == []
    assert source.last_run.failed is True
    assert len(source.last_run.errors) == 2


def test_unparsable_xml_is_recorded_as_a_unit_failure_not_raised() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="this is not xml <<<")

    source = _source(handler)

    postings = list(source.fetch())

    assert postings == []
    assert source.last_run.failed is True
    assert len(source.last_run.errors) == 1


def test_max_postings_per_feed_limits_fetched_items() -> None:
    source = _source(_main_feed_handler, max_postings_per_feed=1)

    raws = list(source.fetch_raw())

    assert len(raws) == 1


def test_fetch_full_description_defaults_to_off_and_makes_no_extra_request() -> None:
    request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(200, text=MAIN_FEED_XML)

    source = _source(handler)
    postings = list(source.fetch())

    assert request_count == 1
    assert "..." in postings[0].description_raw or "product" in postings[0].description_clean


def test_fetch_full_description_replaces_the_truncated_summary() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith(".rss"):
            return httpx.Response(200, text=MAIN_FEED_XML)
        return httpx.Response(200, text=DETAIL_HTML)

    source = _source(handler, fetch_full_description=True)
    postings = list(source.fetch())

    examplecorp = next(p for p in postings if p.company == "ExampleCorp")
    assert "distributed systems" in examplecorp.description_clean
    assert "nested div" in examplecorp.description_clean
    assert "Site navigation" not in examplecorp.description_clean
    assert "Footer content" not in examplecorp.description_clean


def test_fetch_full_description_falls_back_to_summary_on_detail_fetch_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith(".rss"):
            return httpx.Response(200, text=MAIN_FEED_XML)
        return httpx.Response(404)

    source = _source(handler, fetch_full_description=True)
    postings = list(source.fetch())

    examplecorp = next(p for p in postings if p.company == "ExampleCorp")
    assert "We build the platform" in examplecorp.description_clean


def test_fetch_full_description_falls_back_when_container_not_found() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith(".rss"):
            return httpx.Response(200, text=MAIN_FEED_XML)
        return httpx.Response(200, text="<html><body>No listing container here.</body></html>")

    source = _source(handler, fetch_full_description=True)
    postings = list(source.fetch())

    examplecorp = next(p for p in postings if p.company == "ExampleCorp")
    assert "We build the platform" in examplecorp.description_clean


def test_a_systematic_detail_fetch_failure_logs_once_then_tallies(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Every detail page 403ing must not print one warning per posting."""

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith(".rss"):
            return httpx.Response(200, text=_feed_with_items(5))
        return httpx.Response(403)

    source = _source(handler, fetch_full_description=True)

    with caplog.at_level(logging.WARNING):
        postings = list(source.fetch())

    assert len(postings) == 5
    assert all("short summary" in (p.description_clean or "") for p in postings)

    messages = [record.getMessage() for record in caplog.records]
    detailed = [m for m in messages if "could not fetch the full description" in m]
    tally = [m for m in messages if "full-description fetches failed" in m]
    assert len(detailed) == 1
    assert tally == [
        "Source 'weworkremotely': 5 full-description fetches failed, "
        "kept the RSS summaries (fetch_full_description is off by default "
        "for this reason)"
    ]


def test_a_feed_wide_unparsable_pubdate_logs_once_then_tallies(
    caplog: pytest.LogCaptureFixture,
) -> None:
    source = _source(_feed_handler_for(_feed_with_items(4, pubdate="not a date")))

    with caplog.at_level(logging.WARNING):
        postings = list(source.fetch())

    assert len(postings) == 4
    assert all(p.published_at is None for p in postings)

    messages = [record.getMessage() for record in caplog.records]
    first = [m for m in messages if "could not parse the pubDate 'not a date'" in m]
    tally = [m for m in messages if "unparsable pubDate and were kept" in m]
    assert len(first) == 1
    assert tally == [
        "Source 'weworkremotely': 4 postings had an unparsable pubDate "
        "and were kept without a published date"
    ]


def test_unknown_config_key_is_rejected_at_construction() -> None:
    client = httpx.Client(transport=httpx.MockTransport(_main_feed_handler))
    config = PluginConfig(feed=["remote-jobs"])  # typo: feed instead of feeds

    with pytest.raises(SourceConfigError):
        WeWorkRemotelySource("weworkremotely", config, client=client)


def test_empty_feeds_list_is_rejected_at_construction() -> None:
    client = httpx.Client(transport=httpx.MockTransport(_main_feed_handler))
    config = PluginConfig(feeds=[])

    with pytest.raises(SourceConfigError):
        WeWorkRemotelySource("weworkremotely", config, client=client)


# --------------------------------------------------------------------------
# The RSS description is arbitrary HTML from a third party
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "description",
    [
        "<p>Unclosed paragraph with <strong>bold text",
        "<div><p>Nested <div>divs</div> left open",
        "<ul><li>One<li>Two<li>Three",  # implicit list-item closes
        "Plain text with a stray < angle bracket",
        "<p>Entities: caf&eacute; &amp; co&nbsp;</p>",
        "<script>alert(1)</script><p>After the script.</p>",
    ],
)
def test_a_malformed_rss_description_still_yields_the_posting(description: str) -> None:
    """Markup this source does not control must degrade, never drop a job."""
    source = _source(_feed_handler_for(_rss_with_description(description)))

    postings = list(source.fetch())

    assert len(postings) == 1
    assert source.last_run.skipped == 0
    assert source.last_run.failed is False
    assert postings[0].description_clean is not None
    assert "<p>" not in postings[0].description_clean


def test_a_description_that_is_only_markup_leaves_clean_text_empty() -> None:
    source = _source(_feed_handler_for(_rss_with_description("<p></p><div></div>")))

    postings = list(source.fetch())

    assert len(postings) == 1
    assert postings[0].description_clean == ""


def test_html_entities_in_the_description_are_decoded() -> None:
    source = _source(_feed_handler_for(_rss_with_description("<p>R&amp;D in caf&eacute;s</p>")))

    postings = list(source.fetch())

    assert postings[0].description_clean == "R&D in cafés"


def test_http_policy_options_are_part_of_the_source_configuration() -> None:
    """The docstring tells callers to pace this source; the key must exist.

    `min_request_interval` has to survive the source's own `extra="forbid"`
    re-validation, or the only cure for the `fetch_full_description`
    fan-out is a config file that refuses to load.
    """
    source = WeWorkRemotelySource(
        "weworkremotely",
        PluginConfig(
            fetch_full_description=True,
            min_request_interval=1.5,
            max_retries=1,
            backoff_base=0.01,
            timeout=2.5,
        ),
    )
    with source:
        assert source._min_request_interval == 1.5


def test_an_unparsable_pubdate_is_logged_not_swallowed(caplog: pytest.LogCaptureFixture) -> None:
    source = _source(_main_feed_handler)
    raw = {
        "title": "Acme: Backend Engineer",
        "link": "https://weworkremotely.com/remote-jobs/acme-backend-engineer",
        "pubDate": "last tuesday",
    }

    with caplog.at_level(logging.WARNING):
        posting = source.normalize(raw)

    assert posting.published_at is None
    assert any("last tuesday" in record.getMessage() for record in caplog.records)
