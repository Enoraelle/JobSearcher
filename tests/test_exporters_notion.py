"""Tests for the optional Notion exporter, with a fully mocked HTTP client.

No test here performs real network I/O (see CLAUDE.md): every request is
served by ``_FakeNotion``, a small in-memory model of the Notion REST API
that is faithful enough to exercise schema validation, page creation, and
idempotent re-export.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from jobsearcher.config import PluginConfig
from jobsearcher.exporters.base import ExporterError
from jobsearcher.exporters.notion import NotionExporter
from jobsearcher.models import ApplicationStatus, JobPosting

_KEY_ENV = "NOTION_API_KEY"
_DB_ID = "db-1234567890"


@pytest.fixture(autouse=True)
def _clear_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_KEY_ENV, raising=False)


def _with_key(monkeypatch: pytest.MonkeyPatch, value: str = "secret_token") -> None:
    monkeypatch.setenv(_KEY_ENV, value)


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


def _opts(**overrides: Any) -> PluginConfig:
    fields: dict[str, Any] = {"database_id": _DB_ID}
    fields.update(overrides)
    return PluginConfig(**fields)


def _full_schema() -> dict[str, dict[str, str]]:
    """A database whose properties all match what the exporter expects."""
    return {
        "Name": {"type": "title"},
        "URL": {"type": "url"},
        "Status": {"type": "select"},
        "Company": {"type": "rich_text"},
        "Score": {"type": "number"},
        "Source": {"type": "select"},
    }


class _FakeResponse:
    def __init__(self, status_code: int, json_data: dict[str, Any]) -> None:
        self.status_code = status_code
        self._json_data = json_data
        self.text = str(json_data)

    def json(self) -> dict[str, Any]:
        return self._json_data


class _FakeNotion:
    """An in-memory stand-in for the Notion REST API."""

    def __init__(
        self,
        *,
        schema: dict[str, dict[str, str]] | None = None,
        database_id: str = _DB_ID,
        raise_on_request: Exception | None = None,
        status_override: tuple[int, dict[str, Any]] | None = None,
    ) -> None:
        self._schema = _full_schema() if schema is None else schema
        self._database_id = database_id
        self._raise_on_request = raise_on_request
        self._status_override = status_override
        self.pages: dict[str, dict[str, Any]] = {}
        self.requests: list[dict[str, Any]] = []
        self._next_id = 0
        self.closed = False

    # -- httpx.Client surface -------------------------------------------------

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
    ) -> _FakeResponse:
        self.requests.append({"method": method, "url": url, "headers": headers, "json": json})

        if self._raise_on_request is not None:
            raise self._raise_on_request
        if self._status_override is not None:
            return _FakeResponse(*self._status_override)

        path = url.split("/v1", 1)[1]
        if method == "GET" and path == f"/databases/{self._database_id}":
            return _FakeResponse(200, {"properties": self._schema})
        if method == "GET" and path.startswith("/databases/"):
            return _FakeResponse(404, _notion_error(404, "object_not_found", "Not found."))
        if method == "POST" and path == f"/databases/{self._database_id}/query":
            return self._handle_query(json or {})
        if method == "POST" and path == "/pages":
            return self._handle_create(json or {})
        if method == "PATCH" and path.startswith("/pages/"):
            return self._handle_update(path.rsplit("/", 1)[1], json or {})
        return _FakeResponse(404, _notion_error(404, "object_not_found", f"No route for {path}."))

    def close(self) -> None:
        self.closed = True

    # -- behaviour ----------------------------------------------------------

    def _handle_query(self, body: dict[str, Any]) -> _FakeResponse:
        wanted = body["filter"]["url"]["equals"]
        for page_id, props in self.pages.items():
            if props.get("URL", {}).get("url") == wanted:
                return _FakeResponse(200, {"results": [{"id": page_id}]})
        return _FakeResponse(200, {"results": []})

    def _handle_create(self, body: dict[str, Any]) -> _FakeResponse:
        self._next_id += 1
        page_id = f"page-{self._next_id}"
        self.pages[page_id] = dict(body["properties"])
        return _FakeResponse(200, {"id": page_id, "properties": self.pages[page_id]})

    def _handle_update(self, page_id: str, body: dict[str, Any]) -> _FakeResponse:
        self.pages[page_id].update(body["properties"])
        return _FakeResponse(200, {"id": page_id, "properties": self.pages[page_id]})


def _notion_error(status: int, code: str, message: str) -> dict[str, Any]:
    return {"object": "error", "status": status, "code": code, "message": message}


# --------------------------------------------------------------------------
# Happy path
# --------------------------------------------------------------------------


def test_creates_one_page_per_posting(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_key(monkeypatch)
    notion = _FakeNotion()
    postings = [
        _posting(url="https://example.com/jobs/1", score=90),
        _posting(url="https://example.com/jobs/2", score=40),
    ]

    result = NotionExporter(client=notion).export(postings, _opts())

    assert len(notion.pages) == 2
    assert result.exported == 2
    assert result.detail == f"2 created, 0 updated in Notion database {_DB_ID}"


def test_status_is_written_to_a_select_property(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_key(monkeypatch)
    notion = _FakeNotion()
    posting = _posting(status=ApplicationStatus.APPLIED, score=10)

    NotionExporter(client=notion).export([posting], _opts())

    (props,) = notion.pages.values()
    assert props["Status"] == {"select": {"name": "applied"}}
    assert props["Score"] == {"number": 10}


def test_reexport_updates_pages_without_duplicating(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_key(monkeypatch)
    notion = _FakeNotion()
    postings = [_posting(url="https://example.com/jobs/1", score=50)]

    NotionExporter(client=notion).export(postings, _opts())
    result = NotionExporter(client=notion).export(
        [_posting(url="https://example.com/jobs/1", score=75)], _opts()
    )

    assert len(notion.pages) == 1
    assert result.detail == f"0 created, 1 updated in Notion database {_DB_ID}"
    (props,) = notion.pages.values()
    assert props["Score"] == {"number": 75}


def test_property_names_are_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_key(monkeypatch)
    schema = {
        "Job title": {"type": "title"},
        "Link": {"type": "url"},
        "Status": {"type": "select"},
        "Company": {"type": "rich_text"},
        "Score": {"type": "number"},
        "Source": {"type": "select"},
    }
    notion = _FakeNotion(schema=schema)

    NotionExporter(client=notion).export(
        [_posting(score=1)],
        _opts(title_property="Job title", url_property="Link"),
    )

    (props,) = notion.pages.values()
    assert props["Job title"] == {"title": [{"text": {"content": "Backend Engineer"}}]}
    assert "Link" in props


def test_provided_client_is_not_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_key(monkeypatch)
    notion = _FakeNotion()

    NotionExporter(client=notion).export([_posting(score=1)], _opts())

    assert notion.closed is False


# --------------------------------------------------------------------------
# Credentials and configuration
# --------------------------------------------------------------------------


def test_missing_api_key_names_the_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    notion = _FakeNotion()
    with pytest.raises(ExporterError, match=_KEY_ENV):
        NotionExporter(client=notion).export([_posting()], _opts())
    assert notion.requests == []


def test_api_key_in_config_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_key(monkeypatch)
    with pytest.raises(ExporterError, match=r"`api_key`"):
        NotionExporter(client=_FakeNotion()).export([_posting()], _opts(api_key="secret_in_config"))


def test_missing_database_id_says_what_to_add(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_key(monkeypatch)
    with pytest.raises(ExporterError, match=r"database_id"):
        NotionExporter(client=_FakeNotion()).export([_posting()], PluginConfig())


def test_unknown_option_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_key(monkeypatch)
    with pytest.raises(ExporterError, match=r"unknown option `databse_id`"):
        NotionExporter(client=_FakeNotion()).export([_posting()], PluginConfig(databse_id=_DB_ID))


# --------------------------------------------------------------------------
# Schema problems — the messages users are most likely to hit
# --------------------------------------------------------------------------


def test_missing_property_says_which_one_to_create(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_key(monkeypatch)
    schema = _full_schema()
    del schema["Score"]
    notion = _FakeNotion(schema=schema)

    with pytest.raises(ExporterError) as excinfo:
        NotionExporter(client=notion).export([_posting()], _opts())

    message = str(excinfo.value)
    assert "'Score'" in message
    assert "Number" in message
    assert "score_property" in message
    # Nothing was written once the schema check failed.
    assert notion.pages == {}


def test_wrong_property_type_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_key(monkeypatch)
    schema = _full_schema()
    schema["Score"] = {"type": "rich_text"}
    notion = _FakeNotion(schema=schema)

    with pytest.raises(ExporterError, match=r"'Score'.*rich_text.*number|number.*'Score'"):
        NotionExporter(client=notion).export([_posting()], _opts())


def test_multiple_schema_problems_are_all_listed(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_key(monkeypatch)
    notion = _FakeNotion(schema={"Name": {"type": "title"}})

    with pytest.raises(ExporterError) as excinfo:
        NotionExporter(client=notion).export([_posting()], _opts())

    message = str(excinfo.value)
    for name in ("'URL'", "'Status'", "'Company'", "'Score'", "'Source'"):
        assert name in message


# --------------------------------------------------------------------------
# Notion API errors
# --------------------------------------------------------------------------


def test_unknown_database_404_is_explained(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_key(monkeypatch)
    notion = _FakeNotion(database_id="a-different-db")

    with pytest.raises(ExporterError, match=r"Connections"):
        NotionExporter(client=notion).export([_posting()], _opts())


def test_bad_token_401_is_explained(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_key(monkeypatch)
    notion = _FakeNotion(
        status_override=(401, _notion_error(401, "unauthorized", "API token is invalid."))
    )

    with pytest.raises(ExporterError, match=re.compile(r"401.*integration", re.DOTALL)):
        NotionExporter(client=notion).export([_posting()], _opts())


def test_rate_limit_429_is_explained(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_key(monkeypatch)
    notion = _FakeNotion(status_override=(429, _notion_error(429, "rate_limited", "Slow down.")))

    with pytest.raises(ExporterError, match=r"rate-limit"):
        NotionExporter(client=notion).export([_posting()], _opts())


def test_validation_error_on_write_points_at_properties(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_key(monkeypatch)

    class _RejectWrites(_FakeNotion):
        def _handle_create(self, body: dict[str, Any]) -> _FakeResponse:
            return _FakeResponse(
                400,
                _notion_error(400, "validation_error", "Score is expected to be number."),
            )

    with pytest.raises(ExporterError, match=r"create it in the database"):
        NotionExporter(client=_RejectWrites()).export([_posting(score=5)], _opts())


def test_network_error_is_wrapped(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_key(monkeypatch)
    notion = _FakeNotion(raise_on_request=httpx.ConnectError("no route to host"))

    with pytest.raises(ExporterError, match=r"could not reach the Notion API"):
        NotionExporter(client=notion).export([_posting()], _opts())


def test_owned_client_is_created_and_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_key(monkeypatch)
    created: list[_FakeNotion] = []

    def _fake_client_factory(*args: Any, **kwargs: Any) -> _FakeNotion:
        client = _FakeNotion()
        created.append(client)
        return client

    monkeypatch.setattr("jobsearcher.exporters.notion.httpx.Client", _fake_client_factory)

    NotionExporter().export([_posting(score=1)], _opts())

    assert len(created) == 1
    assert created[0].closed is True
