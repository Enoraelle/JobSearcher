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
  it, counts it as skipped, and moves on to the next item. It logs the
  first such failure of each kind in full and then only tallies the rest
  (see `_record_repeated`), so a feed that broke the same way on every item
  does not bury the run in identical warnings. The same helper is available
  to subclasses for any other per-item failure that can repeat — a detail
  page that 404s for every posting, an unparsable date on every item.
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
source) and a `failed` flag with a precise meaning: *the source produced
nothing usable* — either it could not start at all, or every unit it
attempted failed. A partial failure — some units succeeded, one didn't, or
the source delivered postings and then broke — leaves `failed` at `False`
and records the failure in `errors` instead. Without that distinction, "70
companies, all fine" and "70 companies, all 404ing because the token
expired" would both report zero
errors and be indistinguishable from a caller's point of view; and without
`errors` at all, "this source found nothing today" and "this source has
been broken for three weeks" would produce the exact same empty output. Both
ambiguities are expensive precisely because nothing ever surfaces them.
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Generator, Iterator
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any, Final, TypeVar

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from jobsearcher import __version__
from jobsearcher.config import PluginConfig
from jobsearcher.language import detect_language
from jobsearcher.models import JobPosting

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT: Final[float] = 10.0
DEFAULT_MAX_RETRIES: Final[int] = 3
DEFAULT_BACKOFF_BASE: Final[float] = 1.0
PROJECT_URL: Final[str] = "https://github.com/Enoraelle/JobSearcher"
# Sent to every third-party site this tool talks to. It carries the real
# version rather than a literal that drifts, and the project URL rather
# than an individual: the point of an identifiable User-Agent is that a
# site owner who wants to reach the project can, so a URL that does not
# resolve is worse than none.
USER_AGENT: Final[str] = f"JobSearcher/{__version__} (+{PROJECT_URL})"

# Retried with backoff by `_request`: rate limiting and server-side errors.
# Anything else non-2xx (401, 403, 404, ...) is treated as a config problem.
_RETRYABLE_STATUS_CODES: Final[frozenset[int]] = frozenset({429, 500, 502, 503, 504})


class SourceHttpConfig(BaseModel):
    """The HTTP-policy options every source accepts, whatever else it reads.

    `Source` applies the same timeout, retry, backoff and rate-limiting
    policy to every source, and each of those is a number the user has a
    legitimate reason to change: a slow board needs a longer timeout, and a
    site that dislikes bursts needs `min_request_interval` — which is the
    only thing standing between We Work Remotely's opt-in
    `fetch_full_description` fan-out and one request per posting at full
    speed against someone else's server.

    They live here, on a base model each source's own config inherits,
    rather than being repeated per source: a source that forgot to declare
    them would reject them as unknown keys (every source config forbids
    extras), so the advice to set them would name an option that does not
    exist. Inheriting also means each source keeps one complete schema of
    everything it accepts, which is what its `extra="forbid"` is validated
    against.

    Attributes:
        enabled: Whether the pipeline runs this source at all.
        timeout: Seconds before a single HTTP request gives up.
        max_retries: Retry attempts for one request, on top of the first.
        backoff_base: Base seconds for exponential backoff between retries.
        min_request_interval: Minimum seconds between two requests from
            this source. ``0`` (the default) disables rate limiting.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    timeout: float = Field(default=DEFAULT_TIMEOUT, gt=0.0)
    max_retries: int = Field(default=DEFAULT_MAX_RETRIES, ge=0)
    backoff_base: float = Field(default=DEFAULT_BACKOFF_BASE, ge=0.0)
    min_request_interval: float = Field(default=0.0, ge=0.0)


class _HttpPolicyOnly(SourceHttpConfig):
    """The same options, read out of a block that also carries other keys.

    ``extra="ignore"``: the caller (the pipeline) has the whole source
    block, source-specific keys included, and only needs the shared part.
    Those other keys are still checked — by the source itself, against its
    own strict schema, a moment later.
    """

    model_config = ConfigDict(extra="ignore")


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
        errors: One message per failed unit (or the single message from a
            source that couldn't start at all), each naming the offending
            value and the config field it came from. Empty if nothing
            failed.
    """

    yielded: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        """Whether the source produced nothing usable this run.

        `True` only when the run yielded no posting *and* something went
        wrong: either it could not start at all, or every unit it attempted
        (see the module docstring's "unit-level" isolation) failed. A
        partial failure — some units succeeded, one or more didn't — is
        `False`; read `errors` for that case. This distinction is the whole
        point: it tells "found nothing today" (`failed=False, errors=[]`)
        apart from "everything is broken" (`failed=True`) apart from "one
        unit out of many is broken" (`failed=False, errors=[...]`).

        It is derived from the counters rather than assigned at the end of a
        run so that it reads correctly at any moment — including on a run a
        caller abandoned part-way with `--limit`, where a trailing
        assignment would never execute.
        """
        return self.yielded == 0 and bool(self.errors)


@dataclass
class _RepeatingCondition:
    """Running tally for a per-item failure that tends to recur identically.

    Some failures happen once per posting and the same way every time: a
    source that fetches a detail page per posting against a server that
    refuses them, or one normalizing a feed whose date format just changed.
    One log line per item then buries the rest of the run. `_record_repeated`
    logs the first occurrence of each `key` in full and only counts the
    others; `_log_repeated_summaries`, called once `fetch()` is done, emits a
    single tally line per key that recurred.

    Attributes:
        summary: The end-of-run line, logged only if the condition recurred.
            ``{count}`` in it is replaced with the total number of
            occurrences.
        count: How many times the condition has been recorded this run.
    """

    summary: str
    count: int = 0


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


def read_http_policy(name: str, config: PluginConfig) -> SourceHttpConfig:
    """Read the shared HTTP-policy options out of one source's config block.

    Args:
        name: The source's configured name, for the error message.
        config: That source's raw configuration block, source-specific keys
            included.

    Returns:
        The policy, filled with the shared defaults for whatever the block
        does not set.

    Raises:
        SourceConfigError: If one of the options holds an unusable value
            (a negative interval, a zero timeout).
    """
    try:
        return _HttpPolicyOnly.model_validate(config.model_dump())
    except ValidationError as exc:
        problems = "; ".join(
            f"`{'.'.join(str(part) for part in error['loc'])}`: {error['msg']}"
            for error in exc.errors()
        )
        raise SourceConfigError(f"Invalid HTTP options for source {name!r}: {problems}") from exc


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
        timeout: float | None = None,
        max_retries: int | None = None,
        backoff_base: float | None = None,
        min_request_interval: float | None = None,
    ) -> None:
        """Initialize the source.

        The HTTP policy comes from `config` (see `SourceHttpConfig`), the
        same way the Notion exporter reads its own `requests_per_second`: a
        policy only a caller could supply would be unreachable from
        config.yaml, which is where the person being rate-limited by a site
        actually works. The keyword arguments below override it — `None`
        means "whatever the config block says" — and exist for tests, which
        need a backoff measured in milliseconds rather than seconds.

        Args:
            name: The source's registered name, used as `JobPosting.source`
                and in log messages.
            config: This source's configuration block.
            client: An `httpx.Client` to use for requests. If omitted, one is
                created (with the resolved timeout and the shared
                User-Agent) and closed by this instance's `close()`. Pass one
                in for testing; a client passed in brings its own timeout.
            timeout: Seconds before a single request gives up.
            max_retries: Maximum retry attempts for a single `_request` call,
                on top of the first attempt.
            backoff_base: Base seconds for exponential backoff between
                retries (`backoff_base * 2**attempt`).
            min_request_interval: Minimum seconds to leave between requests
                made through `_request`, for rate limiting. `0` disables
                rate limiting.

        Raises:
            SourceConfigError: If the config block holds an unusable value
                for one of the shared HTTP options.
        """
        self.name = name
        self.config = config
        self.last_run: SourceRunStats = SourceRunStats()
        self._repeated: dict[str, _RepeatingCondition] = {}

        policy = read_http_policy(name, config)
        self._max_retries = policy.max_retries if max_retries is None else max_retries
        self._backoff_base = policy.backoff_base if backoff_base is None else backoff_base
        self._min_request_interval = (
            policy.min_request_interval if min_request_interval is None else min_request_interval
        )

        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=policy.timeout if timeout is None else timeout,
            headers={"User-Agent": USER_AGENT},
        )
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

    def fetch(self) -> Generator[JobPosting, None, None]:
        """Fetch and normalize postings, isolating failures (see module docstring).

        Returned as a generator rather than a bare iterator so a caller that
        stops early (``--limit``) can `close()` it and run its cleanup,
        instead of leaving it suspended mid-request.

        Resets `last_run` at the start of the call and updates it as
        iteration proceeds, so its counters describe everything consumed so
        far — including on a run the caller stops part-way through.

        Yields:
            Successfully normalized postings.
        """
        self.last_run = SourceRunStats()
        self._repeated = {}
        try:
            for raw in self.fetch_raw():
                try:
                    posting = self.normalize(raw)
                except (KeyError, ValueError, ValidationError) as exc:
                    self.last_run.skipped += 1
                    # Grouped by exception type so a feed that broke the same
                    # way on every item logs once and tallies, while a
                    # genuinely different malformation still gets its own line.
                    self._record_repeated(
                        f"malformed-item:{type(exc).__name__}",
                        first_message=f"skipping a malformed posting ({exc})",
                        summary=(
                            "skipped {count} malformed postings of the same kind "
                            f"({type(exc).__name__})"
                        ),
                    )
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
            # Where this surfaced says nothing about when the source broke:
            # `fetch_raw` is a generator, so even a failure on its very first
            # step arrives here, mid-loop. What separates "could not start"
            # from "stopped part-way" is whether anything came out first, and
            # that is also what `last_run.failed` keys off.
            self.last_run.errors.append(str(exc))
            if self.last_run.yielded == 0:
                logger.error("Source %r could not start: %s", self.name, exc)
            else:
                logger.error(
                    "Source %r stopped after %d posting(s): %s",
                    self.name,
                    self.last_run.yielded,
                    exc,
                )
        finally:
            # Runs on normal exhaustion and on an early `close()` alike, so a
            # partial run still gets its tally of any per-item condition that
            # repeated (see `_record_repeated`).
            self._log_repeated_summaries()

    def _record_repeated(self, key: str, *, first_message: str, summary: str) -> None:
        """Note one occurrence of a per-item condition that tends to recur.

        The first occurrence of each ``key`` this run is logged at WARNING
        with ``first_message``; every later one only increments a counter. If
        the key recurred, `_log_repeated_summaries` (called at the end of
        `fetch()`) logs ``summary`` once, with ``{count}`` replaced by the
        total. This keeps a systematic failure — every detail page returning
        403, say — to one detailed line plus one tally, instead of one line
        per posting drowning the rest of the run.

        Args:
            key: Groups occurrences counted together. Use a distinct key per
                distinct failure so their tallies stay separate.
            first_message: The full message for the first occurrence. It
                should say what failed and what was kept instead.
            summary: The end-of-run tally line. ``{count}`` is substituted
                with the number of occurrences.
        """
        entry = self._repeated.get(key)
        if entry is None:
            entry = self._repeated[key] = _RepeatingCondition(summary=summary)
        entry.count += 1
        if entry.count == 1:
            logger.warning("Source %r: %s", self.name, first_message)

    def _log_repeated_summaries(self) -> None:
        """Emit one tally line for every per-item condition that recurred."""
        for entry in self._repeated.values():
            if entry.count > 1:
                logger.warning("Source %r: %s", self.name, entry.summary.format(count=entry.count))

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
