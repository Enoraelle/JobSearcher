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

## Absolute rules

- **Never run `git push`, nor any command that rewrites history**
  (e.g. `git rebase`, `git commit --amend`, `git reset --hard`,
  `git push --force`). Leave publishing and history changes to the user.
- **Never add a new dependency** (runtime, dev, or optional extra) without
  asking the user first and getting explicit approval.
