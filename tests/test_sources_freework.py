"""Tests for jobsearcher.sources.freework. No real HTTP call is made:
httpx is driven entirely through httpx.MockTransport."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from jobsearcher.config import PluginConfig
from jobsearcher.models import WorkMode
from jobsearcher.sources.base import SourceConfigError
from jobsearcher.sources.freework import FreeworkSource

FIXTURES_DIR = Path(__file__).parent / "fixtures"
SEARCH_HTML = (FIXTURES_DIR / "freework_search.html").read_text(encoding="utf-8")
NO_RESULTS_HTML = (FIXTURES_DIR / "freework_no_results.html").read_text(encoding="utf-8")
CHANGED_LAYOUT_HTML = (FIXTURES_DIR / "freework_changed_layout.html").read_text(encoding="utf-8")


def _source(
    handler: Any, *, keywords: list[str] | None = None, **extra_config: Any
) -> FreeworkSource:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    config = PluginConfig(keywords=keywords or ["python django"], **extra_config)
    return FreeworkSource("freework", config, client=client, backoff_base=0.01)


def _search_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, text=SEARCH_HTML)


def test_fetch_yields_valid_cards_and_skips_the_malformed_one() -> None:
    source = _source(_search_handler)

    postings = list(source.fetch())

    assert len(postings) == 2
    assert source.last_run.yielded == 2
    assert source.last_run.skipped == 1
    assert source.last_run.failed is False
    assert source.last_run.errors == []


def test_normalize_extracts_all_fields_and_resolves_a_relative_url() -> None:
    source = _source(_search_handler)
    raw = {
        "title": "Développeur Backend Django Freelance",
        "url": "https://www.free-work.com/fr/tech-it/mission/django-backend-freelance",
        "company": "ExampleCorp",
        "location": "Paris - Télétravail partiel",
        "contract_type": "Freelance",
        "salary_text": "500-600€/jour",
        "description": "Mission de 6 mois pour renforcer l'équipe backend.",
    }

    posting = source.normalize(raw)

    assert posting.title == "Développeur Backend Django Freelance"
    assert posting.company == "ExampleCorp"
    assert posting.location == "Paris - Télétravail partiel"
    assert posting.work_mode == WorkMode.HYBRID
    assert posting.contract_type == "Freelance"
    assert posting.salary_text == "500-600€/jour"
    assert "6 mois" in (posting.description_clean or "")


def test_relative_card_link_is_resolved_against_the_response_url() -> None:
    source = _source(_search_handler)

    raws = list(source.fetch_raw())

    django_card = next(raw for raw in raws if "Django" in raw.get("title", ""))
    assert django_card["url"] == (
        "https://www.free-work.com/fr/tech-it/mission/django-backend-freelance"
    )


def test_normalize_detects_full_remote_and_defaults_missing_company() -> None:
    source = _source(_search_handler)
    raw = {
        "title": "Ingénieur Python Full Remote",
        "url": "https://www.free-work.com/fr/tech-it/mission/full-remote-python",
        "location": "France - 100% télétravail",
        "contract_type": "Freelance",
    }

    posting = source.normalize(raw)

    assert posting.work_mode == WorkMode.REMOTE
    assert posting.company == "Unknown"


def test_normalize_raises_key_error_when_title_or_url_is_missing() -> None:
    source = _source(_search_handler)
    raw = {"company": "BrokenCardCo", "location": "Lyon"}

    with pytest.raises(KeyError, match="layout has probably changed"):
        source.normalize(raw)


def test_no_results_marker_is_a_genuine_empty_result_not_a_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=NO_RESULTS_HTML)

    source = _source(handler)

    postings = list(source.fetch())

    assert postings == []
    assert source.last_run.yielded == 0
    assert source.last_run.failed is False
    assert source.last_run.errors == []


def test_changed_layout_is_recorded_as_a_unit_failure_with_an_explicit_message() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=CHANGED_LAYOUT_HTML)

    source = _source(handler)

    postings = list(source.fetch())

    assert postings == []
    assert source.last_run.failed is True
    assert len(source.last_run.errors) == 1
    assert "layout has probably changed" in source.last_run.errors[0]
    assert "python django" in source.last_run.errors[0]
    assert "sources.freework.keywords" in source.last_run.errors[0]


def test_one_bad_keyword_among_several_does_not_abort_the_others() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "broken" in str(request.url):
            return httpx.Response(200, text=CHANGED_LAYOUT_HTML)
        return httpx.Response(200, text=SEARCH_HTML)

    source = _source(handler, keywords=["python django", "broken keyword"])

    postings = list(source.fetch())

    assert len(postings) == 2
    assert source.last_run.failed is False
    assert len(source.last_run.errors) == 1
    assert "broken keyword" in source.last_run.errors[0]


def test_every_keyword_failing_marks_the_source_failed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=CHANGED_LAYOUT_HTML)

    source = _source(handler, keywords=["one", "two"])

    postings = list(source.fetch())

    assert postings == []
    assert source.last_run.failed is True
    assert len(source.last_run.errors) == 2


def test_a_keyword_that_404s_is_a_config_error_and_marks_the_source_failed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    source = _source(handler, keywords=["this-keyword-404s"])

    postings = list(source.fetch())

    assert postings == []
    assert source.last_run.failed is True
    assert len(source.last_run.errors) == 1
    assert "this-keyword-404s" in source.last_run.errors[0]
    assert "sources.freework.keywords" in source.last_run.errors[0]


def test_max_postings_per_keyword_limits_fetched_cards() -> None:
    source = _source(_search_handler, max_postings_per_keyword=1)

    raws = list(source.fetch_raw())

    assert len(raws) == 1


def test_unknown_config_key_is_rejected_at_construction() -> None:
    client = httpx.Client(transport=httpx.MockTransport(_search_handler))
    config = PluginConfig(keyword=["python django"])  # typo: keyword instead of keywords

    with pytest.raises(SourceConfigError):
        FreeworkSource("freework", config, client=client)


def test_missing_keywords_is_rejected_at_construction() -> None:
    client = httpx.Client(transport=httpx.MockTransport(_search_handler))
    config = PluginConfig()

    with pytest.raises(SourceConfigError):
        FreeworkSource("freework", config, client=client)
