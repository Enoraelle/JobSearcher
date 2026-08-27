"""Exporters for writing scored job postings to external formats and destinations.

The file formats (:class:`~jobsearcher.exporters.files.CsvExporter`,
:class:`~jobsearcher.exporters.files.JsonExporter`,
:class:`~jobsearcher.exporters.files.MarkdownExporter`) use only the standard
library and are always importable.
:class:`~jobsearcher.exporters.notion.NotionExporter` is a bonus behind the
``notion`` extra and is imported on demand — :func:`get_exporter` is the
only place that reaches for it.
"""

from __future__ import annotations

from jobsearcher.exporters.base import Exporter, ExporterError, ExportResult
from jobsearcher.exporters.files import CsvExporter, JsonExporter, MarkdownExporter

# Format name -> always-importable exporter class. ``notion`` is handled
# separately in get_exporter because importing it needs the ``notion`` extra.
_BUILTIN_EXPORTERS: dict[str, type[Exporter]] = {
    "csv": CsvExporter,
    "json": JsonExporter,
    "markdown": MarkdownExporter,
}

#: Every format name :func:`get_exporter` accepts, for help text and validation.
EXPORTER_FORMATS: tuple[str, ...] = (*_BUILTIN_EXPORTERS, "notion")


class ExporterConfigError(Exception):
    """An unknown export format was requested."""


def get_exporter(fmt: str) -> Exporter:
    """Build the exporter for a format name.

    Args:
        fmt: One of :data:`EXPORTER_FORMATS`.

    Returns:
        A ready exporter.

    Raises:
        ExporterConfigError: If ``fmt`` is not a known format.
        ModuleNotFoundError: If ``fmt`` is ``notion`` but the ``notion``
            extra is not installed (the message says how to fix it).
    """
    builtin = _BUILTIN_EXPORTERS.get(fmt)
    if builtin is not None:
        return builtin()
    if fmt == "notion":
        from jobsearcher.exporters.notion import NotionExporter

        return NotionExporter()
    raise ExporterConfigError(
        f"unknown export format {fmt!r}; expected one of: {', '.join(EXPORTER_FORMATS)}"
    )


__all__ = [
    "EXPORTER_FORMATS",
    "CsvExporter",
    "ExportResult",
    "Exporter",
    "ExporterConfigError",
    "ExporterError",
    "JsonExporter",
    "MarkdownExporter",
    "get_exporter",
]
