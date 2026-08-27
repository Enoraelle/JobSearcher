"""Smoke tests for the jobsearcher package and end-to-end tests for its CLI.

Every command is exercised through Click's ``CliRunner`` in an isolated
filesystem. The only source used is the network-free ``fake`` source
registered in ``conftest.py``.
"""

from __future__ import annotations

import json
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


def test_init_creates_config_from_example(runner: CliRunner) -> None:
    with open("config.example.yaml", "w", encoding="utf-8") as handle:
        yaml.safe_dump(_config_dict(), handle)

    result = _invoke(runner, "init")

    assert result.exit_code == 0
    with open("config.yaml", encoding="utf-8") as handle:
        assert "profile" in handle.read()


def test_init_refuses_to_overwrite_without_force(runner: CliRunner) -> None:
    with open("config.example.yaml", "w", encoding="utf-8") as handle:
        yaml.safe_dump(_config_dict(), handle)
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
