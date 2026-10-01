"""On-demand previews of machine-readable files (first rows of CSV / JSON).

Only the catalogued `download_url` of a dataset is ever fetched (no arbitrary URLs), robots.txt
is respected, at most PREVIEW_BYTES are read, and results are cached for CACHE_HOURS.
"""

from __future__ import annotations

import csv
import io
import json
import sqlite3
from urllib.parse import urlsplit

import httpx

from .monitor import Monitor
from .repository import now_iso

PREVIEW_BYTES = 512_000
MAX_ROWS = 25
MAX_COLS = 30
CACHE_HOURS = 24


def previewable(ds: dict) -> bool:
    url = ds.get("download_url") or ""
    path = urlsplit(url).path.lower()
    return bool(url) and (path.endswith((".csv", ".json", ".tsv")) or bool({"CSV", "JSON"} & set(ds.get("formats", []))))


def _parse(body: bytes, content_type: str, url: str) -> dict:
    text = body.decode("utf-8-sig", errors="replace")
    ct = content_type.lower()
    path = urlsplit(url).path.lower()
    looks_json = "json" in ct or path.endswith(".json") or text.lstrip()[:1] in "[{"
    if looks_json:
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        if data is not None:
            records = data
            if isinstance(data, dict):
                records = next((v for v in data.values() if isinstance(v, list)), [data])
            if isinstance(records, list) and len(records) >= 2 and isinstance(records[0], list) \
                    and all(isinstance(x, dict) for x in records[1]):
                records = records[1]  # World Bank style [meta, rows]
            if isinstance(records, list) and records and all(isinstance(r, dict) for r in records[:MAX_ROWS]):
                cols = list(dict.fromkeys(k for r in records[:MAX_ROWS] for k in r))[:MAX_COLS]
                rows = [[_cell(r.get(c)) for c in cols] for r in records[:MAX_ROWS]]
                return {"status": "ok", "kind": "json", "columns": cols, "rows": rows,
                        "truncated": len(records) > MAX_ROWS}
            return {"status": "unsupported", "note": "JSON is not a list of records"}
    if b"\x00" in body[:2048] or body[:2] == b"PK":
        return {"status": "unsupported", "note": "Binary or compressed file (e.g. ZIP/XLSX); open it from the source."}
    sample = text[:20_000]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    lines = text.splitlines()
    if len(body) >= PREVIEW_BYTES and lines:
        lines = lines[:-1]  # last line may be cut off
    reader = csv.reader(io.StringIO("\n".join(lines)), dialect)
    rows = []
    try:
        for i, row in enumerate(reader):
            rows.append(row[:MAX_COLS])
            if i > MAX_ROWS:
                break
    except csv.Error:
        if len(rows) < 2:
            return {"status": "unsupported", "note": "The file could not be read as a table."}
    if len(rows) < 2 or max(len(r) for r in rows) < 2:
        return {"status": "unsupported", "note": "The file does not look like a table."}
    header, body_rows = rows[0], rows[1:MAX_ROWS + 1]
    # HXL hashtag row (common on HDX) → skip it
    if body_rows and all((c or "").startswith("#") or not c for c in body_rows[0]):
        body_rows = body_rows[1:]
    return {"status": "ok", "kind": "csv", "columns": header, "rows": body_rows,
            "truncated": len(rows) > MAX_ROWS or len(body) >= PREVIEW_BYTES}


def _cell(v):
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)[:200]
    return "" if v is None else str(v)[:200]


async def fetch_preview(url: str) -> dict:
    mon = Monitor(host_delay=0)
    async with mon._client() as client:
        if not await mon._allowed(client, url):
            return {"status": "blocked", "note": "The source's robots.txt does not allow automated access."}
        try:
            async with client.stream("GET", url) as resp:
                if resp.status_code >= 400:
                    return {"status": "error", "note": f"Source returned HTTP {resp.status_code}"}
                chunks, size = [], 0
                async for chunk in resp.aiter_bytes():
                    chunks.append(chunk)
                    size += len(chunk)
                    if size >= PREVIEW_BYTES:
                        break
                return _parse(b"".join(chunks)[:PREVIEW_BYTES], resp.headers.get("content-type", ""), str(resp.url))
        except httpx.HTTPError as exc:
            return {"status": "error", "note": f"Could not reach the source ({type(exc).__name__})."}


async def get_preview(conn: sqlite3.Connection, ds: dict) -> dict:
    if not previewable(ds):
        return {"status": "unsupported", "note": "No machine-readable direct download for this dataset."}
    row = conn.execute(
        "SELECT * FROM dataset_previews WHERE dataset_id=? AND fetched_at > datetime('now', ?)",
        (ds["id"], f"-{CACHE_HOURS} hours")).fetchone()
    if row:
        result = {"status": row["status"], "kind": row["kind"], "columns": json.loads(row["columns"] or "[]"),
                  "rows": json.loads(row["rows"] or "[]"), "truncated": bool(row["truncated"]), "note": row["note"],
                  "fetched_at": row["fetched_at"], "cached": True}
    else:
        result = await fetch_preview(ds["download_url"])
        result["fetched_at"] = now_iso()
        result["cached"] = False
        if result["status"] == "error":
            return result  # transient (timeout, 5xx): don't cache, retry on the next visit
        conn.execute(
            "INSERT OR REPLACE INTO dataset_previews (dataset_id, fetched_at, status, kind, columns, rows, truncated, note) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (ds["id"], result["fetched_at"], result["status"], result.get("kind"),
             json.dumps(result.get("columns", []), ensure_ascii=False),
             json.dumps(result.get("rows", []), ensure_ascii=False), int(result.get("truncated", False)),
             result.get("note")))
        conn.commit()
    return result
