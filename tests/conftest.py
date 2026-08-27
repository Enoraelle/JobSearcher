"""Shared test fixtures and a network-free fake source.

The fake source is registered once here (conftest is imported before any
test module) and replays postings straight from its configuration block, so
pipeline and CLI tests never touch the network — see CLAUDE.md.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

from jobsearcher.models import JobPosting, WorkMode
from jobsearcher.sources.base import Source, SourceUnavailableError, register_source

_DEFAULT_TEXT = "We build things with Python and Django every day."


@register_source("fake")
class FakeSource(Source):
    """A source that replays postings from its own config block.

    Config keys it reads (all optional): ``postings`` (a list of raw dicts,
    each needing ``url`` and ``title``), and ``fail`` (raise a
    source-level error after yielding its items).
    """

    def __init__(self, name: str, config: Any, **kwargs: Any) -> None:
        super().__init__(name, config, **kwargs)
        self._raw: list[dict[str, Any]] = list(getattr(config, "postings", []))
        self._fail = bool(getattr(config, "fail", False))

    def fetch_raw(self) -> Iterator[dict[str, Any]]:
        yield from self._raw
        if self._fail:
            raise SourceUnavailableError("fake source is down")

    def normalize(self, raw: dict[str, Any]) -> JobPosting:
        text = raw.get("description", _DEFAULT_TEXT)
        return JobPosting(
            url=raw["url"],
            title=raw["title"],
            company=raw.get("company", "FakeCo"),
            source=self.name,
            description_raw=text,
            description_clean=text,
            work_mode=WorkMode(raw["work_mode"]) if raw.get("work_mode") else WorkMode.UNKNOWN,
            fetched_at=datetime.now(UTC),
        )


def raw_posting(url: str, title: str, **extra: Any) -> dict[str, Any]:
    """Build a raw posting dict for :class:`FakeSource`'s ``postings`` config."""
    return {"url": url, "title": title, **extra}
