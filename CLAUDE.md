# CLAUDE.md

Guidance for Claude Code (and any other AI assistant) working in this repository.

## Project

JobSearcher is a command-line tool that collects job postings from multiple
sources, stores them locally, scores them against user-defined criteria, and
exports the results (e.g. to files or external services such as Notion).

## Target architecture

The package lives under `src/jobsearcher/` and is split by responsibility:

- `models.py` — data models (job postings, sources, scores, config schemas)
  built on pydantic. No I/O here.
- `config.py` — loading and validation of user configuration (YAML) into the
  models above.
- `storage/` — persistence layer for collected job postings (e.g. a local
  database). Owns all read/write access to stored data.
- `sources/` — one module per job source (scraper or API client). Each source
  is responsible only for fetching and normalizing postings into the shared
  models; it must not know about storage, scoring, or export.
- `scoring/` — pure functions/classes that turn a job posting (plus config)
  into a score. No I/O.
- `exporters/` — turn stored/scored postings into an external representation
  (file formats, third-party services). No scraping logic.
- `pipeline.py` — orchestration only: run the enabled sources, filter by the
  configured keywords, store, score the unscored postings, export. Each
  phase is independent and resumable (it persists as it goes); the phase
  methods are generators so the CLI can show progress. No rendering, no
  argument parsing.
- `cli.py` — the `jobsearcher` console entry point (Click + Rich). Parses
  arguments, calls `pipeline` (for fetch/score/export/run) or `storage`
  directly (for list/show/status), renders the result, and picks the exit
  code. Contains no business logic of its own.

Dependencies should flow one way: `sources` and `exporters` depend on
`models`; `scoring` depends on `models`; `storage` depends on `models`;
`pipeline` depends on all of those; `cli` depends on everything else. Avoid
circular imports between the subpackages.

## File vs. package

Use a package (a directory with `__init__.py`) only when a subsystem has
multiple interchangeable implementations of the same contract: `sources/`,
`storage/`, `scoring/`, `exporters/`. Use a single flat module when there is
exactly one implementation: `models.py`, `config.py`.

If a flat module like `config.py` grows past 250 lines, split it (e.g. into
`config/schema.py` for the pydantic models and `config/loader.py` for
resolution/validation logic), re-exporting the same public names unchanged
from the new `config/__init__.py` so callers don't need to change their
imports.

## Conventions

- **English only.** All code, comments, docstrings, commit messages, and
  documentation in this repository are written in English, with no
  exceptions.
- **Docstrings.** Google-style docstrings on all public modules, classes, and
  functions.
- **Typing.** Full type hints everywhere; the codebase is checked with
  `mypy --strict`. Avoid `Any` unless there is no reasonable alternative.
- **Tests never touch the network.** Tests must not perform real HTTP
  requests or hit external services. Mock or stub any `httpx` client (or
  other I/O) instead.
- **Formatting and linting.** `ruff check` and `ruff format` are the source
  of truth for style; don't hand-fight the formatter's choices.

## Open questions

- **Refresh strategy for already-stored postings.** `SqliteStorage.save_many`
  ([sqlite.py](src/jobsearcher/storage/sqlite.py)) inserts with
  `INSERT OR IGNORE` keyed on URL, so a posting already in the database is
  never updated by a later scrape — not just its score, but also its title,
  description, and every other field, even if the source changed them. This
  is deliberate for v1 (see the docstring on `save_many`): it guarantees a
  score is never silently clobbered by a rescrape. Nothing yet refreshes
  stale non-scoring content, though. Revisit before v2 — options include a
  separate `refresh_many`/`upsert_content` operation that updates everything
  except score/status/summary/matched_skills/missing_skills, or a
  content-hash check to detect and apply only real changes.

- **`normalize()` makes HTTP calls in the We Work Remotely source.** With
  `fetch_full_description: true`,
  `WeWorkRemotelySource.normalize`
  ([weworkremotely.py](src/jobsearcher/sources/weworkremotely.py)) issues
  one extra GET per posting to replace the truncated RSS description. That
  contradicts the split `Source` is built on — `fetch_raw` owns all HTTP,
  `normalize` converts one already-fetched item — and the surrounding
  machinery assumes it: `fetch()` treats a `normalize` failure as a
  malformed *item* (it catches only `KeyError`/`ValueError`/
  `ValidationError` and counts `skipped`), and the per-unit error isolation
  in `fetch_raw` never sees these requests, so a detail-page failure is
  only logged — it reaches neither `skipped` nor `last_run.errors`, and a
  posting silently degraded to its truncated description counts as a clean
  success. Rate limiting is affected the same way: `min_request_interval`
  is the source's single global spacing, sized in every other source for
  one request per unit, which is why this source's docstring has to tell
  callers to set it when they enable the option. Deliberate for v1 (the
  fan-out is opt-in and degrades rather than fails), and no code change is
  proposed here — but anyone reasoning about item counts, recorded errors,
  or request pacing should know `normalize` is not side-effect-free.
  Revisit alongside a decision on where a two-phase fetch (list, then
  detail) belongs: most plausibly in `fetch_raw`, which already owns unit
  boundaries and error recording.

## Absolute rules

- **Never run `git push`, nor any command that rewrites history**
  (e.g. `git rebase`, `git commit --amend`, `git reset --hard`,
  `git push --force`). Leave publishing and history changes to the user.
- **Never add a new dependency** (runtime, dev, or optional extra) without
  asking the user first and getting explicit approval.
