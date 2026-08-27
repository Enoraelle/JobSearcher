"""The exporter contract.

An :class:`Exporter` turns a batch of stored (usually scored) postings into
an external representation: a file on disk, a page in a third-party service,
etc. It is the mirror image of a :mod:`~jobsearcher.sources` source and obeys
the same one-way dependency rule (see CLAUDE.md): it depends on
:mod:`~jobsearcher.models` and :mod:`~jobsearcher.config` only, and knows
nothing about scraping, scoring, or storage internals.

Implementations live one per module in this package. The offline file
formats (``files.py``) are always importable; :class:`NotionExporter`
(``notion.py``) is a bonus behind the ``notion`` extra and is imported on
demand, never from this package's ``__init__``.

Every failure an exporter reports to its caller is an :class:`ExporterError`
whose message is *actionable*: it names the option to set, the path that
could not be written, or the exact property to create in the remote service.
A bare stack trace is never an acceptable outcome for a misconfigured
export.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from jobsearcher.config import PluginConfig
from jobsearcher.models import JobPosting


class ExporterError(Exception):
    """An export could not be completed.

    The message must tell the user what to change. Examples: "this file
    exporter needs a ``path`` option", "could not write to /x/y.csv:
    Permission denied", "the Notion database is missing a property named
    'Score' (type: Number); create it, or point ``score_property`` at an
    existing Number property".
    """


@dataclass(frozen=True)
class ExportResult:
    """What an :meth:`Exporter.export` call did.

    Attributes:
        exported: How many postings were written to the destination.
        detail: A short, human-readable, past-tense summary suitable for
            printing to the user (e.g. ``"wrote 12 postings to jobs.md"`` or
            ``"3 created, 9 updated in Notion database abc123"``).
    """

    exported: int
    detail: str


class Exporter(Protocol):
    """Turns postings into an external representation."""

    def export(
        self, postings: Sequence[JobPosting], options: PluginConfig
    ) -> ExportResult:
        """Write ``postings`` to this exporter's destination.

        Args:
            postings: The postings to export. The exporter decides the
                output order (the built-in exporters sort by score, highest
                first); it must not mutate the postings.
            options: This exporter's configuration block, straight from
                ``config.yaml``'s ``exporters:`` section. Each implementation
                validates it into its own typed schema and rejects unknown
                keys, so a typo surfaces instead of being ignored. Secrets
                (API keys) are never read from here — only from the
                environment.

        Returns:
            A summary of what was exported.

        Raises:
            ExporterError: If the export could not be completed. The message
                names what the user must fix.
        """
        ...


__all__ = ["ExportResult", "Exporter", "ExporterError"]
