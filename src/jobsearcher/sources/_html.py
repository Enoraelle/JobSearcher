"""Plain-text extraction from the HTML fragments sources receive.

Several sources get a posting body as an HTML fragment and need the same
thing from it: readable plain text, with block-level elements turned into
line breaks so adjacent paragraphs and list items don't run together into
one word-salad line. That text feeds the scorer's haystack, the language
heuristic, and every exporter, so all sources must produce it identically.

This lives in one module, and not once per source, because the extraction
has already had to change for all of its callers at once: what a source
receives is not always ready to be parsed as markup (see
:func:`jobsearcher.sources.greenhouse._description_html`). A second copy is
a second place to forget.

It is internal to :mod:`jobsearcher.sources` — nothing outside the package
should reach for it.
"""

from __future__ import annotations

from html.parser import HTMLParser
from typing import Final

# Block-level tags that should introduce a line break when extracting plain
# text, so e.g. adjacent <p> tags don't run together into one line.
BLOCK_TAGS: Final[frozenset[str]] = frozenset(
    {"p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr"}
)


class HTMLTextExtractor(HTMLParser):
    """Extracts plain text from HTML, inserting line breaks at block tags."""

    def __init__(self) -> None:
        super().__init__()
        self._chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        self._chunks.append(data)

    def get_text(self) -> str:
        """Return everything fed so far, joined."""
        return "".join(self._chunks)


def strip_html(html_content: str) -> str:
    """Convert an HTML fragment into cleaned-up plain text.

    Args:
        html_content: Real HTML markup. A caller whose source delivers
            *escaped* markup must unescape it first — this function parses
            what it is given, and escaped tags would survive as literal
            text.

    Returns:
        The text content, with block-level elements separated by newlines
        and blank lines removed. Empty for input that holds no text.
    """
    if not html_content.strip():
        return ""
    extractor = HTMLTextExtractor()
    extractor.feed(html_content)
    extractor.close()
    lines = (line.strip() for line in extractor.get_text().splitlines())
    return "\n".join(line for line in lines if line)


__all__ = ["BLOCK_TAGS", "HTMLTextExtractor", "strip_html"]
