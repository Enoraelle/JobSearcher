"""Offline file exporters: CSV, JSON, and Markdown.

These three use only the standard library. They share
one option, ``output_path`` (where to write), and one ordering rule: postings come
out sorted by score, highest first, with unscored postings last. That order
is the point of an export (you read the top of the list first), so it is
applied to every format, not just the human-readable one. Missing parent
directories in ``output_path`` are created on write (see :func:`_write_text`).

- **CSV** (:class:`CsvExporter`): one row per posting, a fixed column set,
  list-valued fields joined with ``"; "`` so every cell is a scalar. For
  spreadsheets and quick filtering — which is why a cell that would open a
  formula is prefixed with an apostrophe first (see :func:`_defuse_formula`);
  the text in it came off the web and this file is meant to be opened in
  Excel or Sheets.
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
    "detected_language",
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

# Excel, LibreOffice and Google Sheets all read a cell starting with one of
# these as a formula rather than as text.
_FORMULA_LEADS: tuple[str, ...] = ("=", "+", "-", "@")

# Markdown's structural characters inside a link label, escaped in one pass
# so the backslash this adds cannot itself be swallowed by a backslash
# already in the text. See `_markdown_text` for why scraped values never
# reach the output unescaped.
_MARKDOWN_ESCAPES: dict[int, str] = str.maketrans({"\\": r"\\", "[": r"\[", "]": r"\]"})

# Characters that end (or cannot appear in) a bare Markdown link
# destination, and so force the angle-bracket form. See
# `_markdown_destination`.
_DESTINATION_NEEDS_BRACKETS: tuple[str, ...] = ("(", ")", "<", ">", " ")


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


def _defuse_formula(value: str) -> str:
    """Stop a spreadsheet from evaluating a cell that came off the web.

    Titles, companies and descriptions are scraped text, and a cell opening
    with ``=``, ``+``, ``-`` or ``@`` is a formula to every major spreadsheet
    — including ones that fetch a remote URL. Prefixing an apostrophe is the
    convention those applications use to mean "this is text"; they strip it
    on display and it costs nothing to a program reading the file.

    Args:
        value: One cell's text.

    Returns:
        ``value``, prefixed with an apostrophe if it would otherwise be read
        as a formula.
    """
    if value.startswith(_FORMULA_LEADS):
        return f"'{value}"
    return value


def _flatten_cell(value: Any) -> Any:
    """Render one record value as a CSV cell.

    Missing values become empty cells, and text is defused first (see
    :func:`_defuse_formula`); numbers pass through as they are, so the
    ``score`` column stays numeric.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return _defuse_formula(value)
    return value


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
        flatten: If true (CSV), join list fields into a single string,
            replace ``None`` with ``""``, and defuse cells a spreadsheet
            would evaluate as formulas. If false (JSON), keep lists as
            lists and ``None`` as ``null``, and leave text exactly as
            stored — the apostrophe is a spreadsheet convention and would
            be data corruption to a program reading the JSON.
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
        # Blank when the language heuristic could not decide (see
        # `JobPosting.detected_language`); the language filter that `list`,
        # `export` and `run` share reads this same field, so an export that
        # omitted it could not be reproduced from its own output.
        "detected_language": posting.detected_language,
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
        record = {key: _flatten_cell(value) for key, value in record.items()}
    return record


def _write_text(path: Path, text: str) -> None:
    try:
        # An `output_path` in a not-yet-existing subdirectory
        # (`out/2026-09/jobs.json`) is a reasonable thing to configure; create
        # the parent chain rather than failing with `FileNotFoundError`. For a
        # bare filename `path.parent` is `.`, and the call is a no-op.
        path.parent.mkdir(parents=True, exist_ok=True)
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


def _markdown_text(value: str) -> str:
    """Render scraped text as Markdown *text*, not as markup.

    Titles and company names come off the web, and the bullet built below
    puts them inside a link label — where a bracket is syntax. "Engineer
    [Remote]" is an entirely ordinary title and already breaks the link; a
    company name containing ``](https://elsewhere.test)`` closes the label
    early and points the link somewhere else entirely. A newline is the
    third case: a bullet is one line, and a title carrying a line break
    splits one posting into two list items, the second of which is not a
    posting.

    Backslashes are escaped first (in the same pass, via ``str.translate``),
    so an escape this function adds cannot be neutralized by one already in
    the text.

    Args:
        value: Text to place inside Markdown output.

    Returns:
        ``value`` with internal whitespace collapsed to single spaces and
        every Markdown-structural character escaped.
    """
    return " ".join(value.split()).translate(_MARKDOWN_ESCAPES)


def _markdown_destination(url: str) -> str:
    """Render a URL as a link destination that survives its own punctuation.

    A bare destination ends at the first ``)``, so a perfectly valid posting
    URL containing one truncates the link and spills the rest of itself into
    the visible text. Angle brackets are Markdown's own way to delimit a
    destination, and are used only for the URLs that need them: wrapping
    every link would make the common case noisier for no gain. The only
    characters that cannot appear between them are ``<`` and ``>``, which
    are percent-encoded — lossless for a URL, unlike escaping.

    Args:
        url: The posting's normalized URL.

    Returns:
        The destination, ready to be placed between the link's parentheses.
    """
    if not any(char in url for char in _DESTINATION_NEEDS_BRACKETS):
        return url
    return "<" + url.replace("<", "%3C").replace(">", "%3E") + ">"


def _markdown_bullet(posting: JobPosting) -> str:
    score = "n/a" if posting.score is None else str(posting.score)
    label = f"{_markdown_text(posting.title)} — {_markdown_text(posting.company)}"
    segments = [f"**{score}** — [{label}]({_markdown_destination(posting.url)})"]
    if posting.work_mode is not WorkMode.UNKNOWN:
        segments.append(posting.work_mode.value)
    if posting.location:
        segments.append(_markdown_text(posting.location))
    if posting.eligible_locations:
        joined = ", ".join(_markdown_text(place) for place in posting.eligible_locations)
        segments.append(f"eligible: {joined}")
    return "- " + " · ".join(segments)


__all__ = ["CsvExporter", "JsonExporter", "MarkdownExporter"]
