"""Extension point for job posting sources (scrapers, API clients, etc.).

A `Source` fetches raw postings from one external place and normalizes them
into `JobPosting`. It knows nothing about storage, scoring, or export — see
the "Target architecture" section of CLAUDE.md.

Error handling policy (a deliberate design choice, not an oversight):

JobSearcher collects from several independent, third-party-controlled
sources on an unattended schedule. A single broken source — a board that
started 404ing, a rate limit, a network blip — must never abort the whole
run. Failures are isolated at three levels:

- Item-level: `normalize()` raises `KeyError`, `ValueError`, or
  `pydantic.ValidationError` for one malformed raw item. `fetch()` catches
  it, logs a warning, counts it as skipped, and moves on to the next item.
- Unit-level: many sources fetch from several independent sub-units in one
  run (Greenhouse fetches one board per configured company slug; a realistic
  config lists ten to seventy of them). One bad unit — a misspelled slug, a
  board that's down — must not cost the postings from every other unit. A
  subclass whose work naturally decomposes this way should catch
  `SourceError` per unit inside `fetch_raw`, call `self._record_error(...)`
  (which logs and appends to `last_run.errors`), and continue to the next
  unit rather than letting the exception propagate. See
  `GreenhouseSource.fetch_raw` for the reference implementation.
- Source-level: `fetch_raw()` raises a `SourceError` when the source cannot
  even start (e.g. authentication failure, a global outage) — something no
  per-unit loop could isolate because nothing unit-specific has happened
  yet. `fetch()` catches it, logs an error, and stops yielding for that
  source; it does not propagate to the caller.

Source-level failures are split into two subclasses because they call for
different reactions, at whichever of the two levels above they're raised:

- `SourceConfigError`: the configuration is wrong and retrying changes
  nothing (a 404 for one company slug almost always means that slug is
  misspelled). No retry; the message must name the offending value (e.g. the
  slug) and the config field it came from.
- `SourceUnavailableError`: a transient condition — timeout, network error,
  5xx, or rate limiting (429). `_request()` retries these with exponential
  backoff before giving up.

Both inherit from `SourceError` so a caller that only wants the general
"this failed" case can catch that alone.

Every `fetch()` call records a `SourceRunStats` on `last_run`, with an
`errors` entry for every failed unit (or the single failure that stopped the
source from starting) and a `failed` flag with a precise meaning: *the
source produced nothing usable* — either it could not start at all, or every
unit it attempted failed. A partial failure — some units succeeded, one
didn't — leaves `failed` at `False` and records the failure in `errors`
instead. Without that distinction, "70 companies, all fine" and "70
companies, all 404ing because the token expired" would both report zero
errors and be indistinguishable from a caller's point of view; and without
`errors` at all, "this source found nothing today" and "this source has
been broken for three weeks" would produce the exact same empty output. Both
ambiguities are expensive precisely because nothing ever surfaces them.
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any, Final, TypeVar

import httpx
from pydantic import ValidationError

from jobsearcher.config import PluginConfig
from jobsearcher.language import detect_language
from jobsearcher.models import JobPosting

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT: Final[float] = 10.0
DEFAULT_MAX_RETRIES: Final[int] = 3
DEFAULT_BACKOFF_BASE: Final[float] = 1.0
USER_AGENT: Final[str] = "JobSearcher/1.0 (+https://github.com/nathanaellewong/jobsearcher)"

# Retried with backoff by `_request`: rate limiting and server-side errors.
# Anything else non-2xx (401, 403, 404, ...) is treated as a config problem.
_RETRYABLE_STATUS_CODES: Final[frozenset[int]] = frozenset({429, 500, 502, 503, 504})


class SourceError(Exception):
    """Base class for a source-level (as opposed to per-item) fetch failure."""


class SourceConfigError(SourceError):
    """The source failed because its configuration is wrong.

    No amount of retrying fixes this — e.g. a 404 on an entire board almost
    always means the configured company slug is misspelled. Never retried;
    the message should name the offending config value and field.
    """


class SourceUnavailableError(SourceError):
    """The source failed because of a transient condition.

    Timeouts, network errors, 5xx responses, and rate limiting (429) all
    land here. `_request` retries these with exponential backoff before
    raising.
    """


@dataclass
class SourceRunStats:
    """Outcome counters for one `Source.fetch()` call.

    Attributes:
        yielded: Number of postings successfully normalized and yielded.
        skipped: Number of raw items that failed to normalize and were
            skipped (item-level failure).
        failed: `True` only if the source produced nothing usable this run:
            either `fetch_raw` raised before anything could be attempted, or
            every unit it attempted (see the module docstring's "unit-level"
            isolation) failed. A partial failure — some units succeeded, one
            or more didn't — leaves this `False`; check `errors` for that
            case instead. This distinction is the point of the field: it's
            what tells "found nothing today" (`failed=False, errors=[]`)
            apart from "every unit is broken" (`failed=True`) apart from
            "one unit out of many is broken" (`failed=False, errors=[...]`).
        errors: One message per failed unit (or the single message from a
            source that couldn't start at all), each naming the offending
            value and the config field it came from. Empty if nothing
            failed.
    """

    yielded: int = 0
    skipped: int = 0
    failed: bool = False
    errors: list[str] = field(default_factory=list)


_SourceT = TypeVar("_SourceT", bound="Source")
_REGISTRY: dict[str, type[Source]] = {}


def register_source(name: str) -> Callable[[type[_SourceT]], type[_SourceT]]:
    """Class decorator that registers a `Source` implementation under a name.

    Adding a new source is then just a new module that decorates its class
    with `@register_source("its-name")` — no existing file needs to change.

    Args:
        name: The name sources are configured under (e.g. the key used in
            `config.yaml`'s `sources:` section).

    Returns:
        A decorator that registers and returns the class unchanged.

    Raises:
        ValueError: If `name` is already registered.
    """

    def decorator(cls: type[_SourceT]) -> type[_SourceT]:
        if name in _REGISTRY:
            raise ValueError(f"A source is already registered under {name!r}: {_REGISTRY[name]!r}")
        _REGISTRY[name] = cls
        return cls

    return decorator


def get_source_class(name: str) -> type[Source] | None:
    """Look up a registered source class by name.

    Args:
        name: The name it was registered under via `register_source`.

    Returns:
        The source class, or `None` if no source is registered under `name`.
    """
    return _REGISTRY.get(name)


def registered_source_names() -> list[str]:
    """Return the names of all currently registered sources, sorted."""
    return sorted(_REGISTRY)


class Source(ABC):
    """Base class for one job source.

    Subclasses implement `fetch_raw` (get raw items from the external
    source) and `normalize` (turn one raw item into a `JobPosting`).
    `fetch()` orchestrates the two and provides the error isolation and
    `last_run` stats described in this module's docstring; subclasses
    should not override it.

    `_request` gives subclasses a shared HTTP policy — identifiable
    User-Agent, timeout, rate limiting, and retry with exponential backoff —
    so individual sources don't reimplement it against a raw `httpx.Client`.
    """

    def __init__(
        self,
        name: str,
        config: PluginConfig,
        *,
        client: httpx.Client | None = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_base: float = DEFAULT_BACKOFF_BASE,
        min_request_interval: float = 0.0,
    ) -> None:
        """Initialize the source.

        Args:
            name: The source's registered name, used as `JobPosting.source`
                and in log messages.
            config: This source's configuration block.
            client: An `httpx.Client` to use for requests. If omitted, one is
                created (with the shared timeout and User-Agent) and closed
                by this instance's `close()`. Pass one in for testing.
            max_retries: Maximum retry attempts for a single `_request` call,
                on top of the first attempt.
            backoff_base: Base seconds for exponential backoff between
                retries (`backoff_base * 2**attempt`).
            min_request_interval: Minimum seconds to leave between requests
                made through `_request`, for rate limiting. `0` (default)
                disables rate limiting.
        """
        self.name = name
        self.config = config
        self.last_run: SourceRunStats = SourceRunStats()

        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=DEFAULT_TIMEOUT, headers={"User-Agent": USER_AGENT}
        )
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._min_request_interval = min_request_interval
        self._last_request_at: float | None = None

    def close(self) -> None:
        """Close the underlying HTTP client, if this instance owns one."""
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> Source:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    @abstractmethod
    def fetch_raw(self) -> Iterator[dict[str, Any]]:
        """Yield raw items from the external source, one dict per posting.

        Implementations should use `_request` for HTTP calls to get the
        shared timeout/retry/rate-limit/User-Agent policy for free.

        If the work naturally decomposes into independent units (e.g. one
        API call per configured company), catch `SourceError` per unit and
        call `self._record_error(...)` instead of letting it propagate — one
        bad unit must not cost the postings from the others. Only let a
        `SourceError` propagate out of this method for a failure that
        prevents starting at all (e.g. authentication).
        """

    @abstractmethod
    def normalize(self, raw: dict[str, Any]) -> JobPosting:
        """Convert one raw item into a `JobPosting`.

        Args:
            raw: One item as yielded by `fetch_raw`.

        Returns:
            The normalized posting.

        Raises:
            KeyError: If a required field is missing from `raw`.
            ValueError: If a field is present but has an unusable value.
            pydantic.ValidationError: If `JobPosting` construction fails.
        """

    def fetch(self) -> Iterator[JobPosting]:
        """Fetch and normalize postings, isolating failures (see module docstring).

        Resets `last_run` at the start of the call and updates it as
        iteration proceeds. `fetch()` is itself a generator: since Python
        doesn't run generator code until it's iterated, `last_run` will
        report the finished run's counters only once the caller has fully
        consumed the returned iterator.

        Yields:
            Successfully normalized postings.
        """
        self.last_run = SourceRunStats()
        try:
            for raw in self.fetch_raw():
                try:
                    posting = self.normalize(raw)
                except (KeyError, ValueError, ValidationError) as exc:
                    self.last_run.skipped += 1
                    logger.warning("Source %r: skipping a malformed posting (%s)", self.name, exc)
                    continue
                # Language tagging happens here, once, rather than in every
                # source's normalize(): it is the same heuristic for all of
                # them and needs the finished posting's text. It never drops
                # a posting — an undetermined verdict is just left as None.
                posting.detected_language = detect_language(
                    f"{posting.title}\n{posting.description_clean or posting.description_raw}"
                )
                self.last_run.yielded += 1
                yield posting
        except SourceError as exc:
            self.last_run.failed = True
            self.last_run.errors.append(str(exc))
            logger.error("Source %r could not start: %s", self.name, exc)
            return

        if self.last_run.yielded == 0 and self.last_run.errors:
            self.last_run.failed = True

    def _record_error(self, message: str) -> None:
        """Log and record a unit-level failure on `last_run`, without raising.

        Use this from `fetch_raw` when one independent unit of collection
        (e.g. one company's board) fails but others should still be
        attempted — see the module docstring's "unit-level" isolation.

        Args:
            message: A message naming the offending value and the config
                field it came from.
        """
        self.last_run.errors.append(message)
        logger.error("Source %r: %s", self.name, message)

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """Perform an HTTP request under this source's shared policy.

        Applies rate limiting, then attempts the request up to
        `max_retries + 1` times. Timeouts, network errors, and retryable
        status codes (429, 5xx) are retried with exponential backoff and
        raise `SourceUnavailableError` once retries are exhausted. Any
        other non-2xx status is treated as a configuration problem and
        raises `SourceConfigError` immediately, without retrying.

        Args:
            method: The HTTP method (e.g. `"GET"`).
            url: The request URL.
            **kwargs: Passed through to `httpx.Client.request`.

        Returns:
            The successful response.

        Raises:
            SourceConfigError: On a non-retryable non-2xx status.
            SourceUnavailableError: On persistent timeouts, network errors,
                or retryable status codes.
        """
        self._respect_rate_limit()
        last_exc: Exception | None = None

        for attempt in range(self._max_retries + 1):
            try:
                response = self._client.request(method, url, **kwargs)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_exc = exc
                if attempt == self._max_retries:
                    raise SourceUnavailableError(
                        f"{url} was unreachable after {attempt + 1} attempt(s): {exc}"
                    ) from exc
                self._sleep_backoff(attempt)
                continue

            if response.status_code in _RETRYABLE_STATUS_CODES:
                last_exc = httpx.HTTPStatusError(
                    f"{response.status_code} response",
                    request=response.request,
                    response=response,
                )
                if attempt == self._max_retries:
                    raise SourceUnavailableError(
                        f"{url} kept returning {response.status_code} after "
                        f"{attempt + 1} attempt(s)."
                    ) from last_exc
                self._sleep_backoff(attempt)
                continue

            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise SourceConfigError(f"{url} returned {response.status_code}: {exc}") from exc

            return response

        raise SourceUnavailableError(f"Failed to fetch {url}") from last_exc

    def _respect_rate_limit(self) -> None:
        """Sleep if needed so requests stay `min_request_interval` seconds apart."""
        if self._min_request_interval > 0 and self._last_request_at is not None:
            remaining = self._min_request_interval - (time.monotonic() - self._last_request_at)
            if remaining > 0:
                time.sleep(remaining)
        self._last_request_at = time.monotonic()

    def _sleep_backoff(self, attempt: int) -> None:
        """Sleep for the exponential backoff delay of a given retry attempt."""
        time.sleep(self._backoff_base * (2**attempt))
