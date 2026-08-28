# JobSearcher

A remote job search returns hundreds of postings, and the large majority of
them do not fit: wrong stack, wrong seniority, wrong location, or a role that
only shares a job title with what you do. Reading each one to find that out
is the actual cost of a job search. JobSearcher collects postings from
several sources into one local database and ranks them by how much of *your*
skill set each posting really covers — the overlap between the technologies
you list and the technologies the posting talks about — rather than by
whether a keyword appears. The ones worth reading end up at the top; the rest
stay searchable but out of the way.

Everything runs locally. The default path uses no API key: an offline
scorer, a SQLite file, and Markdown/CSV/JSON output. An optional LLM scorer
and an optional Notion exporter are available for those who want them.

## Installation

JobSearcher is not on PyPI yet; install it from a clone.

```bash
git clone https://github.com/Enoraelle/JobSearcher
cd JobSearcher
pip install .
```

This puts a `jobsearcher` command on your path. Python 3.11 or newer is
required.

Optional extras:

| Extra                            | What it adds                                              |
| -------------------------------- | -------------------------------------------------------- |
| `pip install ".[llm]"`           | The LLM scorer (`scoring.backend: llm`).                 |
| `pip install ".[notion]"`        | The Notion exporter (`jobsearcher export notion`).       |
| `pip install ".[dev]"`           | pytest, ruff, mypy — for working on JobSearcher itself.  |

Then create a configuration file:

```bash
jobsearcher init
```

`jobsearcher init` writes `config.yaml` into the current directory from the
example bundled in the package
([`src/jobsearcher/config.example.yaml`](src/jobsearcher/config.example.yaml)),
so it works the same from a `pip install` as from a clone. `config.yaml`
holds your profile and is where every command looks first; it is gitignored.
Every command also accepts `--config PATH` if you keep it elsewhere. The rest
of this README explains what to put in it.

## Quick start (no API key)

The `config.yaml` that `jobsearcher init` just wrote runs end to end with no
editing. It collects from We Work Remotely's public feed, scores every
posting offline against the example profile, and writes a Markdown digest.

```bash
jobsearcher init          # write config.yaml from the bundled example
jobsearcher run           # fetch -> score -> export, in one pass
jobsearcher list          # the ranked table
jobsearcher show <id>     # one posting in full, by the id prefix from the table
```

A real run on 2026-08-28, in a terminal 100 columns wide (your postings and
scores will differ — the feed changes daily, and the scores are relative to
the example profile):

```
$ jobsearcher run
────────────────────────────────────────────── fetch ──────────────────────────────────────────────
Fetch
┌────────────────┬─────────┬──────┬──────────┬─────┬───────┬────────┐
│ source         │ yielded │ kept │ filtered │ new │ dupes │ errors │
├────────────────┼─────────┼──────┼──────────┼─────┼───────┼────────┤
│ weworkremotely │      75 │   61 │       14 │  61 │     0 │      0 │
└────────────────┴─────────┴──────┴──────────┴─────┴───────┴────────┘
────────────────────────────────────────────── score ──────────────────────────────────────────────
Scored 61, failed 0.
───────────────────────────────────────────── export ──────────────────────────────────────────────
markdown: wrote 61 postings to digest.md

$ jobsearcher list --limit 3
┌──────────┬───────┬──────────────┬──────────────┬──────────────┬────────┬───────────────┬────────┐
│ id       │ score │ title        │ company      │ source       │ mode   │ location      │ status │
├──────────┼───────┼──────────────┼──────────────┼──────────────┼────────┼───────────────┼────────┤
│ 1d8c3b45 │    43 │ AI-Assisted  │ LMG Staffing │ weworkremot… │ remote │ Medellin      │ new    │
│          │       │ Software     │ Solutions    │              │        │               │        │
│          │       │ Engineer,    │              │              │        │               │        │
│          │       │ Web          │              │              │        │               │        │
│          │       │ Applications │              │              │        │               │        │
│ aa3d4686 │    29 │ Automation   │ Toptal       │ weworkremot… │ remote │ USA Only      │ new    │
│          │       │ Engineer     │              │              │        │               │        │
│          │       │ (UIPath) for │              │              │        │               │        │
│          │       │ innovative   │              │              │        │               │        │
│          │       │ AI Project   │              │              │        │               │        │
│ 24899f43 │    29 │ FULL TIME:   │ Yooli        │ weworkremot… │ remote │ Anywhere in   │ new    │
│          │       │ Software     │              │              │        │ the World     │        │
│          │       │ Engineer     │              │              │        │               │        │
│          │       │ Position -   │              │              │        │               │        │
│          │       │ React and    │              │              │        │               │        │
│          │       │ Rest         │              │              │        │               │        │
└──────────┴───────┴──────────────┴──────────────┴──────────────┴────────┴───────────────┴────────┘
```

`jobsearcher show` takes the id prefix from the first column and prints one
posting in full, including the score breakdown and the description (trimmed
here):

```
$ jobsearcher show 1d8c3b45
           id  1d8c3b4577fa
        title  AI-Assisted Software Engineer, Web Applications
      company  LMG Staffing Solutions
       source  weworkremotely
          url  https://weworkremotely.com/remote-jobs/lmg-staffing-solutions-ai-assisted-software-…
     location  Medellin
    work mode  remote
     eligible  Medellin
     language  en
    published  2026-08-04
      fetched  2026-08-28
       status  new
        score  43
      summary  matched 3/7 skills (python, django, docker); not mentioned: django rest framework,
               postgresql, celery, rest apis; location does not fit; work mode fits
      matched  python, django, docker
not mentioned  django rest framework, postgresql, celery, rest apis
─────────────────────────────────────────── description ───────────────────────────────────────────
Overview We are looking for a software engineer to build and scale our internal web applications.
Your mission is to deliver high-quality, maintainable solutions using a Next.js front end and a
Django back end, while actively using AI tools and Model Context Protocols (MCPs) to accelerate
development and enforce clean architecture.
...
```

The phases are independent and each one persists as it goes, so
`jobsearcher fetch`, `jobsearcher score`, and `jobsearcher export markdown`
can be run separately, and re-running any of them resumes rather than
restarts. `jobsearcher run` just chains all three.

### The commands

| Command                             | What it does                                                            |
| ----------------------------------- | ---------------------------------------------------------------------- |
| `jobsearcher init`                  | Create `config.yaml` from the bundled example.                        |
| `jobsearcher fetch`                 | Collect from the enabled sources, keyword-filter, store. No scoring.  |
| `jobsearcher score`                 | Score the postings that have no score yet.                            |
| `jobsearcher list`                  | Show stored postings as a table (`--json` for a JSON array).          |
| `jobsearcher show <id\|url>`         | Show one posting in full, including its description.                  |
| `jobsearcher status <id> <status>`  | Move a posting through your application pipeline (`applied`, ...).    |
| `jobsearcher export <format>`       | Write stored postings to `markdown`, `csv`, `json`, or `notion`.     |
| `jobsearcher run`                   | `fetch`, then `score`, then `export` — cron-friendly.                |

Global flags: `--config PATH` to use a different file, `-v`/`-vv` for more
logging, and `--dry-run` to compute everything and write nothing (no
database, no files, no outbound API calls).

## Configuration

`config.yaml` is a single YAML file. `profile.role` is the only strictly
required field; the sections below fill in the rest.

- **`search`** — filters applied while collecting. `title_keywords_include`
  and `title_keywords_exclude` are case-insensitive substring matches
  against the posting title: an excluded term anywhere in the title drops
  the posting, and if any include terms are set at least one must appear.
  `languages` is *not* a collection filter — every posting is kept and
  tagged with a best-effort detected language (`en`/`fr`/`de`/`es`, or blank
  when undetermined); this list is only the default filter `jobsearcher
  list` applies at display time, overridable per run with `--language`.

- **`profile`** — the scoring criteria. `skills` is the set the scorer looks
  for in each posting; `absent_skills` are technologies you want to avoid,
  and a posting that mentions one is penalized. `locations` and `work_mode`
  are reported as a separate fit check next to the score, never folded into
  it.

- **`sources`** — one block per source, each independently toggled with
  `enabled`. Remaining keys are source-specific (see the table below). A
  source left disabled is skipped entirely.

- **`storage`** — `backend: sqlite` and a `path`. SQLite is the only
  backend.

- **`scoring`** — `backend: keyword_match` (default, offline) or
  `backend: llm` (see [Scoring](#scoring)).

- **`exporters`** — one block per output format, each toggled with
  `enabled`. The file formats need an `output_path`.

A complete example, the one `jobsearcher init` installs:

```yaml
search:
  title_keywords_include: []
  title_keywords_exclude: [senior, staff, principal]
  languages: [en]

profile:
  role: "Python/Django Backend Developer"
  skills:
    - python
    - django
    - django rest framework
    - postgresql
    - celery
    - docker
    - rest apis
  absent_skills: [php, symfony]
  experience_years: 3
  work_mode: remote
  locations: [France, "Remote - EU"]

sources:
  weworkremotely:
    enabled: true
    feeds: [remote-jobs, remote-programming-jobs]
    fetch_full_description: false
    max_postings_per_feed: 50
  greenhouse:
    enabled: false
    companies: [example-company]
  freework:
    enabled: false
    keywords: ["python django", "développeur backend"]
    max_postings_per_keyword: 50

storage:
  backend: sqlite
  path: "./jobsearcher.db"

scoring:
  backend: keyword_match
  synonyms:
    - [postgresql, postgres, psql]
    - [rest apis, rest api, restful]
    - [kubernetes, k8s]
  absent_skill_penalty: 0.34
  absent_skill_penalty_cap: 1.0

exporters:
  markdown:
    enabled: true
    output_path: "./digest.md"
  csv:
    enabled: false
    output_path: "./jobs.csv"
  json:
    enabled: false
    output_path: "./jobs.json"
```

See [`src/jobsearcher/config.example.yaml`](src/jobsearcher/config.example.yaml)
for the same file with every option commented, including the opt-in `llm`
scoring block and the `notion` exporter block. It is what `jobsearcher init`
writes.

## Sources

| Source           | How it collects                                                  | Coverage                                                                                          |
| ---------------- | -------------------------------------------------------------- | ---------------------------------------------------------------------------------------------- |
| `weworkremotely` | RSS feeds — the site-wide `remote-jobs` feed and per-category feeds (`feeds:` list). | Remote-only jobs listed on We Work Remotely. Enabled by default; needs no configuration. RSS descriptions are truncated unless `fetch_full_description: true`. |
| `greenhouse`     | JSON API — one request per company slug against the public Greenhouse Job Board API (`companies:` list). | Postings on the Greenhouse-hosted board of each company you name. You choose the companies; nothing is discovered automatically. |
| `freework`       | HTML scrape — one search request per keyword against free-work.com's tech/IT job search (`keywords:` list). | French freelance / contract ("mission") postings. Fragile: free-work.com publishes no API, so a redesign can break it (see [Limitations](#limitations)). |

Adding a source is a single new file and no change to existing ones — see
[CONTRIBUTING.md](CONTRIBUTING.md).

## Scoring

### The default scorer (`keyword_match`)

Offline, deterministic, and needs no API key. The score is **skill coverage
on a 0–100 scale**: the fraction of `profile.skills` that appear in the
posting's title and description, minus a penalty for each `absent_skills`
entry the posting mentions.

- Matching folds case and accents and understands token boundaries, so
  `java` does not match `javascript` and `node.js` stays one token.
  Multi-word skills like `django rest framework` match as a phrase.
- `scoring.synonyms` is a list of equivalence classes: `[postgresql,
  postgres, psql]` makes any one of the three count as any other. Matching
  is already spelling-tolerant, so only add genuinely different names.
- Each matched `absent_skills` entry subtracts `absent_skill_penalty` (default
  0.34, so roughly three ruled-out skills zero a score), capped at
  `absent_skill_penalty_cap`.
- `location` and `work_mode` fit are computed and shown next to the score
  (`jobsearcher show`), and can be filtered on (`jobsearcher list --remote`),
  but are deliberately never mixed into the number.

The score's supporting detail — which skills matched, which did not, which
were penalized — is stored per posting and shown by `jobsearcher show`.

### The optional LLM scorer (`llm`)

A bonus for refining a bounded number of postings with a language model.
Install the extra and switch the backend:

```bash
pip install ".[llm]"
```

```yaml
scoring:
  backend: llm
  base_url: "https://api.openai.com/v1"   # any OpenAI-compatible endpoint
  model: "gpt-4o-mini"
  api_key_env: "OPENAI_API_KEY"           # the key is read from this env var, never from config
  max_postings_per_run: 25                # hard cap on calls per `jobsearcher score`
```

```bash
export OPENAI_API_KEY=sk-...
jobsearcher score
```

The API key is only ever read from the environment variable named by
`api_key_env`. `max_postings_per_run` bounds the cost: once it is reached the
scoring phase stops cleanly and leaves the rest for the next run. Unlike the
keyword scorer, the LLM scorer also reads *stated requirements* out of the
posting text and reports the ones your profile does not cover.

## Architecture

```
sources/  ──▶  keyword filter  ──▶  storage/  ──▶  scoring/  ──▶  exporters/
(fetch &        (pipeline, on       (SQLite,       (unscored     (markdown, csv,
 normalize)      the title)          dedup by URL)  postings)     json, notion)
                                         ▲                            
                                   cli/  │  reads for list / show / status
```

The package lives under `src/jobsearcher/`, split by responsibility with a
one-way dependency flow:

| Module        | Role                                                                                       |
| ------------- | ---------------------------------------------------------------------------------------- |
| `models.py`   | Pydantic data models: `JobPosting`, `ScoreResult`, the enums. No I/O.                   |
| `config.py`   | Load and validate `config.yaml` into typed models.                                      |
| `language.py` | The tiny function-word heuristic that tags each posting's language.                     |
| `sources/`    | One module per source. Fetches and normalizes into `JobPosting`; knows nothing about storage, scoring, or export. |
| `storage/`    | Persistence. Owns all read/write access to stored postings. SQLite today.               |
| `scoring/`    | Pure posting + profile → `ScoreResult`. No I/O (the `llm` scorer is the deliberate exception). |
| `exporters/`  | Stored postings → an external representation (file or service). No scraping logic.      |
| `pipeline.py` | Orchestration only: run sources, filter, store, score the unscored, export. Each phase is independent and resumable. |
| `cli.py`      | The `jobsearcher` command. Parses arguments, calls `pipeline` or `storage`, renders with Rich, picks the exit code. No business logic. |

`sources`, `storage`, `scoring`, and `exporters` are packages because each
has interchangeable implementations behind a shared contract; `models`,
`config`, and `language` are flat modules because each has exactly one.

Storage uses `INSERT OR IGNORE` keyed on the normalized posting URL: a
posting already in the database is never overwritten by a later fetch, so a
score is never silently clobbered by a rescrape.

## Limitations

- **HTML scrapers break when the site's layout changes.** `freework`, and
  We Work Remotely's `fetch_full_description` option, depend on the pages'
  markup, which no site owes a scraper. When a layout changes, `freework`
  fails with a message that says the layout has probably changed (rather
  than a stack trace), and `fetch_full_description` quietly falls back to
  the truncated RSS description. Expect to update selectors periodically.
- **The keyword scorer is lexical, not semantic.** It matches names, not
  meaning: it will not infer that a posting asking for "DRF" wants Django
  unless you add that synonym. Terse postings produce low coverage without
  anything being wrong — the score is "how much overlap is *visible* in the
  text", not a judgement on the job.
- **LLM scoring costs API calls.** Every posting it scores is a paid request
  to your endpoint. `max_postings_per_run` is the only guardrail; there is
  no caching of verdicts between runs.
- **Notion needs manual setup on the Notion side.** You must create an
  internal integration, share the target database with it, and create the
  database properties JobSearcher writes (`Name`/title, `URL`/url,
  `Status`/select, `Company`/text, `Score`/number, `Source`/select) with
  those exact types. JobSearcher checks the schema up front and names each
  missing or mistyped property, but it cannot create them for you.
- **Stored postings are not refreshed.** Once a posting's URL is in the
  database, a later fetch leaves its title, description, and other fields
  untouched even if the source changed them. This is deliberate for v1 (it
  protects the score); see "Open questions" in [CLAUDE.md](CLAUDE.md).
- **Language detection is a small heuristic.** A few dozen function words
  per language, `en`/`fr`/`de`/`es` only. It returns "undetermined" rather
  than guess on short text, and undetermined postings are never hidden by a
  language filter.
- **Deduplication is per normalized URL.** The same job cross-posted to two
  sources under two URLs is stored twice.
- **There is no database repair command.** A stored row that can no longer
  be decoded — a value written by a newer version of JobSearcher, a
  hand-edited database, a file damaged by a crash mid-write — is skipped by
  reads rather than allowed to take the command down. The row stays in the
  database, `jobsearcher list` and `jobsearcher export` report how many rows
  they had to skip, and each one's id and URL is logged. Nothing can mend
  it, though: the recourse is to delete the database file (`storage.path`,
  `./jobsearcher.db` by default) and collect again. Deleting it also
  discards every score and every application status you had recorded, so
  export what you care about first — `jobsearcher export json` reads through
  the same skip-and-report path and will save everything still readable.

## License

MIT — see [LICENSE](LICENSE).
