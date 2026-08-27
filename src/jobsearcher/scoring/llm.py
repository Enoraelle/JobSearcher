"""Optional LLM-backed scorer, gated behind the ``llm`` extra and a budget.

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
- **Bounded cost.** ``max_postings_per_run`` caps how many postings this
  scorer will spend a call on. Once the cap is hit, :meth:`LlmScorer.score`
  raises :class:`BudgetExhaustedError` so the caller can fall back to the keyword
  score.
- **Structured, validated output.** The model is asked for JSON; the reply
  is parsed into :class:`_LlmVerdict` (pydantic). An invalid reply is
  retried once and then given up on with :class:`LlmScoringError` — the
  posting is left unscored rather than scored from a guess.
- **Friendly missing-dependency error.** Importing this module without the
  ``llm`` extra installed raises with "install jobsearcher[llm] to use this
  scorer", not a bare ``ImportError``.
"""

from __future__ import annotations

import json
import os
from types import TracebackType
from typing import Any, Final

try:  # The ``llm`` extra's dependencies. Keep this the only place they enter.
    import httpx
    from pydantic import BaseModel, ConfigDict, Field, ValidationError
except ModuleNotFoundError as exc:  # pragma: no cover - deps ship with the base package today
    raise ModuleNotFoundError("install jobsearcher[llm] to use this scorer") from exc

from jobsearcher.config import BackendSectionConfig, ProfileConfig
from jobsearcher.models import JobPosting, ScoreResult
from jobsearcher.scoring.base import evaluate_location_match, evaluate_work_mode_match

_DEFAULT_BASE_URL: Final = "https://api.openai.com/v1"
_DEFAULT_MODEL: Final = "gpt-4o-mini"
_DEFAULT_KEY_ENV: Final = "OPENAI_API_KEY"
_DEFAULT_BUDGET: Final = 25
_DEFAULT_TIMEOUT: Final = 30.0

# One initial attempt plus one retry: a model that returns malformed JSON
# twice in a row is not worth a third round trip.
_MAX_ATTEMPTS: Final = 2

_SYSTEM_PROMPT: Final = (
    "You score how well a job posting fits a candidate profile. "
    "Reply with a single JSON object and nothing else, with keys: "
    '"score" (integer 0-100, how well the candidate fits), '
    '"summary" (one sentence), '
    '"matched_skills" (profile skills the posting requires), '
    '"missing_requirements" (skills the posting requires that the profile lacks), '
    '"penalized_skills" (skills the posting requires that the candidate wants to avoid).'
)


class LlmScoringError(RuntimeError):
    """The model could not produce a valid score for a posting.

    Raised after the retry is exhausted. The caller should leave the posting
    unscored (or fall back to the keyword score); it must not invent one.
    """


class BudgetExhaustedError(RuntimeError):
    """The per-run posting budget for the LLM scorer has been spent."""


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
    max_postings_per_run: int = Field(default=_DEFAULT_BUDGET, ge=0)
    timeout: float = Field(default=_DEFAULT_TIMEOUT, gt=0.0)


def _extract_json_object(content: str) -> str:
    """Pull the JSON object out of a reply, tolerating ```` ```json ```` fences."""
    text = content.strip()
    if text.startswith("```"):
        text = text[3:]
        if text[:4].lower() == "json":
            text = text[4:]
        text = text.split("```", 1)[0]
    return text.strip()


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
            ValueError: If ``config`` does not validate as
                :class:`LlmScorerConfig`.
            LlmScoringError: If the configured environment variable holds no
                API key.
        """
        try:
            self._config = LlmScorerConfig.model_validate(config.model_dump())
        except ValidationError as exc:
            raise ValueError(f"Invalid LLM scorer configuration: {exc}") from exc

        api_key = os.environ.get(self._config.api_key_env, "").strip()
        if not api_key:
            raise LlmScoringError(
                f"environment variable {self._config.api_key_env!r} is not set; "
                f"the LLM scorer needs an API key there (it is never read from config)"
            )
        self._api_key = api_key

        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=self._config.timeout)
        self._scored = 0

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
            BudgetExhaustedError: If ``max_postings_per_run`` has already been
                reached this run.
            LlmScoringError: If the model does not return a valid verdict
                within :data:`_MAX_ATTEMPTS` attempts.
        """
        if self._scored >= self._config.max_postings_per_run:
            raise BudgetExhaustedError(
                f"LLM scorer budget of {self._config.max_postings_per_run} "
                f"posting(s) per run is spent"
            )
        # Count the attempt before the call: a failed call still costs money,
        # so it must count against the budget.
        self._scored += 1

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

    def _request_verdict(self, posting: JobPosting, profile: ProfileConfig) -> _LlmVerdict:
        payload = self._build_payload(posting, profile)
        url = f"{self._config.base_url.rstrip('/')}/chat/completions"
        headers = {"Authorization": f"Bearer {self._api_key}"}

        last_error = "no attempt made"
        for _ in range(_MAX_ATTEMPTS):
            try:
                response = self._client.post(url, headers=headers, json=payload)
                response.raise_for_status()
                content = response.json()["choices"][0]["message"]["content"]
                return _LlmVerdict.model_validate_json(_extract_json_object(content))
            except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
                # pydantic's ValidationError is a ValueError subclass, so a
                # malformed reply and a schema-violating one both land here.
                last_error = f"{type(exc).__name__}: {exc}"

        raise LlmScoringError(
            f"the model at {self._config.base_url} did not return a valid score "
            f"after {_MAX_ATTEMPTS} attempt(s): {last_error}"
        )

    def _build_payload(self, posting: JobPosting, profile: ProfileConfig) -> dict[str, Any]:
        description = posting.description_clean or posting.description_raw
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
