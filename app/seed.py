"""Load the category taxonomy and the verified seed catalog into the database.

Usage:  python -m app.seed [--reset]

Seed files live in seed/sources/*.json (format: seed/SEED_SPEC.md). Every seed dataset
carries evidence (URL + note + date) from a check of the live source, so it is loaded
as "verified". Loading is idempotent: records are upserted by slug.

The app calls sync() on every start, so after pulling new seed data you only restart the
server: changed seed files are loaded, unchanged ones are skipped.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path

from pydantic import ValidationError

from . import db, monitor, papers, repository as repo
from .config import BASE_DIR, settings
from .models import DatasetIn, OrganizationIn

SEED_DIR = BASE_DIR / "seed"


def load_categories(conn: sqlite3.Connection, path: Path = SEED_DIR / "categories.json") -> int:
    items = json.loads(path.read_text(encoding="utf-8"))
    for i, item in enumerate(items):
        parent = repo.upsert_category(conn, item["name"], None, item.get("description"), item.get("icon"), i)
        for j, sub in enumerate(item.get("subcategories", [])):
            sub = {"name": sub} if isinstance(sub, str) else sub
            sub_id = repo.upsert_category(conn, sub["name"], parent, sub.get("description"), sort_order=j)
            for alias in sub.get("aliases", []):
                conn.execute("INSERT OR REPLACE INTO category_aliases (alias, category_id) VALUES (?, ?)",
                             (alias, sub_id))
    return len(items)


def load_source_file(conn: sqlite3.Connection, path: Path, actor: str = "seed") -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    report = {"file": path.name, "organizations": 0, "created": 0, "updated": 0, "unchanged": 0, "errors": []}
    for raw in data.get("organizations", []):
        try:
            repo.upsert_organization(conn, OrganizationIn(**raw), actor)
            report["organizations"] += 1
        except (ValidationError, repo.Invalid) as exc:
            report["errors"].append(f"org {raw.get('slug')}: {exc}")
    for raw in data.get("datasets", []):
        if not raw.get("evidence_url"):
            report["errors"].append(f"dataset {raw.get('slug')}: missing evidence_url (not loaded)")
            continue
        raw = dict(raw)
        raw.setdefault("verification_status", "verified")
        raw.setdefault("verified_by", "seed research (live source check)")
        try:
            ds = DatasetIn(**raw)
            exists = ds.slug and conn.execute("SELECT 1 FROM datasets WHERE slug=?", (repo.slugify(ds.slug),)).fetchone()
            if exists:
                slug = repo.slugify(ds.slug)
                before = conn.execute("SELECT updated_at FROM datasets WHERE slug=?", (slug,)).fetchone()[0]
                repo.update_dataset(conn, slug, ds, actor)
                after = conn.execute("SELECT updated_at FROM datasets WHERE slug=?", (slug,)).fetchone()[0]
                report["updated" if after != before else "unchanged"] += 1
            else:
                repo.create_dataset(conn, ds, actor)
                report["created"] += 1
        except (ValidationError, repo.Invalid) as exc:
            report["errors"].append(f"dataset {raw.get('slug')}: {exc}".replace("\n", " ")[:400])
    return report


def seed(conn: sqlite3.Connection, source_dir: Path = SEED_DIR / "sources") -> list[dict]:
    """Load everything from seed/ unconditionally (the CLI); boot uses sync()."""
    return sync(conn, source_dir, force=True)["catalog"]


def _fingerprint(paths: list[Path]) -> str:
    """Changes whenever a seed file is added, removed, edited or touched."""
    basis = "\n".join(f"{p.name}:{p.stat().st_size}:{p.stat().st_mtime_ns}" for p in sorted(paths))
    return hashlib.sha256(basis.encode()).hexdigest()


def _state(conn: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    return conn.execute("SELECT fingerprint, rows FROM seed_state WHERE name=?", (name,)).fetchone()


def _save_state(conn: sqlite3.Connection, name: str, fingerprint: str, rows: int) -> None:
    conn.execute("INSERT INTO seed_state (name, fingerprint, rows, loaded_at) VALUES (?, ?, ?, datetime('now')) "
                 "ON CONFLICT(name) DO UPDATE SET fingerprint=excluded.fingerprint, rows=excluded.rows, "
                 "loaded_at=excluded.loaded_at", (name, fingerprint, rows))


def sync(conn: sqlite3.Connection, source_dir: Path = SEED_DIR / "sources", papers_dir: Path = papers.SEED_DIR,
         force: bool = False) -> dict:
    """Bring the database up to date with seed/ (run on every boot; idempotent).

    * Catalog (categories.json + sources/*.json): upserted when any of those files changed since the
      last sync, or the database has no categories. Unchanged records keep their updated_at, and rows
      that are not in the seed (harvested drafts, link-check results, view counts) are left alone.
    * Link checks (seed/link_checks.json): applied where newer than the stored results, so a host
      without a persistent disk still shows the last committed check.
    * Papers (seed/papers/*.json): rebuilt when those files changed or the stored count differs.
    A seed file that fails to load (e.g. half-written JSON) is reported and retried on the next boot;
    it never stops the site from starting.
    """
    db.init_schema(conn)
    out: dict = {"catalog": [], "catalog_loaded": False, "papers_loaded": False, "errors": []}

    catalog_files = [SEED_DIR / "categories.json", *sorted(source_dir.glob("*.json"))]
    fp = _fingerprint([p for p in catalog_files if p.exists()])
    st = _state(conn, "catalog")
    empty = not conn.execute("SELECT 1 FROM categories LIMIT 1").fetchone()
    if force or empty or not st or st["fingerprint"] != fp:
        ok = True
        try:
            load_categories(conn)
        except (OSError, ValueError, KeyError) as exc:
            ok = False
            out["errors"].append(f"categories.json: {exc}")
        for path in catalog_files[1:]:
            try:
                out["catalog"].append(load_source_file(conn, path))
            except (OSError, ValueError) as exc:  # unreadable or malformed JSON
                ok = False
                out["errors"].append(f"{path.name}: {exc}")
        if ok:
            _save_state(conn, "catalog", fp, conn.execute("SELECT COUNT(*) FROM datasets").fetchone()[0])
        out["catalog_loaded"] = True
        conn.commit()

    # Link-check results exported by `python -m app.monitor` (needs the catalog rows to exist).
    snap = monitor.SNAPSHOT_PATH
    if snap.exists():
        fp = _fingerprint([snap])
        st = _state(conn, "link_checks")
        if force or out["catalog_loaded"] or not st or st["fingerprint"] != fp:
            try:
                n = monitor.load_snapshot(conn, snap)
                _save_state(conn, "link_checks", fp, n)
                out["link_checks_loaded"] = n
            except (OSError, ValueError, KeyError) as exc:
                conn.rollback()
                out["errors"].append(f"{snap.name}: {exc}")
            conn.commit()

    paper_files = sorted(papers_dir.glob("*.json"))
    # The paper -> category mapping depends on the category list and on the mapping table's version too.
    fp = _fingerprint(paper_files + [SEED_DIR / "categories.json"]) + ":" + papers.MAPPING_VERSION
    st = _state(conn, "papers")
    if force or not st or st["fingerprint"] != fp or st["rows"] != papers.count(conn):
        try:
            papers.load(conn, papers_dir)
            _save_state(conn, "papers", fp, papers.count(conn))
            out["papers_loaded"] = True
        except (OSError, ValueError) as exc:
            conn.rollback()
            out["errors"].append(f"papers: {exc}")
        conn.commit()
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--reset", action="store_true", help="Delete the database file first")
    args = parser.parse_args()
    if args.reset and settings.database_path.exists():
        for suffix in ("", "-wal", "-shm"):
            Path(str(settings.database_path) + suffix).unlink(missing_ok=True)
    with db.session() as conn:
        for r in seed(conn):
            print(f"{r['file']}: {r['organizations']} orgs, {r['created']} created, {r['updated']} updated, "
                  f"{r['unchanged']} unchanged"
                  + (f", {len(r['errors'])} errors" if r["errors"] else ""))
            for e in r["errors"]:
                print("   !", e)
        print(repo.stats(conn))
        print(f"papers: {papers.count(conn)}")


if __name__ == "__main__":
    main()
