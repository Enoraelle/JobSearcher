"""Optional LLM-backed scorer, gated behind a budget.

This scorer is a **bonus**. JobSearcher scores every posting with the
offline :class:`~jobsearcher.scoring.keyword.KeywordScorer` by default and
never requires an API key. Enable this one explicitly in ``scoring:`` to
refine a bounded number of postings per run with a language model.

Every constraint below follows from the project's "usable with zero API
keys" rule:

- **No vendor lock-in.** It talks to any OpenAI-compatible
  ``/chat/completions`` endpoint; the base URL and model name are
  configuration. Point it at OpenAI, a self-hosted server, or any
  compatible gateway.
- **Key from the environment only.** The API key is read from an environment
  variable whose name is config (``api_key_env``, default
  ``OPENAI_API_KEY``). It is never read from the config file, which is
  version-controlled.
- **Bounded cost, on both axes.** ``max_requests_per_run`` caps how many
  requests this scorer will make — requests, not postings, because a
  retried posting costs two of them and a budget that counted postings
  would quietly authorize twice the spend. Once the cap is hit,
  :meth:`LlmScorer.score` raises :class:`BudgetExhaustedError` so the
  caller can fall back to the keyword score. ``max_description_chars``
  caps the other axis: the budget bounds how many calls go out, and only
  truncation bounds what each one costs, since postings carry
  descriptions of no fixed length.
- **Structured, validated output.** The model is asked for JSON; the reply
  is parsed into :class:`_LlmVerdict` (pydantic). An invalid reply is
  retried once and then given up on with :class:`LlmScoringError` — the
  posting is left unscored rather than scored from a guess.
- **Retries only where a retry can help.** A timeout or a 429/5xx is worth
  a second attempt; a 400 or a 401 is a permanent refusal, and paying for
  a second identical call cannot change it. Those fail immediately.

Nothing here needs installing: this scorer speaks plain HTTP to an
OpenAI-compatible endpoint with the ``httpx`` the rest of the package
already uses. Switching ``scoring.backend`` to ``llm`` and exporting the key
is all it takes.
"""

from __future__ import annotations

import json
import os
from types import TracebackType
from typing import Any, Final

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from jobsearcher.config import BackendSectionConfig, ProfileConfig
from jobsearcher.models import JobPosting, ScoreResult
from jobsearcher.scoring.base import (
    ScorerBudgetExhaustedError,
    ScorerConfigError,
    ScoringError,
    evaluate_location_match,
    evaluate_work_mode_match,
)

_DEFAULT_BASE_URL: Final = "https://api.openai.com/v1"
_DEFAULT_MODEL: Final = "gpt-4o-mini"
_DEFAULT_KEY_ENV: Final = "OPENAI_API_KEY"
_DEFAULT_BUDGET: Final = 25
_DEFAULT_TIMEOUT: Final = 30.0
# Roughly a thousand tokens of description: enough for the requirements
# section of a long posting, without letting one verbose ad cost a multiple
# of every other call.
_DEFAULT_MAX_DESCRIPTION_CHARS: Final = 4000

# One initial attempt plus one retry: a model that returns malformed JSON
# twice in a row is not worth a third round trip.
_MAX_ATTEMPTS: Final = 2

# Worth a second attempt: the same request may well succeed later. Every
# other non-2xx status is a permanent refusal (bad key, bad model name,
# malformed request) that a retry only pays for twice.
_RETRYABLE_STATUS_CODES: Final[frozenset[int]] = frozenset({429, 500, 502, 503, 504})

# Pre-rename spelling of `max_requests_per_run`, kept only to be rejected
# with an explanation. See LlmScorerConfig._explain_renamed_budget.
_LEGACY_BUDGET_KEY: Final = "max_postings_per_run"

_SYSTEM_PROMPT: Final = (
    "You score how well a job posting fits a candidate profile. "
    "Reply with a single JSON object and nothing else, with keys: "
    '"score" (integer 0-100, how well the candidate fits), '
    '"summary" (one sentence), '
    '"matched_skills" (profile skills the posting requires), '
    '"missing_requirements" (skills the posting requires that the profile lacks), '
    '"penalized_skills" (skills the posting requires that the candidate wants to avoid).'
)


class LlmConfigError(ScorerConfigError):
    """This scorer cannot run as configured: a bad option, or no API key.

    A :class:`~jobsearcher.scoring.base.ScorerConfigError`, which is what
    the pipeline catches before the scoring phase starts. A missing API key
    is the single most likely failure of this backend, and it has to reach
    the user as a sentence naming the environment variable to export — not
    as a traceback out of the middle of a run.
    """


class LlmScoringError(ScoringError):
    """The model could not produce a valid score for a posting.

    Raised after the retry is exhausted. A per-posting failure under the
    scorer contract (see :class:`~jobsearcher.scoring.base.Scorer`): the
    caller leaves the posting unscored (or falls back to the keyword score)
    and carries on; it must not invent a score.
    """


class BudgetExhaustedError(ScorerBudgetExhaustedError):
    """The per-run request budget for the LLM scorer has been spent.

    Subclasses the backend-neutral
    :class:`~jobsearcher.scoring.base.ScorerBudgetExhaustedError` so the pipeline
    can react to "stop scoring" without importing this optional module.
    """


class _LlmVerdict(BaseModel):
    """The JSON contract the model is asked to fill. Extra keys are ignored."""

    model_config = ConfigDict(extra="ignore")

    score: int = Field(ge=0, le=100)
    summary: str
    matched_skills: list[str] = Field(default_factory=list)
    missing_requirements: list[str] = Field(default_factory=list)
    penalized_skills: list[str] = Field(default_factory=list)


class LlmScorerConfig(BaseModel):
    """Validated ``scoring:`` block for the LLM scorer.

    ``extra="forbid"`` so an unknown key is an error, not a silent no-op.
    Note there is no ``api_key`` field, deliberately: the key never lives in
    config, only in the environment (see ``api_key_env``).
    """

    model_config = ConfigDict(extra="forbid")

    backend: str
    base_url: str = _DEFAULT_BASE_URL
    model: str = _DEFAULT_MODEL
    api_key_env: str = _DEFAULT_KEY_ENV
    max_requests_per_run: int = Field(default=_DEFAULT_BUDGET, ge=0)
    """Hard cap on API calls per run, retries included. ``0`` disables scoring."""

    max_description_chars: int = Field(default=_DEFAULT_MAX_DESCRIPTION_CHARS, ge=0)
    """How much of a posting description to send. ``0`` sends all of it."""

    timeout: float = Field(default=_DEFAULT_TIMEOUT, gt=0.0)

    @model_validator(mode="before")
    @classmethod
    def _explain_renamed_budget(cls, data: Any) -> Any:
        """Turn the old budget key into a migration message, not "extra input".

        ``max_postings_per_run`` was not merely renamed: it capped postings
        and now caps requests. Left to ``extra="forbid"``, the config would
        be refused with "Extra inputs are not permitted" — true, and useless:
        it names neither the new key nor the change of meaning, so someone
        would rename 25 to 25 and believe they had migrated while their
        budget silently changed nature.

        Args:
            data: The raw ``scoring:`` mapping, before field validation.

        Returns:
            ``data`` unchanged when it carries no legacy key.

        Raises:
            ValueError: If ``max_postings_per_run`` is present.
        """
        # `mode="before"` hands over whatever the caller passed, hence Any.
        if isinstance(data, dict) and _LEGACY_BUDGET_KEY in data:
            raise ValueError(
                f"`{_LEGACY_BUDGET_KEY}` is now `max_requests_per_run`, and it counts "
                "something else: the budget caps API requests, not postings, because a "
                "posting whose first reply is unusable is retried once and costs two. "
                "Renaming the key is not enough: choose the number of requests you "
                "want to pay for per run, which for the same number of postings is up "
                "to twice the old value."
            )
        return data


def _extract_json_object(content: str) -> str:
    """Pull the JSON object out of a reply, tolerating ```` ```json ```` fences."""
    text = content.strip()
    if text.startswith("```"):
        text = text[3:]
        if text[:4].lower() == "json":
            text = text[4:]
        text = text.split("```", 1)[0]
    return text.strip()


def _explain_config_error(exc: ValidationError) -> str:
    """Render a config failure as one line the user can act on.

    Same shape as the exporters' explainers: pydantic's own rendering
    ("1 validation error for LlmScorerConfig ... [type=..., input_value=...]")
    buries the part that matters, including the messages this module's own
    validators wrote for the user.

    Args:
        exc: The failure raised while validating the ``scoring:`` block.

    Returns:
        A single message naming every problem and what to change.
    """
    parts: list[str] = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"])
        if error["type"] == "value_error":
            # Raised by a validator in this module: already user-facing.
            parts.append(error["msg"].removeprefix("Value error, "))
        elif error["type"] == "extra_forbidden":
            parts.append(f"unknown option `{location}` (check for a typo)")
        else:
            parts.append(f"`{location}`: {error['msg']}")
    return "invalid `llm` scorer configuration: " + "; ".join(parts)


def _truncate(text: str, limit: int) -> str:
    """Cut ``text`` to at most ``limit`` characters, marking that it was cut.

    Args:
        text: The text to bound.
        limit: The maximum length, or ``0`` for no limit.

    Returns:
        ``text`` unchanged when it already fits, otherwise its first
        ``limit`` characters with the last one replaced by an ellipsis so
        the model can tell the description was cut short.
    """
    if limit <= 0 or len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "\u2026"


class LlmScorer:
    """Scores postings with an OpenAI-compatible chat model. See the module docstring."""

    def __init__(
        self,
        config: BackendSectionConfig,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        """Build the scorer, validating config and resolving the API key.

        Args:
            config: The raw ``scoring:`` block.
            client: An ``httpx.Client`` to use for requests. If omitted, one
                is created with the configured timeout and closed by
                :meth:`close`. Pass one in for testing.

        Raises:
            LlmConfigError: If ``config`` does not validate as
                :class:`LlmScorerConfig`, or the configured environment
                variable holds no API key. Both are config failures under
                the scorer contract (see
                :class:`~jobsearcher.scoring.base.Scorer`).
        """
        try:
            self._config = LlmScorerConfig.model_validate(config.model_dump())
        except ValidationError as exc:
            raise LlmConfigError(_explain_config_error(exc)) from exc

        api_key = os.environ.get(self._config.api_key_env, "").strip()
        if not api_key:
            raise LlmConfigError(
                f"environment variable {self._config.api_key_env!r} is not set; "
                f"the LLM scorer needs an API key there (it is never read from config)"
            )
        self._api_key = api_key

        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=self._config.timeout)
        self._requests = 0

    def close(self) -> None:
        """Close the underlying HTTP client, if this instance owns one."""
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> LlmScorer:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def score(self, posting: JobPosting, profile: ProfileConfig) -> ScoreResult:
        """Score one posting with the model.

        Args:
            posting: The posting to score.
            profile: The scoring criteria.

        Returns:
            The model's verdict, merged with the shared eligibility checks.

        Raises:
            BudgetExhaustedError: If ``max_requests_per_run`` has already
                been reached this run, or is reached partway through this
                posting's attempts.
            LlmScoringError: If the model refuses the request permanently,
                or does not return a valid verdict within
                :data:`_MAX_ATTEMPTS` attempts.
        """
        self._ensure_budget()
        verdict = self._request_verdict(posting, profile)
        matched = set(verdict.matched_skills)
        return ScoreResult(
            score=verdict.score,
            summary=verdict.summary,
            matched_skills=verdict.matched_skills,
            unmatched_profile_skills=[s for s in profile.skills if s not in matched],
            missing_requirements=verdict.missing_requirements,
            penalized_skills=verdict.penalized_skills,
            location_match=evaluate_location_match(posting, profile),
            work_mode_match=evaluate_work_mode_match(posting, profile),
        )

    def _ensure_budget(self) -> None:
        """Raise unless at least one more request fits in this run's budget."""
        if self._requests >= self._config.max_requests_per_run:
            raise BudgetExhaustedError(
                f"LLM scorer budget of {self._config.max_requests_per_run} "
                f"request(s) per run is spent"
            )

    def _request_verdict(self, posting: JobPosting, profile: ProfileConfig) -> _LlmVerdict:
        payload = self._build_payload(posting, profile)
        url = f"{self._config.base_url.rstrip('/')}/chat/completions"
        headers = {"Authorization": f"Bearer {self._api_key}"}

        last_error = "no attempt made"
        for attempt in range(_MAX_ATTEMPTS):
            if attempt:
                # A retry is another paid call, so it has to fit in the
                # budget like the first one did.
                self._ensure_budget()
            # Count the attempt before the call: a failed call still costs
            # money, so it must count against the budget.
            self._requests += 1
            try:
                response = self._client.post(url, headers=headers, json=payload)
                response.raise_for_status()
                content = response.json()["choices"][0]["message"]["content"]
                return _LlmVerdict.model_validate_json(_extract_json_object(content))
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if status not in _RETRYABLE_STATUS_CODES:
                    raise LlmScoringError(
                        f"the model at {self._config.base_url} refused the request "
                        f"with HTTP {status}; retrying would cost another call and "
                        f"return the same answer. Check `api_key_env` "
                        f"({self._config.api_key_env}), `base_url` and `model`."
                    ) from exc
                last_error = f"{type(exc).__name__}: {exc}"
            except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
                # pydantic's ValidationError is a ValueError subclass, so a
                # malformed reply and a schema-violating one both land here.
                last_error = f"{type(exc).__name__}: {exc}"

        raise LlmScoringError(
            f"the model at {self._config.base_url} did not return a valid score "
            f"after {_MAX_ATTEMPTS} attempt(s): {last_error}"
        )

    def _build_payload(self, posting: JobPosting, profile: ProfileConfig) -> dict[str, Any]:
        description = _truncate(
            posting.description_clean or posting.description_raw,
            self._config.max_description_chars,
        )
        user_prompt = json.dumps(
            {
                "profile": {
                    "role": profile.role,
                    "skills": profile.skills,
                    "absent_skills": profile.absent_skills,
                    "experience_years": profile.experience_years,
                },
                "posting": {
                    "title": posting.title,
                    "company": posting.company,
                    "location": posting.location,
                    "description": description,
                },
            },
            ensure_ascii=False,
        )
        return {
            "model": self._config.model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
        }
