"""Best-effort language detection for collected postings (the v1 heuristic).

``search.languages`` in the configuration never removes a posting during
collection: a posting dropped at fetch time is gone with no record of how
many were dropped or which. Instead every posting is *tagged* with a
detected language when it is collected (see
:meth:`jobsearcher.sources.base.Source.fetch`) and filtered at read time,
where a single ``--language all`` brings the hidden ones back. This module
is that tagger.

The heuristic counts how many function words ("stopwords") from each
supported language occur in the posting's text. It is deliberately tiny — no
dependency, a few dozen words per language — and honest about uncertainty:
it returns ``None`` rather than guess when the text is too short or no
language wins clearly. ``None`` means "undetermined", which is a state kept
distinct from "a language outside the supported set": a posting tagged
``None`` is never hidden by a language filter.

This module has a single implementation, so it stays a flat file — see the
"File vs. package" convention in CLAUDE.md.
"""

from __future__ import annotations

import re
from typing import Final

# Supported language codes, in the order ties are unlikely to matter.
SUPPORTED_LANGUAGES: Final[tuple[str, ...]] = ("en", "fr", "de", "es")


# Function-word sets. Kept short and deliberately distinctive; overlap
# between languages (e.g. "la"/"en" in both French and Spanish) is fine
# because only the relative counts matter.
def _words(text: str) -> frozenset[str]:
    """Build a stopword set from a space-separated string (readability only)."""
    return frozenset(text.split())


_STOPWORDS: Final[dict[str, frozenset[str]]] = {
    "en": _words(
        "the and for with you are our this that will have your from who about "
        "their was were they them then"
    ),
    "fr": _words(
        "le la les des une un dans pour avec vous nous est et au aux du que qui "
        "sur par ce sont votre notre"
    ),
    "de": _words(
        "und der die das mit für von den ist wir sie ein eine auf im zu dem "
        "nicht auch bei sind werden"
    ),
    "es": _words(
        "el la los las una con para que por del en un se su como más al este son nuestro tus"
    ),
}

_TOKEN_RE: Final[re.Pattern[str]] = re.compile(r"[a-zà-ÿ]+", re.IGNORECASE)

# Below this many word tokens, the text carries too little signal to call.
_MIN_TOKENS: Final[int] = 12
# The winning language must have at least this many stopword hits...
_MIN_HITS: Final[int] = 3
# ...and beat the runner-up by at least this margin. Otherwise: undetermined.
_MIN_MARGIN: Final[int] = 2


def detect_language(text: str) -> str | None:
    """Guess the language of ``text`` from function-word frequency.

    Args:
        text: The posting text to classify (typically its title and cleaned
            description joined together).

    Returns:
        One of :data:`SUPPORTED_LANGUAGES`, or ``None`` when the text is too
        short or no language wins by a clear margin.
    """
    tokens = [match.group(0).lower() for match in _TOKEN_RE.finditer(text)]
    if len(tokens) < _MIN_TOKENS:
        return None

    scores = {
        language: sum(token in words for token in tokens) for language, words in _STOPWORDS.items()
    }
    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    (best_language, best_hits), (_, runner_up_hits) = ranked[0], ranked[1]

    if best_hits < _MIN_HITS or best_hits - runner_up_hits < _MIN_MARGIN:
        return None
    return best_language
