# Contributing

Everything in this repository — code, comments, docstrings, commit messages,
this file — is written in **English**, with no exceptions.

## Development environment

Python 3.11 or newer. From a clone:

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

The `dev` extra pulls in pytest, pytest-cov, ruff, and mypy. The `-e`
(editable) install means your source changes take effect without a
reinstall. The `jobsearcher` command is on your path from here.

To exercise the optional scorer and exporter, add their extras too:
`pip install -e ".[dev,llm,notion]"`.

## The checks

CI (`.github/workflows/ci.yml`) runs three things on Python 3.11 and 3.12.
Run all three locally before opening a pull request:

```bash
ruff check .          # lint
ruff format .         # formatting (CI does not run this, but keep the tree formatted)
mypy                  # type check, in --strict mode via pyproject config
pytest                # tests, with coverage
```

- **ruff** is the single source of truth for style. Its config is in
  `pyproject.toml`. Don't hand-fight the formatter.
- **mypy** runs in strict mode. Every function is fully annotated; avoid
  `Any` unless there is genuinely no alternative (and leave a comment when
  there isn't).
- **pytest** is configured in `pyproject.toml` to collect from `tests/` and
  report coverage. Run a single file with `pytest tests/test_scoring_keyword.py`,
  a single test with `pytest tests/test_scoring_keyword.py::test_name`, and
  skip the coverage report with `pytest --no-cov` while iterating.

### Tests never touch the network

Tests must not perform real HTTP requests or hit external services. Drive
`httpx` through `httpx.MockTransport` (see the source tests), or use the
network-free `FakeSource` registered in `tests/conftest.py` for pipeline and
CLI tests. A test that needs a real response should replay a recorded
fixture from `tests/fixtures/` instead.

## Architecture in one paragraph

`src/jobsearcher/` is split by responsibility with a one-way dependency
flow: `sources/` and `exporters/` and `scoring/` and `storage/` each depend
only on `models.py` (and `config.py`); `pipeline.py` depends on all of them;
`cli.py` depends on everything. `sources/`, `storage/`, `scoring/`, and
`exporters/` are packages because each has interchangeable implementations
behind a shared contract; `models.py`, `config.py`, and `language.py` are
flat modules because each has exactly one. Keep this flow intact — no
circular imports between subpackages, and no I/O in `models` or `scoring`
(the `llm` scorer is the one deliberate exception). See
[CLAUDE.md](CLAUDE.md) for the full statement.

---

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

Adding one should never require changing an existing file *except*
`sources/__init__.py` (step 4) and `src/jobsearcher/config.example.yaml`
(step 5). The steps below use `sources/greenhouse.py` as the reference
example.

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

### 5. Add the config shape to `src/jobsearcher/config.example.yaml`

This file ships as package data (`jobsearcher init` writes it), so it lives
inside the package, not at the repository root. Under `sources:`, following
the existing entries:

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

---

## Adding a scorer

A scorer turns one `JobPosting` plus the user's `ProfileConfig` into a
`ScoreResult`. The contract is the `Scorer` protocol in
[`scoring/base.py`](src/jobsearcher/scoring/base.py): a single
`score(posting, profile) -> ScoreResult` method. It is a structural
protocol, so there is no base class to subclass — a plain class with that
method is a scorer. If your scorer holds a resource (an HTTP client), give
it a `close()` method too; the pipeline calls it when the phase ends.

Unlike sources, scorers are **not** auto-registered — `get_scorer` in
[`scoring/__init__.py`](src/jobsearcher/scoring/__init__.py) dispatches on
`scoring.backend` explicitly. To add one:

1. Create `src/jobsearcher/scoring/<your_scorer>.py` with your class.
   Follow the two existing modules:
   [`keyword.py`](src/jobsearcher/scoring/keyword.py) for an offline, pure
   scorer, [`llm.py`](src/jobsearcher/scoring/llm.py) for an online one.
2. Re-validate your slice of the config into a typed model with
   `extra="forbid"`, exactly as `KeywordScorerConfig` does. Never read a
   secret from config — only from an environment variable named by a config
   field (see `LlmScorerConfig.api_key_env`).
3. Reuse `evaluate_location_match` / `evaluate_work_mode_match` from
   `scoring/base.py` for the eligibility fields rather than reimplementing
   them; those are deliberately not keyword-specific.
4. If your scorer is metered (costs money or rate-limited calls), raise a
   subclass of `ScorerBudgetExhaustedError` when the budget is spent. The
   pipeline catches it and ends the phase cleanly, leaving the rest for a
   later run — it is not a failure.
5. Wire it into `get_scorer`: add a branch for your `backend` value, and
   import your module *inside that branch* if it depends on an optional
   extra (see how `llm` is imported lazily).
6. Document the config block in `src/jobsearcher/config.example.yaml`,
   commented out if it needs an extra or a key.
7. Test it offline. For an HTTP-backed scorer, inject an
   `httpx.MockTransport` client (see
   [`tests/test_scoring_llm.py`](tests/test_scoring_llm.py)); assert on the
   budget behaviour and on malformed-response handling, not just the happy
   path.

## Adding an exporter

An exporter turns a batch of stored postings into an external
representation. The contract is the `Exporter` protocol in
[`exporters/base.py`](src/jobsearcher/exporters/base.py): a single
`export(postings, options) -> ExportResult` method. Every failure it reports
must be an `ExporterError` whose message says what the user has to change —
the option to set, the path that could not be written, the exact remote
property to create. A bare stack trace is never acceptable for a
misconfigured export.

Like scorers, exporters are dispatched explicitly, not auto-registered. To
add one:

1. Create the class. Put it in
   [`exporters/files.py`](src/jobsearcher/exporters/files.py) if it is
   another offline file format, or its own module if it talks to a service
   (follow [`notion.py`](src/jobsearcher/exporters/notion.py)).
2. Validate `options` into a typed model with `extra="forbid"`, so a
   mistyped option is an error rather than a silent no-op. Read secrets only
   from the environment.
3. Decide the output order yourself — the built-in exporters sort by score,
   highest first, unscored last (`_by_score` in `files.py`). Do not mutate
   the postings.
4. Register it in
   [`exporters/__init__.py`](src/jobsearcher/exporters/__init__.py): add it
   to `_BUILTIN_EXPORTERS` (always-importable formats) or give it a lazy
   branch in `get_exporter` (formats behind an extra, like `notion`). Either
   way it must appear in `EXPORTER_FORMATS`, which drives the CLI's format
   validation and help text.
5. Document the config block in `src/jobsearcher/config.example.yaml`.
6. Test it against a `tmp_path` file, or an `httpx.MockTransport` for a
   service. Cover the misconfiguration paths — missing required option,
   unknown option, unwritable path — and assert the error message names the
   fix. See [`tests/test_exporters_files.py`](tests/test_exporters_files.py)
   and [`tests/test_exporters_notion.py`](tests/test_exporters_notion.py).

---

## Commit conventions

- **English only**, like everything else in the repository.
- Follow the [Conventional Commits](https://www.conventionalcommits.org)
  style already in the history: a `type: summary` subject in the
  imperative mood, e.g. `feat: add Remotive source`, `fix: keep undetermined
  language visible in list`, `refactor: extract eligibility helpers`,
  `docs: document the LLM scorer`, `test: cover the freework layout-change
  path`, `chore: bump ruff`.
- Keep the subject under ~72 characters. Use the body to explain *why*, not
  *what*, when the change isn't self-evident.
- One logical change per commit. A new source, its `__init__.py` wiring, its
  `src/jobsearcher/config.example.yaml` entry, and its tests belong in the
  same commit; an unrelated formatting sweep does not.
- Do not commit `config.yaml`, `*.db`, or `.coverage` — they are gitignored,
  keep it that way.
- **Never** `git push`, `git rebase`, `git commit --amend`, `git reset
  --hard`, or `git push --force`. Leave publishing and history rewriting to
  the maintainer (see "Absolute rules" in [CLAUDE.md](CLAUDE.md)).

## Adding a dependency

Don't, without asking the maintainer first and getting explicit approval —
runtime, dev, or optional extra alike. The HTML-scraping stack is
BeautifulSoup with the stdlib `html.parser` backend; RSS is
`xml.etree.ElementTree`. Reach for what's already here before proposing
something new.
