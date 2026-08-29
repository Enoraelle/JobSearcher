"""Shared inference of a posting's work arrangement from its free text.

No job board this package reads exposes a structured remote/hybrid flag, so
every source has to read the arrangement out of prose. That is one problem
with one correct answer, and it lived here as two: a careful version in
``greenhouse.py`` and an eight-line substring test in ``freework.py`` that
read "This role is not remote" as REMOTE and matched "remote" inside
"Remotely-Managed Ltd". Copying the good one into the second module would
have fixed the symptom and rebuilt the cause — two copies diverge again, and
the next source makes three.

What is shared here is the *mechanism*; the vocabulary stays with the
source. Free-work.com's postings say "Télétravail total" and We Work
Remotely's say nothing at all, so each caller passes the markers its site
uses (see ``remote_markers`` / ``hybrid_markers``) and gets the same reading
rules applied to them.

Two rules make the difference, and both are why a substring test is not
enough:

- **Whole tokens, not substrings.** "remote" inside "Remotely-Managed" is
  not a remote job. Text and markers are folded (case and accents dropped)
  and cut into word tokens, so a marker matches only as a complete run of
  words — which also makes multi-word markers work as phrases, and makes
  "100% télétravail" match "100 % télétravail".
- **Negations are read.** "This role is not remote" and "Pas de télétravail"
  are the most common ways a posting mentions an arrangement it does *not*
  offer. A marker preceded by a negator within :data:`NEGATION_WINDOW`
  tokens does not count.

``--remote`` filters on the result, so everything here errs toward
ONSITE/UNKNOWN rather than claiming an arrangement the posting never
asserted: a wrongly-included posting wastes a reader's time on a job they
cannot take.

This module is internal to :mod:`jobsearcher.sources` — nothing outside the
package should reach for it.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Final

from jobsearcher.models import WorkMode

_TOKEN_RE: Final[re.Pattern[str]] = re.compile(r"[0-9a-z]+")

#: Markers for a source whose postings say plainly "remote" / "hybrid".
DEFAULT_REMOTE_MARKERS: Final[frozenset[str]] = frozenset({"remote"})
DEFAULT_HYBRID_MARKERS: Final[frozenset[str]] = frozenset({"hybrid"})

# A marker preceded by one of these within `NEGATION_WINDOW` tokens is a
# mention of the arrangement being ruled *out*, not offered. The set is kept
# small and unambiguous on purpose: a missed negation only restores the
# previous, over-eager behaviour, while a word like "can" would negate the
# sentences that grant remote work. Both languages this package's sources
# publish in are covered, since a French-language board negates in French.
NEGATORS: Final[frozenset[str]] = frozenset(
    {
        # English
        "no",
        "not",
        "never",
        "neither",
        "nor",
        "without",
        "rarely",
        # French
        "non",
        "pas",
        "sans",
        "aucun",
        "aucune",
        "jamais",
    }
)
NEGATION_WINDOW: Final[int] = 3


def tokenize(text: str) -> tuple[str, ...]:
    """Fold ``text`` and cut it into comparable word tokens.

    Folding first means case, accents and punctuation stop mattering:
    ``"Télétravail total"`` and ``"teletravail  TOTAL"`` both become
    ``("teletravail", "total")``.

    Args:
        text: Any free text, in any of the languages the sources read.

    Returns:
        The tokens, in order.
    """
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    folded = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return tuple(_TOKEN_RE.findall(folded))


def is_asserted(tokens: tuple[str, ...], marker: tuple[str, ...]) -> bool:
    """Whether ``marker`` occurs in ``tokens`` as an unnegated whole phrase.

    Args:
        tokens: The tokens of the text being searched.
        marker: The tokens of the marker phrase to look for.

    Returns:
        Whether at least one occurrence of ``marker`` stands unnegated.
    """
    if not marker or len(marker) > len(tokens):
        return False
    span = len(marker)
    for index in range(len(tokens) - span + 1):
        if tokens[index : index + span] != marker:
            continue
        if not NEGATORS.intersection(tokens[max(0, index - NEGATION_WINDOW) : index]):
            return True
    return False


def _any_asserted(tokens: tuple[str, ...], markers: frozenset[str]) -> bool:
    """Whether any of ``markers`` is asserted in ``tokens``."""
    return any(is_asserted(tokens, tokenize(marker)) for marker in markers)


def infer_work_mode(
    primary: str | None,
    secondary: str | None = None,
    *,
    remote_markers: frozenset[str] = DEFAULT_REMOTE_MARKERS,
    hybrid_markers: frozenset[str] = DEFAULT_HYBRID_MARKERS,
) -> WorkMode:
    """Best-effort work arrangement, for a source that has no field for it.

    ``primary`` is consulted first and on its own, then ``secondary``. The
    order matters: ``primary`` is the board's own short statement about this
    job (a location field, a contract line), while ``secondary`` is prose in
    which the word "remote" turns up in asides about other teams and in
    boilerplate. Within each text, remote wins over hybrid.

    Args:
        primary: The board's own statement about the job — its location or
            contract field. Also decides the fallback: a posting that names
            a place without naming an arrangement is read as on-site, one
            that names neither stays unknown.
        secondary: A second, weaker text to consult if ``primary`` says
            nothing about the arrangement (a description, a contract type).
        remote_markers: Phrases this source's postings use for remote work.
        hybrid_markers: Phrases this source's postings use for hybrid work.

    Returns:
        The inferred :class:`~jobsearcher.models.WorkMode`.
    """
    for text in (primary, secondary):
        if not text:
            continue
        tokens = tokenize(text)
        if _any_asserted(tokens, remote_markers):
            return WorkMode.REMOTE
        if _any_asserted(tokens, hybrid_markers):
            return WorkMode.HYBRID

    if primary:
        return WorkMode.ONSITE
    return WorkMode.UNKNOWN


__all__ = [
    "DEFAULT_HYBRID_MARKERS",
    "DEFAULT_REMOTE_MARKERS",
    "NEGATION_WINDOW",
    "NEGATORS",
    "infer_work_mode",
    "is_asserted",
    "tokenize",
]
