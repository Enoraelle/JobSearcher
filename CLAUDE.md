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
- `cli.py` — the `jobsearcher` console entry point (Click). Wires the layers
  above together; contains no business logic of its own.

Dependencies should flow one way: `sources` and `exporters` depend on
`models`; `scoring` depends on `models`; `storage` depends on `models`;
`cli` depends on everything else. Avoid circular imports between the
subpackages.

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

## Absolute rules

- **Never run `git push`, nor any command that rewrites history**
  (e.g. `git rebase`, `git commit --amend`, `git reset --hard`,
  `git push --force`). Leave publishing and history changes to the user.
- **Never add a new dependency** (runtime, dev, or optional extra) without
  asking the user first and getting explicit approval.
