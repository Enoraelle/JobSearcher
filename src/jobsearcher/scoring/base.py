"""The scoring contract, plus the scorer-agnostic eligibility checks.

A :class:`Scorer` turns one :class:`~jobsearcher.models.JobPosting` and the
user's :class:`~jobsearcher.config.ProfileConfig` into a
:class:`~jobsearcher.models.ScoreResult`. Implementations live one per module
in this package (``keyword.py``, ``llm.py``); this module holds only the
protocol they satisfy and two pure helpers they share.

:func:`evaluate_location_match` and :func:`evaluate_work_mode_match` live
here, rather than inside one scorer, because every scorer needs the same
answer and the logic is not keyword-specific — an LLM scorer reuses them
unchanged. Both return ``bool | None``, where ``None`` means "the posting
does not say" or "the profile states no constraint": a state that must never
be collapsed into ``False`` (which means "the posting says something
incompatible").

This module has a single implementation of each helper, so it stays a flat
file — see the "File vs. package" convention in CLAUDE.md.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Final, Protocol

from jobsearcher.config import ProfileConfig
from jobsearcher.models import JobPosting, ScoreResult, WorkMode

_WORD_RE: Final[re.Pattern[str]] = re.compile(r"[0-9a-z]+")


class ScorerError(Exception):
    """Base class for every failure a scorer is allowed to raise.

    The three subclasses below are the whole contract between a scorer and
    the code that runs it. The pipeline catches *these*, by name — not
    whatever concrete class today's scorers happen to use. An ``except``
    clause that covers a scorer's errors by coincidence (``RuntimeError``,
    ``ValueError``) works until the next scorer raises something else, and
    then a routine misconfiguration reaches the user as a traceback.

    A scorer that needs a new failure mode subclasses one of these rather
    than inventing a class the pipeline has never heard of.
    """


class ScorerConfigError(ScorerError):
    """The scorer cannot be built or run as configured.

    Raised from a scorer's constructor: an unknown option, a value out of
    range, a missing API key. Retrying changes nothing — the user has to
    edit ``config.yaml`` or their environment, so the message must say
    which. The pipeline turns this into a
    :class:`~jobsearcher.pipeline.PipelineError` before the phase starts.
    """


class ScoringError(ScorerError):
    """The scorer could not produce a score for one posting.

    A per-posting failure, not a phase failure: the pipeline records it,
    leaves that posting unscored for a later run, and moves on to the next
    one.
    """


class ScorerBudgetExhaustedError(ScorerError):
    """A metered scorer has spent its per-run budget; stop calling it.

    Raised by scorers that cost money or rate-limited calls (see
    :class:`~jobsearcher.scoring.llm.LlmScorer`). The pipeline catches this
    to end the scoring phase cleanly, leaving the remaining postings
    unscored for a later run rather than treating it as a failure. The
    always-available :class:`~jobsearcher.scoring.keyword.KeywordScorer`
    never raises it.
    """


class Scorer(Protocol):
    """Turns a posting plus a profile into a :class:`ScoreResult`.

    A scorer may raise exactly three things, and the pipeline handles each
    differently:

    - :class:`ScorerConfigError`, from the constructor, when the backend
      cannot be built as configured. It ends the phase before it starts.
    - :class:`ScoringError`, from :meth:`score`, when this one posting could
      not be scored. The posting is left unscored and the phase continues.
    - :class:`ScorerBudgetExhaustedError`, from :meth:`score`, when a
      metered scorer has spent its per-run budget. The phase ends cleanly
      and the remaining postings wait for the next run.

    Anything else escaping a scorer is a bug in that scorer, not a
    condition the pipeline pretends to understand.
    """

    def score(self, posting: JobPosting, profile: ProfileConfig) -> ScoreResult:
        """Score ``posting`` against ``profile``.

        Implementations must be free of side effects and must not perform
        network I/O unless they are explicitly an online scorer (see
        ``llm.py``, which is gated behind a per-run budget).

        Args:
            posting: The posting to score.
            profile: The job seeker's profile, the scoring criteria.

        Returns:
            The score and its supporting detail.

        Raises:
            ScoringError: If this posting could not be scored.
            ScorerBudgetExhaustedError: If a metered scorer has spent its
                per-run budget.
        """
        ...


def _fold(text: str) -> str:
    """Lowercase and strip diacritics, for accent-insensitive comparison."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def _place_tokens(text: str) -> tuple[str, ...]:
    """Split a place name into comparable word tokens.

    Folding first means case, accents and punctuation stop mattering:
    ``"Île-de-France"`` and ``"Ile de France"`` both become
    ``("ile", "de", "france")``.
    """
    return tuple(_WORD_RE.findall(_fold(text)))


def _contains_phrase(haystack: tuple[str, ...], needle: tuple[str, ...]) -> bool:
    """Whether ``needle`` occurs in ``haystack`` as a run of whole tokens."""
    if not needle or len(needle) > len(haystack):
        return False
    span = len(needle)
    return any(haystack[i : i + span] == needle for i in range(len(haystack) - span + 1))


def evaluate_location_match(posting: JobPosting, profile: ProfileConfig) -> bool | None:
    """Whether the posting's location is compatible with the profile's.

    Comparison is on whole tokens, not on substrings. A plain substring test
    reports a match whenever one name's letters happen to occur inside
    another's — ``"US"`` inside ``"Toulouse"``, ``"IN"`` inside
    ``"Berlin"``, ``"Nice"`` inside ``"Venice"`` — and country codes, the
    shortest and most common thing to configure, are exactly where it goes
    wrong most. This field is read as an answer to "can I take this job", so
    a wrong ``True`` costs more than the ``None`` that means "cannot tell":
    it puts an unreachable posting on the shortlist wearing the look of a
    checked fact.

    Matching stays deliberately two-directional: a configured ``"France"``
    matches a posting in ``"Paris, France"``, and a configured
    ``"Remote - EU"`` matches a posting that only says ``"EU"``.

    Args:
        posting: The posting whose ``location`` and ``eligible_locations``
            are checked.
        profile: The profile whose ``locations`` express the constraint.

    Returns:
        ``None`` if the profile lists no locations, or the posting carries
        neither a ``location`` nor any ``eligible_locations`` (nothing to
        compare). Otherwise ``True`` if any configured location and any of
        the posting's locations contain one another as a run of whole
        tokens, and ``False`` if none do.
    """
    if not profile.locations:
        return None
    candidates = [loc for loc in (posting.location, *posting.eligible_locations) if loc]
    if not candidates:
        return None
    candidate_tokens = [_place_tokens(c) for c in candidates]
    for wanted in profile.locations:
        wanted_tokens = _place_tokens(wanted)
        if any(
            _contains_phrase(candidate, wanted_tokens) or _contains_phrase(wanted_tokens, candidate)
            for candidate in candidate_tokens
        ):
            return True
    return False


def evaluate_work_mode_match(posting: JobPosting, profile: ProfileConfig) -> bool | None:
    """Whether the posting's work mode matches the profile's.

    Args:
        posting: The posting whose ``work_mode`` is checked.
        profile: The profile whose ``work_mode`` expresses the preference.

    Returns:
        ``None`` if either side is :attr:`~jobsearcher.models.WorkMode.UNKNOWN`
        (the posting does not say, or the profile states no preference).
        Otherwise whether the two work modes are equal.
    """
    if posting.work_mode is WorkMode.UNKNOWN or profile.work_mode is WorkMode.UNKNOWN:
        return None
    return posting.work_mode is profile.work_mode


__all__ = [
    "ScoreResult",
    "Scorer",
    "ScorerBudgetExhaustedError",
    "ScorerConfigError",
    "ScorerError",
    "ScoringError",
    "evaluate_location_match",
    "evaluate_work_mode_match",
]
