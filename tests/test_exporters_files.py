"""Tests for the offline file exporters, verified against real written files.

Each test writes to a real path inside pytest's ``tmp_path`` and reads the
bytes back with an independent parser (``csv``, ``json``) or by inspecting
the text — no mocking of the filesystem.
"""

from __future__ import annotations

import csv
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from jobsearcher.config import PluginConfig
from jobsearcher.exporters.base import ExporterError
from jobsearcher.exporters.files import CsvExporter, JsonExporter, MarkdownExporter
from jobsearcher.models import JobPosting, WorkMode


def _posting(**overrides: Any) -> JobPosting:
    fields: dict[str, Any] = {
        "url": "https://example.com/jobs/1",
        "title": "Backend Engineer",
        "company": "Acme",
        "source": "example",
        "description_raw": "Build things.",
        "fetched_at": datetime(2026, 1, 1, tzinfo=UTC),
    }
    fields.update(overrides)
    return JobPosting(**fields)


def _opts(**kwargs: Any) -> PluginConfig:
    return PluginConfig(**kwargs)


def _three_postings() -> list[JobPosting]:
    return [
        _posting(url="https://example.com/jobs/low", title="Low", score=20),
        _posting(url="https://example.com/jobs/high", title="High", score=90),
        _posting(url="https://example.com/jobs/mid", title="Mid", score=55),
    ]


# --------------------------------------------------------------------------
# CSV
# --------------------------------------------------------------------------


def test_csv_writes_header_and_one_row_per_posting(tmp_path: Path) -> None:
    path = tmp_path / "jobs.csv"
    result = CsvExporter().export(_three_postings(), _opts(output_path=str(path)))

    rows = list(csv.DictReader(path.read_text(encoding="utf-8").splitlines()))
    assert [row["title"] for row in rows] == ["High", "Mid", "Low"]
    assert result.exported == 3
    assert "jobs.csv" in result.detail


def test_csv_joins_list_fields_into_one_cell(tmp_path: Path) -> None:
    path = tmp_path / "jobs.csv"
    posting = _posting(
        score=70,
        eligible_locations=["EU", "Germany"],
        matched_skills=["python", "django"],
    )
    CsvExporter().export([posting], _opts(output_path=str(path)))

    row = next(csv.DictReader(path.read_text(encoding="utf-8").splitlines()))
    assert row["eligible_locations"] == "EU; Germany"
    assert row["matched_skills"] == "python; django"


def test_csv_renders_missing_optional_fields_as_empty(tmp_path: Path) -> None:
    path = tmp_path / "jobs.csv"
    CsvExporter().export([_posting()], _opts(output_path=str(path)))

    row = next(csv.DictReader(path.read_text(encoding="utf-8").splitlines()))
    assert row["score"] == ""
    assert row["published_at"] == ""
    assert row["summary"] == ""


@pytest.mark.parametrize("lead", ["=", "+", "-", "@"])
def test_csv_defuses_cells_a_spreadsheet_would_run_as_a_formula(tmp_path: Path, lead: str) -> None:
    """Titles come from the web, and the CSV is meant for a spreadsheet."""
    path = tmp_path / "jobs.csv"
    title = f'{lead}HYPERLINK("http://evil.test","Backend Engineer")'
    CsvExporter().export([_posting(title=title)], _opts(output_path=str(path)))

    row = next(csv.DictReader(path.read_text(encoding="utf-8").splitlines()))
    assert row["title"] == f"'{title}"


def test_csv_defuses_every_web_sourced_column(tmp_path: Path) -> None:
    path = tmp_path / "jobs.csv"
    posting = _posting(
        title="=cmd|'/c calc'!A1",
        company="@SUM(1+9)",
        location="+1-555-0100",
        summary="-2+3",
        matched_skills=["=1+1"],
    )
    CsvExporter().export([posting], _opts(output_path=str(path)))

    row = next(csv.DictReader(path.read_text(encoding="utf-8").splitlines()))
    assert row["title"] == "'=cmd|'/c calc'!A1"
    assert row["company"] == "'@SUM(1+9)"
    assert row["location"] == "'+1-555-0100"
    assert row["summary"] == "'-2+3"
    assert row["matched_skills"] == "'=1+1"


def test_csv_leaves_ordinary_cells_alone(tmp_path: Path) -> None:
    path = tmp_path / "jobs.csv"
    CsvExporter().export(
        [_posting(title="Backend Engineer", score=90)], _opts(output_path=str(path))
    )

    row = next(csv.DictReader(path.read_text(encoding="utf-8").splitlines()))
    assert row["title"] == "Backend Engineer"
    assert row["score"] == "90"
    assert not row["url"].startswith("'")


def test_json_does_not_escape_formula_characters(tmp_path: Path) -> None:
    """The apostrophe is a spreadsheet convention; JSON is read by programs."""
    path = tmp_path / "jobs.json"
    JsonExporter().export([_posting(title="=1+1")], _opts(output_path=str(path)))

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["postings"][0]["title"] == "=1+1"


def test_csv_with_no_postings_still_writes_a_header(tmp_path: Path) -> None:
    path = tmp_path / "jobs.csv"
    CsvExporter().export([], _opts(output_path=str(path)))

    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines == [
        "score,title,company,source,status,work_mode,location,"
        "eligible_locations,url,published_at,salary_text,summary,"
        "matched_skills,missing_requirements,penalized_skills"
    ]


# --------------------------------------------------------------------------
# JSON
# --------------------------------------------------------------------------


def test_json_roundtrips_to_real_content_sorted_by_score(tmp_path: Path) -> None:
    path = tmp_path / "jobs.json"
    JsonExporter().export(_three_postings(), _opts(output_path=str(path)))

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["count"] == 3
    assert [p["title"] for p in data["postings"]] == ["High", "Mid", "Low"]
    assert data["postings"][0]["score"] == 90


def test_json_keeps_lists_as_lists_and_dates_as_iso_strings(tmp_path: Path) -> None:
    path = tmp_path / "jobs.json"
    posting = _posting(
        score=70,
        eligible_locations=["EU", "Germany"],
        published_at=datetime(2026, 2, 3, 9, 30, tzinfo=UTC),
    )
    JsonExporter().export([posting], _opts(output_path=str(path)))

    record = json.loads(path.read_text(encoding="utf-8"))["postings"][0]
    assert record["eligible_locations"] == ["EU", "Germany"]
    assert record["published_at"] == "2026-02-03T09:30:00+00:00"
    assert record["score"] == 70


def test_json_unscored_posting_has_null_score(tmp_path: Path) -> None:
    path = tmp_path / "jobs.json"
    JsonExporter().export([_posting()], _opts(output_path=str(path)))

    record = json.loads(path.read_text(encoding="utf-8"))["postings"][0]
    assert record["score"] is None
    assert record["published_at"] is None


def test_json_is_written_as_utf8_not_escaped(tmp_path: Path) -> None:
    path = tmp_path / "jobs.json"
    JsonExporter().export(
        [_posting(company="Naïve Café Ltd", score=10)], _opts(output_path=str(path))
    )

    raw = path.read_text(encoding="utf-8")
    assert "Naïve Café Ltd" in raw
    assert json.loads(raw)["postings"][0]["company"] == "Naïve Café Ltd"


# --------------------------------------------------------------------------
# Markdown
# --------------------------------------------------------------------------


def test_markdown_is_sorted_by_score_with_one_bullet_per_posting(tmp_path: Path) -> None:
    path = tmp_path / "jobs.md"
    MarkdownExporter().export(_three_postings(), _opts(output_path=str(path)))

    text = path.read_text(encoding="utf-8")
    bullets = [line for line in text.splitlines() if line.startswith("- ")]
    assert len(bullets) == 3
    assert bullets[0].index("**90**") >= 0
    assert text.index("High") < text.index("Mid") < text.index("Low")


def test_markdown_bullet_links_to_the_posting(tmp_path: Path) -> None:
    path = tmp_path / "jobs.md"
    MarkdownExporter().export(
        [_posting(score=90, title="Staff Engineer")], _opts(output_path=str(path))
    )

    bullet = next(
        line for line in path.read_text(encoding="utf-8").splitlines() if line.startswith("- ")
    )
    assert "[Staff Engineer — Acme](https://example.com/jobs/1)" in bullet


def test_markdown_shows_geographic_restrictions_when_present(tmp_path: Path) -> None:
    path = tmp_path / "jobs.md"
    postings = [
        _posting(url="https://example.com/a", title="A", score=80, eligible_locations=["US only"]),
        _posting(url="https://example.com/b", title="B", score=70),
    ]
    MarkdownExporter().export(postings, _opts(output_path=str(path)))

    lines = path.read_text(encoding="utf-8").splitlines()
    bullet_a = next(line for line in lines if "](https://example.com/a)" in line)
    bullet_b = next(line for line in lines if "](https://example.com/b)" in line)
    assert "eligible: US only" in bullet_a
    assert "eligible:" not in bullet_b


def test_markdown_marks_unscored_postings(tmp_path: Path) -> None:
    path = tmp_path / "jobs.md"
    MarkdownExporter().export([_posting()], _opts(output_path=str(path)))

    bullet = next(
        line for line in path.read_text(encoding="utf-8").splitlines() if line.startswith("- ")
    )
    assert "**n/a**" in bullet


def test_markdown_with_no_postings_is_still_valid(tmp_path: Path) -> None:
    path = tmp_path / "jobs.md"
    MarkdownExporter().export([], _opts(output_path=str(path)))

    text = path.read_text(encoding="utf-8")
    assert text.startswith("# Job postings")
    assert "No postings to export" in text


def test_markdown_includes_work_mode_and_location(tmp_path: Path) -> None:
    path = tmp_path / "jobs.md"
    MarkdownExporter().export(
        [_posting(score=60, work_mode=WorkMode.REMOTE, location="Berlin")],
        _opts(output_path=str(path)),
    )

    bullet = next(
        line for line in path.read_text(encoding="utf-8").splitlines() if line.startswith("- ")
    )
    assert "remote" in bullet
    assert "Berlin" in bullet


# --------------------------------------------------------------------------
# Options handling (shared by all three)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("exporter", [CsvExporter(), JsonExporter(), MarkdownExporter()])
def test_missing_path_option_is_a_clear_error(exporter: Any) -> None:
    with pytest.raises(ExporterError, match=r"`output_path`"):
        exporter.export([_posting()], _opts())


@pytest.mark.parametrize("exporter", [CsvExporter(), JsonExporter(), MarkdownExporter()])
def test_unknown_option_is_rejected(exporter: Any, tmp_path: Path) -> None:
    with pytest.raises(ExporterError, match=r"unknown option `frmat`"):
        exporter.export([_posting()], _opts(output_path=str(tmp_path / "x"), frmat="csv"))


@pytest.mark.parametrize("exporter", [CsvExporter(), JsonExporter(), MarkdownExporter()])
def test_unwritable_path_is_wrapped_in_an_exporter_error(exporter: Any, tmp_path: Path) -> None:
    missing_dir = tmp_path / "does-not-exist" / "jobs.out"
    with pytest.raises(ExporterError, match=r"could not write to"):
        exporter.export([_posting()], _opts(output_path=str(missing_dir)))


def test_exporters_do_not_use_the_stdlib_csv_default_line_ending(tmp_path: Path) -> None:
    # csv.writer defaults to '\r\n'; the exporter forces '\n' so the file is
    # not littered with blank lines on POSIX and diffs cleanly.
    path = tmp_path / "jobs.csv"
    CsvExporter().export([_posting(score=1)], _opts(output_path=str(path)))
    assert "\r" not in path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# Markdown: scraped text is data, not markup
# --------------------------------------------------------------------------


def _markdown_bullet_for(tmp_path: Path, **overrides: Any) -> str:
    path = tmp_path / "digest.md"
    MarkdownExporter().export([_posting(**overrides)], _opts(output_path=str(path)))
    bullets = [
        line for line in path.read_text(encoding="utf-8").splitlines() if line.startswith("- ")
    ]
    assert len(bullets) == 1, path.read_text(encoding="utf-8")
    return bullets[0]


def test_markdown_keeps_the_link_intact_for_a_title_with_brackets(tmp_path: Path) -> None:
    """ "Engineer [Remote]" is an ordinary title and must not break the link."""
    bullet = _markdown_bullet_for(tmp_path, title="Engineer [Remote]")

    assert r"Engineer \[Remote\]" in bullet
    assert bullet.endswith("(https://example.com/jobs/1)")


def test_markdown_company_cannot_hijack_the_link_target(tmp_path: Path) -> None:
    """A company name is scraped text; it must not be able to retarget a link."""
    bullet = _markdown_bullet_for(tmp_path, company="Acme](https://evil.test)")

    # The injected bracket is escaped, so it stays part of the label text and
    # the link still points where the exporter put it.
    assert r"Acme\](https://evil.test)" in bullet
    assert bullet.endswith("(https://example.com/jobs/1)")


def test_markdown_keeps_one_posting_on_one_bullet(tmp_path: Path) -> None:
    """A newline inside a title must not split the bullet in two."""
    bullet = _markdown_bullet_for(tmp_path, title="Backend Engineer\nRemote  friendly")

    assert "\n" not in bullet
    assert "Backend Engineer Remote friendly" in bullet


def test_markdown_escapes_brackets_in_the_location_fields(tmp_path: Path) -> None:
    bullet = _markdown_bullet_for(
        tmp_path,
        location="Paris [FR]",
        eligible_locations=["EU [only]"],
    )

    assert r"Paris \[FR\]" in bullet
    assert r"EU \[only\]" in bullet


def test_markdown_link_survives_a_parenthesis_in_the_url(tmp_path: Path) -> None:
    """A `)` in the URL ends the link destination early if left bare."""
    bullet = _markdown_bullet_for(tmp_path, url="https://example.com/jobs/lead(backend)")

    assert bullet.endswith("(<https://example.com/jobs/lead(backend)>)")
