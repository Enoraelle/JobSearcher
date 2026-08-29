"""Optional Notion exporter.

This exporter keeps one Notion database page per posting. Re-running it does
**not** create duplicates: each posting is matched to its existing page by
its URL (a Notion ``url`` property), and that page is updated in place;
postings with no page yet get one created. The application status is written
to a ``select`` property so you can sort and board it inside Notion.

Like the LLM scorer, this is a bonus that follows the project's "usable with
zero API keys" rule:

- **Key from the environment only.** The integration token is read from an
  environment variable whose name is config (``api_key_env``, default
  ``NOTION_API_KEY``). It is never read from ``config.yaml``, which is
  version-controlled — putting an ``api_key`` there is a configuration
  error, not a fallback.
- **Nothing to install.** It speaks plain HTTP to the Notion REST API with
  the ``httpx`` the rest of the package already uses, so enabling it is a
  config block and an integration token, not a second install.

Because this is the module most likely to break in a user's setup — wrong
database id, integration not shared, a property named differently or missing
entirely — every failure path raises :class:`~jobsearcher.exporters.base.ExporterError`
with a message that says exactly what to do. In particular, the database
schema is checked up front: before a single page is written, every property
JobSearcher intends to fill is verified to exist with the right type, and if
any is wrong the error lists each one with the Notion property type to
create.

Two consequences of syncing one page at a time shape the rest of this
module:

- **Requests are paced.** Each posting costs two requests (a lookup and a
  create-or-update), and Notion allows roughly three requests per second.
  Left unpaced, a run of a few dozen postings earns a 429 on its own. The
  ``requests_per_second`` option (default ``2.5``, just under Notion's
  advertised ceiling, ``0`` to disable) spaces every request out; the
  limiter is the same one :mod:`jobsearcher.sources.base` applies to
  scraping.
- **An interrupted run reports its progress.** A failure on posting 40 of 75
  still leaves 39 pages in Notion. Re-running is the right fix — the upsert
  is keyed on the posting URL, so nothing is duplicated — but only if the
  user is told where it stopped, so the error raised from inside the write
  loop carries how many pages were created and updated before it.
"""

from __future__ import annotations

import os
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from jobsearcher.config import PluginConfig
from jobsearcher.exporters.base import ExporterError, ExportResult
from jobsearcher.models import JobPosting

_API_ROOT: Final = "https://api.notion.com/v1"
_DEFAULT_KEY_ENV: Final = "NOTION_API_KEY"
_DEFAULT_NOTION_VERSION: Final = "2022-06-28"
_DEFAULT_TIMEOUT: Final = 30.0

# Notion documents an average of three requests per second per integration.
# The default sits deliberately under it: pacing exactly to an advertised
# average earns occasional 429s as soon as latency varies, and the margin
# costs seconds on an export of a hundred postings.
_DEFAULT_REQUESTS_PER_SECOND: Final = 2.5


@dataclass(frozen=True)
class _Field:
    """One posting attribute mirrored to a Notion property."""

    key: str
    """Logical name; the config attribute is ``f"{key}_property"``."""

    notion_type: str
    """The Notion property type the target property must have."""


# The properties JobSearcher writes. All must exist in the database with the
# matching type; the schema check below reports every one that does not.
_FIELDS: Final[tuple[_Field, ...]] = (
    _Field("title", "title"),
    _Field("url", "url"),
    _Field("status", "select"),
    _Field("company", "rich_text"),
    _Field("score", "number"),
    _Field("source", "select"),
)

# Human-readable Notion property-type labels, as shown in Notion's own UI.
_TYPE_LABEL: Final[dict[str, str]] = {
    "title": "Title",
    "url": "URL",
    "select": "Select",
    "rich_text": "Text",
    "number": "Number",
}


class NotionExporterConfig(BaseModel):
    """Validated ``exporters.notion`` block.

    ``extra="forbid"`` so an unknown key (including ``api_key``) is an error.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    database_id: str
    api_key_env: str = _DEFAULT_KEY_ENV
    notion_version: str = _DEFAULT_NOTION_VERSION
    timeout: float = Field(default=_DEFAULT_TIMEOUT, gt=0.0)
    requests_per_second: float = Field(default=_DEFAULT_REQUESTS_PER_SECOND, ge=0.0)
    """How fast to send requests, in requests per second. ``0`` disables pacing."""

    title_property: str = "Name"
    url_property: str = "URL"
    status_property: str = "Status"
    company_property: str = "Company"
    score_property: str = "Score"
    source_property: str = "Source"

    def property_name(self, field_key: str) -> str:
        """Return the configured Notion property name for a logical field."""
        name: str = getattr(self, f"{field_key}_property")
        return name


class NotionExporter:
    """Creates or updates one Notion page per posting. See the module docstring."""

    def __init__(self, *, client: httpx.Client | None = None) -> None:
        """Build the exporter.

        Args:
            client: An ``httpx.Client`` to use for requests. If omitted, one
                is created per :meth:`export` call (with the configured
                timeout) and closed when that call returns. Pass one in for
                testing.
        """
        self._client = client

    def export(self, postings: Sequence[JobPosting], options: PluginConfig) -> ExportResult:
        """Sync ``postings`` into the configured Notion database.

        Raises:
            ExporterError: On any misconfiguration or Notion API failure.
                The message names what to fix.
        """
        config = _load_config(options)
        token = _resolve_token(config)

        owns_client = self._client is None
        client = self._client or httpx.Client(timeout=config.timeout)
        try:
            session = _Session(client, token, config)
            session.check_database_schema()

            created = 0
            updated = 0
            for index, posting in enumerate(postings):
                try:
                    if session.upsert_page(posting):
                        created += 1
                    else:
                        updated += 1
                except ExporterError as exc:
                    raise ExporterError(
                        _interrupted_message(exc, index, len(postings), created, updated)
                    ) from exc
        finally:
            if owns_client:
                client.close()

        return ExportResult(
            created + updated,
            f"{created} created, {updated} updated in Notion database {config.database_id}",
        )


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


def _load_config(options: PluginConfig) -> NotionExporterConfig:
    try:
        return NotionExporterConfig.model_validate(options.model_dump())
    except ValidationError as exc:
        raise ExporterError(_explain_config_error(exc)) from exc


def _explain_config_error(exc: ValidationError) -> str:
    parts: list[str] = []
    for error in exc.errors():
        location = ".".join(str(p) for p in error["loc"])
        if error["loc"] == ("database_id",) and error["type"] == "missing":
            parts.append(
                "missing `database_id`: open the database in Notion, copy the "
                "32-character id from its URL (the part before `?v=`), and set it "
                "as `exporters.notion.database_id`"
            )
        elif error["loc"] == ("api_key",):
            parts.append(
                "remove `api_key`: the Notion token is only ever read from the "
                "environment variable named by `api_key_env` "
                f"(default `{_DEFAULT_KEY_ENV}`), never from config"
            )
        elif error["type"] == "extra_forbidden":
            parts.append(f"unknown option `{location}` (check for a typo)")
        else:
            parts.append(f"`{location}`: {error['msg']}")
    return "invalid `notion` exporter configuration: " + "; ".join(parts)


def _resolve_token(config: NotionExporterConfig) -> str:
    token = os.environ.get(config.api_key_env, "").strip()
    if not token:
        raise ExporterError(
            f"environment variable `{config.api_key_env}` is not set. Create a Notion "
            "internal integration at https://www.notion.so/my-integrations, copy its "
            f"secret, and export it as `{config.api_key_env}`. The token is never read "
            "from config.yaml."
        )
    return token


def _interrupted_message(
    cause: ExporterError, index: int, total: int, created: int, updated: int
) -> str:
    """Restate a mid-loop failure with the progress it would otherwise hide.

    Args:
        cause: The failure raised while syncing one posting.
        index: Zero-based position of the posting that failed.
        total: How many postings the export was asked to sync.
        created: Pages created before the failure.
        updated: Pages updated before the failure.

    Returns:
        The original message, followed by where the export stopped and what
        is already in Notion.
    """
    return (
        f"{cause}\n\nStopped on posting {index + 1} of {total}: {created} created, "
        f"{updated} updated before the failure. Those pages are matched by URL, so "
        "running the export again updates them rather than duplicating them."
    )


# --------------------------------------------------------------------------
# One export run against the Notion API
# --------------------------------------------------------------------------


class _Session:
    """Thin wrapper over the Notion REST calls one export needs."""

    def __init__(self, client: httpx.Client, token: str, config: NotionExporterConfig) -> None:
        self._client = client
        self._config = config
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Notion-Version": config.notion_version,
            "Content-Type": "application/json",
        }
        rate = config.requests_per_second
        self._min_request_interval = 1.0 / rate if rate > 0 else 0.0
        self._last_request_at: float | None = None

    def check_database_schema(self) -> None:
        """Fail early unless every property JobSearcher writes exists and fits.

        Raises:
            ExporterError: If the database cannot be read, or one or more
                target properties are missing or of the wrong type. The
                message lists each problem and the Notion property type to
                create.
        """
        database = self._request(
            "GET",
            f"/databases/{self._config.database_id}",
            context="reading the database schema",
        )
        schema: dict[str, Any] = database.get("properties", {})

        problems: list[str] = []
        for field in _FIELDS:
            name = self._config.property_name(field.key)
            label = _TYPE_LABEL[field.notion_type]
            actual = schema.get(name)
            if actual is None:
                problems.append(
                    f"  - no property named {name!r}: add a {label} property with "
                    f"that name to the database, or point `{field.key}_property` at "
                    f"an existing {label} property"
                )
            elif actual.get("type") != field.notion_type:
                problems.append(
                    f"  - property {name!r} is a {actual.get('type')!r} property, but "
                    f"JobSearcher needs a {field.notion_type!r} ({label}) property "
                    f"here; rename/retype it or set `{field.key}_property` to another one"
                )

        if problems:
            raise ExporterError(
                f"the Notion database {self._config.database_id} is missing "
                "properties JobSearcher needs:\n" + "\n".join(problems)
            )

    def upsert_page(self, posting: JobPosting) -> bool:
        """Create or update the page for one posting.

        Returns:
            ``True`` if a page was created, ``False`` if an existing page
            was updated.
        """
        properties = self._properties_for(posting)
        existing_id = self._find_page_id(posting.url)
        if existing_id is None:
            self._request(
                "POST",
                "/pages",
                context=f"creating a page for {posting.url}",
                json={
                    "parent": {"database_id": self._config.database_id},
                    "properties": properties,
                },
            )
            return True

        self._request(
            "PATCH",
            f"/pages/{existing_id}",
            context=f"updating the page for {posting.url}",
            json={"properties": properties},
        )
        return False

    def _find_page_id(self, url: str) -> str | None:
        payload = self._request(
            "POST",
            f"/databases/{self._config.database_id}/query",
            context=f"looking up the existing page for {url}",
            json={
                "filter": {
                    "property": self._config.url_property,
                    "url": {"equals": url},
                },
                "page_size": 1,
            },
        )
        results: list[dict[str, Any]] = payload.get("results", [])
        if not results:
            return None
        page_id: str = results[0]["id"]
        return page_id

    def _properties_for(self, posting: JobPosting) -> dict[str, Any]:
        values = {
            "title": {"title": [{"text": {"content": posting.title}}]},
            "url": {"url": posting.url},
            "status": {"select": {"name": posting.status.value}},
            "company": {"rich_text": [{"text": {"content": posting.company}}]},
            "score": {"number": posting.score},
            "source": {"select": {"name": posting.source}},
        }
        return {self._config.property_name(field.key): values[field.key] for field in _FIELDS}

    def _request(
        self,
        method: str,
        path: str,
        *,
        context: str,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Perform one Notion API call under the configured rate limit.

        Args:
            method: The HTTP method.
            path: The path below ``/v1``.
            context: What the call was for, quoted in any error message.
            json: The request body, if any.

        Returns:
            The decoded response body.

        Raises:
            ExporterError: If the call could not be made or Notion refused
                it. The message names what to fix.
        """
        url = f"{_API_ROOT}{path}"
        self._respect_rate_limit()
        try:
            response = self._client.request(method, url, headers=self._headers, json=json)
        except httpx.HTTPError as exc:
            raise ExporterError(f"could not reach the Notion API while {context}: {exc}") from exc

        if response.status_code == 200:
            body: dict[str, Any] = response.json()
            return body

        raise self._translate_error(response, context)

    def _respect_rate_limit(self) -> None:
        """Sleep if needed so requests stay `requests_per_second` apart."""
        if self._min_request_interval > 0 and self._last_request_at is not None:
            remaining = self._min_request_interval - (time.monotonic() - self._last_request_at)
            if remaining > 0:
                time.sleep(remaining)
        self._last_request_at = time.monotonic()

    def _translate_error(self, response: httpx.Response, context: str) -> ExporterError:
        try:
            body = response.json()
        except ValueError:
            body = {}
        code = body.get("code", "")
        message = body.get("message") or response.text
        status = response.status_code

        if status == 401:
            return ExporterError(
                f"Notion rejected the token in `{self._config.api_key_env}` (401). "
                "Check the value, and make sure the integration is shared with the "
                "database: open the database in Notion → ••• → Connections → add "
                "your integration."
            )
        if status == 404:
            return ExporterError(
                f"Notion returned 404 while {context}. Either the `database_id` "
                f"({self._config.database_id}) is wrong, or the integration cannot "
                "see that database. In Notion, open the database → ••• → "
                "Connections, add your integration, then copy the id from the URL."
            )
        if status == 429:
            return ExporterError(
                "Notion is rate-limiting this export (429). Wait a minute and run it "
                "again; already-synced pages will be updated, not duplicated."
            )
        if code == "validation_error":
            return ExporterError(
                f"Notion rejected the request while {context}: {message}. If this "
                "names a property, create it in the database (or adjust the "
                "matching `*_property` option)."
            )
        return ExporterError(
            f"Notion API error while {context}: {status} {code}: {message}".rstrip()
        )


__all__ = ["NotionExporter", "NotionExporterConfig"]
