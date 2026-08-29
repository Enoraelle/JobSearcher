"""Smoke tests for the jobsearcher package and end-to-end tests for its CLI.

Every command is exercised through Click's ``CliRunner`` in an isolated
filesystem. The only source used is the network-free ``fake`` source
registered in ``conftest.py``.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner

import jobsearcher
from jobsearcher.cli import main

_WIDE_ENV = {"COLUMNS": "220"}


def test_package_imports() -> None:
    """The jobsearcher package imports and exposes a version string."""
    assert isinstance(jobsearcher.__version__, str)
    assert jobsearcher.__version__


def test_cli_version() -> None:
    """`jobsearcher --version` exits successfully and reports the package version."""
    result = CliRunner().invoke(main, ["--version"])

    assert result.exit_code == 0
    assert jobsearcher.__version__ in result.output


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _config_dict(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "profile": {
            "role": "Backend Developer",
            "skills": ["python", "django"],
            "absent_skills": ["php"],
        },
        "search": {},
        "sources": {},
        "storage": {"backend": "sqlite", "path": "./jobsearcher.db"},
        "scoring": {"backend": "keyword_match"},
        "exporters": {"markdown": {"enabled": True, "output_path": "./digest.md"}},
    }
    base.update(overrides)
    return base


def _fake_source(*titles: str, **extra: Any) -> dict[str, Any]:
    postings = [
        {"url": f"https://jobs.test/{index}", "title": title} for index, title in enumerate(titles)
    ]
    return {"fake": {"enabled": True, "postings": postings, **extra}}


# Long enough, and French enough, for language.detect_language to call it `fr`.
_FRENCH_DESCRIPTION = (
    "Nous recherchons un developpeur backend pour rejoindre notre equipe, "
    "avec des projets de refonte de la plateforme que nous menons pour "
    "les clients qui nous font confiance."
)


def _two_languages() -> dict[str, Any]:
    """One posting whose language is undetermined, one detected as French."""
    return {
        "fake": {
            "enabled": True,
            "postings": [
                {"url": "https://jobs.test/en", "title": "Backend Engineer"},
                {
                    "url": "https://jobs.test/fr",
                    "title": "Developpeur Backend",
                    "description": _FRENCH_DESCRIPTION,
                },
            ],
        }
    }


def _json_exporter() -> dict[str, Any]:
    return {"json": {"enabled": True, "output_path": "./out.json"}}


def _exported_urls(path: str = "out.json") -> list[str]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    urls: list[str] = [posting["url"] for posting in payload["postings"]]
    return urls


@pytest.fixture
def runner() -> Iterator[CliRunner]:
    cli_runner = CliRunner()
    with cli_runner.isolated_filesystem():
        yield cli_runner


def _write_config(**overrides: Any) -> None:
    with open("config.yaml", "w", encoding="utf-8") as handle:
        yaml.safe_dump(_config_dict(**overrides), handle)


def _invoke(runner: CliRunner, *args: str) -> Any:
    return runner.invoke(main, list(args), env=_WIDE_ENV)


# --------------------------------------------------------------------------
# init
# --------------------------------------------------------------------------


def test_init_creates_config_from_the_bundled_example(runner: CliRunner) -> None:
    """`init` works with nothing in the working directory but the package."""
    result = _invoke(runner, "init")

    assert result.exit_code == 0
    assert Path("config.yaml").is_file()
    # The bundled example must survive the round trip as a loadable config.
    listed = _invoke(runner, "list")
    assert listed.exit_code == 0


def test_init_refuses_to_overwrite_without_force(runner: CliRunner) -> None:
    _invoke(runner, "init")

    result = _invoke(runner, "init")
    assert result.exit_code == 1

    forced = _invoke(runner, "init", "--force")
    assert forced.exit_code == 0


# --------------------------------------------------------------------------
# missing / bad config
# --------------------------------------------------------------------------


def test_commands_fail_cleanly_without_a_config(runner: CliRunner) -> None:
    result = _invoke(runner, "list")
    assert result.exit_code == 1
    assert "configuration file" in result.output.lower()


# --------------------------------------------------------------------------
# fetch
# --------------------------------------------------------------------------


def test_fetch_stores_postings(runner: CliRunner) -> None:
    _write_config(sources=_fake_source("Backend Engineer", "Frontend Engineer"))

    result = _invoke(runner, "fetch")

    assert result.exit_code == 0
    listed = _invoke(runner, "list")
    assert "Backend Engineer" in listed.output


def test_fetch_unknown_source_exits_1(runner: CliRunner) -> None:
    _write_config(sources=_fake_source("Backend Engineer"))

    result = _invoke(runner, "fetch", "--source", "does-not-exist")

    assert result.exit_code == 1


def test_fetch_dry_run_stores_nothing(runner: CliRunner) -> None:
    _write_config(sources=_fake_source("Backend Engineer"))

    _invoke(runner, "--dry-run", "fetch")

    listed = _invoke(runner, "list")
    assert "No postings" in listed.output


# --------------------------------------------------------------------------
# score / list
# --------------------------------------------------------------------------


def test_score_then_list_shows_a_score(runner: CliRunner) -> None:
    _write_config(sources=_fake_source("Backend Engineer"))
    _invoke(runner, "fetch")

    scored = _invoke(runner, "score")
    assert scored.exit_code == 0

    listed = _invoke(runner, "list", "--min-score", "1")
    assert "Backend Engineer" in listed.output
    assert "100" in listed.output


def test_list_json_is_machine_readable(runner: CliRunner) -> None:
    _write_config(sources=_fake_source("Backend Engineer"))
    _invoke(runner, "fetch")

    result = _invoke(runner, "list", "--json")

    payload = json.loads(result.output)
    assert len(payload) == 1
    assert payload[0]["title"] == "Backend Engineer"
    assert len(payload[0]["id"]) == 12


def test_list_unscored_filter(runner: CliRunner) -> None:
    _write_config(sources=_fake_source("Backend Engineer", "Frontend Engineer"))
    _invoke(runner, "fetch")
    _invoke(runner, "score", "--limit", "1")

    result = _invoke(runner, "list", "--unscored", "--json")
    assert len(json.loads(result.output)) == 1


# --------------------------------------------------------------------------
# show / status
# --------------------------------------------------------------------------


def _first_id(runner: CliRunner) -> str:
    payload = json.loads(_invoke(runner, "list", "--json").output)
    identifier: str = payload[0]["id"]
    return identifier


def test_show_by_id_prefix_and_by_url(runner: CliRunner) -> None:
    _write_config(sources=_fake_source("Backend Engineer"))
    _invoke(runner, "fetch")
    identifier = _first_id(runner)

    by_prefix = _invoke(runner, "show", identifier[:6])
    assert by_prefix.exit_code == 0
    assert "Backend Engineer" in by_prefix.output

    by_url = _invoke(runner, "show", "https://jobs.test/0")
    assert by_url.exit_code == 0
    assert "Backend Engineer" in by_url.output


def test_show_unknown_ref_exits_1(runner: CliRunner) -> None:
    _write_config(sources=_fake_source("Backend Engineer"))
    _invoke(runner, "fetch")

    result = _invoke(runner, "show", "deadbeef")
    assert result.exit_code == 1
    assert "No posting" in result.output


def test_show_rejects_a_too_short_ref(runner: CliRunner) -> None:
    _write_config(sources=_fake_source("Backend Engineer"))
    _invoke(runner, "fetch")

    result = _invoke(runner, "show", "ab")
    assert result.exit_code == 1


def test_status_transition_is_persisted(runner: CliRunner) -> None:
    _write_config(sources=_fake_source("Backend Engineer"))
    _invoke(runner, "fetch")
    identifier = _first_id(runner)

    result = _invoke(runner, "status", identifier[:6], "applied")
    assert result.exit_code == 0

    listed = _invoke(runner, "list", "--status", "applied", "--json")
    assert len(json.loads(listed.output)) == 1


def test_status_rejects_an_unknown_value(runner: CliRunner) -> None:
    _write_config(sources=_fake_source("Backend Engineer"))
    _invoke(runner, "fetch")

    result = _invoke(runner, "status", "https://jobs.test/0", "banana")
    assert result.exit_code == 2  # Click usage error


# --------------------------------------------------------------------------
# export / run
# --------------------------------------------------------------------------


def test_export_markdown_writes_a_file(runner: CliRunner) -> None:
    _write_config(sources=_fake_source("Backend Engineer"))
    _invoke(runner, "fetch")

    result = _invoke(runner, "export", "markdown", "--output", "out.md")
    assert result.exit_code == 0
    with open("out.md", encoding="utf-8") as handle:
        assert "Backend Engineer" in handle.read()


def test_export_dry_run_does_not_write(runner: CliRunner) -> None:
    _write_config(sources=_fake_source("Backend Engineer"))
    _invoke(runner, "fetch")

    result = _invoke(runner, "--dry-run", "export", "markdown", "--output", "out.md")
    assert result.exit_code == 0
    assert not Path("out.md").exists()


def test_run_executes_all_phases(runner: CliRunner) -> None:
    _write_config(sources=_fake_source("Backend Engineer", "Platform Engineer"))

    result = _invoke(runner, "run")

    assert result.exit_code == 0
    with open("digest.md", encoding="utf-8") as handle:
        body = handle.read()
    assert "Backend Engineer" in body

    listed = json.loads(_invoke(runner, "list", "--json").output)
    assert all(row["score"] is not None for row in listed)


# --------------------------------------------------------------------------
# Rendering of untrusted posting text
# --------------------------------------------------------------------------
#
# Titles and descriptions come from scraped web pages. Rich treats `[...]` as
# markup, which fails two ways: a stray closing tag raises MarkupError (a
# traceback, when the CLI promises an exit code), and a well-formed tag is
# consumed silently, deleting text from the table nobody will notice is gone.

_MARKUP_TITLES = (
    "[/b] Backend Engineer",  # stray closing tag -> MarkupError
    "Dev [bold]role",  # opening tag -> silently swallowed
    "C++ [red]team[/red] lead",  # balanced pair -> silently swallowed
    "Engineer [not-a-style] wanted",
)


def test_list_renders_markup_metacharacters_in_titles_literally(runner: CliRunner) -> None:
    _write_config(sources=_fake_source(*_MARKUP_TITLES))
    _invoke(runner, "fetch")

    result = _invoke(runner, "list")

    assert result.exit_code == 0, result.output
    # Nothing may be dropped: every bracketed fragment survives on screen.
    assert "[/b]" in result.output
    assert "[bold]" in result.output
    assert "[red]" in result.output
    assert "[not-a-style]" in result.output


def test_show_renders_markup_metacharacters_in_title_and_description(
    runner: CliRunner,
) -> None:
    hostile = "Ops [/b] Engineer"
    description = "You will own [deploys] and [/rollbacks] end to end."
    _write_config(
        sources={
            "fake": {
                "enabled": True,
                "postings": [
                    {
                        "url": "https://jobs.test/markup",
                        "title": hostile,
                        "description": description,
                    }
                ],
            }
        }
    )
    _invoke(runner, "fetch")

    result = _invoke(runner, "show", "https://jobs.test/markup")

    assert result.exit_code == 0, result.output
    assert "[/b]" in result.output
    assert "[deploys]" in result.output
    assert "[/rollbacks]" in result.output


def test_score_failure_report_renders_a_markup_title_literally(runner: CliRunner) -> None:
    """The failed-posting list in `score` prints titles too."""
    _write_config(sources=_fake_source("[/i] Data Engineer"))
    _invoke(runner, "fetch")

    result = _invoke(runner, "list", "--json")

    assert result.exit_code == 0, result.output
    assert "[/i] Data Engineer" in json.loads(result.output)[0]["title"]


def test_a_storage_path_in_a_missing_directory_fails_cleanly(runner: CliRunner) -> None:
    """`path: ./data/jobs.db` with no ./data is an ordinary config mistake.

    It must produce the CLI's documented exit 1 and an actionable message,
    not a sqlite3 traceback out of every single command.
    """
    _write_config(storage={"backend": "sqlite", "path": "./data/jobs.db"})

    for command in (["list"], ["fetch"], ["score"], ["export", "markdown"]):
        result = _invoke(runner, *command)

        assert result.exit_code == 1, (command, result.output)
        assert result.exception is None or isinstance(result.exception, SystemExit)
        assert "storage.path" in result.output
        assert "Traceback" not in result.output


# --------------------------------------------------------------------------
# Rows that cannot be read must be counted out loud
# --------------------------------------------------------------------------


def _corrupt_one_stored_row(column: str = "work_mode", value: str = "teleportation") -> str:
    """Break one stored row in place and return its url."""
    connection = sqlite3.connect("jobsearcher.db")
    try:
        (url,) = connection.execute("SELECT url FROM postings ORDER BY url LIMIT 1").fetchone()
        connection.execute(f"UPDATE postings SET {column} = ? WHERE url = ?", (value, url))
        connection.commit()
    finally:
        connection.close()
    return str(url)


def test_list_reports_rows_it_could_not_read(runner: CliRunner) -> None:
    """Skipping a broken row silently is the failure this whole pass is about."""
    _write_config(sources=_fake_source("Backend Engineer", "Django Developer"))
    _invoke(runner, "fetch")
    _corrupt_one_stored_row()

    result = _invoke(runner, "list")

    assert result.exit_code == 0, result.output
    assert "1 stored row" in result.output
    assert "could not be read" in result.output
    # The readable one is still shown.
    assert "Django Developer" in result.output


def test_list_says_nothing_when_every_row_reads(runner: CliRunner) -> None:
    _write_config(sources=_fake_source("Backend Engineer"))
    _invoke(runner, "fetch")

    result = _invoke(runner, "list")

    assert "could not be read" not in result.output


def test_list_json_reports_unreadable_rows_on_stderr_only(runner: CliRunner) -> None:
    """`list --json` must stay pipeable, so the warning goes to stderr."""
    _write_config(sources=_fake_source("Backend Engineer", "Django Developer"))
    _invoke(runner, "fetch")
    _corrupt_one_stored_row()

    result = runner.invoke(
        main, ["list", "--json"], env=_WIDE_ENV, catch_exceptions=False, color=False
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert len(payload) == 1


def test_show_does_not_claim_a_broken_posting_is_absent(runner: CliRunner) -> None:
    """ "No posting matches" would be a lie, and the exact bug moved elsewhere."""
    _write_config(sources=_fake_source("Backend Engineer"))
    _invoke(runner, "fetch")
    url = _corrupt_one_stored_row()

    result = _invoke(runner, "show", url)

    assert result.exit_code == 1
    assert "could not be read" in result.output
    assert "no posting" not in result.output.casefold()
    # The hint continues the sentence naming the ref; it must not restate
    # the subject ("The posting at '...' it is stored but...").
    assert f"The posting at {url!r} is stored" in result.output


def test_export_reports_rows_it_could_not_read(runner: CliRunner) -> None:
    """An export silently short a row is the same silent loss, in a file."""
    _write_config(sources=_fake_source("Backend Engineer", "Django Developer"))
    _invoke(runner, "fetch")
    _corrupt_one_stored_row()

    result = _invoke(runner, "export", "markdown")

    assert result.exit_code == 0, result.output
    assert "1 stored row" in result.output
    assert "could not be read" in result.output


def test_run_reports_rows_it_could_not_read(runner: CliRunner) -> None:
    """`run` is the cron entry point: nobody is watching while it works."""
    _write_config(sources=_fake_source("Backend Engineer", "Django Developer"))
    _invoke(runner, "fetch")
    _corrupt_one_stored_row()

    result = _invoke(runner, "run", "--skip-fetch")

    assert result.exit_code == 0, result.output
    assert "1 stored row" in result.output
    assert "could not be read" in result.output


def test_run_reports_them_from_the_score_phase_when_export_is_skipped(
    runner: CliRunner,
) -> None:
    """`run --skip-score`-style cron shapes must not lose the report."""
    _write_config(sources=_fake_source("Backend Engineer", "Django Developer"))
    _invoke(runner, "fetch")
    _corrupt_one_stored_row()

    result = _invoke(runner, "run", "--skip-fetch", "--skip-export")

    assert result.exit_code == 0, result.output
    assert "1 stored row" in result.output
    assert "could not be read" in result.output


def test_run_says_nothing_when_every_row_reads(runner: CliRunner) -> None:
    _write_config(sources=_fake_source("Backend Engineer"))

    result = _invoke(runner, "run")

    assert result.exit_code == 0, result.output
    assert "could not be read" not in result.output


def test_run_still_succeeds_despite_an_unreadable_row(runner: CliRunner) -> None:
    """A skipped row is reported, not promoted to a phase failure."""
    _write_config(sources=_fake_source("Backend Engineer", "Django Developer"))
    _invoke(runner, "fetch")
    _corrupt_one_stored_row()

    result = _invoke(runner, "run", "--skip-fetch")

    assert result.exit_code == 0
    assert "wrote 1 postings" in result.output


# --------------------------------------------------------------------------
# list and export cover the same postings
# --------------------------------------------------------------------------


def test_export_defaults_to_the_same_language_scope_as_list(runner: CliRunner) -> None:
    """Listing 1 posting and then exporting 2 makes the shown list a lie."""
    _write_config(sources=_two_languages(), exporters=_json_exporter())
    _invoke(runner, "fetch")

    listed = json.loads(_invoke(runner, "list", "--json").output)
    result = _invoke(runner, "export", "json")

    assert result.exit_code == 0
    assert [posting["url"] for posting in listed] == ["https://jobs.test/en"]
    assert _exported_urls() == ["https://jobs.test/en"]


def test_export_language_all_widens_the_scope_like_list(runner: CliRunner) -> None:
    _write_config(sources=_two_languages(), exporters=_json_exporter())
    _invoke(runner, "fetch")

    listed = json.loads(_invoke(runner, "list", "--language", "all", "--json").output)
    _invoke(runner, "export", "json", "--language", "all")

    assert len(listed) == 2
    assert sorted(_exported_urls()) == ["https://jobs.test/en", "https://jobs.test/fr"]


def test_export_takes_an_explicit_language(runner: CliRunner) -> None:
    _write_config(sources=_two_languages(), exporters=_json_exporter())
    _invoke(runner, "fetch")

    _invoke(runner, "export", "json", "--language", "fr")

    # The undetermined posting is never hidden by a language filter.
    assert sorted(_exported_urls()) == ["https://jobs.test/en", "https://jobs.test/fr"]


def test_export_unscored_filter(runner: CliRunner) -> None:
    _write_config(
        sources=_fake_source("Backend Engineer", "Frontend Engineer"),
        exporters=_json_exporter(),
    )
    _invoke(runner, "fetch")
    _invoke(runner, "score", "--limit", "1")

    result = _invoke(runner, "export", "json", "--unscored")

    assert result.exit_code == 0
    assert len(_exported_urls()) == 1


def test_export_unscored_rejects_score_bounds(runner: CliRunner) -> None:
    _write_config(sources=_fake_source("Backend Engineer"), exporters=_json_exporter())
    _invoke(runner, "fetch")

    result = _invoke(runner, "export", "json", "--unscored", "--min-score", "10")

    assert result.exit_code == 2
    assert "--unscored cannot be combined" in result.output


def test_run_exports_the_same_scope_as_list(runner: CliRunner) -> None:
    _write_config(sources=_two_languages(), exporters=_json_exporter())

    _invoke(runner, "run")

    assert _exported_urls() == ["https://jobs.test/en"]


def test_run_language_all_widens_its_export(runner: CliRunner) -> None:
    _write_config(sources=_two_languages(), exporters=_json_exporter())

    _invoke(runner, "run", "--language", "all")

    assert sorted(_exported_urls()) == ["https://jobs.test/en", "https://jobs.test/fr"]


def test_score_explains_the_renamed_llm_budget_key(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A config.yaml carrying the old key must say what to change, and why."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    _write_config(
        sources=_fake_source("Backend Engineer"),
        scoring={"backend": "llm", "max_postings_per_run": 25},
    )
    _invoke(runner, "fetch")

    result = _invoke(runner, "score")

    assert result.exit_code == 1
    assert "max_requests_per_run" in result.output
    assert "Extra inputs are not permitted" not in result.output
