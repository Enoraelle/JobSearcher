"""Tests for jobsearcher.sources.base: the Source contract, registry, and
shared HTTP policy (retry/backoff, rate limiting, error isolation)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from jobsearcher import __version__
from jobsearcher.config import PluginConfig
from jobsearcher.models import JobPosting
from jobsearcher.sources.base import (
    PROJECT_URL,
    USER_AGENT,
    Source,
    SourceConfigError,
    SourceError,
    SourceRunStats,
    SourceUnavailableError,
    get_source_class,
    register_source,
    registered_source_names,
)


class _DummySource(Source):
    """A minimal in-memory Source for exercising fetch()'s orchestration
    logic without any network I/O."""

    def __init__(
        self,
        items: list[dict[str, Any]],
        *,
        fail_with: SourceError | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__("dummy", PluginConfig(), **kwargs)
        self._items = items
        self._fail_with = fail_with

    def fetch_raw(self) -> Iterator[dict[str, Any]]:
        yield from self._items
        if self._fail_with is not None:
            raise self._fail_with

    def normalize(self, raw: dict[str, Any]) -> JobPosting:
        if raw.get("bad"):
            raise ValueError("malformed item")
        return JobPosting(
            url=raw["url"],
            title="Title",
            company="Company",
            source="dummy",
            description_raw="description",
            fetched_at=datetime.now(UTC),
        )


def test_fetch_yields_good_items_and_skips_malformed_ones() -> None:
    items = [
        {"url": "https://example.test/jobs/1"},
        {"bad": True},
        {"url": "https://example.test/jobs/2"},
    ]
    source = _DummySource(items)

    postings = list(source.fetch())

    assert len(postings) == 2
    assert source.last_run == SourceRunStats(yielded=2, skipped=1, errors=[])
    assert source.last_run.failed is False


def test_fetch_isolates_a_source_level_failure() -> None:
    source = _DummySource([], fail_with=SourceUnavailableError("boom"))

    postings = list(source.fetch())

    assert postings == []
    assert source.last_run.yielded == 0
    assert source.last_run.failed is True
    assert source.last_run.errors == ["boom"]


def test_fetch_marks_failed_when_all_units_fail_via_record_error() -> None:
    class _AllUnitsFailSource(_DummySource):
        def fetch_raw(self) -> Iterator[dict[str, Any]]:
            self._record_error("unit 'a' failed")
            self._record_error("unit 'b' failed")
            return
            yield  # pragma: no cover - makes this a generator function

    source = _AllUnitsFailSource([])

    postings = list(source.fetch())

    assert postings == []
    assert source.last_run.yielded == 0
    assert source.last_run.errors == ["unit 'a' failed", "unit 'b' failed"]
    assert source.last_run.failed is True


def test_fetch_leaves_failed_false_on_a_partial_unit_failure() -> None:
    class _PartialFailureSource(_DummySource):
        def fetch_raw(self) -> Iterator[dict[str, Any]]:
            yield {"url": "https://example.test/jobs/1"}
            self._record_error("unit 'b' failed")

    source = _PartialFailureSource([])

    postings = list(source.fetch())

    assert len(postings) == 1
    assert source.last_run.errors == ["unit 'b' failed"]
    assert source.last_run.failed is False


def test_fetch_resets_last_run_on_each_call() -> None:
    source = _DummySource([{"bad": True}])
    list(source.fetch())
    assert source.last_run.skipped == 1

    source._items = [{"url": "https://example.test/jobs/1"}]
    list(source.fetch())
    assert source.last_run == SourceRunStats(yielded=1, skipped=0, errors=[])
    assert source.last_run.failed is False


def test_register_source_and_lookup() -> None:
    name = f"dummy-{uuid.uuid4().hex}"

    @register_source(name)
    class _Registered(_DummySource):
        pass

    assert get_source_class(name) is _Registered
    assert name in registered_source_names()


def test_register_source_rejects_duplicate_name() -> None:
    name = f"dummy-{uuid.uuid4().hex}"

    @register_source(name)
    class _First(_DummySource):
        pass

    with pytest.raises(ValueError, match=name):

        @register_source(name)
        class _Second(_DummySource):
            pass


def test_get_source_class_returns_none_for_unknown_name() -> None:
    assert get_source_class(f"nonexistent-{uuid.uuid4().hex}") is None


def test_request_retries_transient_errors_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("jobsearcher.sources.base.time.sleep", lambda _seconds: None)
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        if attempts["count"] < 3:
            return httpx.Response(503)
        return httpx.Response(200, json={"ok": True})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    source = _DummySource([], client=client, max_retries=3, backoff_base=0.01)

    response = source._request("GET", "https://example.test/jobs")

    assert response.status_code == 200
    assert attempts["count"] == 3


def test_request_404_raises_config_error_without_retrying(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("jobsearcher.sources.base.time.sleep", lambda _seconds: None)
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    source = _DummySource([], client=client, max_retries=3, backoff_base=0.01)

    with pytest.raises(SourceConfigError):
        source._request("GET", "https://example.test/jobs")

    assert attempts["count"] == 1


def test_request_persistent_5xx_raises_unavailable_after_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("jobsearcher.sources.base.time.sleep", lambda _seconds: None)
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        return httpx.Response(503)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    source = _DummySource([], client=client, max_retries=2, backoff_base=0.01)

    with pytest.raises(SourceUnavailableError):
        source._request("GET", "https://example.test/jobs")

    assert attempts["count"] == 3


def test_request_network_error_raises_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("jobsearcher.sources.base.time.sleep", lambda _seconds: None)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    source = _DummySource([], client=client, max_retries=1, backoff_base=0.01)

    with pytest.raises(SourceUnavailableError):
        source._request("GET", "https://example.test/jobs")


def test_respect_rate_limit_sleeps_for_the_remaining_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    times = iter([100.0, 100.2, 100.2])
    monkeypatch.setattr("jobsearcher.sources.base.time.monotonic", lambda: next(times))
    sleep_calls: list[float] = []
    monkeypatch.setattr("jobsearcher.sources.base.time.sleep", sleep_calls.append)

    source = _DummySource([], min_request_interval=1.0)
    source._respect_rate_limit()  # first call: nothing to wait for yet
    source._respect_rate_limit()  # 0.2s elapsed of a required 1.0s -> sleep 0.8s

    assert sleep_calls == [pytest.approx(0.8)]


def test_close_closes_an_owned_client_but_not_an_injected_one() -> None:
    owned = _DummySource([])
    owned.close()
    assert owned._client.is_closed

    injected_client = httpx.Client()
    injected = _DummySource([], client=injected_client)
    injected.close()
    assert not injected_client.is_closed
    injected_client.close()


def test_context_manager_closes_owned_client() -> None:
    with _DummySource([]) as source:
        client = source._client
    assert client.is_closed


# --------------------------------------------------------------------------
# `failed` means "produced nothing usable", at every point it can be read
# --------------------------------------------------------------------------


def test_a_source_that_produced_then_broke_is_not_marked_failed() -> None:
    """A board that dies after 3 postings still delivered 3 postings.

    `failed` is documented as "produced nothing usable" precisely so that
    "found nothing today", "one unit is broken" and "everything is broken"
    stay tellable apart. Collapsing a mid-stream failure into `failed` makes
    the caller discard a run that succeeded.
    """
    source = _DummySource(
        [{"url": "https://example.com/1"}, {"url": "https://example.com/2"}],
        fail_with=SourceUnavailableError("board went down"),
    )

    postings = list(source.fetch())

    assert len(postings) == 2
    assert source.last_run.yielded == 2
    assert source.last_run.failed is False
    assert source.last_run.errors == ["board went down"]


def test_a_source_that_broke_before_producing_anything_is_failed() -> None:
    source = _DummySource([], fail_with=SourceUnavailableError("board went down"))

    postings = list(source.fetch())

    assert postings == []
    assert source.last_run.failed is True
    assert source.last_run.errors == ["board went down"]


def test_failed_is_correct_even_when_the_caller_stops_early() -> None:
    """`--limit` abandons the generator; the flag must still read true.

    Deriving `failed` from the counters rather than setting it in a trailing
    statement is what makes a partially consumed run report honestly.
    """
    source = _DummySource(
        [{"url": f"https://example.com/{index}"} for index in range(5)],
        fail_with=SourceUnavailableError("never reached"),
    )

    fetched = source.fetch()
    first = next(fetched)
    fetched.close()

    assert first.url == "https://example.com/0"
    assert source.last_run.yielded == 1
    assert source.last_run.failed is False


def test_run_stats_failed_is_derived_from_the_counters() -> None:
    assert SourceRunStats().failed is False
    assert SourceRunStats(yielded=0, errors=["boom"]).failed is True
    assert SourceRunStats(yielded=3, errors=["boom"]).failed is False
    assert SourceRunStats(yielded=0, skipped=4).failed is False


def test_user_agent_carries_the_real_version_and_a_project_url() -> None:
    """Sent to every third-party site; a stale version or a dead URL is worse
    than no User-Agent at all."""
    assert f"JobSearcher/{__version__} (+{PROJECT_URL})" == USER_AGENT
    assert PROJECT_URL.startswith("https://")
    # The version is interpolated, not spelled out, so it cannot drift.
    assert __version__ in USER_AGENT
    assert "nathanaellewong" not in USER_AGENT
