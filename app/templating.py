"""Jinja2 environment, filters and helpers shared by the web views."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape
from starlette.datastructures import QueryParams

from . import db, monitor, vocab

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
env = templates.env

MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September",
          "October", "November", "December"]


BD_OFFSET = timedelta(hours=6)  # Bangladesh Standard Time, no DST


def _parse(ts: str | None) -> datetime | None:
    """Parse a stored UTC timestamp and convert it to Bangladesh time for display."""
    if not ts:
        return None
    for fmt, has_time in (("%Y-%m-%d %H:%M:%S", True), ("%Y-%m-%dT%H:%M:%S", True), ("%Y-%m-%d", False)):
        try:
            d = datetime.strptime(ts[:19] if has_time else ts[:10], fmt)
            return d + BD_OFFSET if has_time else d
        except ValueError:
            continue
    return None


def bdtime(ts: str | None) -> str:
    d = _parse(ts)
    return d.strftime("%d %b %Y, %H:%M") + " BST" if d else "—"


def bdclock(ts: str | None) -> str:
    """Short Bangladesh time: "20:57" today, "2 Oct, 02:57" on another day."""
    d = _parse(ts)
    if not d:
        return "—"
    if d.date() == (datetime.now(timezone.utc) + BD_OFFSET).date():
        return d.strftime("%H:%M")
    return f"{d.day} {d.strftime('%b')}, {d.strftime('%H:%M')}"


def longdate(ts: str | None) -> str:
    d = _parse(ts)
    return f"{d.day} {MONTHS[d.month - 1]} {d.year}" if d else "—"


def shortdate(ts: str | None) -> str:
    d = _parse(ts)
    return f"{d.day} {MONTHS[d.month - 1][:3]} {d.year}" if d else "—"


def compact_number(n: int | None) -> str:
    """1234 → '1,234'."""
    return f"{n or 0:,}"


def headline_number(n: int | None) -> str:
    """Hero-stat style: exact below 100, then rounded down with '+' (e.g. 1,234 → '1,200+')."""
    n = n or 0
    if n < 100:
        return str(n)
    step = 100 if n >= 1000 else 10
    floored = n // step * step
    return f"{floored:,}+" if floored != n else f"{n:,}"


def cite_html(text: str) -> Markup:
    """Render citation *italics* markers as <em> safely."""
    return Markup(re.sub(r"\*(.+?)\*", r"<em>\1</em>", str(escape(text))))


def host(url: str | None) -> str:
    return urlsplit(url).netloc.removeprefix("www.") if url else ""


def coverage(ds: dict) -> str:
    s, e = ds.get("temporal_start"), ds.get("temporal_end")
    if s and e:
        return str(s) if s == e else f"{s}–{e}"
    if s:
        return f"{s}–present"
    if e:
        return f"until {e}"
    return "Not specified"


def toggle_url(qp: QueryParams, key: str, value: str, path: str = "/datasets") -> str:
    """URL with `value` added to / removed from multi-valued filter `key`; resets to page 1."""
    items = [(k, v) for k, v in qp.multi_items() if k != "page"]
    if (key, value) in items:
        items.remove((key, value))
    else:
        if key in {"source_type", "sort", "machine_readable"}:
            items = [(k, v) for k, v in items if k != key]
        items.append((key, value))
    qs = urlencode(items)
    return f"{path}?{qs}" if qs else path


def with_params(qp: QueryParams, path: str = "/datasets", **changes) -> str:
    items = [(k, v) for k, v in qp.multi_items() if k not in changes]
    items += [(k, str(v)) for k, v in changes.items() if v is not None and v != ""]
    qs = urlencode(items)
    return f"{path}?{qs}" if qs else path


def source_check() -> dict:
    """State of the header "Check sources" button (running / cooldown / last check)."""
    with db.session() as conn:
        return monitor.refresh_state(conn)


def without(qp: QueryParams, *keys: str, path: str = "/datasets") -> str:
    items = [(k, v) for k, v in qp.multi_items() if k not in keys and k != "page"]
    qs = urlencode(items)
    return f"{path}?{qs}" if qs else path


env.filters.update(longdate=longdate, shortdate=shortdate, bdtime=bdtime, bdclock=bdclock, num=compact_number, headline=headline_number,
                   host=host, coverage=coverage, cite_html=cite_html)
env.globals.update(
    toggle_url=toggle_url, with_params=with_params, without=without, vocab=vocab,
    source_check=source_check,
    LINK=vocab.LINK_STATUSES,
)
