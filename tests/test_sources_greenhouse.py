"""Tests for jobsearcher.sources.greenhouse. No real HTTP call is made:
httpx is driven entirely through httpx.MockTransport."""

from __future__ import annotations

import html
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from jobsearcher.config import PluginConfig
from jobsearcher.models import WorkMode
from jobsearcher.sources.base import SourceConfigError
from jobsearcher.sources.greenhouse import GreenhouseSource

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "greenhouse_jobs.json"
FIXTURE_JOBS = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _source(
    handler: Any,
    *,
    companies: list[str] | None = None,
    **extra_config: Any,
) -> GreenhouseSource:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    config = PluginConfig(companies=companies or ["examplecorp"], **extra_config)
    return GreenhouseSource("greenhouse", config, client=client, backoff_base=0.01)


def _fixture_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json=FIXTURE_JOBS)


def test_fetch_yields_valid_jobs_and_skips_the_malformed_one() -> None:
    source = _source(_fixture_handler)

    postings = list(source.fetch())

    assert len(postings) == 2
    assert source.last_run.yielded == 2
    assert source.last_run.skipped == 1
    assert source.last_run.failed is False
    assert source.last_run.errors == []


def test_normalize_extracts_company_location_work_mode_and_description() -> None:
    source = _source(_fixture_handler)
    raw = {**FIXTURE_JOBS["jobs"][0], "_company_slug": "examplecorp"}

    posting = source.normalize(raw)

    assert posting.company == "Examplecorp"
    assert posting.title == "Senior Backend Engineer"
    assert posting.location == "Remote - US"
    assert posting.work_mode == WorkMode.REMOTE
    assert "Requirements" in posting.description_clean
    assert "<p>" not in posting.description_clean
    assert "US" in posting.eligible_locations
    assert any("United States" in loc for loc in posting.eligible_locations)
    assert posting.published_at is not None


def test_normalize_detects_a_hybrid_restriction_from_the_description() -> None:
    source = _source(_fixture_handler)
    raw = {**FIXTURE_JOBS["jobs"][2], "_company_slug": "examplecorp"}

    posting = source.normalize(raw)

    assert posting.work_mode == WorkMode.HYBRID
    assert any("Germany" in loc for loc in posting.eligible_locations)


def test_normalize_raises_key_error_on_missing_required_field() -> None:
    source = _source(_fixture_handler)
    raw = {**FIXTURE_JOBS["jobs"][1], "_company_slug": "examplecorp"}

    with pytest.raises(KeyError):
        source.normalize(raw)


def test_fetch_raw_attaches_company_slug_per_job() -> None:
    source = _source(_fixture_handler, companies=["examplecorp"])

    raws = list(source.fetch_raw())

    assert all(raw["_company_slug"] == "examplecorp" for raw in raws)


def test_max_postings_per_company_limits_fetched_jobs() -> None:
    source = _source(_fixture_handler, max_postings_per_company=1)

    raws = list(source.fetch_raw())

    assert len(raws) == 1


def test_a_single_bad_slug_is_a_config_error_and_marks_the_source_failed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    source = _source(handler, companies=["this-company-does-not-exist"])

    postings = list(source.fetch())

    assert postings == []
    assert source.last_run.yielded == 0
    assert source.last_run.failed is True
    assert len(source.last_run.errors) == 1
    assert "this-company-does-not-exist" in source.last_run.errors[0]
    assert "sources.greenhouse.companies" in source.last_run.errors[0]


def test_one_bad_slug_among_several_does_not_abort_the_others() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.startswith("/v1/boards/")
        slug = request.url.path.split("/")[3]
        if slug == "bad-company":
            return httpx.Response(404)
        return httpx.Response(200, json=FIXTURE_JOBS)

    source = _source(handler, companies=["good-one", "bad-company", "good-two"])

    postings = list(source.fetch())

    # 2 valid jobs from each of the two good companies (the fixture's third
    # job is dropped for missing "title", per test_fetch_yields_valid_jobs...).
    assert len(postings) == 4
    assert source.last_run.failed is False
    assert len(source.last_run.errors) == 1
    assert "bad-company" in source.last_run.errors[0]
    assert "sources.greenhouse.companies" in source.last_run.errors[0]


def test_every_slug_failing_marks_the_source_failed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    source = _source(handler, companies=["bad-one", "bad-two", "bad-three"])

    postings = list(source.fetch())

    assert postings == []
    assert source.last_run.failed is True
    assert len(source.last_run.errors) == 3


def test_malformed_top_level_response_is_recorded_as_a_unit_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": "shape"})

    source = _source(handler)

    postings = list(source.fetch())

    assert postings == []
    assert source.last_run.failed is True
    assert len(source.last_run.errors) == 1


def test_invalid_json_response_is_recorded_as_a_unit_failure_not_raised() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json")

    source = _source(handler)

    raws = list(source.fetch_raw())

    assert raws == []
    assert len(source.last_run.errors) == 1


def test_unknown_config_key_is_rejected_at_construction() -> None:
    client = httpx.Client(transport=httpx.MockTransport(_fixture_handler))
    config = PluginConfig(companys=["examplecorp"])  # typo: companys instead of companies

    with pytest.raises(SourceConfigError):
        GreenhouseSource("greenhouse", config, client=client)


def test_missing_companies_is_rejected_at_construction() -> None:
    client = httpx.Client(transport=httpx.MockTransport(_fixture_handler))
    config = PluginConfig()

    with pytest.raises(SourceConfigError):
        GreenhouseSource("greenhouse", config, client=client)


def test_normalize_decodes_the_html_escaped_content_field() -> None:
    """The Job Board API returns `content` HTML-escaped (`&lt;p&gt;`, not `<p>`).

    Decoding it is what separates readable text from markup pasted verbatim
    into the score haystack, the language heuristic, and every export.
    """
    source = _source(_fixture_handler)
    raw = {**FIXTURE_JOBS["jobs"][0], "_company_slug": "examplecorp"}

    posting = source.normalize(raw)

    # The fixture is escaped, exactly like the real API.
    assert "&lt;p&gt;" in raw["content"]

    clean = posting.description_clean
    assert clean is not None
    assert "<p>" not in clean
    assert "&lt;" not in clean
    assert "&gt;" not in clean
    # Tag names must not survive as words: they would score as skills.
    assert "strong" not in clean.split()
    assert clean.startswith("We build the platform that powers our product.")
    assert "5+ years of Python" in clean
    # Block elements still become line breaks rather than running together.
    assert "product.Requirements" not in clean


def test_escaped_entities_inside_the_text_are_decoded_once() -> None:
    """`&amp;` in the posting's prose survives as `&`, not as `&amp;`."""
    source = _source(_fixture_handler)
    raw = {
        "absolute_url": "https://boards.greenhouse.io/examplecorp/jobs/9",
        "title": "Research &amp; Development Engineer",
        "_company_slug": "examplecorp",
        "content": "&lt;p&gt;Join R&amp;amp;D: Python &amp;amp; Django.&lt;/p&gt;",
    }

    posting = source.normalize(raw)

    assert posting.description_clean == "Join R&D: Python & Django."


def test_a_timestamp_without_a_timezone_does_not_cost_the_posting() -> None:
    """`updated_at` is optional metadata; it must not be able to drop a job.

    A naive datetime fails JobPosting's timezone validator, which turns a
    secondary field into a ValidationError, which `fetch` counts as a
    malformed item and skips. The posting disappears over a missing offset.
    """
    source = _source(_fixture_handler)
    raw = {
        "absolute_url": "https://boards.greenhouse.io/examplecorp/jobs/7",
        "title": "Backend Engineer",
        "_company_slug": "examplecorp",
        "content": "&lt;p&gt;Python and Django.&lt;/p&gt;",
        "updated_at": "2026-07-15T10:00:00",
    }

    posting = source.normalize(raw)

    assert posting.published_at is not None
    assert posting.published_at.tzinfo is not None
    assert posting.published_at == datetime(2026, 7, 15, 10, 0, tzinfo=UTC)


def test_an_unparsable_timestamp_leaves_published_at_empty() -> None:
    source = _source(_fixture_handler)
    raw = {
        "absolute_url": "https://boards.greenhouse.io/examplecorp/jobs/8",
        "title": "Backend Engineer",
        "_company_slug": "examplecorp",
        "content": "&lt;p&gt;Python.&lt;/p&gt;",
        "updated_at": "last tuesday",
    }

    posting = source.normalize(raw)

    assert posting.published_at is None


def test_an_offset_timestamp_is_normalized_to_utc() -> None:
    source = _source(_fixture_handler)
    raw = {**FIXTURE_JOBS["jobs"][0], "_company_slug": "examplecorp"}

    posting = source.normalize(raw)

    assert posting.published_at == datetime(2026, 7, 15, 14, 0, tzinfo=UTC)


# --------------------------------------------------------------------------
# Work mode: `--remote` filters on this, so a wrong REMOTE is expensive
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "description",
    [
        "This role is not remote; you will be in the office five days a week.",
        "No remote work is available for this position.",
        "This is a non-remote role.",
        "We are an onsite team, without remote options.",
    ],
)
def test_a_negated_mention_does_not_make_a_posting_remote(description: str) -> None:
    source = _source(_fixture_handler)
    raw = {
        "absolute_url": "https://boards.greenhouse.io/examplecorp/jobs/10",
        "title": "Backend Engineer",
        "_company_slug": "examplecorp",
        "location": {"name": "Berlin, Germany"},
        "content": html.escape(f"<p>{description}</p>", quote=False),
    }

    posting = source.normalize(raw)

    assert posting.work_mode is not WorkMode.REMOTE


def test_a_word_containing_remote_does_not_make_a_posting_remote() -> None:
    """Token boundaries: "remotely-managed" is a word, "remoteness" is another."""
    source = _source(_fixture_handler)
    raw = {
        "absolute_url": "https://boards.greenhouse.io/examplecorp/jobs/11",
        "title": "Backend Engineer",
        "_company_slug": "examplecorp",
        "location": {"name": "Berlin, Germany"},
        "content": html.escape("<p>You will maintain remotelog and hybridization tools.</p>"),
    }

    posting = source.normalize(raw)

    assert posting.work_mode is WorkMode.ONSITE


def test_a_plain_mention_still_makes_a_posting_remote() -> None:
    source = _source(_fixture_handler)
    raw = {
        "absolute_url": "https://boards.greenhouse.io/examplecorp/jobs/12",
        "title": "Backend Engineer",
        "_company_slug": "examplecorp",
        "content": html.escape("<p>This is a fully remote position.</p>", quote=False),
    }

    posting = source.normalize(raw)

    assert posting.work_mode is WorkMode.REMOTE


def test_the_location_field_outranks_the_description() -> None:
    """`location.name` is what the board is actually asserting about the job."""
    source = _source(_fixture_handler)
    raw = {
        "absolute_url": "https://boards.greenhouse.io/examplecorp/jobs/13",
        "title": "Backend Engineer",
        "_company_slug": "examplecorp",
        "location": {"name": "Remote - US"},
        "content": html.escape("<p>Our hybrid sister team sits in Berlin.</p>", quote=False),
    }

    posting = source.normalize(raw)

    assert posting.work_mode is WorkMode.REMOTE
