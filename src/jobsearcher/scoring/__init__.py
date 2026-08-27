"""Scoring logic for ranking collected job postings.

:class:`~jobsearcher.scoring.keyword.KeywordScorer` (offline, deterministic,
zero configuration) is the default and is always importable.
:class:`~jobsearcher.scoring.llm.LlmScorer` is a bonus behind the ``llm``
extra and is imported on demand, never from here — so importing this package
never pulls in the optional path.
"""

from jobsearcher.scoring.base import (
    Scorer,
    evaluate_location_match,
    evaluate_work_mode_match,
)
from jobsearcher.scoring.keyword import KeywordScorer

__all__ = [
    "KeywordScorer",
    "Scorer",
    "evaluate_location_match",
    "evaluate_work_mode_match",
]
