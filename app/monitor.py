"""Source monitoring: detect broken links and changes at dataset sources.

Designed to be polite and lawful: it only reads publicly accessible pages, honours
robots.txt, identifies itself with a descriptive User-Agent, throttles requests per
host, and never downloads more than a small, capped amount of each response.

Run from the command line:  python -m app.monitor [--slug SLUG] [--stale-hours 24]
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import httpx

from . import db
from .config import BASE_DIR, settings
from .repository import audit, now_iso

log = logging.getLogger(__name__)

MAX_BYTES = 2_000_000
BROKEN_AFTER_FAILURES = 3
_SENSITIVE_QUERY_KEY = re.compile(
    r"(?:api[_-]?key|access[_-]?key|secret|token|signature|credential|password|auth)",
    re.IGNORECASE,
)


def redact_url_credentials(url: str | None) -> str | None:
    """Remove credentials and signed-auth parameters before storing a URL."""
    if not url:
        return url
    parts = urlsplit(url)
    query = [
        (key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("x-amz-") and not _SENSITIVE_QUERY_KEY.search(key)
    ]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


@dataclass
class CheckResult:
    url: str
    status: str                  # ok | redirected | broken | blocked | error
    http_status: int | None = None
    final_url: str | None = None
    fingerprint: str | None = None
    elapsed_ms: int | None = None
    error: str | None = None


class _LinkCollector(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links: set[str] = set()
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href")
            if href and not href.startswith(("#", "javascript:", "mailto:")):
                self.links.add(href.strip())
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data


def fingerprint(content: bytes, content_type: str, headers: httpx.Headers) -> str:
    """A change signal that ignores noise (timestamps, visitor counters, ads).

    HTML pages are fingerprinted by title + the set of linked documents, which changes
    when publications are added/removed. Files use validators or a content hash.
    """
    if "html" in content_type:
        parser = _LinkCollector()
        try:
            parser.feed(content.decode("utf-8", errors="replace"))
        except Exception:  # malformed HTML: fall back to raw hash
            return "raw:" + hashlib.sha256(content).hexdigest()[:32]
        # Drop session/cache-busting query strings.
        links = sorted({re.sub(r"[?&](sid|session|token|v|_)=[^&]*", "", link) for link in parser.links})
        basis = parser.title.strip() + "\n" + "\n".join(links)
        return "html:" + hashlib.sha256(basis.encode()).hexdigest()[:32]
    if headers.get("etag"):
        return "etag:" + headers["etag"].strip('W/"')
    if headers.get("last-modified"):
        return "lm:" + headers["last-modified"]
    return "sha:" + hashlib.sha256(content).hexdigest()[:32]


class Monitor:
    def __init__(self, host_delay: float | None = None):
        self.user_agent = settings.monitor_user_agent
        self.sem = asyncio.Semaphore(settings.monitor_concurrency)
        self.timeout = settings.monitor_timeout
        self.host_delay = settings.monitor_host_delay if host_delay is None else host_delay
        self._robots: dict[str, RobotFileParser | None] = {}
        self._host_locks: dict[str, asyncio.Lock] = {}
        self._host_last: dict[str, float] = {}

    def _client(self, verify: bool = True) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            headers={"User-Agent": self.user_agent, "Accept": "*/*"},
            timeout=self.timeout, follow_redirects=True, verify=verify,
        )

    async def _throttle(self, host: str) -> None:
        lock = self._host_locks.setdefault(host, asyncio.Lock())
        async with lock:
            wait = self._host_last.get(host, 0) + self.host_delay - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._host_last[host] = time.monotonic()

    async def _allowed(self, client: httpx.AsyncClient, url: str) -> bool:
        parts = urlsplit(url)
        root = f"{parts.scheme}://{parts.netloc}"
        if root not in self._robots:
            rp = None
            try:
                await self._throttle(parts.netloc)
                r = await client.get(root + "/robots.txt")
                if r.status_code == 200 and "text" in r.headers.get("content-type", "text"):
                    rp = RobotFileParser()
                    rp.parse(r.text.splitlines())
            except httpx.HTTPError:
                rp = None  # robots unreachable → treat as allowed (standard behaviour)
            self._robots[root] = rp
        rp = self._robots[root]
        return rp is None or rp.can_fetch(self.user_agent, url)

    async def check(self, url: str) -> CheckResult:
        async with self.sem:
            try:
                return await self._check(url, verify=True)
            except httpx.ConnectError as exc:
                if "CERTIFICATE" in str(exc).upper() or "SSL" in str(exc).upper():
                    # Several government sites have misconfigured TLS; the page itself may still be live.
                    res = await self._check(url, verify=False)
                    res.error = "TLS certificate problem on source site"
                    return res
                raise

    async def _check(self, url: str, verify: bool) -> CheckResult:
        started = time.monotonic()
        host = urlsplit(url).netloc
        async with self._client(verify=verify) as client:
            try:
                if not await self._allowed(client, url):
                    return CheckResult(url, "blocked", error="Disallowed by robots.txt")
                await self._throttle(host)
                async with client.stream("GET", url) as resp:
                    chunks, size = [], 0
                    async for chunk in resp.aiter_bytes():
                        chunks.append(chunk)
                        size += len(chunk)
                        if size >= MAX_BYTES:
                            break
                    body = b"".join(chunks)
            except httpx.ConnectError:
                raise
            except httpx.TimeoutException:
                return CheckResult(url, "error", error="Timed out",
                                   elapsed_ms=int((time.monotonic() - started) * 1000))
            except httpx.HTTPError as exc:
                return CheckResult(url, "error", error=f"{type(exc).__name__}: {exc}"[:300],
                                   elapsed_ms=int((time.monotonic() - started) * 1000))
        elapsed = int((time.monotonic() - started) * 1000)
        code = resp.status_code
        final = redact_url_credentials(str(resp.url))
        if code in (404, 410):
            status = "broken"
        elif code in (401, 403, 429):
            status = "blocked"
        elif code >= 400:
            status = "error"
        elif _moved(url, final):
            status = "redirected"
        else:
            status = "ok"
        fp = fingerprint(body, resp.headers.get("content-type", ""), resp.headers) if code < 400 else None
        return CheckResult(url, status, code, final, fp, elapsed)


def _moved(original: str, final: str) -> bool:
    """A redirect only matters when it leaves the original host or path (not http→https or slash tweaks)."""
    a, b = urlsplit(original), urlsplit(final)
    host = lambda s: s.netloc.lower().removeprefix("www.")  # noqa: E731
    return host(a) != host(b) or a.path.rstrip("/") != b.path.rstrip("/")


def _record(conn: sqlite3.Connection, dataset: sqlite3.Row, result: CheckResult, is_source: bool) -> bool:
    changed = False
    if is_source:
        prev_fp = dataset["content_fingerprint"]
        changed = bool(result.fingerprint and prev_fp and result.fingerprint != prev_fp)
        status = result.status
        if status == "error":
            # Transient failures only become "broken" after repeated consecutive failures.
            recent = [r[0] for r in conn.execute(
                "SELECT status FROM link_checks WHERE dataset_id=? AND url=? ORDER BY id DESC LIMIT ?",
                (dataset["id"], result.url, BROKEN_AFTER_FAILURES - 1))]
            if len(recent) == BROKEN_AFTER_FAILURES - 1 and all(s in ("error", "broken") for s in recent):
                status = "broken"
        conn.execute(
            """UPDATE datasets SET link_status=?, http_status=?, link_checked_at=?,
                   content_fingerprint=COALESCE(?, content_fingerprint),
                   source_changed_at=CASE WHEN ? THEN ? ELSE source_changed_at END
               WHERE id=?""",
            (status, result.http_status, now_iso(), result.fingerprint, int(changed), now_iso(), dataset["id"]),
        )
        result.status = status
    conn.execute(
        """INSERT INTO link_checks (dataset_id, url, status, http_status, final_url, fingerprint, changed,
                                    elapsed_ms, error) VALUES (?,?,?,?,?,?,?,?,?)""",
        (dataset["id"], result.url, result.status, result.http_status, redact_url_credentials(result.final_url),
         result.fingerprint,
         int(changed), result.elapsed_ms, result.error),
    )
    return changed


async def run(conn: sqlite3.Connection, slugs: list[str] | None = None, stale_hours: float | None = None,
              include_downloads: bool = True) -> dict:
    """Check dataset sources and record results. Returns a summary."""
    monitor = Monitor()
    sql = "SELECT * FROM datasets WHERE is_published=1"
    args: list = []
    if slugs:
        sql += f" AND slug IN ({','.join('?' * len(slugs))})"
        args += slugs
    if stale_hours is not None:
        sql += " AND (link_checked_at IS NULL OR link_checked_at < datetime('now', ?))"
        args.append(f"-{stale_hours} hours")
    datasets = conn.execute(sql, args).fetchall()

    jobs = []
    for ds in datasets:
        jobs.append((ds, ds["source_url"], True))
        if include_downloads and ds["download_url"] and ds["download_url"] != ds["source_url"]:
            jobs.append((ds, ds["download_url"], False))

    async def one(ds, url, is_source):
        try:
            res = await monitor.check(url)
        except Exception as exc:  # e.g. httpx.InvalidURL for a malformed catalogued URL
            res = CheckResult(url, "error", error=f"{type(exc).__name__}: {exc}"[:300])
        return ds, res, is_source

    summary = {"checked": 0, "ok": 0, "redirected": 0, "broken": 0, "blocked": 0, "error": 0, "changed": 0}
    # Record each result as soon as it arrives so progress is visible and partial runs are kept.
    for fut in asyncio.as_completed([one(*j) for j in jobs]):
        ds, res, is_source = await fut
        if _record(conn, ds, res, is_source):
            summary["changed"] += 1
        # Commit each result: page views also write to the database and must not wait on a
        # transaction held open across the next network request.
        conn.commit()
        summary["checked"] += 1
        summary[res.status] = summary.get(res.status, 0) + 1
    audit(conn, "monitor", "run", "monitor", None, summary)
    conn.commit()
    return summary


# Check results shipped with the code. Hosts without a persistent disk (Render's free plan)
# rebuild the database from seed/ on every start, so results from a local run are exported
# here, committed, and loaded back at boot by seed.sync().
SNAPSHOT_PATH = BASE_DIR / "seed" / "link_checks.json"
SNAPSHOT_CHECKS_PER_URL = 5  # the dataset page shows the last 5; "broken" needs the last 2


def export_snapshot(conn: sqlite3.Connection, path: Path = SNAPSHOT_PATH) -> int:
    """Write current link state and recent check history to the seed snapshot. Returns datasets written."""
    out: dict = {}
    rows = conn.execute(
        "SELECT id, slug, source_url, link_status, http_status, link_checked_at, content_fingerprint, "
        "source_changed_at FROM datasets WHERE link_checked_at IS NOT NULL ORDER BY slug").fetchall()
    for ds in rows:
        checks = conn.execute(
            """SELECT url, checked_at, status, http_status, final_url, fingerprint, changed, elapsed_ms, error
               FROM (SELECT *, ROW_NUMBER() OVER (PARTITION BY url ORDER BY id DESC) AS n
                     FROM link_checks WHERE dataset_id=?)
               WHERE n <= ? ORDER BY id""", (ds["id"], SNAPSHOT_CHECKS_PER_URL)).fetchall()
        entry = {k: ds[k] for k in ("source_url", "link_status", "http_status", "link_checked_at",
                                    "content_fingerprint", "source_changed_at")}
        entry["checks"] = [
            {
                **{k: v for k, v in dict(c).items() if v is not None},
                "final_url": redact_url_credentials(c["final_url"]),
            }
            for c in checks
        ]
        out[ds["slug"]] = entry
    data = {"exported_at": now_iso(), "datasets": out}
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    tmp.replace(path)  # atomic, so a booting app never reads a half-written file
    return len(out)


def load_snapshot(conn: sqlite3.Connection, path: Path = SNAPSHOT_PATH) -> int:
    """Apply the seed snapshot to datasets it is newer for. Returns datasets updated.

    Skips a dataset when the database already has a check at least as recent, or when its source
    URL changed since the snapshot was taken (those results describe a different link).
    """
    if not path.exists():
        return 0
    data = json.loads(path.read_text(encoding="utf-8"))
    applied = 0
    for slug, snap in data.get("datasets", {}).items():
        ds = conn.execute("SELECT id, source_url, link_checked_at FROM datasets WHERE slug=?", (slug,)).fetchone()
        if not ds or ds["source_url"] != snap.get("source_url") or not snap.get("link_checked_at"):
            continue
        if ds["link_checked_at"] and ds["link_checked_at"] >= snap["link_checked_at"]:
            continue
        conn.execute(
            """UPDATE datasets SET link_status=?, http_status=?, link_checked_at=?, content_fingerprint=?,
                   source_changed_at=? WHERE id=?""",
            (snap["link_status"], snap.get("http_status"), snap["link_checked_at"],
             snap.get("content_fingerprint"), snap.get("source_changed_at"), ds["id"]))
        # Only append checks newer than the stored ones, oldest first, so "ORDER BY id" stays chronological.
        latest = conn.execute("SELECT MAX(checked_at) FROM link_checks WHERE dataset_id=?",
                              (ds["id"],)).fetchone()[0] or ""
        for c in sorted(snap.get("checks", []), key=lambda c: c["checked_at"]):
            if c["checked_at"] > latest:
                conn.execute(
                    """INSERT INTO link_checks (dataset_id, url, checked_at, status, http_status, final_url,
                                                fingerprint, changed, elapsed_ms, error)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (ds["id"], c["url"], c["checked_at"], c["status"], c.get("http_status"), c.get("final_url"),
                     c.get("fingerprint"), c.get("changed", 0), c.get("elapsed_ms"), c.get("error")))
        applied += 1
    return applied


# Runs started from the website ("Check sources now" on the homepage). The button is public,
# so only one run may be active per process and a cooldown stops it being used to hammer
# publisher sites. The cooldown is based on the stored check times (including those loaded
# from the seed snapshot), so it survives restarts.
_web_task: asyncio.Task | None = None


def refresh_state(conn: sqlite3.Connection) -> dict:
    """Whether a web-triggered run is active and, if on cooldown, when the next one is allowed."""
    running = _web_task is not None and not _web_task.done()
    next_at = None
    last = conn.execute("SELECT MAX(link_checked_at) FROM datasets").fetchone()[0]
    if last and settings.refresh_cooldown_hours > 0:
        allowed = datetime.strptime(last[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc) \
            + timedelta(hours=settings.refresh_cooldown_hours)
        if allowed > datetime.now(timezone.utc):
            next_at = allowed.strftime("%Y-%m-%d %H:%M:%S")
    return {"running": running, "next_at": next_at, "last_checked": last}


async def _web_run() -> None:
    with db.session() as conn:
        await run(conn)


def start_web_run(conn: sqlite3.Connection) -> None:
    """Start a background check unless one is running or on cooldown."""
    global _web_task
    state = refresh_state(conn)
    if not state["running"] and not state["next_at"]:
        _web_task = asyncio.create_task(_web_run())
        _web_task.add_done_callback(_log_failure)


def _log_failure(task: asyncio.Task) -> None:
    if not task.cancelled() and task.exception():
        log.error("Web-triggered source check failed", exc_info=task.exception())


def main() -> None:
    parser = argparse.ArgumentParser(description="Check dataset source links for breakage and changes.")
    parser.add_argument("--slug", action="append", help="Only check this dataset (repeatable)")
    parser.add_argument("--stale-hours", type=float, default=None,
                        help="Only check datasets not checked within this many hours")
    parser.add_argument("--no-downloads", action="store_true", help="Skip direct download URLs")
    parser.add_argument("--no-export", action="store_true",
                        help=f"Do not write the results to {SNAPSHOT_PATH.relative_to(BASE_DIR)}")
    parser.add_argument("--export-only", action="store_true",
                        help="Write the snapshot from the database without checking any links")
    args = parser.parse_args()
    with db.session() as conn:
        db.init_schema(conn)
        if not args.export_only:
            print(asyncio.run(run(conn, args.slug, args.stale_hours, include_downloads=not args.no_downloads)))
        if not args.no_export:
            n = export_snapshot(conn)
            print(f"wrote {n} datasets to {SNAPSHOT_PATH.relative_to(BASE_DIR)}")


if __name__ == "__main__":
    main()
