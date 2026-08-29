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
    LlmConfigError,
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


def _status_error(status: int) -> _FakeResponse:
    """A response whose ``raise_for_status`` raises the given HTTP status."""
    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    return _FakeResponse(
        status_error=httpx.HTTPStatusError(
            f"{status}", request=request, response=httpx.Response(status, request=request)
        )
    )


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
    # A config failure under the scorer contract, so the pipeline reports it
    # instead of letting a traceback out mid-run.
    with pytest.raises(LlmConfigError, match=_KEY_ENV):
        _scorer(_FakeClient())


def test_api_key_is_read_from_the_environment_not_config(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_key(monkeypatch, "sk-secret-123")
    client = _FakeClient(_completion(_verdict_json()))

    _scorer(client).score(_posting(), _profile())

    assert client.calls[0]["headers"]["Authorization"] == "Bearer sk-secret-123"


def test_putting_an_api_key_in_config_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_key(monkeypatch)
    with pytest.raises(LlmConfigError, match=r"[Ii]nvalid"):
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


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_a_transient_http_error_is_retried_then_surfaced(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    _with_key(monkeypatch)
    client = _FakeClient(_status_error(status), _status_error(status))

    with pytest.raises(LlmScoringError):
        _scorer(client).score(_posting(), _profile())

    assert len(client.calls) == 2


def test_a_network_error_is_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_key(monkeypatch)
    client = _FakeClient(httpx.ConnectError("no route to host"), _completion(_verdict_json()))

    result = _scorer(client).score(_posting(), _profile())

    assert result.score == 82
    assert len(client.calls) == 2


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_a_permanent_http_error_is_not_retried(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    """A second call for a 400 or a 401 is money spent on a certain failure."""
    _with_key(monkeypatch)
    client = _FakeClient(_status_error(status), _completion(_verdict_json()))

    with pytest.raises(LlmScoringError, match=str(status)):
        _scorer(client).score(_posting(), _profile())

    assert len(client.calls) == 1


# --------------------------------------------------------------------------
# Budget
# --------------------------------------------------------------------------


def test_budget_caps_the_number_of_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_key(monkeypatch)
    client = _FakeClient(_completion(_verdict_json()), _completion(_verdict_json()))
    scorer = _scorer(client, max_requests_per_run=1)

    scorer.score(_posting(url="https://example.com/jobs/a"), _profile())
    with pytest.raises(BudgetExhaustedError):
        scorer.score(_posting(url="https://example.com/jobs/b"), _profile())

    assert len(client.calls) == 1


def test_a_failed_call_still_counts_against_the_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_key(monkeypatch)
    client = _FakeClient(_completion("bad"), _completion("bad"))
    scorer = _scorer(client, max_requests_per_run=1)

    with pytest.raises(BudgetExhaustedError):
        scorer.score(_posting(url="https://example.com/jobs/a"), _profile())
    assert len(client.calls) == 1


def test_a_retry_is_charged_to_the_budget_like_any_other_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two attempts per posting must not buy twice the configured budget."""
    _with_key(monkeypatch)
    client = _FakeClient(
        _completion("bad"),
        _completion(_verdict_json()),
        _completion("bad"),
        _completion(_verdict_json()),
    )
    scorer = _scorer(client, max_requests_per_run=3)

    # First posting: one wasted call plus one good one.
    scorer.score(_posting(url="https://example.com/jobs/a"), _profile())
    # Second posting: only one call left, so its retry is never bought.
    with pytest.raises(BudgetExhaustedError):
        scorer.score(_posting(url="https://example.com/jobs/b"), _profile())

    assert len(client.calls) == 3


def test_the_old_budget_key_explains_the_rename_and_the_new_meaning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ "Extra inputs are not permitted" is true and useless here."""
    _with_key(monkeypatch)

    with pytest.raises(LlmConfigError) as excinfo:
        _scorer(_FakeClient(), max_postings_per_run=25)

    message = str(excinfo.value)
    assert "max_postings_per_run" in message
    assert "max_requests_per_run" in message
    # The value changed meaning, so renaming the key is not a migration.
    assert "request" in message and "posting" in message
    assert "Extra inputs are not permitted" not in message


def test_zero_budget_scores_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_key(monkeypatch)
    client = _FakeClient()

    with pytest.raises(BudgetExhaustedError):
        _scorer(client, max_requests_per_run=0).score(_posting(), _profile())


# --------------------------------------------------------------------------
# Prompt size
# --------------------------------------------------------------------------


def _sent_description(client: _FakeClient) -> str:
    """The posting description as it left for the model."""
    user_message = client.calls[0]["json"]["messages"][1]["content"]
    description: str = json.loads(user_message)["posting"]["description"]
    return description


def test_a_long_description_is_truncated_before_it_is_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The budget bounds the number of calls; only this bounds their size."""
    _with_key(monkeypatch)
    client = _FakeClient(_completion(_verdict_json()))
    posting = _posting(description_clean="word " * 5000)

    _scorer(client, max_description_chars=200).score(posting, _profile())

    sent = _sent_description(client)
    assert len(sent) <= 200
    assert sent.endswith("…")


def test_descriptions_are_truncated_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_key(monkeypatch)
    client = _FakeClient(_completion(_verdict_json()))

    _scorer(client).score(_posting(description_clean="word " * 20000), _profile())

    assert len(_sent_description(client)) < 20000


def test_a_short_description_is_sent_whole(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_key(monkeypatch)
    client = _FakeClient(_completion(_verdict_json()))

    _scorer(client, max_description_chars=200).score(
        _posting(description_clean="Short and sweet."), _profile()
    )

    assert _sent_description(client) == "Short and sweet."


def test_truncation_can_be_turned_off(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_key(monkeypatch)
    client = _FakeClient(_completion(_verdict_json()))
    description = "word " * 5000

    _scorer(client, max_description_chars=0).score(
        _posting(description_clean=description), _profile()
    )

    assert _sent_description(client) == description


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
