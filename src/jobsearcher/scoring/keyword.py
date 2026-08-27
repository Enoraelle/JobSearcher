"""The default, offline skill-overlap scorer.

## What the score means

The score is the fraction of the profile's skills that appear in a posting's
text, on a 0-100 scale, minus a penalty for skills the profile explicitly
rules out. Nothing else feeds it — see :class:`~jobsearcher.models.ScoreResult`
for why location and work-mode fit are reported separately rather than mixed
in.

## How it is computed

1. **Normalization** (:func:`_normalize`), applied identically to every
   skill and to the posting text:

   - Unicode case folding (``str.casefold``).
   - Diacritic removal via NFKD decomposition ("café" -> "cafe").
   - Every character that is not a letter, digit, or whitespace becomes a
     space, **except** ``+``, ``#`` and ``.`` when they sit directly against
     an alphanumeric character — so "c++", "c#", ".net" and "node.js"
     survive as single tokens instead of collapsing to "c" / "net".
   - Runs of whitespace collapse to a single space.

   The posting text is the title and the cleaned description joined by a
   newline (``description_clean``, falling back to ``description_raw``).

2. **Skill matching.** A skill matches the posting when any of its *surface
   forms* occurs in the normalized text on token boundaries — not preceded
   or followed by an alphanumeric — so "java" does not match "javascript",
   and multi-word skills like "django rest framework" match as a phrase. A
   skill's surface forms are the skill itself plus every member of every
   configured synonym group (``scoring.synonyms``, a list of
   equivalence-class lists) that contains it, compared in normalized form.
   Synonym groups are symmetric: configuring ``[postgresql, postgres,
   psql]`` makes any one of the three match text containing any other.

3. **The three skill lists.**

   - ``matched_skills`` = entries of ``profile.skills`` that matched.
   - ``unmatched_profile_skills`` = entries of ``profile.skills`` that did
     not. This is what lowers coverage; it is a weak signal, never a
     judgement on the posting.
   - ``penalized_skills`` = entries of ``profile.absent_skills`` that
     matched (same machinery, synonyms included).

   ``missing_requirements`` is always empty here: reading a posting's stated
   requirements out of its prose is out of scope for a deterministic keyword
   scorer, and is the LLM scorer's job.

4. **Coverage and penalty.**

   ::

       coverage = len(matched_skills) / len(profile.skills)
       penalty  = min(absent_skill_penalty * len(penalized_skills),
                      absent_skill_penalty_cap)
       final    = clamp(coverage - penalty, 0.0, 1.0)
       score    = floor(final * 100 + 0.5)

   ``floor(x * 100 + 0.5)`` is round-half-up to the nearest integer. It is
   chosen deliberately over the built-in :func:`round`, which does
   round-half-to-even (``round(0.5) == 0``, ``round(2.5) == 2``) and
   surprises everyone who expects a .5 to go up.

   If ``profile.skills`` is empty there is nothing to score against: the
   score is ``0`` and the summary says so.

5. **Explaining the penalty.** Whenever the penalty fires, the exact number
   of points it removed and the skills responsible are stated in
   ``summary``, and those same skills are in ``penalized_skills`` as a
   structured list — the summary is never the only place a consumer can find
   them.

Everything here is a pure function of its inputs: no clock, no randomness,
no I/O. The same posting and profile always produce the same
:class:`~jobsearcher.models.ScoreResult`, which is what makes it cheap to
test exhaustively.
"""

from __future__ import annotations

import math
import re
import unicodedata
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from jobsearcher.config import BackendSectionConfig, ProfileConfig
from jobsearcher.models import JobPosting, ScoreResult
from jobsearcher.scoring.base import evaluate_location_match, evaluate_work_mode_match

# Penalty subtracted from the [0, 1] coverage for each skill listed in
# ``profile.absent_skills`` that shows up in a posting. 0.34 is roughly one
# third, so three ruled-out skills in a single posting pull any score down to
# zero; the value is chosen for exactly that property, not fitted to data.
# Both are overridable per config (``scoring.absent_skill_penalty`` /
# ``scoring.absent_skill_penalty_cap``).
DEFAULT_ABSENT_SKILL_PENALTY: Final = 0.34
DEFAULT_ABSENT_SKILL_PENALTY_CAP: Final = 1.0

# Punctuation kept during normalization when pressed against an alphanumeric,
# so "c++", "c#", ".net" and "node.js" stay one token each.
_KEPT_SYMBOLS: Final = "+#."
_DISALLOWED_RE: Final = re.compile(rf"[^0-9a-z\s{re.escape(_KEPT_SYMBOLS)}]")
_LONELY_SYMBOL_RE: Final = re.compile(
    rf"(?<![0-9a-z{re.escape(_KEPT_SYMBOLS)}])[{re.escape(_KEPT_SYMBOLS)}]+(?![0-9a-z])"
)
_WHITESPACE_RE: Final = re.compile(r"\s+")


def _normalize(text: str) -> str:
    """Fold case and accents and flatten punctuation. See the module docstring."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    folded = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    folded = _DISALLOWED_RE.sub(" ", folded)
    folded = _LONELY_SYMBOL_RE.sub(" ", folded)
    return _WHITESPACE_RE.sub(" ", folded).strip()


def _build_synonym_index(groups: list[list[str]]) -> dict[str, frozenset[str]]:
    """Map each normalized term to the full set of its normalized synonyms.

    Groups that share a term are merged transitively (``[a, b]`` and
    ``[b, c]`` make ``a`` and ``c`` synonyms), so configuration order and
    how the classes are split does not matter.
    """
    parent: dict[str, str] = {}

    def find(term: str) -> str:
        parent.setdefault(term, term)
        root = term
        while parent[root] != root:
            root = parent[root]
        while parent[term] != root:  # path compression
            parent[term], term = root, parent[term]
        return root

    for group in groups:
        members = [term for term in (_normalize(t) for t in group) if term]
        for other in members[1:]:
            parent[find(other)] = find(members[0])

    classes: dict[str, set[str]] = {}
    for term in parent:
        classes.setdefault(find(term), set()).add(term)

    return {term: frozenset(members) for members in classes.values() for term in members}


def _surface_forms(skill: str, synonyms: dict[str, frozenset[str]]) -> frozenset[str]:
    """Return the normalized forms that count as an occurrence of ``skill``."""
    normalized = _normalize(skill)
    if not normalized:
        return frozenset()
    return synonyms.get(normalized, frozenset()) | {normalized}


def _occurs(forms: frozenset[str], haystack: str) -> bool:
    """Whether any of ``forms`` occurs in ``haystack`` on token boundaries."""
    return any(
        re.search(rf"(?<![0-9a-z]){re.escape(form)}(?![0-9a-z])", haystack) for form in forms
    )


class KeywordScorerConfig(BaseModel):
    """Validated ``scoring:`` block for the keyword scorer.

    Re-validated from the permissive
    :class:`~jobsearcher.config.BackendSectionConfig` once the backend name
    is known to be this scorer — exactly as ``GreenhouseSourceConfig`` does
    for its source. ``extra="forbid"`` turns a typo like ``synonynms:`` into
    an immediate error instead of a silently ignored key.
    """

    model_config = ConfigDict(extra="forbid")

    backend: str
    synonyms: list[list[str]] = Field(default_factory=list)
    absent_skill_penalty: float = Field(default=DEFAULT_ABSENT_SKILL_PENALTY, ge=0.0)
    absent_skill_penalty_cap: float = Field(
        default=DEFAULT_ABSENT_SKILL_PENALTY_CAP, ge=0.0, le=1.0
    )


class KeywordScorer:
    """Deterministic, offline skill-overlap scorer. See the module docstring."""

    def __init__(self, config: BackendSectionConfig | None = None) -> None:
        """Build the scorer, validating its slice of the configuration.

        Args:
            config: The raw ``scoring:`` block. ``None`` uses every default
                (no synonyms, default penalty), which is what tests and the
                zero-config path want.

        Raises:
            ValueError: If ``config`` does not validate as
                :class:`KeywordScorerConfig` (an unknown key, a negative
                penalty, ...).
        """
        raw = config.model_dump() if config is not None else {"backend": "keyword_match"}
        try:
            self._config = KeywordScorerConfig.model_validate(raw)
        except ValidationError as exc:
            raise ValueError(f"Invalid keyword scorer configuration: {exc}") from exc
        self._synonyms = _build_synonym_index(self._config.synonyms)

    def score(self, posting: JobPosting, profile: ProfileConfig) -> ScoreResult:
        """Score ``posting`` against ``profile``. See the module docstring."""
        haystack = _normalize(
            f"{posting.title}\n{posting.description_clean or posting.description_raw}"
        )

        matched: list[str] = []
        unmatched: list[str] = []
        for skill in profile.skills:
            forms = _surface_forms(skill, self._synonyms)
            (matched if _occurs(forms, haystack) else unmatched).append(skill)

        penalized = [
            skill
            for skill in profile.absent_skills
            if _occurs(_surface_forms(skill, self._synonyms), haystack)
        ]

        location_match = evaluate_location_match(posting, profile)
        work_mode_match = evaluate_work_mode_match(posting, profile)

        if not profile.skills:
            return ScoreResult(
                score=0,
                summary="profile lists no skills; the keyword scorer cannot rank this posting",
                penalized_skills=penalized,
                location_match=location_match,
                work_mode_match=work_mode_match,
            )

        coverage = len(matched) / len(profile.skills)
        penalty = min(
            self._config.absent_skill_penalty * len(penalized),
            self._config.absent_skill_penalty_cap,
        )
        coverage_points = math.floor(coverage * 100 + 0.5)
        final = min(1.0, max(0.0, coverage - penalty))
        score = math.floor(final * 100 + 0.5)

        return ScoreResult(
            score=score,
            summary=_summarize(
                matched=matched,
                unmatched=unmatched,
                penalized=penalized,
                points_removed=coverage_points - score,
                location_match=location_match,
                work_mode_match=work_mode_match,
            ),
            matched_skills=matched,
            unmatched_profile_skills=unmatched,
            penalized_skills=penalized,
            location_match=location_match,
            work_mode_match=work_mode_match,
        )


def _summarize(
    *,
    matched: list[str],
    unmatched: list[str],
    penalized: list[str],
    points_removed: int,
    location_match: bool | None,
    work_mode_match: bool | None,
) -> str:
    """Build the human-readable one-liner. Structured data lives on the model."""
    total = len(matched) + len(unmatched)
    head = f"matched {len(matched)}/{total} skill{'s' if total != 1 else ''}"
    if matched:
        head += f" ({', '.join(matched)})"
    parts = [head]

    if unmatched:
        parts.append(f"not mentioned: {', '.join(unmatched)}")
    if penalized:
        parts.append(
            f"penalty -{points_removed}: posting mentions ruled-out "
            f"skill{'s' if len(penalized) != 1 else ''} {', '.join(penalized)}"
        )
    if location_match is not None:
        parts.append("location fits" if location_match else "location does not fit")
    if work_mode_match is not None:
        parts.append("work mode fits" if work_mode_match else "work mode differs")

    return "; ".join(parts)
