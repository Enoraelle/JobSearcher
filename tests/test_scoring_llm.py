"""Tests for the optional LLM scorer, with a fully mocked HTTP client.

No test here performs real network I/O (see CLAUDE.md): every request is
served by ``_FakeClient``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from jobsearcher.config import BackendSectionConfig, ProfileConfig
from jobsearcher.models import JobPosting, WorkMode
from jobsearcher.scoring.llm import (
    BudgetExhaustedError,
    LlmScorer,
    LlmScoringError,
    _extract_json_object,
)

_KEY_ENV = "OPENAI_API_KEY"


@pytest.fixture(autouse=True)
def _clear_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_KEY_ENV, raising=False)


def _with_key(monkeypatch: pytest.MonkeyPatch, value: str = "sk-test") -> None:
    monkeypatch.setenv(_KEY_ENV, value)


def _posting(**overrides: Any) -> JobPosting:
    fields: dict[str, Any] = {
        "url": "https://example.com/jobs/1",
        "title": "Backend Engineer",
        "company": "Acme",
        "source": "example",
        "description_raw": "We need Python and Django.",
        "fetched_at": datetime(2026, 1, 1, tzinfo=UTC),
    }
    fields.update(overrides)
    return JobPosting(**fields)


def _profile(**overrides: Any) -> ProfileConfig:
    fields: dict[str, Any] = {"role": "Backend Developer", "skills": ["python", "django"]}
    fields.update(overrides)
    return ProfileConfig(**fields)


def _completion(content: str) -> _FakeResponse:
    return _FakeResponse(json_data={"choices": [{"message": {"content": content}}]})


def _verdict_json(**overrides: Any) -> str:
    body = {
        "score": 82,
        "summary": "Strong fit.",
        "matched_skills": ["python"],
        "missing_requirements": ["kubernetes"],
        "penalized_skills": [],
    }
    body.update(overrides)
    return json.dumps(body)


class _FakeResponse:
    def __init__(
        self, *, json_data: dict[str, Any] | None = None, status_error: Exception | None = None
    ) -> None:
        self._json_data = json_data or {}
        self._status_error = status_error

    def raise_for_status(self) -> None:
        if self._status_error is not None:
            raise self._status_error

    def json(self) -> dict[str, Any]:
        return self._json_data


class _FakeClient:
    """Serves a scripted list of responses (or exceptions) to ``.post``."""

    def __init__(self, *responses: Any) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def post(self, url: str, *, headers: dict[str, str], json: dict[str, Any]) -> _FakeResponse:
        self.calls.append({"url": url, "headers": headers, "json": json})
        nxt = self._responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        assert isinstance(nxt, _FakeResponse)
        return nxt

    def close(self) -> None:  # pragma: no cover - not exercised, scorer does not own us
        pass


def _scorer(client: _FakeClient, **config: Any) -> LlmScorer:
    return LlmScorer(BackendSectionConfig(backend="llm", **config), client=client)


# --------------------------------------------------------------------------
# API key handling
# --------------------------------------------------------------------------


def test_missing_api_key_raises_a_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(LlmScoringError, match=_KEY_ENV):
        _scorer(_FakeClient())


def test_api_key_is_read_from_the_environment_not_config(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_key(monkeypatch, "sk-secret-123")
    client = _FakeClient(_completion(_verdict_json()))

    _scorer(client).score(_posting(), _profile())

    assert client.calls[0]["headers"]["Authorization"] == "Bearer sk-secret-123"


def test_putting_an_api_key_in_config_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_key(monkeypatch)
    with pytest.raises(ValueError, match=r"[Ii]nvalid"):
        _scorer(_FakeClient(), api_key="sk-in-config")


def test_custom_key_env_name_is_honoured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_LLM_KEY", "sk-alt")
    client = _FakeClient(_completion(_verdict_json()))

    _scorer(client, api_key_env="MY_LLM_KEY").score(_posting(), _profile())

    assert client.calls[0]["headers"]["Authorization"] == "Bearer sk-alt"


# --------------------------------------------------------------------------
# Happy path
# --------------------------------------------------------------------------


def test_valid_response_becomes_a_score_result(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_key(monkeypatch)
    client = _FakeClient(_completion(_verdict_json()))
    profile = _profile(
        skills=["python", "django", "go"],
        locations=["France"],
        work_mode=WorkMode.REMOTE,
    )
    posting = _posting(location="Paris, France", work_mode=WorkMode.REMOTE)

    result = _scorer(client).score(posting, profile)

    assert result.score == 82
    assert result.summary == "Strong fit."
    assert result.matched_skills == ["python"]
    assert result.missing_requirements == ["kubernetes"]
    # unmatched_profile_skills is derived: profile skills the model didn't list.
    assert result.unmatched_profile_skills == ["django", "go"]
    # Eligibility comes from the shared helpers, not the model.
    assert result.location_match is True
    assert result.work_mode_match is True


def test_request_targets_an_openai_compatible_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_key(monkeypatch)
    client = _FakeClient(_completion(_verdict_json()))

    _scorer(client, base_url="https://llm.internal/v1/").score(_posting(), _profile())

    assert client.calls[0]["url"] == "https://llm.internal/v1/chat/completions"
    assert client.calls[0]["json"]["model"] == "gpt-4o-mini"


def test_fenced_json_reply_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_key(monkeypatch)
    fenced = f"```json\n{_verdict_json()}\n```"
    client = _FakeClient(_completion(fenced))

    result = _scorer(client).score(_posting(), _profile())

    assert result.score == 82


# --------------------------------------------------------------------------
# Invalid replies and retry
# --------------------------------------------------------------------------


def test_one_bad_reply_then_a_good_one_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_key(monkeypatch)
    client = _FakeClient(_completion("not json at all"), _completion(_verdict_json()))

    result = _scorer(client).score(_posting(), _profile())

    assert result.score == 82
    assert len(client.calls) == 2


def test_two_bad_replies_give_up_and_leave_the_posting_unscored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _with_key(monkeypatch)
    client = _FakeClient(_completion("garbage"), _completion("still garbage"))
    posting = _posting()

    with pytest.raises(LlmScoringError):
        _scorer(client).score(posting, _profile())

    assert len(client.calls) == 2
    assert posting.score is None


def test_schema_violation_in_reply_is_treated_as_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_key(monkeypatch)
    out_of_range = _verdict_json(score=250)
    client = _FakeClient(_completion(out_of_range), _completion(out_of_range))

    with pytest.raises(LlmScoringError):
        _scorer(client).score(_posting(), _profile())


def test_http_error_is_retried_then_surfaced(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_key(monkeypatch)
    client = _FakeClient(
        _FakeResponse(status_error=httpx.HTTPError("503")),
        _FakeResponse(status_error=httpx.HTTPError("503")),
    )

    with pytest.raises(LlmScoringError):
        _scorer(client).score(_posting(), _profile())

    assert len(client.calls) == 2


# --------------------------------------------------------------------------
# Budget
# --------------------------------------------------------------------------


def test_budget_caps_the_number_of_postings_scored(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_key(monkeypatch)
    client = _FakeClient(_completion(_verdict_json()), _completion(_verdict_json()))
    scorer = _scorer(client, max_postings_per_run=1)

    scorer.score(_posting(url="https://example.com/jobs/a"), _profile())
    with pytest.raises(BudgetExhaustedError):
        scorer.score(_posting(url="https://example.com/jobs/b"), _profile())

    assert len(client.calls) == 1


def test_a_failed_call_still_counts_against_the_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_key(monkeypatch)
    client = _FakeClient(_completion("bad"), _completion("bad"))
    scorer = _scorer(client, max_postings_per_run=1)

    with pytest.raises(LlmScoringError):
        scorer.score(_posting(url="https://example.com/jobs/a"), _profile())
    with pytest.raises(BudgetExhaustedError):
        scorer.score(_posting(url="https://example.com/jobs/b"), _profile())


def test_zero_budget_scores_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_key(monkeypatch)
    client = _FakeClient()

    with pytest.raises(BudgetExhaustedError):
        _scorer(client, max_postings_per_run=0).score(_posting(), _profile())


# --------------------------------------------------------------------------
# Lifecycle
# --------------------------------------------------------------------------


def test_context_manager_closes_an_owned_client(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_key(monkeypatch)
    # No client passed: the scorer creates and owns one (no network happens
    # at construction), and closing it on exit must not raise.
    with LlmScorer(BackendSectionConfig(backend="llm")) as scorer:
        assert scorer is not None
    scorer.close()  # idempotent


# --------------------------------------------------------------------------
# Helper
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('{"a": 1}', '{"a": 1}'),
        ('```json\n{"a": 1}\n```', '{"a": 1}'),
        ('```\n{"a": 1}\n```', '{"a": 1}'),
        ('  {"a": 1}  ', '{"a": 1}'),
    ],
)
def test_extract_json_object(raw: str, expected: str) -> None:
    assert _extract_json_object(raw) == expected
