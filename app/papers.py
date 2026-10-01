"""Research papers about Bangladesh, harvested from the OpenAlex API.

Every record comes straight from an OpenAlex /works response: nothing is written by hand
and nothing is scraped from Google Scholar, ResearchGate or publisher pages. Fields the
API does not provide stay null. Abstracts are rebuilt from OpenAlex's
`abstract_inverted_index` (word -> positions) only when the API returns one.

Selection (research ABOUT Bangladesh; every paper names Bangladesh in its title or abstract):
  * most-cited: the ~3000 most-cited works matching title_and_abstract.search:bangladesh.
  * recent: ~1000 works published from 2024-01-01 up to the harvest date, the most-cited of
    each year (2024, 2025, 2026 to date) in equal shares, so the set spans the whole period
    instead of the last few weeks. Works already in most-cited are left out.

OpenAlex meters usage (about 1000 credits a day without a key; a search page costs 10), so
the harvest is frugal. One all-years search sorted by citations times out, so the query is
split into year slices, and the slices are merged by citation count: the next page always
comes from the slice whose unread works are cited most, and paging stops as soon as no
slice can still reach the top set. The cursor of every slice is saved in
data/papers_harvest_state.json, so a harvest stopped by the daily limit still writes the
papers collected so far and continues from where it stopped on the next run.

Usage:  python -m app.papers harvest [--limit N] [--out DIR]

The same module serves the catalogue: load() fills the `papers` table and `papers_fts`
index from seed/papers/*.json (called by app.seed), and search / get / count / top /
for_dataset answer the site's queries.
"""

from __future__ import annotations

import argparse
import functools
import html
import json
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from urllib.parse import urlencode

import httpx

from . import nlsearch
from .config import BASE_DIR, settings

API = "https://api.openalex.org/works"
SEED_DIR = BASE_DIR / "seed" / "papers"
STATE_PATH = BASE_DIR / "data" / "papers_harvest_state.json"
USER_AGENT = "BDDataHubPapersHarvester/1.0 (+catalogue of research about Bangladesh; respects OpenAlex rate limits)"

SELECT = ",".join([
    "id", "doi", "title", "authorships", "publication_year", "publication_date", "primary_location",
    "abstract_inverted_index", "open_access", "topics", "cited_by_count", "language",
])
KEYWORD = "title_and_abstract.search:bangladesh"
RECENT_FROM_YEAR = 2024
# Year slices for the keyword pass; a slice that still times out is split in half.
YEAR_SLICES = [(None, 1999), (2000, 2009), (2010, 2014), (2015, 2018), (2019, 2021), (2022, 2023), (2024, 2026)]

PER_PAGE = 200
MIN_INTERVAL = 0.25          # seconds between requests (<= 4 requests a second)
SEARCH_COST, LIST_COST = 10, 1
N_TOP, N_RECENT = 3000, 1000
MAX_AUTHORS, MAX_TOPICS = 20, 5
# OpenAlex author strings that are not people or groups (placeholders and name fragments).
# They are dropped from author lists; real people (including single names) and named groups stay.
PLACEHOLDER_AUTHORS = re.compile(
    r"^(corresponding authors?|anonymous|the editors?|editors?|et al\.?|jr\.?|sr\.?|n/?a|unknown|none|various authors?)$",
    re.IGNORECASE)
AUTHOR_RULE = ("Placeholder author strings (e.g. 'Corresponding authors', 'Anonymous', 'The Editor', a bare "
               "'Jr.' or '*') are dropped; every other author name is kept exactly as OpenAlex gives it.")


def real_author(name: str | None) -> bool:
    name = (name or "").strip()
    return bool(name) and any(ch.isalpha() for ch in name) and not PLACEHOLDER_AUTHORS.match(name)


class BudgetExhausted(Exception):
    """OpenAlex's daily credit limit would be exceeded; progress has been saved."""


class QueryTimeout(Exception):
    """OpenAlex stopped the query as too broad (HTTP 504 query_timeout)."""


# ----------------------------------------------------------------------------- API client

@dataclass
class Client:
    mailto: str | None = None
    http: httpx.Client = field(default_factory=lambda: httpx.Client(
        timeout=90, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}))
    last_request: float = 0.0
    remaining: int | None = None     # credits left today, from the x-ratelimit-remaining header
    requests: int = 0

    def get(self, params: dict, cost: int, url: str = API) -> dict:
        if self.remaining is not None and self.remaining < cost:
            raise BudgetExhausted(f"{self.remaining} OpenAlex credits left today; this request costs {cost}")
        if self.mailto:
            params = {**params, "mailto": self.mailto}
        for attempt in range(5):
            wait = self.last_request + MIN_INTERVAL - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self.last_request = time.monotonic()
            try:
                r = self.http.get(url, params=params)
            except httpx.TransportError:
                time.sleep(2 ** attempt)
                continue
            self.requests += 1
            if "x-ratelimit-remaining" in r.headers:
                self.remaining = int(r.headers["x-ratelimit-remaining"])
            if r.status_code == 200:
                return r.json()
            if r.status_code == 504 and "query_timeout" in r.text:
                raise QueryTimeout(params.get("filter", ""))
            if r.status_code == 429:
                if self.remaining == 0 or "credit" in r.text.lower():
                    raise BudgetExhausted("OpenAlex daily credit limit reached")
                time.sleep(2 ** attempt * 2)
                continue
            if r.status_code >= 500:
                time.sleep(2 ** attempt * 2)
                continue
            r.raise_for_status()
        raise httpx.HTTPError(f"OpenAlex request kept failing: {params.get('filter')}")


def query_url(filter_: str, sort: str) -> str:
    """The query as recorded in the seed files (never includes the mailto address)."""
    return f"{API}?{urlencode({'filter': filter_, 'sort': sort, 'per-page': PER_PAGE})}"


# ----------------------------------------------------------------------------- records

def rebuild_abstract(index: dict | None) -> str | None:
    """OpenAlex stores abstracts as {word: [positions]}; put the words back in order."""
    if not index:
        return None
    words = sorted((pos, word) for word, positions in index.items() for pos in positions)
    text = " ".join(word for _, word in words).strip()
    return text or None


def _short_id(url: str | None, prefix: str) -> str | None:
    return url.removeprefix(prefix) if url else None


def topic_tree(topics: list | None) -> list[list[str | None]]:
    """[topic, subfield, field] for each of the first topics, as OpenAlex names them (None when absent)."""
    out = []
    for t in (topics or [])[:MAX_TOPICS]:
        if t.get("display_name"):
            out.append([t["display_name"], (t.get("subfield") or {}).get("display_name"),
                        (t.get("field") or {}).get("display_name")])
    return out


def _clean_url(url: str | None) -> str | None:
    """OpenAlex sometimes returns URLs with HTML entities left in ("&amp;"); undo that once."""
    return html.unescape(url) if url else url


URL_FIELDS = ("doi", "oa_url", "landing_url")


def to_paper(work: dict, harvested_at: str) -> dict:
    loc = work.get("primary_location") or {}
    source = loc.get("source") or {}
    oa = work.get("open_access") or {}
    return {
        "openalex_id": _short_id(work.get("id"), "https://openalex.org/"),
        "doi": _clean_url(_short_id(work.get("doi"), "https://doi.org/")),
        "title": work.get("title"),
        "authors": [a["author"]["display_name"] for a in (work.get("authorships") or [])
                    if real_author((a.get("author") or {}).get("display_name"))][:MAX_AUTHORS],
        "year": work.get("publication_year"),
        "publication_date": work.get("publication_date"),
        "venue": source.get("display_name"),
        "abstract": rebuild_abstract(work.get("abstract_inverted_index")),
        "is_oa": oa.get("is_oa"),
        "oa_url": _clean_url(oa.get("oa_url")),
        "landing_url": _clean_url(loc.get("landing_page_url")),
        "topics": [t["display_name"] for t in (work.get("topics") or [])[:MAX_TOPICS] if t.get("display_name")],
        "topic_tree": topic_tree(work.get("topics")),
        "cited_by_count": work.get("cited_by_count"),
        "language": work.get("language"),
        "harvested_at": harvested_at,
    }


# ----------------------------------------------------------------------------- harvest

@dataclass
class Harvest:
    """Accumulated works plus which query pages are already done (so a run can resume)."""
    harvested_at: str
    works: dict[str, dict] = field(default_factory=dict)     # openalex_id -> paper
    top_ids: set[str] = field(default_factory=set)
    recent_ids: set[str] = field(default_factory=set)
    done: dict[str, dict] = field(default_factory=dict)      # task key -> {"cursor": str|None, "finished": bool}
    queries: dict[str, list[str]] = field(default_factory=lambda: {"top": [], "recent": []})
    extra: dict = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> Harvest | None:
        if not path.exists():
            return None
        d = json.loads(path.read_text(encoding="utf-8"))
        return cls(d["harvested_at"], d["works"], set(d["top_ids"]), set(d["recent_ids"]), d["done"], d["queries"],
                   d.get("extra", {}))

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "harvested_at": self.harvested_at, "works": self.works, "top_ids": sorted(self.top_ids),
            "recent_ids": sorted(self.recent_ids), "done": self.done, "queries": self.queries, "extra": self.extra,
        }, ensure_ascii=False), encoding="utf-8")

    def add(self, work: dict, bucket: set[str]) -> dict:
        paper = to_paper(work, self.harvested_at)
        pid = paper["openalex_id"]
        if pid:
            self.works[pid] = paper
            bucket.add(pid)
        return paper

    def threshold(self, n: int) -> int:
        """Citation count of the n-th most-cited keepable work collected so far (0 until there are n).

        Only works that will survive selection count (those naming Bangladesh), or the harvest would stop early.
        """
        cites = sorted((self.works[i]["cited_by_count"] or 0 for i in self.top_ids
                        if mentions_bangladesh(self.works[i])), reverse=True)
        return cites[n - 1] if len(cites) >= n else 0


def _slice_filter(lo: int | None, hi: int | None) -> str:
    if lo is None:
        return f"publication_year:<{hi + 1}"
    return f"publication_year:{lo}-{hi}"


def _pages(client: Client, h: Harvest, key: str, filter_: str, sort: str, cost: int, bucket: set[str],
           max_pages: int, keep_going=lambda page: True, record_as: str | None = None) -> None:
    """Fetch cursor pages for one query, resuming from the cache, until a stop condition."""
    state = h.done.setdefault(key, {"cursor": "*", "finished": False, "pages": 0})
    if record_as and query_url(filter_, sort) not in h.queries[record_as]:
        h.queries[record_as].append(query_url(filter_, sort))
    while not state["finished"] and state["pages"] < max_pages and state["cursor"]:
        data = client.get({"filter": filter_, "sort": sort, "per-page": PER_PAGE, "cursor": state["cursor"],
                           "select": SELECT}, cost)
        page = [h.add(w, bucket) for w in data.get("results", [])]
        state["pages"] += 1
        if page:
            state["last_cites"] = page[-1]["cited_by_count"] or 0
        state["cursor"] = (data.get("meta") or {}).get("next_cursor")
        if not page or not state["cursor"] or not keep_going(page):
            state["finished"] = True


def mentions_bangladesh(paper: dict) -> bool:
    return "bangladesh" in f"{paper.get('title') or ''} {paper.get('abstract') or ''}".casefold()


def _merge_top(client: Client, h: Harvest, n_top: int, cache: Path | None) -> None:
    """Most-cited works across the year slices, merged by citation count (k-way merge by pages)."""
    slices = [f"{KEYWORD},{_slice_filter(lo, hi)}" for lo, hi in YEAR_SLICES]
    sort = "cited_by_count:desc"
    frontier: dict[str, int | None] = {}   # slice filter -> citations of its last fetched work (None = not started)
    for f in slices:
        state = h.done.get(f"top:{f}")
        if state and state.get("finished"):
            continue
        last = state.get("last_cites") if state else None
        frontier[f] = last
    pending = list(YEAR_SLICES)
    while frontier:
        # Unstarted slices first (their best works are unknown), then the slice with the highest unread citations.
        f = next((k for k, v in frontier.items() if v is None), None) or max(frontier, key=lambda k: frontier[k])
        if frontier[f] is not None and len(h.top_ids) >= n_top and frontier[f] < h.threshold(n_top):
            break  # nothing unread anywhere can reach the top set
        try:
            state_before = dict(h.done.get(f"top:{f}", {}))
            _pages(client, h, f"top:{f}", f, sort, SEARCH_COST, h.top_ids, state_before.get("pages", 0) + 1,
                   record_as="top")
        except QueryTimeout:
            lo, hi = next(r for r in pending if f"{KEYWORD},{_slice_filter(*r)}" == f)
            if lo is not None and hi - lo < 1:
                raise
            mid = ((lo if lo is not None else 1900) + hi) // 2
            del frontier[f]
            h.done.pop(f"top:{f}", None)
            h.queries["top"] = [q for q in h.queries["top"] if q != query_url(f, sort)]
            for part in [(lo, mid), (mid + 1, hi)]:
                pending.append(part)
                frontier[f"{KEYWORD},{_slice_filter(*part)}"] = None
            continue
        state = h.done[f"top:{f}"]
        if state["finished"]:
            del frontier[f]
        else:
            frontier[f] = state["last_cites"]
        if cache:
            h.save(cache)


def harvest(client: Client, h: Harvest, n_top: int = N_TOP, n_recent: int = N_RECENT,
            cache: Path | None = STATE_PATH, today: date | None = None) -> Harvest:
    today = today or date.today()
    per_year = -(-n_recent // (today.year - RECENT_FROM_YEAR + 1))
    try:
        _merge_top(client, h, n_top, cache)
        # Recent: the most-cited works of each year from 2024 to today (future-dated issues excluded).
        top_ids = {p["openalex_id"] for p in select(h, n_top, 0)[0]}
        for year in range(RECENT_FROM_YEAR, today.year + 1):
            f = f"{KEYWORD},from_publication_date:{year}-01-01,to_publication_date:{min(date(year, 12, 31), today).isoformat()}"
            got = lambda page, y=year: sum(1 for i in h.recent_ids - top_ids  # noqa: E731
                                           if h.works[i]["year"] == y and mentions_bangladesh(h.works[i])) < per_year
            _pages(client, h, f"recent:{year}", f, "cited_by_count:desc", SEARCH_COST, h.recent_ids,
                   -(-per_year // PER_PAGE) * 3, keep_going=got, record_as="recent")
            if cache:
                h.save(cache)
    except BudgetExhausted:
        if cache:
            h.save(cache)
        raise
    return h


def select(h: Harvest, n_top: int = N_TOP, n_recent: int = N_RECENT,
           today: date | None = None) -> tuple[list[dict], list[dict]]:
    """Final most-cited and recent sets: Bangladesh named in title/abstract, deduplicated by id and DOI."""
    today = today or date.fromisoformat(h.harvested_at)

    def dedup(ids, key, seen_doi):
        out = []
        for pid in sorted(ids, key=key):
            p = h.works[pid]
            if not mentions_bangladesh(p):
                continue
            doi = (p["doi"] or "").lower()
            if doi and doi in seen_doi:
                continue
            if doi:
                seen_doi.add(doi)
            out.append(p)
        return out

    by_cites = lambda i: (-(h.works[i]["cited_by_count"] or 0), h.works[i]["openalex_id"])  # noqa: E731
    seen: set[str] = set()
    top = dedup(h.top_ids, by_cites, seen)[:n_top]
    seen = {(p["doi"] or "").lower() for p in top if p["doi"]}
    taken = {p["openalex_id"] for p in top}
    years = list(range(RECENT_FROM_YEAR, today.year + 1))
    per_year = -(-n_recent // len(years)) if n_recent else 0
    recent = []
    for y in years:
        ids = [i for i in h.recent_ids if i not in taken and h.works[i]["year"] == y
               and (h.works[i]["publication_date"] or "") <= today.isoformat()]
        recent += dedup(ids, by_cites, seen)[:per_year]
    recent = sorted(recent, key=lambda p: (p["publication_date"] or "", p["openalex_id"]), reverse=True)[:n_recent]
    return top, recent


def _write_seed_file(path: Path, head: dict, papers: list[dict]) -> None:
    """Header fields indented, then one paper per line (compact, still diff-friendly)."""
    body = ",\n".join(json.dumps(p, ensure_ascii=False, separators=(",", ":")) for p in papers)
    text = json.dumps(head, ensure_ascii=False, indent=1)[:-2] + ',\n "papers": [\n' + body + "\n ]\n}\n"
    json.loads(text)  # never write a file that doesn't parse
    path.write_text(text, encoding="utf-8")


def enrich_topics(client: Client, files: list[Path], batch: int = 100) -> dict:
    """Add `topic_tree` to papers in existing seed files that lack it.

    One cheap list request per 100 papers (filter=openalex_id:W1|W2|..., select=id,topics). The topic
    names and order are re-read from OpenAlex as well, so topics and topic_tree always agree.
    """
    report = {"papers": 0, "updated": 0, "not_returned": 0, "requests": 0}
    for path in files:
        data = json.loads(path.read_text(encoding="utf-8"))
        todo = [p for p in data["papers"] if "topic_tree" not in p]
        report["papers"] += len(data["papers"])
        for i in range(0, len(todo), batch):
            chunk = todo[i:i + batch]
            res = client.get({"filter": "openalex_id:" + "|".join(p["openalex_id"] for p in chunk),
                              "select": "id,topics", "per-page": batch}, LIST_COST)
            report["requests"] += 1
            got = {w["id"].removeprefix("https://openalex.org/"): w.get("topics") for w in res.get("results", [])}
            for p in chunk:
                if p["openalex_id"] in got:
                    tree = topic_tree(got[p["openalex_id"]])
                    p["topic_tree"] = tree
                    p["topics"] = [t[0] for t in tree]
                    report["updated"] += 1
                else:
                    report["not_returned"] += 1
        head = {k: v for k, v in data.items() if k != "papers"}
        head.setdefault("topic_tree_note", "topic_tree = [topic, subfield, field] per topic, from the OpenAlex /works API.")
        _write_seed_file(path, head, data["papers"])
    return report


def write_seed(h: Harvest, top: list[dict], recent: list[dict], out: Path = SEED_DIR) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    files = []
    for name, selection, papers, queries in [
        ("most-cited", "Most-cited works (by cited_by_count) whose title or abstract names Bangladesh, "
                       "deduplicated by OpenAlex id and DOI.", top, h.queries["top"]),
        ("recent", f"Works published {RECENT_FROM_YEAR}-01-01 to {h.harvested_at} whose title or abstract names "
                   "Bangladesh: the most-cited of each year in equal shares, newest first, excluding works "
                   "already in most-cited.json.", recent, h.queries["recent"]),
    ]:
        path = out / f"{name}.json"
        head = {"source": "OpenAlex (https://openalex.org), /works API", "license": "OpenAlex metadata is CC0",
                "harvested_at": h.harvested_at, "queries": queries, "selection": selection,
                "authors_rule": AUTHOR_RULE, "count": len(papers)}
        _write_seed_file(path, head, papers)
        files.append(path)
    return files

# ----------------------------------------------------------------------------- research by category
# Papers are placed in the site's top-level categories by a FIXED table over OpenAlex's own topic
# hierarchy (topic -> subfield -> field); no paper is classified by hand and no text model is used.
# For each of a paper's topics, in OpenAlex's order (primary topic first), the categories are:
#   1. TOPIC_RULES whose pattern matches the topic's name (cross-cutting themes such as poverty or trade),
#   2. SUBFIELD_MAP for the topic's subfield, or else FIELD_MAP for its field.
# A paper can be in several categories; "other" is used only when nothing matches.
# Bump MAPPING_VERSION whenever the tables change, so the next boot rebuilds paper_categories.
MAPPING_VERSION = "2026-10-01.5"

FIELD_MAP = {
    "Medicine": ["health"], "Nursing": ["health"], "Health Professions": ["health"], "Dentistry": ["health"],
    "Immunology and Microbiology": ["health"], "Pharmacology, Toxicology and Pharmaceutics": ["health"],
    "Neuroscience": ["health"], "Psychology": ["health"], "Biochemistry, Genetics and Molecular Biology": ["health"],
    "Veterinary": ["agriculture"],
    "Environmental Science": ["environment"],
    "Agricultural and Biological Sciences": ["agriculture"],
    "Earth and Planetary Sciences": ["environment"],
    "Economics, Econometrics and Finance": ["economy-finance"],
    "Business, Management and Accounting": ["industry-business"],
    "Energy": ["energy"],
    "Engineering": ["science-technology"],
    "Computer Science": ["science-technology"], "Decision Sciences": ["science-technology"],
    "Mathematics": ["science-technology"], "Physics and Astronomy": ["science-technology"],
    "Chemistry": ["science-technology"], "Materials Science": ["science-technology"],
    "Chemical Engineering": ["science-technology"],
    "Social Sciences": ["population-demographics"],
    "Arts and Humanities": ["other"],
}

SUBFIELD_MAP = {
    # Social Sciences
    "Sociology and Political Science": ["population-demographics"],
    "Political Science and International Relations": ["government-public-administration"],
    "Public Administration": ["government-public-administration"],
    "Law": ["government-public-administration"],
    "Safety Research": ["social-protection"],   # OpenAlex files child-welfare and poverty topics here
    "Education": ["education"],
    "Demography": ["population-demographics"],
    "Gender Studies": ["population-demographics"],
    "Anthropology": ["population-demographics"],
    "Transportation": ["transport"],
    "Urban Studies": ["infrastructure"],
    "Geography, Planning and Development": ["geography-gis"],
    "Development": ["social-protection", "economy-finance"],
    "Health": ["health"],
    "Library and Information Sciences": ["science-technology"],
    "Communication": ["science-technology"],
    "Linguistics and Language": ["education"],
    "Archeology": ["other"],
    # Earth and environment
    "Atmospheric Science": ["climate-weather"],
    "Global and Planetary Change": ["climate-weather", "environment"],
    "Oceanography": ["environment", "climate-weather"],
    "Earth-Surface Processes": ["geography-gis", "environment"],
    "Geology": ["geography-gis"],
    "Geophysics": ["geography-gis"],
    "Computers in Earth Sciences": ["geography-gis"],
    "Geochemistry and Petrology": ["environment"],
    "Water Science and Technology": ["environment"],
    "Waste Management and Disposal": ["environment", "infrastructure"],
    "Management, Monitoring, Policy and Law": ["environment"],
    "Health, Toxicology and Mutagenesis": ["environment", "health"],
    "Ecology, Evolution, Behavior and Systematics": ["environment"],
    "Nature and Landscape Conservation": ["environment"],
    "Forestry": ["environment", "agriculture"],
    # Agriculture and food
    "Food Science": ["agriculture", "health"],
    "Nutrition and Dietetics": ["health", "agriculture"],
    "Aquatic Science": ["agriculture", "environment"],
    # Business and economics
    "Tourism, Leisure and Hospitality Management": ["tourism"],
    "Organizational Behavior and Human Resource Management": ["industry-business"],
    "Business and International Management": ["industry-business", "trade-commerce"],
    "Management Information Systems": ["science-technology", "industry-business"],
    "Management of Technology and Innovation": ["science-technology", "industry-business"],
    "Industrial Relations": ["labour-employment"],
    "Finance": ["economy-finance"],
    "Information Systems and Management": ["science-technology"],
    "Management Science and Operations Research": ["industry-business"],
    # Engineering
    "Building and Construction": ["infrastructure"],
    "Civil and Structural Engineering": ["infrastructure"],
    "Ocean Engineering": ["environment"],
    "Electrical and Electronic Engineering": ["energy", "science-technology"],
    "Industrial and Manufacturing Engineering": ["industry-business"],
    "Automotive Engineering": ["energy", "science-technology"],
    "Aerospace Engineering": ["science-technology"],
    "Safety, Risk, Reliability and Quality": ["infrastructure"],
    "Architecture": ["infrastructure"],
}

# Word patterns on OpenAlex topic NAMES (not on paper text), for themes that cut across subfields.
TOPIC_RULES = [(re.compile(rx, re.IGNORECASE), cats) for rx, cats in [
    (r"\b(poverty|microfinance|social protection|welfare|humanitarian|refugee\w*|disabilit\w*|cash transfer|"
     r"social security|inequalit\w*|vulnerab\w*|livelihood\w*)\b", ["social-protection"]),
    (r"\b(labou?r|employment|wages?|workers?|workforce|occupational|garment)\b", ["labour-employment"]),
    (r"\b(trade|exports?|imports?|tariffs?|value chains?|supply chains?|globali[sz]ation)\b", ["trade-commerce"]),
    (r"\b(remittances?|banking|banks?|monetary|inflation|exchange rates?|fiscal|tax\w*|financial|economic growth)\b",
     ["economy-finance"]),
    (r"(^tourism(?!, volunteerism)|diverse aspects of tourism|\b(?!and\b)\w+ tourism|tourism (research|management|marketing|"
     r"education|destinations?|development)|\bhospitality\b|\becotourism\b)", ["tourism"]),
    (r"\b(crime|violence|(?<!environmental )justice|corruption|prisons?|police|policing|"
     r"traffic (accident|safety|injur\w*))\b",
     ["crime-justice"]),
    (r"^(?!.*\bcorporate\b).*\b(elections?|governance|politic\w*|public administration|democra\w*|local government|"
     r"public policy)\b",
     ["government-public-administration"]),
    (r"\b(climate|rainfall|monsoon|cyclones?|floods?|flooding|weather|droughts?|sea[- ]level|tropical storms?|"
     r"disasters?)\b", ["climate-weather"]),
    (r"\b(population|demograph\w*|fertility|migration|migrants?|census|family planning)\b", ["population-demographics"]),
    (r"\b(education\w*|schools?|students?|teachers?|teaching|literacy|universit\w*)\b", ["education"]),
    (r"(\b(?<!sediment )(?<!environment, and )(?<!drug )transport(ation)?\b|\b(?<!internet )traffic\b|"
     r"\b(roads?|railways?|shipping|seaports?|aviation|vehicles?)\b)",
     ["transport"]),
    (r"\b(urban\w*|water supply|sanitation|infrastructure|housing|construction|bridges?)\b", ["infrastructure"]),
    (r"\b(remote sensing|gis|geospatial|land use|land cover|satellite|spatial)\b", ["geography-gis"]),
    (r"\b(energy|electricity|electric power|power systems?|solar|biomass|fuels?|natural gas|petroleum)\b", ["energy"]),
    (r"\b(agricultur\w*|crops?|rice|wheat|fisher\w*|aquaculture|livestock|food security|irrigation|farm\w*)\b",
     ["agriculture"]),
    (r"\b(internet|ict|mobile phones?|telecommunication\w*|digital|e-government)\b", ["science-technology"]),
]]


@functools.lru_cache(maxsize=None)
def _topic_categories(topic: str | None, subfield: str | None, field_: str | None) -> tuple[str, ...]:
    """Categories for one topic. Cached: papers share topics, and this is a pure function of the triple."""
    cats = [c for rx, cs in TOPIC_RULES if topic and rx.search(topic) for c in cs]
    base = SUBFIELD_MAP.get(subfield) or FIELD_MAP.get(field_) or []
    # OpenAlex files tourism topics (e.g. "Cruise Tourism Development") under Transportation: tourism wins.
    if "tourism" in cats:
        base = [c for c in base if c != "transport"]
    return tuple(cats + base)


def categorize(tree: list | None) -> list[str]:
    """Category slugs for a paper's topic_tree, most relevant first (see the tables above)."""
    out: list[str] = []
    for topic, subfield, field_ in tree or []:
        for c in _topic_categories(topic, subfield, field_):
            if c != "other" and c not in out:
                out.append(c)
    return out or ["other"]


# ----------------------------------------------------------------------------- catalogue (database side)

_COLUMNS = ["openalex_id", "doi", "title", "authors", "year", "publication_date", "venue", "abstract", "is_oa",
            "oa_url", "landing_url", "topics", "cited_by_count", "language", "harvested_at"]
SORTS = {
    "relevance": None,  # bm25 rank when there is a query, otherwise the same as "citations"
    "citations": "p.cited_by_count DESC, p.year DESC, p.id",
    "year": "p.year DESC, p.publication_date DESC, p.cited_by_count DESC, p.id",
}


def load(conn: sqlite3.Connection, source_dir: Path = SEED_DIR) -> dict:
    """Replace the papers table with the contents of seed/papers/*.json (deduplicated by id and DOI).

    Records without a title are skipped rather than filled in; the report counts them.
    """
    report = {"files": 0, "loaded": 0, "duplicates": 0, "skipped_no_title": 0}
    conn.execute("DELETE FROM paper_categories")
    conn.execute("DELETE FROM paper_topics")
    conn.execute("DELETE FROM papers")
    conn.execute("DELETE FROM papers_fts")
    seen_ids, seen_dois = set(), set()
    # Rows are collected and inserted in three batches, which keeps the boot-time load well under a second.
    paper_rows, topic_rows, fts_rows, cat_rows = [], [], [], []
    cat_ids = {r[0]: r[1] for r in conn.execute("SELECT slug, id FROM categories WHERE parent_id IS NULL")}
    for path in sorted(source_dir.glob("*.json")):
        report["files"] += 1
        for p in json.loads(path.read_text(encoding="utf-8")).get("papers", []):
            p = {**p, **{k: _clean_url(p.get(k)) for k in URL_FIELDS}}
            doi = (p.get("doi") or "").lower()
            if p.get("openalex_id") in seen_ids or (doi and doi in seen_dois):
                report["duplicates"] += 1
                continue
            if not p.get("title"):
                report["skipped_no_title"] += 1
                continue
            seen_ids.add(p["openalex_id"])
            if doi:
                seen_dois.add(doi)
            authors = [a for a in (p.get("authors") or []) if real_author(a)]
            topics = p.get("topics") or []
            row = {**{k: p.get(k) for k in _COLUMNS}, "authors": json.dumps(authors, ensure_ascii=False),
                   "topics": json.dumps(topics, ensure_ascii=False),
                   "is_oa": None if p.get("is_oa") is None else int(bool(p["is_oa"])),
                   "cited_by_count": p.get("cited_by_count") or 0}
            pid = len(paper_rows) + 1
            paper_rows.append([pid] + [row[k] for k in _COLUMNS])
            topic_rows += [(pid, t) for t in dict.fromkeys(topics)]
            cat_rows += [(pid, cat_ids[c], rank) for rank, c in enumerate(
                c for c in categorize(p.get("topic_tree")) if c in cat_ids)]
            fts_rows.append((pid, p["title"], p.get("abstract") or "", " ".join(authors), p.get("venue") or "",
                             " ".join(topics)))
    conn.executemany(f"INSERT INTO papers (id, {', '.join(_COLUMNS)}) VALUES (?, {', '.join('?' * len(_COLUMNS))})",
                     paper_rows)
    conn.executemany("INSERT INTO paper_topics (paper_id, topic) VALUES (?, ?)", topic_rows)
    conn.executemany("INSERT INTO paper_categories (paper_id, category_id, rank) VALUES (?, ?, ?)", cat_rows)
    conn.executemany("INSERT INTO papers_fts (rowid, title, abstract, authors, venue, topics) VALUES (?,?,?,?,?,?)",
                     fts_rows)
    report["loaded"] = len(paper_rows)
    return report


def _categories_for(conn: sqlite3.Connection, ids: list[int]) -> dict[int, list[tuple[str, str]]]:
    """{paper id: [(slug, name), ...]} ordered by derivation rank (primary topic first)."""
    out: dict[int, list[tuple[str, str]]] = {}
    for i in range(0, len(ids), 900):  # SQLite parameter limit
        chunk = ids[i:i + 900]
        for pid, slug, name in conn.execute(
                f"SELECT pc.paper_id, c.slug, c.name FROM paper_categories pc JOIN categories c ON c.id = pc.category_id "
                f"WHERE pc.paper_id IN ({','.join('?' * len(chunk))}) ORDER BY pc.paper_id, pc.rank", chunk):
            out.setdefault(pid, []).append((slug, name))
    return out


def _papers(conn: sqlite3.Connection, rows: list[sqlite3.Row]) -> list[dict]:
    cats = _categories_for(conn, [r["id"] for r in rows])
    return [_paper(r, cats) for r in rows]


def _paper(row: sqlite3.Row, cats: dict[int, list[tuple[str, str]]] | None = None) -> dict:
    d = {k: row[k] for k in _COLUMNS}
    d["categories"] = (cats or {}).get(row["id"], [])
    d["authors"] = json.loads(d["authors"] or "[]")
    d["topics"] = json.loads(d["topics"] or "[]")
    d["is_oa"] = None if d["is_oa"] is None else bool(d["is_oa"])
    return d


def _terms(q: str) -> list[str]:
    words = re.findall(r"[\wঀ-৿]+", (q or "").lower())
    return [w for w in words if w not in nlsearch.STOPWORDS] or words


def _match(conn, q: str) -> dict[int, float]:
    """FTS scores (lower is better) for a free-text query: all terms first, any term as a fallback."""
    terms = _terms(q)
    for mode in ("AND", "OR"):
        expr = nlsearch.build_fts(terms, mode=mode)
        if not expr:
            return {}
        try:
            rows = conn.execute("SELECT rowid, bm25(papers_fts, 10.0, 2.0, 3.0, 2.0, 3.0) FROM papers_fts "
                                "WHERE papers_fts MATCH ?", (expr,)).fetchall()
        except sqlite3.OperationalError:
            rows = []
        if rows or len(terms) < 2:
            return {r[0]: r[1] for r in rows}
    return {}


def search(conn: sqlite3.Connection, q: str | None = None, year_from: int | None = None, year_to: int | None = None,
           topic: str | None = None, oa: bool | None = None, venue: str | None = None, sort: str = "relevance",
           page: int = 1, per_page: int = 20, category: str | None = None) -> dict:
    """Filtered, paged paper search.

    Returns {"items", "total", "page", "pages", "facets"}. Facets describe the matching set; the topic,
    venue and category facets ignore their own filter, and "years" ignores the year filter, so each can
    be widened. `category` is a top-level category slug; an unknown slug simply matches nothing.
    """
    scores = _match(conn, q) if q and q.strip() else None
    per_page = max(1, min(int(per_page or 20), 100))

    def where(skip: str | None = None) -> tuple[str, list]:
        clauses, args = [], []
        if scores is not None:
            clauses.append(f"p.id IN ({','.join(str(i) for i in scores) or 'NULL'})")
        if year_from is not None and skip != "years":
            clauses.append("p.year >= ?")
            args.append(int(year_from))
        if year_to is not None and skip != "years":
            clauses.append("p.year <= ?")
            args.append(int(year_to))
        if topic and skip != "topic":
            clauses.append("p.id IN (SELECT paper_id FROM paper_topics WHERE topic = ?)")
            args.append(topic)
        if oa is not None:
            clauses.append("p.is_oa = ?")
            args.append(int(bool(oa)))
        if venue and skip != "venue":
            clauses.append("p.venue = ?")
            args.append(venue)
        if category and skip != "category":
            clauses.append("p.id IN (SELECT pc.paper_id FROM paper_categories pc JOIN categories c "
                           "ON c.id = pc.category_id WHERE c.slug = ?)")
            args.append(category)
        return " AND ".join(clauses), args

    def sql_where(clause: str, extra: str = "") -> str:
        parts = [x for x in (clause, extra) if x]
        return ("WHERE " + " AND ".join(parts)) if parts else ""

    w, a = where()
    total = conn.execute(f"SELECT COUNT(*) FROM papers p {sql_where(w)}", a).fetchone()[0]
    pages = max(1, -(-total // per_page))
    page = max(1, min(int(page or 1), pages))
    if sort == "relevance" and scores:
        rows = conn.execute(f"SELECT p.* FROM papers p {sql_where(w)}", a).fetchall()
        rows = sorted(rows, key=lambda r: (scores.get(r["id"], 0.0), -(r["cited_by_count"] or 0), r["id"]))
        rows = rows[(page - 1) * per_page: page * per_page]
    else:
        order = SORTS.get(sort) or SORTS["citations"]
        rows = conn.execute(f"SELECT p.* FROM papers p {sql_where(w)} ORDER BY {order} LIMIT ? OFFSET ?",
                            [*a, per_page, (page - 1) * per_page]).fetchall()

    wt, at = where("topic")
    topics = conn.execute(f"SELECT t.topic, COUNT(*) n FROM paper_topics t JOIN papers p ON p.id = t.paper_id "
                          f"{sql_where(wt)} GROUP BY t.topic ORDER BY n DESC, t.topic LIMIT 20", at).fetchall()
    wv, av = where("venue")
    venues = conn.execute(f"SELECT p.venue, COUNT(*) n FROM papers p {sql_where(wv, 'p.venue IS NOT NULL')} "
                          f"GROUP BY p.venue ORDER BY n DESC, p.venue LIMIT 20", av).fetchall()
    wy, ay = where("years")
    ymin, ymax = conn.execute(f"SELECT MIN(p.year), MAX(p.year) FROM papers p {sql_where(wy)}", ay).fetchone()
    wc, ac = where("category")
    cats = conn.execute(f"SELECT c.slug, c.name, COUNT(*) n FROM paper_categories pc "
                        f"JOIN categories c ON c.id = pc.category_id JOIN papers p ON p.id = pc.paper_id "
                        f"{sql_where(wc)} GROUP BY c.id ORDER BY n DESC, c.sort_order", ac).fetchall()
    return {
        "items": _papers(conn, rows),
        "total": total,
        "page": page,
        "pages": pages,
        "facets": {"topic": [(r[0], r[1]) for r in topics], "venue": [(r[0], r[1]) for r in venues],
                   "years": (ymin, ymax), "category": [(r[0], r[1], r[2]) for r in cats]},
    }


def get(conn: sqlite3.Connection, openalex_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM papers WHERE openalex_id = ?", (openalex_id,)).fetchone()
    return _papers(conn, [row])[0] if row else None


def count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0]


def top(conn: sqlite3.Connection, n: int = 10) -> list[dict]:
    """The n most-cited papers."""
    rows = conn.execute(f"SELECT p.* FROM papers p ORDER BY {SORTS['citations']} LIMIT ?", (n,)).fetchall()
    return _papers(conn, rows)


def for_dataset(conn: sqlite3.Connection, ds: dict, n: int = 5) -> list[dict]:
    """Papers that look related to a dataset (title words, preferring ones that also name the publisher).

    Only a suggestion: it matches words, so it can miss or over-include, and it never claims that a
    paper used the dataset.
    """
    org = ds.get("organization") or {}
    words = [w for w in _terms(ds.get("title", "")) if len(w) >= 4 and not w.isdigit()][:6]
    if not words:
        return []
    expr = nlsearch.build_fts(words, mode="OR")
    publisher = nlsearch.build_fts(_terms(org.get("name", "")), mode="AND") if org.get("name") else ""
    rows = []
    for e in ([f"({expr}) AND ({publisher})"] if publisher else []) + [expr]:
        try:
            rows = conn.execute("SELECT rowid, bm25(papers_fts, 10.0, 2.0, 3.0, 2.0, 3.0) s FROM papers_fts "
                                "WHERE papers_fts MATCH ? ORDER BY s LIMIT ?", (e, n * 2)).fetchall()
        except sqlite3.OperationalError:
            rows = []
        if len(rows) >= n:
            break
    ids = [r[0] for r in rows]
    if not ids:
        return []
    found = {r["id"]: r for r in conn.execute(f"SELECT * FROM papers WHERE id IN ({','.join('?' * len(ids))})", ids)}
    keep, titles = [], set()
    for i in ids:
        if i in found and found[i]["title"].casefold() not in titles:  # same paper listed twice in OpenAlex
            titles.add(found[i]["title"].casefold())
            keep.append(found[i])
    return _papers(conn, keep[:n])


def category_counts(conn: sqlite3.Connection) -> list[tuple[str, str, int]]:
    """(slug, name, papers) for every top-level category in site order, zeros included."""
    return [(r[0], r[1], r[2]) for r in conn.execute(
        "SELECT c.slug, c.name, COUNT(pc.paper_id) FROM categories c "
        "LEFT JOIN paper_categories pc ON pc.category_id = c.id "
        "WHERE c.parent_id IS NULL GROUP BY c.id ORDER BY c.sort_order, c.name")]


IN_CATEGORY_ORDERS = {"citations": SORTS["citations"], "newest": "p.publication_date DESC, p.year DESC, p.id"}


def in_category(conn: sqlite3.Connection, slug: str, n: int = 10, order: str = "citations") -> list[dict]:
    """The first n papers of a category, most-cited ("citations") or most recent ("newest") first."""
    rows = conn.execute(
        f"SELECT p.* FROM papers p JOIN paper_categories pc ON pc.paper_id = p.id "
        f"JOIN categories c ON c.id = pc.category_id WHERE c.slug = ? "
        f"ORDER BY {IN_CATEGORY_ORDERS.get(order, IN_CATEGORY_ORDERS['citations'])} LIMIT ?", (slug, n)).fetchall()
    return _papers(conn, rows)


def for_category(conn: sqlite3.Connection, slug: str, n: int = 5) -> list[dict]:
    """Most-cited papers of a category (used on the dataset category pages)."""
    return in_category(conn, slug, n, "citations")



# ----------------------------------------------------------------------------- filling thin categories

TOPICS_API = "https://api.openalex.org/topics"
FILL_FILE = "by-category.json"
FILL_TARGET = 100


def seed_category_counts(folder: Path = SEED_DIR, exclude: str | None = FILL_FILE) -> tuple[dict, set, set]:
    """Category counts, OpenAlex ids and DOIs of the papers already in the seed files."""
    counts: dict[str, int] = {}
    ids, dois = set(), set()
    for path in sorted(folder.glob("*.json")):
        if path.name == exclude:
            continue
        for p in json.loads(path.read_text(encoding="utf-8")).get("papers", []):
            ids.add(p["openalex_id"])
            if p.get("doi"):
                dois.add(p["doi"].lower())
            for c in categorize(p.get("topic_tree")):
                counts[c] = counts.get(c, 0) + 1
    return counts, ids, dois


def topic_index(client: Client, h: Harvest, cache: Path | None) -> list[list[str]]:
    """Every OpenAlex topic as [id, name, subfield, field] (about 25 cheap list pages, cached in the state file)."""
    if h.extra.get("topics_complete"):
        return h.extra["topics"]
    topics = h.extra.setdefault("topics", [])
    cursor = h.extra.get("topics_cursor", "*")
    while cursor:
        data = client.get({"per-page": PER_PAGE, "cursor": cursor, "select": "id,display_name,subfield,field"},
                          LIST_COST, url=TOPICS_API)
        for t in data.get("results", []):
            topics.append([t["id"].rsplit("/", 1)[-1], t.get("display_name"), (t.get("subfield") or {}).get("display_name"),
                           (t.get("field") or {}).get("display_name")])
        cursor = (data.get("meta") or {}).get("next_cursor") if data.get("results") else None
        h.extra["topics_cursor"] = cursor
        if cache:
            h.save(cache)
    h.extra["topics_complete"] = True
    return topics


def fill_categories(client: Client, h: Harvest, target: int = FILL_TARGET, cache: Path | None = STATE_PATH,
                    folder: Path = SEED_DIR, max_pages: int = 5) -> dict:
    """Add most-cited Bangladesh papers to categories that have fewer than `target`.

    The topics searched are exactly those categorize() puts in the category, so a new paper lands
    there for the same reason an existing one does. Kept papers must name Bangladesh in the title or
    abstract, map to the category through their own topics, and not be in any other seed file.
    """
    counts, ids, dois = seed_category_counts(folder)
    report = {"before": dict(counts), "added": {}, "ceiling": {}}
    index = topic_index(client, h, cache)
    every = {c for v in FIELD_MAP.values() for c in v} | {c for v in SUBFIELD_MAP.values() for c in v} \
        | {c for _, v in TOPIC_RULES for c in v}
    thin = sorted(c for c in every if c != "other" and counts.get(c, 0) < target)
    for slug in thin:
        topic_ids = [t[0] for t in index if slug in categorize([t[1:]])]
        kept = h.extra.setdefault("fill", {}).setdefault(slug, [])
        exhausted = True
        for i in range(0, len(topic_ids), 100):
            chunk = topic_ids[i:i + 100]
            f = f"{KEYWORD},topics.id:{'|'.join(chunk)}"
            key = f"fill:{slug}:{i}"

            bucket: set[str] = set()
            _pages(client, h, key, f, "cited_by_count:desc", SEARCH_COST, bucket, max_pages,
                   keep_going=lambda page, slug=slug: (_keep_fill(h, slug, bucket, ids, dois) or True) and
                   counts.get(slug, 0) + len(h.extra["fill"][slug]) < target, record_as="fill")
            _keep_fill(h, slug, bucket, ids, dois)
            state = h.done[key]
            if counts.get(slug, 0) + len(kept) >= target:
                exhausted = False
                break
            if not state["finished"] and state["cursor"]:
                exhausted = False      # stopped by max_pages, not by running out of works
            if cache:
                h.save(cache)
        report["added"][slug] = len(kept)
        if exhausted and counts.get(slug, 0) + len(kept) < target:
            report["ceiling"][slug] = counts.get(slug, 0) + len(kept)
    if cache:
        h.save(cache)
    return report


def _keep_fill(h: Harvest, slug: str, bucket: set[str], ids: set, dois: set) -> None:
    kept = h.extra["fill"][slug]
    taken = {x for v in h.extra["fill"].values() for x in v}
    for pid in sorted(bucket, key=lambda i: (-(h.works[i]["cited_by_count"] or 0), i)):
        p = h.works[pid]
        doi = (p.get("doi") or "").lower()
        if (pid in ids or pid in taken or (doi and doi in dois) or not mentions_bangladesh(p)
                or slug not in categorize(p.get("topic_tree"))):
            continue
        kept.append(pid)
        taken.add(pid)
        if doi:
            dois.add(doi)


def write_fill(h: Harvest, target: int, report: dict, folder: Path = SEED_DIR) -> Path:
    papers_out, seen = [], set()
    for slug, pids in h.extra.get("fill", {}).items():
        for pid in pids:
            if pid not in seen:
                seen.add(pid)
                papers_out.append(h.works[pid])
    papers_out.sort(key=lambda p: (-(p["cited_by_count"] or 0), p["openalex_id"]))
    head = {"source": "OpenAlex (https://openalex.org), /works API", "license": "OpenAlex metadata is CC0",
            "harvested_at": h.harvested_at, "queries": h.queries.get("fill", []),
            "selection": (f"Fills research categories that had fewer than {target} papers. For each, the OpenAlex topics "
                          "that app.papers.categorize() maps to the category are searched together with "
                          "title_and_abstract.search:bangladesh, most-cited first. Kept: works that name Bangladesh in "
                          "the title or abstract, map to that category through their own topics, and are not in the "
                          "other seed files."),
            "categories_filled": {k: len(v) for k, v in h.extra.get("fill", {}).items()},
            "ceilings": report.get("ceiling", {}),
            "mapping_version": MAPPING_VERSION, "authors_rule": AUTHOR_RULE,
            "topic_tree_note": "topic_tree = [topic, subfield, field] per topic, from the OpenAlex /works API.",
            "count": len(papers_out)}
    path = folder / FILL_FILE
    _write_seed_file(path, head, papers_out)
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description="Harvest research papers about Bangladesh from OpenAlex.")
    sub = parser.add_subparsers(dest="command", required=True)
    hp = sub.add_parser("harvest", help="Fetch works from OpenAlex and write seed/papers/*.json")
    hp.add_argument("--limit", type=int, default=None,
                    help=f"Size of the most-cited set (default {N_TOP}); the recent set is a third of it")
    hp.add_argument("--out", type=Path, default=SEED_DIR, help="Output folder (default seed/papers)")
    hp.add_argument("--fresh", action="store_true", help="Ignore a cached partial harvest and start over")
    fp = sub.add_parser("fill-categories", help=f"Add papers to categories with fewer than --target (default {FILL_TARGET})")
    fp.add_argument("--target", type=int, default=FILL_TARGET)
    fp.add_argument("--dir", type=Path, default=SEED_DIR, help="Seed folder (default seed/papers)")
    ep = sub.add_parser("enrich-topics", help="Add OpenAlex subfield/field to papers in seed/papers/*.json")
    ep.add_argument("--dir", type=Path, default=SEED_DIR, help="Seed folder (default seed/papers)")
    args = parser.parse_args()

    if args.command == "fill-categories":
        cache = STATE_PATH if args.dir == SEED_DIR else args.dir / ".fill_state.json"
        h = Harvest.load(cache) or Harvest(harvested_at=date.today().isoformat())
        h.queries.setdefault("fill", [])
        client = Client(mailto=settings.openalex_mailto)
        complete = True
        try:
            report = fill_categories(client, h, args.target, cache, args.dir)
        except BudgetExhausted as exc:
            complete = False
            report = {"ceiling": {}}
            print(f"Stopped early: {exc}.")
        path = write_fill(h, args.target, report, args.dir)
        if complete:
            cache.unlink(missing_ok=True)
        print({k: v for k, v in report.items()})
        print(f"{path}: {json.loads(path.read_text(encoding='utf-8'))['count']} papers, {path.stat().st_size / 1e6:.2f} MB; "
              f"{client.requests} requests, {client.remaining} credits left today.")
        if not complete:
            print(f"Incomplete; state kept in {cache}. Run the same command after the limit resets.")
        return

    if args.command == "enrich-topics":
        client = Client(mailto=settings.openalex_mailto)
        try:
            print(enrich_topics(client, sorted(args.dir.glob("*.json"))))
        except BudgetExhausted as exc:
            print(f"Stopped early: {exc}. Run it again after the limit resets; finished files are saved.")
            raise SystemExit(2)
        print(f"{client.remaining} credits left today.")
        return

    n_top = args.limit or N_TOP
    n_recent = max(1, n_top // 3) if args.limit else N_RECENT
    # Test runs (--limit or --out elsewhere) keep their cache next to their output, not in data/.
    cache = STATE_PATH if (args.limit is None and args.out == SEED_DIR) else args.out / ".harvest_state.json"
    h = None if args.fresh else Harvest.load(cache)
    if h:
        print(f"Resuming the harvest started {h.harvested_at} ({len(h.works)} works cached).")
    h = h or Harvest(harvested_at=date.today().isoformat())
    client = Client(mailto=settings.openalex_mailto)
    complete = True
    try:
        harvest(client, h, n_top, n_recent, cache)
    except BudgetExhausted as exc:
        complete = False
        print(f"Stopped early: {exc}.")
    # Write what has been collected either way; an incomplete harvest keeps its state file so the
    # next run continues from the saved cursors.
    top, recent = select(h, n_top, n_recent)
    files = write_seed(h, top, recent, args.out)
    if complete:
        cache.unlink(missing_ok=True)
    for f in files:
        print(f"{f}: {json.loads(f.read_text(encoding='utf-8'))['count']} papers, {f.stat().st_size / 1e6:.1f} MB")
    print(f"{client.requests} API requests this run; {client.remaining} credits left today.")
    if not complete:
        print(f"Incomplete: {len(top)}/{n_top} most-cited, {len(recent)}/{n_recent} recent. State kept in {cache}; "
              f"run the same command after the limit resets to continue.")


if __name__ == "__main__":
    main()
