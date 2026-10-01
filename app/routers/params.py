"""Parse search filters from query strings."""

from __future__ import annotations

from starlette.datastructures import QueryParams

from ..repository import SearchParams


def _multi(qp: QueryParams, key: str) -> list[str]:
    values: list[str] = []
    for raw in qp.getlist(key):
        values += [v.strip() for v in raw.split(",") if v.strip()]
    return list(dict.fromkeys(values))


def _bool(qp: QueryParams, key: str) -> bool | None:
    v = qp.get(key)
    if v is None or v == "":
        return None
    return v.lower() in {"1", "true", "yes", "on"}


def _int(qp: QueryParams, key: str, default: int | None = None) -> int | None:
    try:
        v = int(qp.get(key)) if qp.get(key) not in (None, "") else default
    except ValueError:
        return default
    # Keep values inside SQLite's integer range (page offsets, years).
    return v if v is None or -1_000_000 <= v <= 1_000_000 else default


def search_params(qp: QueryParams, per_page_default: int = 20) -> SearchParams:
    return SearchParams(
        q=(qp.get("q") or "").strip()[:300],
        categories=_multi(qp, "category"),
        organizations=_multi(qp, "organization"),
        formats=_multi(qp, "format"),
        levels=_multi(qp, "level"),
        frequencies=_multi(qp, "frequency"),
        verification=_multi(qp, "verification"),
        link_status=_multi(qp, "link_status"),
        source_type=qp.get("source_type") or None,
        machine_readable=_bool(qp, "machine_readable"),
        year_from=_int(qp, "year_from"),
        year_to=_int(qp, "year_to"),
        language=qp.get("language") or None,
        sort=qp.get("sort") or "relevance",
        page=_int(qp, "page", 1) or 1,
        per_page=_int(qp, "per_page", per_page_default) or per_page_default,
        smart=_bool(qp, "smart") is not False,
    )
