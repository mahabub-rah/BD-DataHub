# BD DataHub

**Find Bangladesh data in one place: discover, access, cite.**

BD DataHub is a searchable catalogue of publicly available datasets and research papers about
Bangladesh. It does **not** host or redistribute data. For each dataset it stores standardized
**metadata** and links to the **original publisher**: ministries, departments, Bangladesh Bank,
statutory bodies, and international organizations that publish Bangladesh-specific data.

| In the seed catalogue | |
|---|---|
| Datasets | 336, from 96 publishers, each checked against the live source |
| Research papers | 4,468, metadata from [OpenAlex](https://openalex.org) |
| Categories | 20 standard categories, shared by datasets and papers |

Built with FastAPI, Uvicorn, Jinja2 and SQLite (FTS5). It runs on Render's free plan with no database server to manage.

## Contents

- [Quick start](#quick-start)
- [Features](#features)
- [Site map](#site-map)
- [Command-line tools](#command-line-tools)
- [Research papers](#research-papers)
- [Configuration](#configuration)
- [Deploy for free on Render](#deploy-for-free-on-render)
- [Data and verification policy](#data-and-verification-policy)
- [Adding or correcting a dataset](#adding-or-correcting-a-dataset)
- [Tests](#tests)
- [Project layout](#project-layout)
- [Roadmap](#roadmap)
- [License](#license)

## Quick start

Requires **Python 3.14** (pinned in [`.python-version`](.python-version)).

```bash
git clone <this-repository-url>
cd bd_data_hub
python -m venv .venv
.venv\Scripts\activate            # Windows   (macOS/Linux: source .venv/bin/activate)
pip install -r requirements.txt
uvicorn app.main:app --reload     # open http://127.0.0.1:8000
```

You don't need any setup beyond this. On first start the app builds `data/bd_data_hub.db` from the files in
[`seed/`](seed), so the full catalogue and papers are available right away.

On later starts the app syncs the database with `seed/` and reloads only the seed files that changed,
so a normal restart takes a fraction of a second. **After pulling new seed data, restart the server.**
The sync never deletes or overwrites records that are not in the seed, such as harvested drafts,
link-check results and view counts. Link-check results in `seed/link_checks.json` are applied only
where they are newer than those already in the database. If a seed file is malformed, the site still starts, logs a
warning, and tries that file again on the next start.

## Features

**Discover**
- Full-text search (SQLite FTS5 with BM25 ranking), English↔Bangla synonyms and prefix matching.
- Natural-language queries: `monthly rainfall csv 2015-2020` becomes frequency, format and year
  filters. When filters would return nothing, the app relaxes them automatically.
- Faceted filters: category and subcategory, publisher, format, machine-readable, update frequency,
  geographic level and coverage years.
- `/search?q=…` searches datasets and papers together.

**Organize**
- 20 standard categories with extensible subcategories. Aliases map synonyms onto canonical
  subcategories (for example *CPI* → *Inflation & Prices*).
- Controlled vocabularies for formats, geographic levels and update frequencies.
- The homepage separates **Bangladesh official sources** from **international sources**
  (`/datasets?source_type=official` or `international`).

**Verify**
- Each record stores a verification status, date, verifier, evidence URL and evidence note.
- A link checker (`python -m app.monitor`) finds broken links (after 3 consecutive failures) and
  **changes**, such as new or removed documents on a source page. It respects robots.txt, sends a
  descriptive User-Agent, throttles requests per host and reads at most 2 MB per page.

**Access and cite**
- One click to the source page or the direct file. CSV and JSON files can be previewed on the
  dataset page (first 25 rows, up to 30 columns).
- Citations in APA 7, MLA 9, Chicago, Harvard, BibTeX and RIS on every dataset page, and in
  APA 7, MLA 9, BibTeX and RIS on every paper page. Dataset citations can also be downloaded as
  `.bib` or `.ris` files.
- schema.org `Dataset` JSON-LD on every dataset page, for Google Dataset Search, plus `sitemap.xml`
  and `robots.txt`.
- "Related datasets", scored by shared subcategories, categories, tags and publisher.

**Research**
- `/papers`: full-text search over titles, authors, abstracts, venues and topics. Filters for research
  category, topic, journal or venue, publication years and open access. Sort by best match,
  citations or date.
- `/research`: papers by category, with the most-cited and newest papers in each. Each category page
  also lists datasets on the same topic, and each dataset category page shows its most-cited papers.

**Harvest**
- Importers for the Humanitarian Data Exchange (HDX) and the World Bank Indicators API. They create
  **unpublished drafts** for review, so nothing appears on the site until it is checked.

## Site map

| Path | Page |
|---|---|
| `/` | Homepage: search, official and international sources, "Check sources now" |
| `/datasets` | Dataset search with facets (`?q=`, `?category=`, `?source_type=`, …) |
| `/datasets/{slug}` | Dataset record, evidence, link-check history, preview, citations |
| `/datasets/{slug}/cite.bib`, `/cite.ris` | Citation downloads |
| `/categories`, `/organizations`, `/organizations/{slug}` | Browse by category or publisher |
| `/papers`, `/papers/{openalex_id}` | Research paper search and paper pages |
| `/research`, `/research/{category}` | Research by category |
| `/search?q=` | Search datasets and papers together |
| `/about` | About the project |
| `/healthz`, `/robots.txt`, `/sitemap.xml` | Health check and crawler files |

## Command-line tools

```bash
python -m app.seed [--reset]                 # load all of seed/ now (normally a restart is enough)
python -m app.monitor [--stale-hours 24]     # check source links and write seed/link_checks.json
python -m app.monitor --no-export            # check links without writing seed/link_checks.json
python -m app.monitor --export-only          # write seed/link_checks.json without checking links
python -m app.harvest hdx [--limit 50]       # import HDX Bangladesh datasets as review drafts
python -m app.harvest worldbank [--limit N] [--export-seed seed/sources/worldbank.json]
                                             # World Bank source databases with Bangladesh data, as drafts
python -m app.papers harvest [--limit N] [--out DIR] [--fresh]   # rebuild seed/papers/*.json from OpenAlex
python -m app.papers fill-categories [--target N] [--dir DIR]    # add papers to thin research categories
python -m app.papers enrich-topics [--dir DIR]                   # add OpenAlex subfield/field to seed papers
```

Harvested records are hidden unless you pass `--publish`. The World Bank harvester creates one record
per World Bank source database (Gender Statistics, International Debt Statistics, …) and keeps it
only if a live API request returns Bangladesh values. That request becomes the record's evidence.
`--export-seed FILE` writes the records as a seed file, which is how
[`seed/sources/worldbank.json`](seed/sources/worldbank.json) was built.

## Research papers

The papers catalogue holds **metadata only**, from the OpenAlex `/works` API (OpenAlex metadata is
CC0). There is no full text. Each paper links to its DOI, the publisher's page and, where OpenAlex
knows one, an open-access copy. Nothing is written by hand: fields the API does not return stay
empty, and abstracts are rebuilt from OpenAlex's abstract index only when it has one.

### Selection

Every paper names Bangladesh in its title or abstract. [`seed/papers/`](seed/papers) holds:

| File | Papers | Contents |
|---|---|---|
| `most-cited.json` | 3,000 | The most-cited such works. |
| `recent.json` | 1,000 | Works published from 1 January 2024 to the harvest date: the most-cited of each year in equal shares, excluding works in the most-cited set. |
| `by-category.json` | 468 | Extra papers for categories the other two files leave thin (see [Filling thin categories](#filling-thin-categories)). |

All papers are deduplicated by OpenAlex ID and DOI, and placeholder author strings such as
"Corresponding authors" or "Anonymous" are dropped. Each file records its queries, harvest date and
selection rule.

### Refreshing

`python -m app.papers harvest` fetches the works again and rewrites `most-cited.json` and `recent.json`.

| Option | Effect |
|---|---|
| `--limit N` | Size of the most-cited set (default 3,000). The recent set is a third of it. |
| `--out DIR` | Write the files somewhere other than `seed/papers/`. |
| `--fresh` | Ignore a saved partial harvest and start over. |

OpenAlex meters usage per day: about 1,000 credits without an API key, and each search page costs
10. The harvester makes at most four requests a second and pages through year slices only as far as
it needs to. If the daily credits run out, it writes the papers collected so far and saves its place
in `data/papers_harvest_state.json` (or next to `--out`). Run the same command after the limit resets
to continue.

### How papers get categories

Papers use the same top-level categories as datasets. A paper's categories come from **fixed lookup
tables over OpenAlex's own topic hierarchy** (topic → subfield → field), defined in
[`app/papers.py`](app/papers.py). No paper is classified by hand, and no text model or machine
learning is involved. For each of a paper's OpenAlex topics, primary topic first, the app checks:

1. `TOPIC_RULES`: word patterns matched against the topic's *name* (not the paper's text), for themes
   that cut across subfields such as poverty, trade, climate or education;
2. then `SUBFIELD_MAP` for the topic's subfield, or else `FIELD_MAP` for its field.

A paper can belong to several categories, ordered by relevance. "Other" is used only when nothing
matches. The hierarchy comes from each paper's `topic_tree` in the seed files. `enrich-topics` adds
it to older seed papers that lack it, using one OpenAlex list request per 100 papers.

**Changing the mapping:** edit the tables in `app/papers.py`, bump `MAPPING_VERSION`, and restart.
The startup sync fingerprints the paper seed files, `categories.json` and `MAPPING_VERSION`, and
rebuilds paper categories when any of them changes.

### Filling thin categories

`python -m app.papers fill-categories` adds papers to every category that has fewer than `--target`
papers in the other seed files. For each thin category it searches the OpenAlex topics that the tables
above map to that category, combined with the Bangladesh title/abstract search, most-cited first. It
keeps only works that name Bangladesh, map to that category through their own topics, and are not
already in another seed file. The output, `seed/papers/by-category.json`, records the queries, the
categories filled, any category that ran out of works before reaching the target ("ceilings"), and the
`MAPPING_VERSION` used.

| Option | Effect |
|---|---|
| `--target N` | Fill each category up to N papers (default 100). |
| `--dir DIR` | Seed folder to read and write (default `seed/papers/`). |

It uses the same daily credit budget and resume behaviour as the harvester. Its state is saved in
`data/papers_harvest_state.json`, or `DIR/.fill_state.json` with `--dir`.

## Configuration

Every setting has a default, so configuration is optional. To override a setting, set an environment
variable or add it to a `.env` file in the project root (`.env` is git-ignored).
For local setup, copy [`.env.example`](.env.example) to `.env`; never commit credentials or signed
URLs. Link-check results redact API keys, access keys, tokens, signatures, and other authentication
parameters before they are stored in the database or [`seed/link_checks.json`](seed/link_checks.json).

| Variable | Default | Purpose |
|---|---|---|
| `BDH_DATABASE` | `data/bd_data_hub.db` | SQLite database path. |
| `BDH_ALLOWED_HOSTS` | *(any host)* | Comma-separated host names the site answers to, e.g. `bd-data-hub.onrender.com`. |
| `BDH_REFRESH_COOLDOWN_HOURS` | `6` | Minimum hours between link checks started from the "Check sources now" button. |
| `BDH_MONITOR_CONCURRENCY` | `6` | Link checker: simultaneous requests. |
| `BDH_MONITOR_TIMEOUT` | `25` | Link checker: request timeout in seconds. |
| `BDH_MONITOR_HOST_DELAY` | `2` | Link checker: seconds between requests to the same host. |
| `BDH_MONITOR_UA` | `BangladeshDataHubLinkChecker/1.0 (…)` | Link checker User-Agent. |
| `BDH_OPENALEX_MAILTO` | *(unset)* | Contact e-mail sent to OpenAlex by `app.papers` commands (OpenAlex's "polite pool"). |

## Deploy for free on Render

The repository includes a [`render.yaml`](render.yaml) Blueprint for Render's **free** web service.

1. Push the project to GitHub. `data/` and `.venv/` are already git-ignored.
2. On [render.com](https://render.com), sign in with GitHub, choose **New → Blueprint**, and pick the
   repository. Render reads `render.yaml`.
3. Click **Apply**. The build installs `requirements.txt` and starts
   `uvicorn app.main:app --host 0.0.0.0 --port $PORT --workers 1 --proxy-headers --forwarded-allow-ips="*"`,
   with `/healthz` as the health check.
4. Once it is live, open **Environment** and set `BDH_ALLOWED_HOSTS` to the address Render gave you.

On the free plan:

- **There is no persistent disk.** Each start rebuilds SQLite from `seed/`. To change the catalogue,
  edit the seed files, commit and push, and Render redeploys automatically.
- **Link-check results come from `seed/link_checks.json`.** Run the checker on your own machine, then
  commit the file and push:

  ```bash
  python -m app.monitor
  git add seed/link_checks.json
  git commit -m "Update link checks"
  git push
  ```

  Each start loads the file, so the results survive restarts, and the last 5 checks per link keep
  the "broken after 3 consecutive failures" rule working across runs. Checks started on Render with
  the "Check sources now" button, and view counts, still reset on every restart or redeploy.
- **The service sleeps when idle.** After 15 minutes without visitors, the first request takes about
  30–60 seconds.
- **Keep one worker.** The "Check sources now" button allows one background run per process.

Hosts with a read-only file system, such as Vercel, are not supported because the app writes to
SQLite at runtime.

## Data and verification policy

**No dataset is fabricated.** Every entry in `seed/sources/*.json` was added only after the
publisher's page was loaded and shown to provide the dataset. Most entries were checked on
27 September 2026. The `worldbank.json` entries were checked on 1 October 2026 through the World Bank
Indicators API.

- Each entry records an `evidence_url`, an `evidence_note` describing what was seen, and `checked_at`.
  The loader refuses entries without evidence.
- For `worldbank.json`, the `evidence_url` is an API request that returned Bangladesh values. The
  `evidence_note` also records whether the linked DataBank page loaded with that database's name.
- Fields that could not be confirmed, such as licence or some coverage years, are left empty rather
  than guessed.
- Sources that could not be loaded (57 so far) are listed under `unreachable` in each file instead
  of being filled in from memory.
- Records added later through a harvester start as **Not yet verified** until someone checks the source.

## Adding or correcting a dataset

1. Open the publisher's page and confirm it provides the dataset.
2. Add or edit the entry in the matching `seed/sources/*.json` file (economy, environment, governance,
   social, portals). The format is documented in [`seed/SEED_SPEC.md`](seed/SEED_SPEC.md).
3. Fill in `evidence_url`, `evidence_note` and `checked_at`. Leave any field you could not confirm empty.
4. Restart the server, check the dataset page, run the tests, then open a pull request.

## Tests

```bash
python -m unittest discover -s tests
```

The smoke tests in [`tests/test_smoke.py`](tests/test_smoke.py) use the standard library and FastAPI's
`TestClient` against a temporary database built from `seed/`, so they need no extra packages and make no
network requests. They cover every page type, search edge cases (Bangla, natural-language and malformed
queries), citation downloads, the sitemap, 404 handling and gzip responses.

## Project layout

```
app/
  main.py            FastAPI app, middleware, error pages
  config.py          settings from environment / .env
  schema.sql, db.py  SQLite schema (with FTS5 index) and connections
  models.py          Pydantic validation for datasets and organizations
  repository.py      CRUD, faceted search, stats, recommendations
  nlsearch.py        natural-language query parsing and EN/BN synonym expansion
  vocab.py           controlled vocabularies (formats, levels, frequencies)
  citation.py        APA / MLA / Chicago / Harvard / BibTeX / RIS
  serializers.py     schema.org JSON-LD
  monitor.py         polite link checker and change detection
  preview.py         on-demand first-rows preview of CSV/JSON files
  harvest.py         harvesters: CKAN (HDX) and the World Bank Indicators API
  papers.py          OpenAlex harvester, topic → category mapping, papers search
  seed.py            taxonomy and seed loader, startup sync
  routers/           web.py (datasets, organizations, categories), papers.py (papers, research, search)
  templates/, static/
seed/
  categories.json    the 20 categories, subcategories and aliases
  SEED_SPEC.md       seed file format
  sources/*.json     the dataset catalogue
  papers/*.json      research papers (OpenAlex metadata)
  link_checks.json   committed link-check results (written by app.monitor; created on first run)
tests/              smoke tests (python -m unittest discover -s tests)
render.yaml          Render Blueprint (free web service)
```

## Roadmap

- Semantic or LLM-assisted search, and "ask the catalogue"
- More harvesters (BBS and ministry sitemaps) with review queues
- A standardized access layer: tidy CSV/JSON mirrors where licences permit
- A Bangla-language interface

## License

The code does not have a license yet. Dataset metadata describes third-party publications, so the data
itself stays under each publisher's own terms. Paper metadata comes from OpenAlex under CC0.
