"""Scoring logic for ranking collected job postings.

:class:`~jobsearcher.scoring.keyword.KeywordScorer` (offline, deterministic,
zero configuration) is the default and is always importable.
:class:`~jobsearcher.scoring.llm.LlmScorer` is a bonus that costs API
calls, and is imported on demand — :func:`get_scorer` is the only place that
reaches for it, and only when ``scoring.backend`` asks for it.
"""

from __future__ import annotations

from jobsearcher.config import BackendSectionConfig
from jobsearcher.scoring.base import (
    Scorer,
    ScorerBudgetExhaustedError,
    ScorerConfigError,
    ScorerError,
    ScoringError,
    evaluate_location_match,
    evaluate_work_mode_match,
)
from jobsearcher.scoring.keyword import KeywordScorer

_KEYWORD_BACKENDS = frozenset({"keyword_match", "keyword"})


class ScoringConfigError(ScorerConfigError):
    """The ``scoring:`` configuration names an unknown backend.

    A :class:`~jobsearcher.scoring.base.ScorerConfigError`, so a caller that
    handles "this scorer cannot be built" handles an unknown backend name
    with it, without enumerating the ways a backend can be wrong.
    """


def get_scorer(config: BackendSectionConfig) -> Scorer:
    """Build the scorer named by ``config``.

    Args:
        config: The validated ``scoring:`` block. ``backend`` selects the
            implementation (``keyword_match`` or ``llm``); the rest is
            backend-specific and re-validated by the scorer itself.

    Returns:
        A ready scorer.

    Raises:
        ScoringConfigError: If ``backend`` is neither ``keyword_match`` nor
            ``llm``.
        ScorerConfigError: If the backend-specific configuration is invalid
            (an unknown option, a value out of range, a missing API key).
            ``ScoringConfigError`` is itself one of these, so catching
            ``ScorerConfigError`` alone covers every way this can fail.
    """
    if config.backend in _KEYWORD_BACKENDS:
        return KeywordScorer(config)
    if config.backend == "llm":
        from jobsearcher.scoring.llm import LlmScorer

        return LlmScorer(config)
    raise ScoringConfigError(
        f"unknown scoring backend {config.backend!r}; expected 'keyword_match' or 'llm'"
    )


__all__ = [
    "KeywordScorer",
    "Scorer",
    "ScorerBudgetExhaustedError",
    "ScorerConfigError",
    "ScorerError",
    "ScoringConfigError",
    "ScoringError",
    "evaluate_location_match",
    "evaluate_work_mode_match",
    "get_scorer",
]
