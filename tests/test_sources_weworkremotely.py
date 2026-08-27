"""Tests for jobsearcher.sources.weworkremotely. No real HTTP call is made:
httpx is driven entirely through httpx.MockTransport."""

from __future__ import annotations

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
