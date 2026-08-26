"""Tests for jobsearcher.models."""

from datetime import UTC, datetime, timezone

import pytest
from pydantic import ValidationError

from jobsearcher.models import ApplicationStatus, JobPosting, WorkMode, normalize_job_url

FETCHED_AT = datetime(2026, 8, 1, tzinfo=UTC)


def _make_posting(**overrides: object) -> JobPosting:
    fields: dict[str, object] = {
        "url": "https://example.com/jobs/123",
        "title": "Backend Engineer",
        "company": "Acme",
        "source": "linkedin",
        "description_raw": "<p>Join us</p>",
        "fetched_at": FETCHED_AT,
    }
    fields.update(overrides)
    return JobPosting(**fields)  # type: ignore[arg-type]


def test_minimal_valid_posting_has_expected_defaults() -> None:
    posting = _make_posting()

    assert posting.work_mode is WorkMode.UNKNOWN
    assert posting.status is ApplicationStatus.NEW
    assert posting.eligible_locations == []
    assert posting.matched_skills == []
    assert posting.missing_skills == []
    assert posting.score is None


def test_raw_url_defaults_to_the_url_the_caller_supplied() -> None:
    posting = _make_posting(url="http://Example.com/Jobs/123/?utm_source=twitter")

    assert posting.raw_url == "http://Example.com/Jobs/123/?utm_source=twitter"
    assert posting.url == "https://example.com/Jobs/123"


def test_raw_url_can_be_set_explicitly() -> None:
    posting = _make_posting(url="https://example.com/jobs/123", raw_url="https://short.link/abc")

    assert posting.raw_url == "https://short.link/abc"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("HTTPS://Example.COM/jobs/123", "https://example.com/jobs/123"),
        ("http://example.com/jobs/123", "https://example.com/jobs/123"),
        ("https://example.com/jobs/123/", "https://example.com/jobs/123"),
        ("https://example.com/", "https://example.com/"),
        ("https://example.com/jobs/123#apply", "https://example.com/jobs/123"),
    ],
)
def test_url_normalization_cases(raw: str, expected: str) -> None:
    assert normalize_job_url(raw) == expected


def test_url_tracking_params_are_stripped() -> None:
    without_tracking = normalize_job_url("https://example.com/jobs/123?id=42")
    with_tracking = normalize_job_url(
        "https://example.com/jobs/123?id=42&utm_source=newsletter&fbclid=xyz"
    )

    assert with_tracking == without_tracking


def test_url_query_param_order_does_not_matter() -> None:
    first = normalize_job_url("https://example.com/jobs?a=1&b=2")
    second = normalize_job_url("https://example.com/jobs?b=2&a=1")

    assert first == second


@pytest.mark.parametrize("param_name", ["gh_jid", "id"])
def test_url_significant_params_are_kept(param_name: str) -> None:
    normalized = normalize_job_url(f"https://example.com/jobs?{param_name}=42")

    assert f"{param_name}=42" in normalized


def test_url_empty_value_params_are_dropped() -> None:
    normalized = normalize_job_url("https://example.com/jobs?id=42&ref=")

    assert "ref=" not in normalized
    assert "id=42" in normalized


def test_url_without_scheme_or_host_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _make_posting(url="not-a-url")


@pytest.mark.parametrize("field", ["title", "company", "source"])
def test_blank_identity_fields_are_rejected(field: str) -> None:
    with pytest.raises(ValidationError):
        _make_posting(**{field: "   "})


def test_naive_datetime_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _make_posting(fetched_at=datetime(2026, 8, 1))


def test_datetime_is_normalized_to_utc() -> None:
    from datetime import timedelta

    plus_two = timezone(timedelta(hours=2))
    posting = _make_posting(fetched_at=datetime(2026, 8, 1, 14, 0, tzinfo=plus_two))

    assert posting.fetched_at == datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
    assert posting.fetched_at.tzinfo is UTC


@pytest.mark.parametrize("score", [-1, 101])
def test_score_out_of_bounds_is_rejected(score: int) -> None:
    with pytest.raises(ValidationError):
        _make_posting(score=score)


@pytest.mark.parametrize("score", [0, 50, 100])
def test_score_within_bounds_is_accepted(score: int) -> None:
    posting = _make_posting(score=score)

    assert posting.score == score
