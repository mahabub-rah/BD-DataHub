"""Harvest dataset metadata from official APIs: CKAN portals (the Humanitarian Data Exchange)
and the World Bank Indicators API.

Harvested records come straight from the publisher's API, but they have not been reviewed
by a person. They are therefore imported as **unpublished, unverified drafts** that stay
hidden from the public site. Pass --publish to make them visible.

For the World Bank, each record is one source database (Gender Statistics, IDS, ...) and is
kept only if a live API request returns Bangladesh values. --export-seed also writes those
records as a seed file (format: seed/SEED_SPEC.md) with that request as evidence.

Usage:  python -m app.harvest hdx [--limit 50] [--publish] [--update]
        python -m app.harvest worldbank [--limit 10] [--export-seed seed/sources/worldbank.json]
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import time
from collections import Counter
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import httpx
from pydantic import ValidationError

from . import db, repository as repo
from .config import settings
from .models import DatasetIn, OrganizationIn


@dataclass(frozen=True)
class CkanSource:
    key: str
    name: str
    api: str               # base URL of the CKAN action API
    dataset_url: str       # HTML page pattern, {name} is the CKAN package name
    query: dict
    slug_prefix: str


SOURCES = {
    "hdx": CkanSource(
        key="hdx",
        name="Humanitarian Data Exchange",
        api="https://data.humdata.org/api/3/action",
        dataset_url="https://data.humdata.org/dataset/{name}",
        query={"fq": "groups:bgd"},
        slug_prefix="hdx",
    ),
}

FORMAT_MAP = {
    "csv": "CSV", "xlsx": "XLSX", "xls": "XLS", "json": "JSON", "geojson": "GeoJSON", "xml": "XML",
    "pdf": "PDF", "shp": "SHP", "zipped shapefile": "SHP", "shapefile": "SHP", "zip": "ZIP",
    "web app": "Dashboard", "html": "HTML", "docx": "DOCX", "doc": "DOCX",
}

# HDX data_update_frequency is expressed in days.
FREQUENCY_MAP = {"0": "Real-time", "1": "Daily", "7": "Weekly", "14": "Irregular", "30": "Monthly",
                 "90": "Quarterly", "180": "Half-yearly", "365": "Annual", "-1": "One-time", "-2": "Irregular"}

# Keyword → category, checked against tags and title.
CATEGORY_KEYWORDS = [
    ("Population & Demographics", ["population", "census", "demograph", "baseline"]),
    ("Health", ["health", "disease", "cholera", "covid", "dengue", "nutrition", "hospital", "mortality"]),
    ("Education", ["education", "school"]),
    ("Agriculture", ["agricultur", "crop", "food production", "livestock", "fisher"]),
    ("Climate & Weather", ["climate", "rainfall", "weather", "flood", "cyclone", "hazard", "disaster", "drought"]),
    ("Environment", ["environment", "river", "water", "forest"]),
    ("Economy & Finance", ["economy", "price", "market", "cash", "financ", "inflation"]),
    ("Geography & GIS", ["boundar", "administrative division", "gazetteer", "geodata", "openstreetmap", "facilities", "gis"]),
    ("Transport", ["road", "transport", "port", "airport"]),
    ("Social Protection", ["refugee", "rohingya", "displace", "humanitarian", "3w", "idp", "poverty", "protection"]),
    ("Infrastructure", ["infrastructure", "shelter", "sanitation", "wash"]),
    ("Energy", ["energy", "electric", "power"]),
    ("Crime & Justice", ["conflict", "violence", "security incident", "crime"]),
]


def _years(pkg: dict) -> tuple[int | None, int | None]:
    m = re.findall(r"(\d{4})-\d{2}-\d{2}", pkg.get("dataset_date") or "")
    if not m:
        return None, None
    ys = sorted(int(y) for y in m)
    end = ys[-1] if "*" not in (pkg.get("dataset_date") or "") else None
    return ys[0], end


def _categories(pkg: dict) -> list[str]:
    text = " ".join([pkg.get("title", "")] + [t.get("name", "") for t in pkg.get("tags", [])]).lower()
    cats = [name for name, words in CATEGORY_KEYWORDS if any(w in text for w in words)]
    return cats[:3] or ["Other"]


def to_dataset(src: CkanSource, pkg: dict, org_slug: str, publish: bool) -> DatasetIn:
    formats = []
    for res in pkg.get("resources", []):
        f = FORMAT_MAP.get((res.get("format") or "").strip().lower())
        if f and f not in formats:
            formats.append(f)
    start, end = _years(pkg)
    package_url = f"{src.api}/package_show?id={pkg['name']}"
    first_file = next((r.get("url") for r in pkg.get("resources", []) if r.get("url", "").startswith("http")), None)
    return DatasetIn(
        slug=f"{src.slug_prefix}-{pkg['name']}"[:80],
        title=pkg["title"][:300],
        organization=org_slug,
        description=re.sub(r"\s+", " ", pkg.get("notes") or "").strip()[:5000],
        source_url=src.dataset_url.format(name=pkg["name"]),
        download_url=first_file,
        categories=_categories(pkg),
        formats=formats or ["Other"],
        geographic_coverage="Bangladesh",
        temporal_start=start,
        temporal_end=end,
        update_frequency=FREQUENCY_MAP.get(str(pkg.get("data_update_frequency")), "Unknown"),
        license=pkg.get("license_title") or None,
        languages=["en"],
        tags=[t["name"] for t in pkg.get("tags", [])][:15],
        verification_status="unverified",
        evidence_url=package_url,
        evidence_note=f"Harvested from the {src.name} CKAN API ({src.query}) on {date.today().isoformat()}; "
                      "awaiting manual review.",
        is_published=publish,
    )


def _org(conn, src: CkanSource, pkg: dict, actor: str) -> str:
    org = pkg.get("organization") or {}
    title = org.get("title") or src.name
    slug = f"{src.slug_prefix}-org-{org.get('name') or 'unknown'}"[:80]
    existing = conn.execute("SELECT slug FROM organizations WHERE lower(name)=lower(?)", (title,)).fetchone()
    if existing:
        return existing["slug"]
    lowered = title.lower()
    org_type = ("International Organization"
                if any(k in lowered for k in ("united nations", "unicef", "unhcr", "who", "wfp", "ocha", "iom",
                                              "world bank", "fao", "undp", "unfpa", "reach", "icrc"))
                else "Government Department" if "bangladesh" in lowered and ("bureau" in lowered or "ministry" in lowered
                                                                           or "department" in lowered)
                else "Non-Governmental Organization")
    return repo.upsert_organization(conn, OrganizationIn(
        slug=slug, name=title, org_type=org_type,
        description=(org.get("description") or "")[:1000] or None,
        website=f"https://data.humdata.org/organization/{org['name']}" if org.get("name") and src.key == "hdx" else None,
    ), actor)


def fetch(src: CkanSource, limit: int | None = None) -> list[dict]:
    client = httpx.Client(timeout=60, headers={"User-Agent": settings.monitor_user_agent})
    out, start, rows = [], 0, 100
    while True:
        r = client.get(f"{src.api}/package_search", params={**src.query, "rows": rows, "start": start})
        r.raise_for_status()
        result = r.json()["result"]
        out += result["results"]
        start += rows
        if start >= result["count"] or (limit and len(out) >= limit) or not result["results"]:
            break
    return out[:limit] if limit else out


def harvest(conn: sqlite3.Connection, src: CkanSource, packages: list[dict], publish: bool = False,
            update: bool = False, actor: str = "harvester") -> dict:
    report = {"source": src.key, "seen": len(packages), "created": 0, "updated": 0, "skipped": 0, "errors": []}
    for pkg in packages:
        if pkg.get("private") or pkg.get("state", "active") != "active":
            report["skipped"] += 1
            continue
        try:
            org_slug = _org(conn, src, pkg, actor)
            ds = to_dataset(src, pkg, org_slug, publish)
            _store(conn, ds, report, publish, update, actor)
        except (ValidationError, KeyError, repo.Invalid) as exc:
            report["errors"].append(f"{pkg.get('name')}: {exc}"[:300])
    repo.audit(conn, actor, "harvest", "source", src.key, {k: v for k, v in report.items() if k != "errors"})
    conn.commit()
    return report


def _store(conn: sqlite3.Connection, ds: DatasetIn, report: dict, publish: bool, update: bool, actor: str) -> None:
    # Never duplicate a record that is already catalogued (e.g. a manually verified seed entry).
    dup = conn.execute("SELECT slug FROM datasets WHERE source_url=?",
                       (ds.source_url,)).fetchone()
    own = conn.execute("SELECT slug, verification_status, is_published FROM datasets WHERE slug=?",
                       (repo.slugify(ds.slug),)).fetchone()
    if own and update and own["verification_status"] == "unverified":
        ds.is_published = bool(own["is_published"]) or publish
        repo.update_dataset(conn, own["slug"], ds, actor)
        report["updated"] += 1
    elif own or dup:
        report["skipped"] += 1
    else:
        repo.create_dataset(conn, ds, actor)
        report["created"] += 1


# --------------------------------------------------------------------------- World Bank

WB_API = "https://api.worldbank.org/v2"
WB_ORG = "world-bank"  # declared in seed/sources/portals.json
# Source databases already catalogued by hand; never exported twice.
WB_ALREADY_CATALOGUED = {"WDI": "wb-wdi-bangladesh"}
WB_BATCH = 20          # indicators per Bangladesh data request (longer URLs are rejected)
WB_PROBE = 300         # indicators checked per source at most
WB_SERIES_PROBE = 40   # indicators checked one by one on multi-dimensional sources
WB_NAMES = 20          # indicator names listed in each description

# World Bank indicator topics → BD DataHub categories.
WB_TOPICS = {
    "Agriculture & Rural Development": "Agriculture", "Aid Effectiveness": "Economy & Finance",
    "Economy & Growth": "Economy & Finance", "Education": "Education", "Energy & Mining": "Energy",
    "Environment": "Environment", "Financial Sector": "Economy & Finance", "Health": "Health",
    "Infrastructure": "Infrastructure", "Social Protection & Labor": "Social Protection",
    "Poverty": "Social Protection", "Private Sector": "Industry & Business",
    "Public Sector": "Government & Public Administration", "Science & Technology": "Science & Technology",
    "Social Development": "Population & Demographics", "Urban Development": "Infrastructure",
    "Gender": "Population & Demographics", "Climate Change": "Climate & Weather",
    "External Debt": "Economy & Finance", "Trade": "Trade & Commerce",
}
# A source whose name says what it is about takes that category first; topics fill the rest.
# Order matters: the first group whose words appear in the name wins.
WB_NAME_CATEGORIES = [
    ("Economy & Finance", ["debt", "price", "remittance", "savings", "wealth accounts", "findex", "icp",
                           "comparison program", "economic monitor", "economic prospects", "fpn"]),
    ("Health", ["health", "nutrition", "malnutrition"]),
    ("Education", ["education", "human capital"]),
    ("Environment", ["environment, social and governance"]),
    ("Government & Public Administration", ["governance", "bureaucracy", "pefa", "procurement", "statistical performance",
                                            "policy and institutional", "identification", "regulatory"]),
    ("Industry & Business", ["enterprise", "doing business", "exporter", "economic fitness"]),
    ("Labour & Employment", ["job", "labor", "labour"]),
    ("Population & Demographics", ["gender", "population", "disability"]),
    ("Social Protection", ["social protection", "poverty"]),
    ("Economy & Finance", ["financ", "economic"]),
    ("Energy", ["energy"]),
    ("Transport", ["logistics"]),
    ("Climate & Weather", ["climate"]),
    ("Environment", ["environment"]),
]


class WorldBank:
    """Small, throttled client for the World Bank Indicators API (no key required)."""

    def __init__(self, delay: float = 0.5):
        self.client = httpx.Client(timeout=90, follow_redirects=True,
                                   headers={"User-Agent": settings.monitor_user_agent})
        self.delay = delay

    def _json(self, url: str, **params):
        time.sleep(self.delay)
        r = self.client.get(url, params={"format": "json", **params})
        r.raise_for_status()
        return r.json()

    def sources(self) -> list[dict]:
        return self._json(f"{WB_API}/sources", per_page=500)[1]

    def indicators(self, source_id: str) -> tuple[int, list[dict]]:
        """(total, first indicators) of a source, keeping only id, name and topics."""
        data = self._json(f"{WB_API}/indicator", source=source_id, per_page=WB_PROBE)
        if not isinstance(data, list) or len(data) < 2 or not data[1]:
            return 0, []
        keep = [{"id": r["id"], "name": r["name"].strip(),
                 "topics": [t.get("value", "").strip() for t in r.get("topics") or []]} for r in data[1]]
        return int(data[0].get("total") or len(keep)), keep

    def country_values(self, source_id: str, ids: list[str]) -> list[dict] | None:
        """Bangladesh values via /country/BGD/indicator/…; None if the source is not served on that path."""
        data = self._json(f"{WB_API}/country/BGD/indicator/{';'.join(ids)}", source=source_id, per_page=20000)
        if not isinstance(data, list) or len(data) < 2:
            return None  # [{"message": [...]}]: indicators of multi-dimensional sources are not found here
        return [{"id": r["indicator"]["id"], "date": r["date"]} for r in data[1] or []
                if r.get("value") is not None and _is_bangladesh(r)]

    def series_path(self, source_id: str, indicator: str) -> str:
        """Multi-dimensional /sources/… URL; debt-style sources are read at their world total."""
        concepts = self._json(f"{WB_API}/sources/{source_id}/concepts")
        ids = {c.get("id") for s in (concepts.get("source") or []) for c in s.get("concept") or []}             if isinstance(concepts, dict) else set()
        area = "/counterpart-area/WLD" if "Counterpart-Area" in ids else ""
        return f"{WB_API}/sources/{source_id}/country/BGD/series/{indicator}{area}/time/all"

    def series_values(self, url: str) -> tuple[str | None, list[dict]]:
        """(last updated, Bangladesh values) from a series_path() URL."""
        data = self._json(url, per_page=1000)
        if not isinstance(data, dict):
            return None, []
        out = []
        for item in (data.get("source") or {}).get("data") or []:
            if item.get("value") is None:
                continue
            dims = {v.get("concept"): v.get("value") for v in item.get("variable") or []}
            if "BGD" in {v.get("id") for v in item.get("variable") or []}:
                out.append({"id": dims.get("Series"), "date": str(dims.get("Time", ""))})
        return data.get("lastupdated"), out

    def page_title(self, url: str) -> tuple[int | None, str]:
        """(HTTP status, <title>) of a page; (None, "") if it could not be loaded."""
        time.sleep(self.delay)
        try:
            r = self.client.get(url)
        except httpx.HTTPError:
            return None, ""
        m = re.search(r"<title>([^<]*)", r.text) if r.status_code == 200 else None
        return r.status_code, (m.group(1).strip() if m else "")


def _is_bangladesh(row: dict) -> bool:
    # Some sources leave countryiso3code empty and identify the country only by country.id / name.
    country = row.get("country") or {}
    return "BGD" in (row.get("countryiso3code"), country.get("id")) or country.get("value") == "Bangladesh"


def _wb_frequency(dates: list[str]) -> str:
    if any("M" in d for d in dates):
        return "Monthly"
    if any("Q" in d for d in dates):
        return "Quarterly"
    return "Annual" if dates and all(re.fullmatch(r"\d{4}", d) for d in dates) else "Unknown"


def _wb_categories(name: str, topics: list[str]) -> list[str]:
    """Category named by the source itself first, then the most common indicator topics (at most two)."""
    lowered = name.lower()
    named = next((c for c, words in WB_NAME_CATEGORIES if any(w in lowered for w in words)), None)
    cats = list(dict.fromkeys([named, *topics] if named else topics))
    return cats[:2] or ["Other"]


def wb_probe(wb: WorldBank, src: dict) -> dict | None:
    """Prove a source database has Bangladesh data. Returns the facts found, or None."""
    total, indicators = wb.indicators(src["id"])
    by_id = {i["id"]: i for i in indicators}
    found: dict[str, list[str]] = {}  # indicator id → dates with a Bangladesh value
    checked, series_path = 0, False
    for n in range(0, len(indicators), WB_BATCH):
        batch = [i["id"] for i in indicators[n:n + WB_BATCH]]
        rows = wb.country_values(src["id"], batch)
        if rows is None:
            series_path = True
            break
        checked += len(batch)
        for row in rows:
            found.setdefault(row["id"], []).append(row["date"])
        if len(found) >= WB_NAMES:
            break
    if series_path:
        template = wb.series_path(src["id"], "{indicator}")
        for ind in indicators[:WB_SERIES_PROBE]:
            checked += 1
            _, rows = wb.series_values(template.replace("{indicator}", ind["id"]))
            if rows:
                found[ind["id"]] = [r["date"] for r in rows]
            if len(found) >= WB_NAMES:
                break
    if not found:
        return None
    # Evidence: one request anyone can open, for the indicator with the most Bangladesh values.
    best = max(found, key=lambda i: len(found[i]))
    if series_path:
        url = template.replace("{indicator}", best)
        evidence_url = f"{url}?format=json"
        updated, rows = wb.series_values(url)
        dates = sorted(r["date"] for r in rows)
    else:
        evidence_url = f"{WB_API}/country/BGD/indicator/{best}?source={src['id']}&format=json"
        data = wb._json(f"{WB_API}/country/BGD/indicator/{best}", source=src["id"], per_page=20000)
        rows = [r for r in data[1] or [] if r.get("value") is not None] if isinstance(data, list) and len(data) > 1 else []
        updated, dates = data[0].get("lastupdated") if rows else None, sorted(r["date"] for r in rows)
    if not rows:
        return None  # the evidence request itself must show Bangladesh values
    all_dates = [d for ds in found.values() for d in ds]
    years = [int(d[:4]) for d in all_dates if d[:4].isdigit()]
    projected = max((y for y in years if y > date.today().year), default=None)  # e.g. debt service projections
    years = [y for y in years if y <= date.today().year]
    topics = Counter(WB_TOPICS[t] for i in found for t in by_id[i]["topics"] if t in WB_TOPICS)
    name = src["name"].replace("�", "–").strip()
    span = dates[0] if dates[0] == dates[-1] else f"{dates[0]}–{dates[-1]}"
    # DataBank pages are keyed by name, not API id: keep one only if it loads and its title names this source.
    page = f"https://databank.worldbank.org/source/{repo.slugify(name)}"
    status, title = wb.page_title(page)
    page_ok = status == 200 and name.lower() in title.lower()
    return {
        "source": src, "name": name, "indicator_total": total, "checked": checked, "with_data": len(found),
        "partial": checked < total, "names": [by_id[i]["name"] for i in found][:WB_NAMES],
        "years": (min(years), max(years)) if years else (None, None), "projected_to": projected,
        "frequency": _wb_frequency(all_dates),
        "categories": _wb_categories(name, [c for c, _ in topics.most_common()]),
        "evidence_url": evidence_url,
        "evidence_facts": (f"returned {len(rows)} non-null Bangladesh value{'s' if len(rows) != 1 else ''} for "
                           f"'{by_id[best]['name']}' ({span}); source last updated {updated or src['lastupdated']}"),
        "page": page if page_ok else None,
        "page_check": (f"DataBank page {page} loaded (HTTP 200) with title '{title}'" if page_ok
                       else f"no matching DataBank page at {page} (HTTP {status or 'error'}), so the API request is the source link"),
    }


def wb_record(info: dict) -> dict:
    """Seed-format record (seed/SEED_SPEC.md) for one probed World Bank source database."""
    src, (y0, y1) = info["source"], info["years"]
    checked = (f"all {info['checked']:,} checked" if info["with_data"] == info["checked"] and info["partial"]
               else f"of the {info['checked']:,} checked, {info['with_data']}" if info["partial"]
               else f"{info['with_data']} of them")
    years = "" if not y0 else f" ({y0})" if y0 == y1 else f" ({y0}–{y1})"
    if info["projected_to"]:
        years += f", plus projections to {info['projected_to']}"
    description = (
        f"World Bank {info['name']} database, available through the World Bank Indicators API"
        f"{' and DataBank' if info['page'] else ''}. It holds {info['indicator_total']:,} indicators; "
        f"{checked} returned values for Bangladesh{years}. "
        f"Indicators with Bangladesh data include: {'; '.join(info['names'])}."
    )
    return {
        "slug": f"wb-{src['code'].lower()}-bangladesh",
        "title": f"{info['name']} - Bangladesh",
        "organization": WB_ORG,
        "description": description[:5000],
        "source_url": info["page"] or info["evidence_url"],
        "download_url": None,
        "categories": info["categories"],
        "subcategories": [],
        "formats": ["JSON", "XML"],
        "geographic_coverage": "Bangladesh",
        "geographic_level": "National",
        "temporal_start": y0,
        "temporal_end": y1,
        "update_frequency": info["frequency"],
        "license": None,
        "languages": ["en"],
        "tags": ["world bank", src["code"].lower(), "indicators api"],
        "evidence_url": info["evidence_url"],
        "evidence_note": (f"World Bank Indicators API (source {src['id']}, {src['code']}) {info['evidence_facts']}; "
                          f"{info['page_check']}."),
        "checked_at": date.today().isoformat(),
    }


def fetch_worldbank(limit: int | None = None, on_record=None) -> tuple[list[dict], list[dict], list[str]]:
    """Probe World Bank source databases one at a time. Returns (records, unreachable, skipped notes)."""
    wb = WorldBank()
    records, unreachable, skipped = [], [], []
    for src in wb.sources():
        if limit and len(records) >= limit:
            break
        if src["code"] in WB_ALREADY_CATALOGUED:
            skipped.append(f"{src['code']}: already catalogued as {WB_ALREADY_CATALOGUED[src['code']]}")
            continue
        if src.get("dataavailability") != "Y":
            skipped.append(f"{src['code']}: no data available through the API")
            continue
        try:
            info = wb_probe(wb, src)
        except (httpx.HTTPError, ValueError) as exc:
            unreachable.append({"url": f"{WB_API}/indicator?source={src['id']}&format=json",
                                "note": f"{src['name']}: {type(exc).__name__}"})
            continue
        if info:
            records.append(wb_record(info))
            print(f"  + {src['code']}: {info['with_data']} indicators with Bangladesh data", flush=True)
            if on_record:
                on_record(records, unreachable)
        else:
            skipped.append(f"{src['code']}: no Bangladesh values returned")
    return records, unreachable, skipped


def harvest_worldbank(conn: sqlite3.Connection, records: list[dict], publish: bool = False,
                      update: bool = False, actor: str = "harvester") -> dict:
    report = {"source": "worldbank", "seen": len(records), "created": 0, "updated": 0, "skipped": 0, "errors": []}
    for raw in records:
        try:
            ds = DatasetIn(**{**raw, "evidence_note": raw["evidence_note"] + " Awaiting manual review."},
                           verification_status="unverified", is_published=publish)
            _store(conn, ds, report, publish, update, actor)
        except (ValidationError, repo.Invalid) as exc:
            report["errors"].append(f"{raw['slug']}: {exc}"[:300])
    repo.audit(conn, actor, "harvest", "source", "worldbank", {k: v for k, v in report.items() if k != "errors"})
    conn.commit()
    return report


def _write_seed(path: Path, records: list[dict], unreachable: list[dict]) -> None:
    body = {"organizations": [], "datasets": records, "unreachable": unreachable}
    path.write_text(json.dumps(body, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Harvest metadata from official APIs as review drafts.")
    parser.add_argument("source", choices=sorted([*SOURCES, "worldbank"]))
    parser.add_argument("--limit", type=int, default=None, help="Maximum packages / source databases to fetch")
    parser.add_argument("--publish", action="store_true",
                        help="Publish immediately (still marked 'Not yet verified')")
    parser.add_argument("--update", action="store_true", help="Refresh previously harvested, unverified drafts")
    parser.add_argument("--export-seed", type=Path, default=None,
                        help="worldbank only: also write the records to this seed file (rewritten after each one)")
    args = parser.parse_args()
    if args.source == "worldbank":
        save = (lambda recs, unr: _write_seed(args.export_seed, recs, unr)) if args.export_seed else None
        records, unreachable, skipped = fetch_worldbank(args.limit, save)
        if args.export_seed:
            _write_seed(args.export_seed, records, unreachable)
            print(f"wrote {len(records)} datasets to {args.export_seed}")
        for note in skipped:
            print("  -", note)
        with db.session() as conn:
            db.init_schema(conn)
            report = harvest_worldbank(conn, records, args.publish, args.update)
    else:
        packages = fetch(SOURCES[args.source], args.limit)
        with db.session() as conn:
            db.init_schema(conn)
            report = harvest(conn, SOURCES[args.source], packages, args.publish, args.update)
    print({k: v for k, v in report.items() if k != "errors"})
    for e in report["errors"][:20]:
        print("  !", e)


if __name__ == "__main__":
    main()
