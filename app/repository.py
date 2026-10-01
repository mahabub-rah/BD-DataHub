"""Data access layer: CRUD, faceted full-text search, stats and recommendations."""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any

from . import nlsearch, vocab
from .models import DatasetIn, OrganizationIn


class NotFound(Exception):
    pass


class Invalid(ValueError):
    pass


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def slugify(text: str, max_len: int = 80) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text[:max_len].rstrip("-") or "item"


def audit(conn: sqlite3.Connection, actor: str, action: str, entity: str,
          entity_id: str | None = None, detail: Any = None) -> None:
    conn.execute(
        "INSERT INTO audit_log (actor, action, entity, entity_id, detail) VALUES (?,?,?,?,?)",
        (actor, action, entity, entity_id,
         json.dumps(detail, ensure_ascii=False) if detail is not None else None),
    )


# --------------------------------------------------------------------------- categories

def upsert_category(conn, name: str, parent_id: int | None = None, description: str | None = None,
                    icon: str | None = None, sort_order: int = 0) -> int:
    base = slugify(name)
    if parent_id:
        parent_slug = conn.execute("SELECT slug FROM categories WHERE id=?", (parent_id,)).fetchone()[0]
        base = f"{parent_slug}--{base}"
    row = conn.execute("SELECT id FROM categories WHERE slug=?", (base,)).fetchone()
    if row:
        conn.execute(
            "UPDATE categories SET name=?, description=COALESCE(?, description), "
            "icon=COALESCE(?, icon), sort_order=? WHERE id=?",
            (name, description, icon, sort_order, row["id"]),
        )
        return row["id"]
    cur = conn.execute(
        "INSERT INTO categories (slug, name, parent_id, description, icon, sort_order) VALUES (?,?,?,?,?,?)",
        (base, name, parent_id, description, icon, sort_order),
    )
    return cur.lastrowid


def find_category(conn, key: str, parent_id: int | None = None) -> sqlite3.Row | None:
    key = key.strip()
    if parent_id is None:
        return conn.execute(
            "SELECT * FROM categories WHERE parent_id IS NULL AND (slug=? OR lower(name)=lower(?))",
            (key, key),
        ).fetchone()
    return conn.execute(
        "SELECT * FROM categories WHERE parent_id=? AND (slug=? OR lower(name)=lower(?))",
        (parent_id, key, key),
    ).fetchone()


def category_tree(conn) -> list[dict]:
    """Top-level categories with subcategories and dataset counts (subcategory counts roll up)."""
    counts = {
        r["category_id"]: r["n"]
        for r in conn.execute(
            "SELECT dc.category_id, COUNT(DISTINCT d.id) n FROM dataset_categories dc "
            "JOIN datasets d ON d.id=dc.dataset_id WHERE d.is_published=1 GROUP BY dc.category_id"
        )
    }
    rows = conn.execute("SELECT * FROM categories ORDER BY sort_order, name").fetchall()
    tops = [dict(r) | {"children": [], "count": counts.get(r["id"], 0)} for r in rows if r["parent_id"] is None]
    by_id = {t["id"]: t for t in tops}
    for r in rows:
        if r["parent_id"] in by_id:
            by_id[r["parent_id"]]["children"].append(dict(r) | {"count": counts.get(r["id"], 0)})
    return tops


def get_category(conn, slug: str) -> dict:
    row = conn.execute("SELECT * FROM categories WHERE slug=?", (slug,)).fetchone()
    if not row:
        raise NotFound(slug)
    cat = dict(row)
    cat["parent"] = (dict(conn.execute("SELECT * FROM categories WHERE id=?", (row["parent_id"],)).fetchone())
                     if row["parent_id"] else None)
    cat["children"] = [dict(r) for r in conn.execute(
        "SELECT * FROM categories WHERE parent_id=? ORDER BY sort_order, name", (row["id"],))]
    return cat


# --------------------------------------------------------------------------- organizations

def upsert_organization(conn, org: OrganizationIn, actor: str = "system") -> str:
    slug = org.slug or slugify(org.acronym or org.name)
    existing = conn.execute("SELECT id FROM organizations WHERE slug=?", (slug,)).fetchone()
    values = (org.name, org.name_bn, org.acronym, org.org_type, org.parent, org.website, org.description)
    if existing:
        conn.execute(
            "UPDATE organizations SET name=?, name_bn=COALESCE(?, name_bn), acronym=COALESCE(?, acronym), "
            "org_type=COALESCE(?, org_type), parent=COALESCE(?, parent), website=COALESCE(?, website), "
            "description=COALESCE(?, description), updated_at=? WHERE slug=?",
            (*values, now_iso(), slug),
        )
        action = "update"
    else:
        conn.execute(
            "INSERT INTO organizations (name, name_bn, acronym, org_type, parent, website, description, slug) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (*values, slug),
        )
        action = "create"
    audit(conn, actor, action, "organization", slug)
    # Organisation name is part of each dataset's search document.
    for (ds_id,) in conn.execute(
        "SELECT d.id FROM datasets d JOIN organizations o ON o.id=d.organization_id WHERE o.slug=?", (slug,)
    ).fetchall():
        reindex(conn, ds_id)
    return slug


def list_organizations(conn, org_type: str | None = None, q: str | None = None) -> list[dict]:
    where, args = ["1=1"], []
    if org_type:
        where.append("o.org_type=?")
        args.append(org_type)
    if q:
        where.append("(o.name LIKE ? OR o.acronym LIKE ? OR o.name_bn LIKE ?)")
        args += [f"%{q}%"] * 3
    rows = conn.execute(
        f"""SELECT o.*, COUNT(d.id) AS dataset_count
            FROM organizations o LEFT JOIN datasets d ON d.organization_id=o.id AND d.is_published=1
            WHERE {' AND '.join(where)} GROUP BY o.id
            ORDER BY dataset_count DESC, o.name""",
        args,
    ).fetchall()
    return [dict(r) for r in rows]


def get_organization(conn, slug: str) -> dict:
    row = conn.execute("SELECT * FROM organizations WHERE slug=?", (slug,)).fetchone()
    if not row:
        raise NotFound(slug)
    org = dict(row)
    org["dataset_count"] = conn.execute(
        "SELECT COUNT(*) FROM datasets WHERE organization_id=? AND is_published=1", (row["id"],)
    ).fetchone()[0]
    return org


# --------------------------------------------------------------------------- datasets

def _resolve_categories(conn, data: DatasetIn) -> list[int]:
    ids: list[int] = []
    tops = []
    for key in data.categories:
        row = find_category(conn, key)
        if not row:
            raise Invalid(f"Unknown category: {key!r}")
        tops.append(row["id"])
        ids.append(row["id"])
    if not tops:
        other = find_category(conn, "Other")
        if other:
            tops.append(other["id"])
            ids.append(other["id"])
    for sub in data.subcategories:
        ids += resolve_subcategory(conn, sub, tops)
    return list(dict.fromkeys(ids))


# Labels that describe a document type rather than a topic are not useful as subcategories.
IGNORED_SUBCATEGORIES = {"annual report", "annual reports", "report", "reports", "statistics", "data",
                         "dataset", "publication", "publications"}


def resolve_subcategory(conn, name: str, tops: list[int]) -> list[int]:
    """Map a free-text subcategory to category ids (the subcategory and, if needed, its parent).

    Order: exact name under one of the dataset's categories → a known alias → the same name
    under any category → otherwise create it under the first category (extensible taxonomy).
    """
    if name.strip().lower() in IGNORED_SUBCATEGORIES:
        return []
    for top in tops:
        if row := find_category(conn, name, parent_id=top):
            return [row["id"]]
    row = conn.execute(
        "SELECT c.id, c.parent_id FROM category_aliases a JOIN categories c ON c.id=a.category_id WHERE a.alias=?",
        (name.strip(),)).fetchone()
    if not row:
        row = conn.execute(
            "SELECT id, parent_id FROM categories WHERE parent_id IS NOT NULL AND lower(name)=lower(?) "
            "ORDER BY sort_order LIMIT 1", (name.strip(),)).fetchone()
    if row:
        return [row["id"], row["parent_id"]] if row["parent_id"] else [row["id"]]
    if not tops:
        return []
    return [upsert_category(conn, name, parent_id=tops[0], sort_order=100)]


def _unique_slug(conn, base: str, exclude_id: int | None = None) -> str:
    slug, n = base, 2
    while True:
        row = conn.execute("SELECT id FROM datasets WHERE slug=?", (slug,)).fetchone()
        if not row or row["id"] == exclude_id:
            return slug
        slug = f"{base}-{n}"
        n += 1


def _write_children(conn, dataset_id: int, data: DatasetIn, category_ids: list[int]) -> None:
    conn.execute("DELETE FROM dataset_categories WHERE dataset_id=?", (dataset_id,))
    conn.execute("DELETE FROM dataset_formats WHERE dataset_id=?", (dataset_id,))
    conn.execute("DELETE FROM dataset_tags WHERE dataset_id=?", (dataset_id,))
    conn.executemany("INSERT INTO dataset_categories VALUES (?,?)", [(dataset_id, c) for c in category_ids])
    conn.executemany("INSERT INTO dataset_formats VALUES (?,?)", [(dataset_id, f) for f in data.formats])
    conn.executemany("INSERT OR IGNORE INTO dataset_tags VALUES (?,?)",
                     [(dataset_id, t.lower()) for t in data.tags])


def _dataset_columns(data: DatasetIn, org_id: int) -> dict[str, Any]:
    return {
        "title": data.title.strip(),
        "organization_id": org_id,
        "description": data.description.strip(),
        "source_url": data.source_url,
        "download_url": data.download_url,
        "geographic_coverage": data.geographic_coverage or "Bangladesh",
        "geographic_level": data.geographic_level,
        "temporal_start": data.temporal_start,
        "temporal_end": data.temporal_end,
        "update_frequency": data.update_frequency,
        "license": data.license,
        "languages": json.dumps(data.languages),
        "verification_status": data.verification_status,
        "evidence_url": data.evidence_url,
        "evidence_note": data.evidence_note,
        "is_published": int(data.is_published),
    }


def _org_id(conn, slug: str) -> int:
    row = conn.execute("SELECT id FROM organizations WHERE slug=?", (slug,)).fetchone()
    if not row:
        raise Invalid(f"Unknown organization: {slug!r}")
    return row["id"]


def create_dataset(conn, data: DatasetIn, actor: str = "system") -> str:
    org_id = _org_id(conn, data.organization)
    category_ids = _resolve_categories(conn, data)
    slug = _unique_slug(conn, slugify(data.slug or data.title))
    cols = _dataset_columns(data, org_id) | {"slug": slug}
    if data.verification_status == "verified":
        cols["verified_at"] = data.checked_at or now_iso()
        cols["verified_by"] = data.verified_by or actor
    cur = conn.execute(
        f"INSERT INTO datasets ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
        list(cols.values()),
    )
    _write_children(conn, cur.lastrowid, data, category_ids)
    reindex(conn, cur.lastrowid)
    audit(conn, actor, "create", "dataset", slug)
    return slug


def update_dataset(conn, slug: str, data: DatasetIn, actor: str = "system") -> str:
    row = conn.execute("SELECT * FROM datasets WHERE slug=?", (slug,)).fetchone()
    if not row:
        raise NotFound(slug)
    org_id = _org_id(conn, data.organization)
    category_ids = _resolve_categories(conn, data)
    new_slug = _unique_slug(conn, slugify(data.slug), exclude_id=row["id"]) if data.slug else slug
    cols = _dataset_columns(data, org_id) | {"slug": new_slug, "updated_at": now_iso()}
    if data.verification_status == "verified" and row["verification_status"] != "verified":
        cols["verified_at"] = data.checked_at or now_iso()
        cols["verified_by"] = data.verified_by or actor
    if data.source_url != row["source_url"]:
        # New source: previous monitoring results no longer apply.
        cols |= {"link_status": "unchecked", "http_status": None, "content_fingerprint": None,
                 "link_checked_at": None}
    changed = [k for k in cols if k in row.keys() and row[k] != cols[k] and k != "updated_at"]
    old_children = (
        sorted(r[0] for r in conn.execute("SELECT category_id FROM dataset_categories WHERE dataset_id=?", (row["id"],))),
        sorted(r[0] for r in conn.execute("SELECT format FROM dataset_formats WHERE dataset_id=?", (row["id"],))),
        sorted(r[0] for r in conn.execute("SELECT tag FROM dataset_tags WHERE dataset_id=?", (row["id"],))),
    )
    new_children = (sorted(category_ids), sorted(data.formats), sorted({t.lower() for t in data.tags}))
    if not changed and old_children == new_children:
        return slug  # nothing to do; keep updated_at meaningful
    conn.execute(
        f"UPDATE datasets SET {', '.join(f'{k}=?' for k in cols)} WHERE id=?",
        [*cols.values(), row["id"]],
    )
    _write_children(conn, row["id"], data, category_ids)
    reindex(conn, row["id"])
    if old_children != new_children:
        changed.append("classification")
    audit(conn, actor, "update", "dataset", new_slug, {"changed": changed})
    return new_slug


def reindex(conn, dataset_id: int) -> None:
    row = conn.execute(
        """SELECT d.id, d.title, d.description, d.is_published,
                  o.name || ' ' || COALESCE(o.acronym,'') || ' ' || COALESCE(o.name_bn,'') AS org
           FROM datasets d JOIN organizations o ON o.id=d.organization_id WHERE d.id=?""",
        (dataset_id,),
    ).fetchone()
    conn.execute("DELETE FROM datasets_fts WHERE rowid=?", (dataset_id,))
    if not row:
        return
    tags = " ".join(r[0] for r in conn.execute("SELECT tag FROM dataset_tags WHERE dataset_id=?", (dataset_id,)))
    cats = " ".join(r[0] for r in conn.execute(
        "SELECT c.name FROM categories c JOIN dataset_categories dc ON dc.category_id=c.id WHERE dc.dataset_id=?",
        (dataset_id,)))
    conn.execute(
        "INSERT INTO datasets_fts (rowid, title, description, tags, organization, categories) VALUES (?,?,?,?,?,?)",
        (dataset_id, row["title"], row["description"], tags, row["org"], cats),
    )


def _hydrate(conn, rows: list[sqlite3.Row]) -> list[dict]:
    """Attach organization, categories, formats and tags to dataset rows."""
    if not rows:
        return []
    ids = [r["id"] for r in rows]
    marks = ",".join("?" * len(ids))
    fmts: dict[int, list[str]] = {}
    for r in conn.execute(f"SELECT dataset_id, format FROM dataset_formats WHERE dataset_id IN ({marks})", ids):
        fmts.setdefault(r[0], []).append(r[1])
    tags: dict[int, list[str]] = {}
    for r in conn.execute(f"SELECT dataset_id, tag FROM dataset_tags WHERE dataset_id IN ({marks}) ORDER BY tag", ids):
        tags.setdefault(r[0], []).append(r[1])
    cats: dict[int, list[dict]] = {}
    for r in conn.execute(
        f"""SELECT dc.dataset_id, c.slug, c.name, p.slug AS parent_slug
            FROM dataset_categories dc JOIN categories c ON c.id=dc.category_id
            LEFT JOIN categories p ON p.id=c.parent_id
            WHERE dc.dataset_id IN ({marks}) ORDER BY c.parent_id IS NOT NULL, c.sort_order""",
        ids,
    ):
        cats.setdefault(r["dataset_id"], []).append(
            {"slug": r["slug"], "name": r["name"], "parent_slug": r["parent_slug"]})
    out = []
    order = {f: i for i, f in enumerate(vocab.FORMATS)}
    for r in rows:
        d = dict(r)
        d["is_published"] = bool(d.get("is_published", 1))
        d["languages"] = json.loads(d.get("languages") or "[]")
        d["formats"] = sorted(fmts.get(r["id"], []), key=lambda f: order.get(f, 99))
        d["tags"] = tags.get(r["id"], [])
        all_cats = cats.get(r["id"], [])
        d["categories"] = [c for c in all_cats if not c["parent_slug"]]
        d["subcategories"] = [c for c in all_cats if c["parent_slug"]]
        d["organization"] = {
            "slug": d.pop("org_slug"), "name": d.pop("org_name"), "acronym": d.pop("org_acronym"),
            "org_type": d.pop("org_type"), "website": d.pop("org_website"),
        }
        d.pop("score", None)
        out.append(d)
    return out


_SELECT = """SELECT d.*, o.slug AS org_slug, o.name AS org_name, o.acronym AS org_acronym,
                    o.org_type AS org_type, o.website AS org_website
             FROM datasets d JOIN organizations o ON o.id=d.organization_id"""


def get_dataset(conn, slug: str) -> dict:
    row = conn.execute(f"{_SELECT} WHERE d.slug=?", (slug,)).fetchone()
    if not row or not row["is_published"]:
        raise NotFound(slug)
    return _hydrate(conn, [row])[0]


def record_view(conn, dataset_id: int) -> None:
    conn.execute("UPDATE datasets SET view_count=view_count+1 WHERE id=?", (dataset_id,))


def related_datasets(conn, dataset: dict, limit: int = 6) -> list[dict]:
    """Recommend datasets sharing categories, tags or publisher, weighted by overlap."""
    rows = conn.execute(
        f"""{_SELECT.replace('SELECT d.*', 'SELECT d.*, s.score')}
            JOIN (
                SELECT id, SUM(w) AS score FROM (
                    SELECT dc2.dataset_id AS id, CASE WHEN c.parent_id IS NULL THEN 3 ELSE 5 END AS w
                      FROM dataset_categories dc1
                      JOIN dataset_categories dc2 ON dc2.category_id=dc1.category_id
                      JOIN categories c ON c.id=dc1.category_id
                     WHERE dc1.dataset_id=:id
                    UNION ALL
                    SELECT t2.dataset_id, 2 FROM dataset_tags t1
                      JOIN dataset_tags t2 ON t2.tag=t1.tag WHERE t1.dataset_id=:id
                    UNION ALL
                    SELECT id, 2 FROM datasets WHERE organization_id=:org
                ) GROUP BY id
            ) s ON s.id=d.id
            WHERE d.id != :id AND d.is_published=1
            ORDER BY s.score DESC, d.verification_status='verified' DESC, d.title
            LIMIT :limit""",
        {"id": dataset["id"], "org": dataset["organization_id"], "limit": limit},
    ).fetchall()
    return _hydrate(conn, rows)


# --------------------------------------------------------------------------- search

SORTS = {
    "relevance": "Relevance",
    "updated": "Recently updated",
    "title": "Title A–Z",
    "coverage": "Most recent coverage",
    "popular": "Most viewed",
}


_OFFICIAL_SQL = ",".join(f"'{t}'" for t in sorted(vocab.OFFICIAL_ORG_TYPES))

ONGOING_FREQUENCIES = ("Real-time", "Daily", "Weekly", "Monthly", "Quarterly", "Half-yearly", "Annual")


@dataclass
class SearchParams:
    q: str = ""
    categories: list[str] = field(default_factory=list)
    organizations: list[str] = field(default_factory=list)
    formats: list[str] = field(default_factory=list)
    levels: list[str] = field(default_factory=list)
    frequencies: list[str] = field(default_factory=list)
    verification: list[str] = field(default_factory=list)
    link_status: list[str] = field(default_factory=list)
    source_type: str | None = None          # official | international
    machine_readable: bool | None = None
    year_from: int | None = None
    year_to: int | None = None
    language: str | None = None
    sort: str = "relevance"
    page: int = 1
    per_page: int = 20
    smart: bool = True                      # apply natural-language interpretation


def _where(p: SearchParams, fts_ids: set[int] | None, exclude: str | None = None) -> tuple[str, list]:
    clauses, args = ["d.is_published=1"], []
    if fts_ids is not None:
        if not fts_ids:
            clauses.append("0")
        else:
            clauses.append(f"d.id IN ({','.join(str(i) for i in fts_ids)})")

    def multi(dim: str, values: list[str], column: str):
        if values and exclude != dim:
            clauses.append(f"{column} IN ({','.join('?' * len(values))})")
            args.extend(values)

    if p.categories and exclude != "category":
        marks = ",".join("?" * len(p.categories))
        clauses.append(
            f"""d.id IN (SELECT dc.dataset_id FROM dataset_categories dc JOIN categories c ON c.id=dc.category_id
                         LEFT JOIN categories pc ON pc.id=c.parent_id
                         WHERE c.slug IN ({marks}) OR pc.slug IN ({marks}))""")
        args.extend(p.categories * 2)
    multi("organization", p.organizations, "o.slug")
    if p.formats and exclude != "format":
        clauses.append(f"d.id IN (SELECT dataset_id FROM dataset_formats WHERE format IN ({','.join('?' * len(p.formats))}))")
        args.extend(p.formats)
    multi("level", p.levels, "d.geographic_level")
    multi("frequency", p.frequencies, "d.update_frequency")
    multi("verification", p.verification, "d.verification_status")
    multi("link_status", p.link_status, "d.link_status")
    if p.source_type:
        if p.source_type == "international":
            clauses.append("o.org_type='International Organization'")
        elif p.source_type == "official":
            clauses.append(f"o.org_type IN ({_OFFICIAL_SQL})")
        elif p.source_type == "other":
            clauses.append(f"(o.org_type IS NULL OR o.org_type NOT IN ({_OFFICIAL_SQL}, 'International Organization'))")
    if p.machine_readable:
        mr = ",".join(f"'{f}'" for f in sorted(vocab.MACHINE_READABLE))
        clauses.append(f"d.id IN (SELECT dataset_id FROM dataset_formats WHERE format IN ({mr}))")
    if p.year_from or p.year_to:
        # An ongoing series (regular frequency, no end year) is treated as covering up to today.
        # With no start year we only know it covers recent years, so its start counts as this year.
        ongoing = "d.update_frequency IN (" + ",".join(f"'{f}'" for f in ONGOING_FREQUENCIES) + ")"
        start = f"COALESCE(d.temporal_start, CASE WHEN {ongoing} THEN CAST(strftime('%Y','now') AS INTEGER) END, d.temporal_end)"
        end = f"COALESCE(d.temporal_end, CASE WHEN {ongoing} THEN 9999 END, d.temporal_start)"
        clauses.append(f"({start} IS NOT NULL)")
        if p.year_from:
            clauses.append(f"{end} >= ?")
            args.append(p.year_from)
        if p.year_to:
            clauses.append(f"{start} <= ?")
            args.append(p.year_to)
    if p.language:
        clauses.append("d.languages LIKE ?")
        args.append(f'%"{p.language}"%')
    return " AND ".join(clauses), args


def _fts_scores(conn, expr: str) -> dict[int, float]:
    try:
        rows = conn.execute(
            "SELECT rowid, bm25(datasets_fts, 10.0, 2.0, 4.0, 5.0, 3.0) FROM datasets_fts WHERE datasets_fts MATCH ?",
            (expr,),
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    return {r[0]: r[1] for r in rows}


def _text_match(conn, parsed: nlsearch.ParsedQuery) -> tuple[dict[int, float] | None, str]:
    """Run the FTS query with progressive relaxation. Returns (scores, strategy)."""
    if not parsed.terms:
        return None, "none"
    scores = _fts_scores(conn, nlsearch.build_fts(parsed.terms))
    if scores:
        return scores, "all"
    non_place = [t for t in parsed.terms if t not in nlsearch.PLACES]
    if parsed.places and non_place:
        scores = _fts_scores(conn, nlsearch.build_fts(non_place))
        if scores:
            return scores, "without_place"
    scores = _fts_scores(conn, nlsearch.build_fts(parsed.terms, mode="OR"))
    return scores, "any" if scores else "none_found"


def search(conn, p: SearchParams) -> dict:
    p = replace(p)  # interpreted filters must not leak back into the caller's (user-chosen) params
    p.per_page = max(1, min(p.per_page, 100))
    p.page = max(1, p.page)
    parsed = nlsearch.parse(p.q) if p.q else nlsearch.ParsedQuery(raw="")
    if not p.smart and p.q:
        parsed = nlsearch.ParsedQuery(raw=p.q, terms=nlsearch.parse(p.q).terms)
        # Literal mode: keep every word, including format/frequency words.
        parsed.terms = [t for t in re.findall(r"[\wঀ-৿]+", p.q.lower()) if t not in nlsearch.STOPWORDS]
    applied: list[str] = []
    explicit = replace(p)  # user-chosen filters, before natural-language interpretation
    if p.smart and parsed.has_filters:
        # Only apply interpreted filters the user hasn't already set explicitly.
        if parsed.year_from and not p.year_from:
            p.year_from = parsed.year_from
        if parsed.year_to and not p.year_to:
            p.year_to = parsed.year_to
        if parsed.formats and not p.formats:
            p.formats = parsed.formats
        if parsed.frequencies and not p.frequencies:
            p.frequencies = parsed.frequencies
        if parsed.levels and not p.levels:
            p.levels = parsed.levels
        applied = parsed.interpreted

    scores, strategy = _text_match(conn, parsed)
    fts_ids = set(scores) if scores is not None else None

    base_from = "FROM datasets d JOIN organizations o ON o.id=d.organization_id"
    where, args = _where(p, fts_ids)
    total = conn.execute(f"SELECT COUNT(*) {base_from} WHERE {where}", args).fetchone()[0]
    relaxed: list[str] = []
    if total == 0 and applied:
        # Interpreted filters were too strict: fall back to the words alone, keep the user's own filters.
        relaxed, applied = applied, []
        p = explicit
        where, args = _where(p, fts_ids)
        total = conn.execute(f"SELECT COUNT(*) {base_from} WHERE {where}", args).fetchone()[0]
    # A page past the end shows the last page rather than an empty "no datasets" list.
    p.page = min(p.page, max(1, -(-total // p.per_page)))

    sort = p.sort if p.sort in SORTS else "relevance"
    if sort == "relevance" and not scores:
        sort = "updated" if not p.q else "title"
    order = {
        "updated": "d.updated_at DESC, d.title",
        "title": "d.title COLLATE NOCASE",
        "coverage": "COALESCE(d.temporal_end, d.temporal_start, 0) DESC, d.title",
        "popular": "d.view_count DESC, d.title",
    }.get(sort)

    if sort == "relevance":
        # Rank in Python: bm25 (lower is better) plus a small boost for verified/official sources.
        rows = conn.execute(
            f"SELECT d.id, d.verification_status, o.org_type {base_from} WHERE {where}", args).fetchall()

        def rank(r):
            s = scores.get(r["id"], 0.0)
            if r["verification_status"] == "verified":
                s -= 0.5
            if r["org_type"] in vocab.OFFICIAL_ORG_TYPES:
                s -= 0.25
            return s

        ranked = [r["id"] for r in sorted(rows, key=rank)]
        page_ids = ranked[(p.page - 1) * p.per_page: p.page * p.per_page]
        if page_ids:
            fetched = {r["id"]: r for r in conn.execute(
                f"{_SELECT} WHERE d.id IN ({','.join(str(i) for i in page_ids)})").fetchall()}
            page_rows = [fetched[i] for i in page_ids]
        else:
            page_rows = []
    else:
        page_rows = conn.execute(
            f"{_SELECT} WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?",
            [*args, p.per_page, (p.page - 1) * p.per_page],
        ).fetchall()

    return {
        "interpreted": applied,
        "relaxed_filters": relaxed,
        "match_strategy": strategy,
        "total": total,
        "page": p.page,
        "pages": max(1, -(-total // p.per_page)),
        "sort": sort,
        "items": _hydrate(conn, page_rows),
        "facets": facets(conn, p, fts_ids),
    }


def facets(conn, p: SearchParams, fts_ids: set[int] | None) -> dict[str, list[dict]]:
    """Disjunctive facet counts: each facet is counted with every *other* filter applied."""
    base = "FROM datasets d JOIN organizations o ON o.id=d.organization_id"
    out: dict[str, list[dict]] = {}

    w, a = _where(p, fts_ids, exclude="category")
    out["category"] = [dict(r) for r in conn.execute(
        f"""SELECT c.slug AS value, c.name AS label, COUNT(DISTINCT d.id) AS count
            {base} JOIN dataset_categories dc ON dc.dataset_id=d.id
            JOIN categories c ON c.id=dc.category_id AND c.parent_id IS NULL
            WHERE {w} GROUP BY c.id ORDER BY count DESC, c.sort_order""", a)]

    w, a = _where(p, fts_ids, exclude="organization")
    out["organization"] = [dict(r) for r in conn.execute(
        f"""SELECT o.slug AS value, COALESCE(o.acronym, o.name) AS label, o.name AS title, COUNT(*) AS count
            {base} WHERE {w} GROUP BY o.id ORDER BY count DESC, o.name""", a)]

    w, a = _where(p, fts_ids, exclude="format")
    order = {f: i for i, f in enumerate(vocab.FORMATS)}
    rows = [dict(r) for r in conn.execute(
        f"""SELECT f.format AS value, f.format AS label, COUNT(DISTINCT d.id) AS count
            {base} JOIN dataset_formats f ON f.dataset_id=d.id WHERE {w} GROUP BY f.format""", a)]
    out["format"] = sorted(rows, key=lambda r: order.get(r["value"], 99))

    for dim, col, vocab_list in (("level", "d.geographic_level", vocab.GEOGRAPHIC_LEVELS),
                                 ("frequency", "d.update_frequency", vocab.FREQUENCIES)):
        w, a = _where(p, fts_ids, exclude=dim)
        idx = {v: i for i, v in enumerate(vocab_list)}
        rows = [dict(r) for r in conn.execute(
            f"SELECT {col} AS value, {col} AS label, COUNT(*) AS count {base} WHERE {w} AND {col} IS NOT NULL "
            f"GROUP BY {col}", a)]
        out[dim] = sorted(rows, key=lambda r: idx.get(r["value"], 99))

    return out


# --------------------------------------------------------------------------- stats

def stats(conn) -> dict:
    one = lambda sql, *a: conn.execute(sql, a).fetchone()[0]  # noqa: E731
    return {
        "datasets": one("SELECT COUNT(*) FROM datasets WHERE is_published=1"),
        "categories": one("SELECT COUNT(*) FROM categories WHERE parent_id IS NULL"),
        "official": one(
            f"""SELECT COUNT(*) FROM datasets d JOIN organizations o ON o.id=d.organization_id
                WHERE d.is_published=1 AND o.org_type IN ({_OFFICIAL_SQL})"""),
        "international": one(
            """SELECT COUNT(*) FROM datasets d JOIN organizations o ON o.id=d.organization_id
               WHERE d.is_published=1 AND o.org_type='International Organization'"""),
        "other": one(
            f"""SELECT COUNT(*) FROM datasets d JOIN organizations o ON o.id=d.organization_id
                WHERE d.is_published=1 AND (o.org_type IS NULL
                      OR o.org_type NOT IN ({_OFFICIAL_SQL}, 'International Organization'))"""),
        "machine_readable": one(
            f"""SELECT COUNT(*) FROM datasets d WHERE is_published=1 AND (id IN
                (SELECT dataset_id FROM dataset_formats WHERE format IN
                ({','.join(f"'{f}'" for f in sorted(vocab.MACHINE_READABLE))})))"""),
        "last_updated": one("SELECT MAX(updated_at) FROM datasets WHERE is_published=1"),
        "last_checked": one("SELECT MAX(link_checked_at) FROM datasets"),
    }


def recent_datasets(conn, limit: int = 6) -> list[dict]:
    rows = conn.execute(
        f"{_SELECT} WHERE d.is_published=1 ORDER BY d.updated_at DESC, d.id DESC LIMIT ?", (limit,)).fetchall()
    return _hydrate(conn, rows)
