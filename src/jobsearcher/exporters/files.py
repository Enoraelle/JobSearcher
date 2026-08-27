"""Offline file exporters: CSV, JSON, and Markdown.

These three use only the standard library — no extra to install. They share
one option, ``output_path`` (where to write), and one ordering rule: postings come
out sorted by score, highest first, with unscored postings last. That order
is the point of an export (you read the top of the list first), so it is
applied to every format, not just the human-readable one.

- **CSV** (:class:`CsvExporter`): one row per posting, a fixed column set,
  list-valued fields joined with ``"; "`` so every cell is a scalar. For
  spreadsheets and quick filtering.
- **JSON** (:class:`JsonExporter`): a ``{"count": N, "postings": [...]}``
  object; list fields stay lists and datetimes are ISO-8601 strings. For
  feeding another tool.
- **Markdown** (:class:`MarkdownExporter`): a heading plus one bullet per
  posting, each bullet carrying the score, a link to the posting, and — when
  the posting carries them — its geographic eligibility restrictions. Meant
  to be read by a person.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from jobsearcher.config import PluginConfig
from jobsearcher.exporters.base import ExporterError, ExportResult
from jobsearcher.models import JobPosting, WorkMode

# Column order for CSV, and key order for each JSON record. Kept in one place
# so the two formats never drift apart.
_FIELD_NAMES: tuple[str, ...] = (
    "score",
    "title",
    "company",
    "source",
    "status",
    "work_mode",
    "location",
    "eligible_locations",
    "url",
    "published_at",
    "salary_text",
    "summary",
    "matched_skills",
    "missing_requirements",
    "penalized_skills",
)

# Record fields that are lists on the model.
_LIST_FIELDS: frozenset[str] = frozenset(
    {"eligible_locations", "matched_skills", "missing_requirements", "penalized_skills"}
)

_LIST_SEPARATOR = "; "


class _FileOptions(BaseModel):
    """The ``exporters.<name>`` block for a file exporter.

    ``extra="forbid"`` so a mistyped option is an error, not a silent no-op.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    output_path: Path


def _resolve_path(options: PluginConfig) -> Path:
    """Validate ``options`` and return the output path.

    Raises:
        ExporterError: If ``output_path`` is missing or another option is
            unknown.
    """
    try:
        return _FileOptions.model_validate(options.model_dump()).output_path
    except ValidationError as exc:
        raise ExporterError(_explain_options_error(exc)) from exc


def _explain_options_error(exc: ValidationError) -> str:
    parts: list[str] = []
    for error in exc.errors():
        location = ".".join(str(p) for p in error["loc"])
        if error["loc"] == ("output_path",) and error["type"] == "missing":
            parts.append(
                "this file exporter needs an `output_path` option saying where to "
                "write the file (e.g. `output_path: jobs.csv`)"
            )
        elif error["type"] == "extra_forbidden":
            parts.append(f"unknown option `{location}` (check for a typo)")
        else:
            parts.append(f"`{location}`: {error['msg']}")
    return "invalid file exporter configuration: " + "; ".join(parts)


def _by_score(postings: Iterable[JobPosting]) -> list[JobPosting]:
    """Sort postings by score descending, unscored last, ties broken by title."""
    return sorted(
        postings,
        key=lambda p: (p.score is None, -(p.score or 0), p.title.casefold()),
    )


def _record(posting: JobPosting, *, flatten: bool) -> dict[str, Any]:
    """Build one output record from a posting.

    Args:
        posting: The posting to render.
        flatten: If true (CSV), join list fields into a single string and
            replace ``None`` with ``""``. If false (JSON), keep lists as
            lists and ``None`` as ``null``.
    """
    published_at = posting.published_at.isoformat() if posting.published_at else None
    record: dict[str, Any] = {
        "score": posting.score,
        "title": posting.title,
        "company": posting.company,
        "source": posting.source,
        "status": posting.status.value,
        "work_mode": posting.work_mode.value,
        "location": posting.location,
        "eligible_locations": list(posting.eligible_locations),
        "url": posting.url,
        "published_at": published_at,
        "salary_text": posting.salary_text,
        "summary": posting.summary,
        "matched_skills": list(posting.matched_skills),
        "missing_requirements": list(posting.missing_requirements),
        "penalized_skills": list(posting.penalized_skills),
    }
    if flatten:
        for key in _LIST_FIELDS:
            record[key] = _LIST_SEPARATOR.join(record[key])
        record = {key: ("" if value is None else value) for key, value in record.items()}
    return record


def _write_text(path: Path, text: str) -> None:
    try:
        path.write_text(text, encoding="utf-8")
    except OSError as exc:
        raise ExporterError(f"could not write to {path}: {exc}") from exc


class CsvExporter:
    """Writes postings as a CSV file (one row per posting)."""

    def export(self, postings: Sequence[JobPosting], options: PluginConfig) -> ExportResult:
        path = _resolve_path(options)
        ordered = _by_score(postings)

        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=_FIELD_NAMES, lineterminator="\n")
        writer.writeheader()
        for posting in ordered:
            writer.writerow(_record(posting, flatten=True))

        _write_text(path, buffer.getvalue())
        return ExportResult(len(ordered), f"wrote {len(ordered)} postings to {path}")


class JsonExporter:
    """Writes postings as a single JSON object."""

    def export(self, postings: Sequence[JobPosting], options: PluginConfig) -> ExportResult:
        path = _resolve_path(options)
        ordered = _by_score(postings)

        payload = {
            "count": len(ordered),
            "postings": [_record(posting, flatten=False) for posting in ordered],
        }
        text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"

        _write_text(path, text)
        return ExportResult(len(ordered), f"wrote {len(ordered)} postings to {path}")


class MarkdownExporter:
    """Writes a human-readable Markdown digest, one bullet per posting."""

    def export(self, postings: Sequence[JobPosting], options: PluginConfig) -> ExportResult:
        path = _resolve_path(options)
        ordered = _by_score(postings)

        _write_text(path, _render_markdown(ordered))
        return ExportResult(len(ordered), f"wrote {len(ordered)} postings to {path}")


def _render_markdown(ordered: Sequence[JobPosting]) -> str:
    lines = ["# Job postings", ""]
    if not ordered:
        lines.append("_No postings to export._")
        return "\n".join(lines) + "\n"

    lines.append(f"_{len(ordered)} postings, sorted by score (highest first)._")
    lines.append("")
    for posting in ordered:
        lines.append(_markdown_bullet(posting))
    return "\n".join(lines) + "\n"


def _markdown_bullet(posting: JobPosting) -> str:
    score = "n/a" if posting.score is None else str(posting.score)
    segments = [f"**{score}** — [{posting.title} — {posting.company}]({posting.url})"]
    if posting.work_mode is not WorkMode.UNKNOWN:
        segments.append(posting.work_mode.value)
    if posting.location:
        segments.append(posting.location)
    if posting.eligible_locations:
        segments.append(f"eligible: {', '.join(posting.eligible_locations)}")
    return "- " + " · ".join(segments)


__all__ = ["CsvExporter", "JsonExporter", "MarkdownExporter"]
