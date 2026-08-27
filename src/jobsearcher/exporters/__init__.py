"""Exporters for writing scored job postings to external formats and destinations.

The file formats (:class:`~jobsearcher.exporters.files.CsvExporter`,
:class:`~jobsearcher.exporters.files.JsonExporter`,
:class:`~jobsearcher.exporters.files.MarkdownExporter`) use only the standard
library and are always importable.
:class:`~jobsearcher.exporters.notion.NotionExporter` is a bonus behind the
``notion`` extra and is imported on demand, never from here — so importing
this package never pulls in the optional path.
"""

from jobsearcher.exporters.base import Exporter, ExporterError, ExportResult
from jobsearcher.exporters.files import CsvExporter, JsonExporter, MarkdownExporter

__all__ = [
    "CsvExporter",
    "ExportResult",
    "Exporter",
    "ExporterError",
    "JsonExporter",
    "MarkdownExporter",
]
