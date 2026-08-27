# Contributing

## Three shapes of source, and which to copy from

Every source fetches from one of three kinds of external place, and the
codebase has one reference example of each:

| Shape                 | Reference                                                    | Stability                                                                 |
| ---------------------- | ------------------------------------------------------------- | -------------------------------------------------------------------------- |
| JSON API                | [`sources/greenhouse.py`](src/jobsearcher/sources/greenhouse.py)       | A published contract. Fields are typed and named; a missing field is a genuine anomaly. |
| RSS/XML feed             | [`sources/weworkremotely.py`](src/jobsearcher/sources/weworkremotely.py) | A published feed format, but not necessarily every field on every item (We Work Remotely truncates `<description>`, for instance). |
| Scraped HTML page       | [`sources/freework.py`](src/jobsearcher/sources/freework.py)           | No contract at all. The site owes scrapers nothing and can change its markup at any time. |

Pick your reference by what you're pointed at, not by how similar the target
site "feels" to one you already know:

- **The source publishes a JSON API** (even an undocumented one you found in
  the site's network tab) → copy `greenhouse.py`. Parse the response with
  ordinary dict/key access; a missing field is a legitimate `KeyError`
  because the API has a shape it's supposed to honor.
- **The source publishes an RSS/Atom feed** → copy `weworkremotely.py`.
  Parse it with `xml.etree.ElementTree` (stdlib, no new dependency). Expect
  feeds to under-supply content compared to the full page (see We Work
  Remotely's truncated descriptions) more often than to omit structural
  fields outright.
- **The only way to get the data is scraping rendered HTML** → copy
  `freework.py`. This is the fragile case, and it's worth reading that
  module's docstring in full before starting: selectors live as named
  constants at the top of the module and nowhere else, every lookup that
  can fail returns `None` instead of raising deep inside a parse tree, and
  a page whose selectors no longer match anything must fail with a message
  that says outright that the site's layout has probably changed — never
  an `AttributeError` on `None`. `freework.py` uses BeautifulSoup
  (`beautifulsoup4`, with the stdlib `html.parser` backend — no `lxml`) for
  real CSS-selector support; if your source also needs HTML scraping, reuse
  that dependency rather than adding another HTML parsing library.

If a source doesn't cleanly fit one shape (e.g. an HTML page that embeds a
JSON blob in a `<script>` tag), copy whichever reference dominates the
*failure modes* you'll need to handle — a source that can fail because its
markup moved should follow `freework.py`'s error-message discipline even if
most of its fields come out of embedded JSON.

## Adding a new source

A source fetches postings from one external place (an API, a scraped page)
and normalizes them into `JobPosting`. It must not touch storage, scoring,
or export — see "Target architecture" in [CLAUDE.md](CLAUDE.md).

Adding one should never require changing an existing file. The steps below
use `sources/greenhouse.py` as the reference example.

### 1. Create `src/jobsearcher/sources/<your_source>.py`

Subclass `Source` from [`sources/base.py`](src/jobsearcher/sources/base.py)
and implement its two abstract methods:

```python
from jobsearcher.sources.base import Source, register_source


@register_source("your_source")
class YourSource(Source):
    def fetch_raw(self) -> Iterator[dict[str, Any]]:
        """Yield raw items from the external source. Use self._request()
        for HTTP calls — it gives you the shared timeout, retry with
        exponential backoff, rate limiting, and User-Agent for free."""
        ...

    def normalize(self, raw: dict[str, Any]) -> JobPosting:
        """Convert one raw item into a JobPosting. Raise KeyError/ValueError/
        pydantic.ValidationError for a malformed item — fetch() catches it,
        logs it, and skips it rather than aborting the whole source."""
        ...
```

`@register_source("your_source")` is what makes the source discoverable by
the name used in `config.yaml`'s `sources:` section — no registry file to
edit by hand.

### 2. Define a typed config model, with `extra="forbid"`

`PluginConfig` (in `config.py`) is deliberately permissive (`extra="allow"`)
because it's shared by every source and doesn't know which one it is. Once
your `__init__` knows it's building *your* source, re-validate into a model
that forbids unknown keys, so a typo in `config.yaml` raises immediately
instead of silently producing a source that collects nothing:

```python
class YourSourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    # ... your fields ...

    
class YourSource(Source):
    def __init__(self, name: str, config: PluginConfig, **kwargs: Any) -> None:
        super().__init__(name, config, **kwargs)
        try:
            self._config = YourSourceConfig.model_validate(config.model_dump())
        except ValidationError as exc:
            raise SourceConfigError(f"Invalid configuration for source {name!r}: {exc}") from exc
```

### 3. Distinguish the two failure classes, and isolate failures at the right radius

`_request` raises one of two exceptions on an HTTP failure, and `fetch_raw`
should raise the same distinction for failures it detects itself (e.g. an
unexpected response shape):

- `SourceConfigError` for anything retrying won't fix — bad credentials, a
  404 that means a misconfigured identifier, an unexpected response shape.
  Name the offending config value and field in the message.
- `SourceUnavailableError` for transient conditions — timeouts, network
  errors, 5xx, rate limiting. `_request` already retries these with
  exponential backoff before raising.

Both inherit `SourceError`. What you do with the exception depends on where
in `fetch_raw` it happens:

- If your source fetches from several independent sub-units in one run (one
  API call per configured company, board, or query — Greenhouse fetches one
  company slug at a time, and a realistic config lists dozens of them),
  **catch it per unit**, call `self._record_error(...)`, and `continue` to
  the next unit. One bad unit must not cost every other unit's postings —
  see `GreenhouseSource.fetch_raw` for the reference implementation.
- Only let the exception propagate out of `fetch_raw` for a failure that
  stops the source from starting at all (e.g. authentication), since nothing
  unit-specific has happened yet to isolate.

Either way you never need to override `fetch()` itself: it catches a
propagated `SourceError`, and combined with anything recorded via
`_record_error`, produces `self.last_run: SourceRunStats` — `errors` lists
every failure (unit-level or the one that stopped the source from starting),
and `failed` is `True` only when the source produced nothing usable at all
(no units succeeded), `False` for a partial failure. See the module
docstring in [`sources/base.py`](src/jobsearcher/sources/base.py) for the
full rationale.

### 4. Register the module import

Add your module to
[`sources/__init__.py`](src/jobsearcher/sources/__init__.py) so importing
`jobsearcher.sources` registers it as a side effect:

```python
from jobsearcher.sources import your_source as your_source
```

### 5. Add the config shape to `config.example.yaml`

Under `sources:`, following the existing entries:

```yaml
sources:
  your_source:
    enabled: true
    # your fields
```

### 6. Write tests with recorded fixtures — no real HTTP calls

Tests must not hit the network (see CLAUDE.md's "Tests never touch the
network"). Record a real response from your source into
`tests/fixtures/` (anonymize any personal data first), and drive `httpx`
through `httpx.MockTransport` rather than mocking at a higher level:

```python
def handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json=FIXTURE_PAYLOAD)


client = httpx.Client(transport=httpx.MockTransport(handler))
source = YourSource("your_source", config, client=client)
```

At minimum, cover:

- **The happy path** against the recorded fixture: `normalize()` produces
  the fields you expect.
- **A malformed item** in an otherwise-valid response: assert it's skipped,
  not raised, and that `source.last_run` reflects it
  (`skipped` incremented, `yielded` for the rest, `failed=False`).
- **If your source has sub-units** (like Greenhouse's companies): one unit
  failing (e.g. a 404) among several that succeed should still yield the
  other units' postings, add exactly one entry naming the failing unit to
  `source.last_run.errors`, and leave `failed=False`. Also test every unit
  failing at once: `failed` should then be `True`.
- **A source that cannot start at all** (e.g. authentication failure, or a
  response missing a top-level key your whole source depends on): assert
  `fetch()` yields nothing and `source.last_run.failed` is `True`.
- **A config-level mistake** (unknown key, missing required field): assert
  it raises `SourceConfigError` at construction.

See [`tests/test_sources_greenhouse.py`](tests/test_sources_greenhouse.py)
and [`tests/fixtures/greenhouse_jobs.json`](tests/fixtures/greenhouse_jobs.json)
for a complete JSON API example,
[`tests/test_sources_weworkremotely.py`](tests/test_sources_weworkremotely.py)
and its `tests/fixtures/weworkremotely_*` files for the RSS shape,
[`tests/test_sources_freework.py`](tests/test_sources_freework.py) and its
`tests/fixtures/freework_*.html` files for the HTML-scraping shape
(including a fixture that specifically exercises the "layout has probably
changed" path), and [`tests/test_sources_base.py`](tests/test_sources_base.py)
for tests that exercise `Source`'s shared retry/backoff/rate-limit policy
directly.
