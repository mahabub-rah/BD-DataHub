"""Research papers about Bangladesh. Metadata comes from OpenAlex; we link out to the full text."""

from __future__ import annotations

import dataclasses
import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from starlette.datastructures import QueryParams

from .. import citation, db, papers, repository as repo
from ..templating import templates
from .params import search_params

router = APIRouter(include_in_schema=False)

PER_PAGE = 20
MAX_PAGE = 500
SORTS = {"relevance": "Best match", "citations": "Most cited", "year": "Newest first"}
CITE_STYLES = {"apa": "APA 7th", "mla": "MLA 9th", "bibtex": "BibTeX", "ris": "RIS"}
GROUP_SIZE = 5  # results per group on /search and datasets shown on a research category page


def _year(value: str | None) -> int | None:
    """Year from a query string; blanks and junk are ignored rather than rejected with a 422."""
    try:
        year = int(value) if value else None
    except ValueError:
        return None
    return year if year and 1800 <= year <= 2100 else None


def _doi_url(doi: str | None) -> str | None:
    if not doi:
        return None
    return doi if doi.startswith("http") else f"https://doi.org/{doi}"


def _paper_query(q, year_from, year_to, topic, oa, venue, category, sort, page) -> tuple[dict, int]:
    """Normalised filters for papers.search, plus the page number; bad values are dropped, never a 422."""
    params = {
        "q": (q or "").strip()[:200] or None,
        "year_from": _year(year_from),
        "year_to": _year(year_to),
        "topic": topic or None,
        "oa": oa in ("1", "true", "on"),  # off means "no filter", never "closed access only"
        "venue": venue or None,
        "category": category or None,
        "sort": sort if sort in SORTS else "relevance",
    }
    try:
        page_n = min(max(int(page or 1), 1), MAX_PAGE)
    except ValueError:
        page_n = 1
    return params, page_n


def _run_search(conn, params: dict, page_n: int) -> dict:
    res = papers.search(conn, page=page_n, per_page=PER_PAGE, **{**params, "oa": True if params["oa"] else None})
    # papers.search clamps past-the-end pages to the last one; keep its numbers, only adding our MAX_PAGE cap.
    res["pages"] = min(res["pages"], MAX_PAGE)
    res["page"] = min(res["page"], res["pages"])
    return res


@router.get("/papers", response_class=HTMLResponse)
def paper_search(request: Request, q: str | None = None, year_from: str | None = None, year_to: str | None = None,
                 topic: str | None = None, oa: str | None = None, venue: str | None = None,
                 category: str | None = None, sort: str = "relevance", page: str | None = None,
                 conn: sqlite3.Connection = Depends(db.get_db)):
    params, page_n = _paper_query(q, year_from, year_to, topic, oa, venue, category, sort, page)
    res = _run_search(conn, params, page_n)
    filtered = any(params[k] for k in ("q", "year_from", "year_to", "topic", "oa", "venue", "category"))
    category_names = {slug: name for slug, name, _ in papers.category_counts(conn)}
    return templates.TemplateResponse(request, "papers.html", {
        "p": params, "res": res, "sorts": SORTS, "filtered": filtered, "doi_url": _doi_url,
        "category_names": category_names,
    })


RESEARCH_SAMPLE = 3


@router.get("/research", response_class=HTMLResponse)
def research(request: Request, conn: sqlite3.Connection = Depends(db.get_db)):
    """Research by category: an index of all categories, then a few papers from each one that has any."""
    categories = papers.category_counts(conn)
    sections = [
        {"slug": slug, "name": name, "count": n,
         "cited": papers.in_category(conn, slug, RESEARCH_SAMPLE, order="citations"),
         "newest": papers.in_category(conn, slug, RESEARCH_SAMPLE, order="newest")}
        for slug, name, n in categories if n
    ]
    return templates.TemplateResponse(request, "research.html", {
        "categories": categories, "sections": sections, "total": papers.count(conn),
    })


@router.get("/research/{slug}", response_class=HTMLResponse)
def research_category(request: Request, slug: str, year_from: str | None = None, year_to: str | None = None,
                      oa: str | None = None, sort: str = "citations", page: str | None = None,
                      conn: sqlite3.Connection = Depends(db.get_db)):
    match = next(((s, name, n) for s, name, n in papers.category_counts(conn) if s == slug), None)
    if not match:
        raise HTTPException(404, "There is no research category with that name.")
    # No search words here, so "best match" means nothing: fall back to most cited.
    params, page_n = _paper_query(None, year_from, year_to, None, oa, None, slug,
                                  sort if sort != "relevance" else "citations", page)
    res = _run_search(conn, params, page_n)
    datasets = repo.search(conn, repo.SearchParams(categories=[slug], per_page=GROUP_SIZE))
    return templates.TemplateResponse(request, "research_category.html", {
        "category": {"slug": slug, "name": match[1], "count": match[2]},
        "p": params, "res": res, "sorts": SORTS, "datasets": datasets,
        "filtered": any(params[k] for k in ("year_from", "year_to", "oa")),
    })


@router.get("/papers/{openalex_id}", response_class=HTMLResponse)
def paper_detail(request: Request, openalex_id: str, conn: sqlite3.Connection = Depends(db.get_db)):
    paper = papers.get(conn, openalex_id)
    if not paper:
        raise HTTPException(404, "No paper with that ID is in the catalogue.")
    citations = {key: citation.cite(paper, key) for key in CITE_STYLES}
    return templates.TemplateResponse(request, "paper.html", {
        "paper": paper, "citations": citations, "styles": CITE_STYLES, "doi_url": _doi_url(paper.get("doi")),
    })


@router.get("/search", response_class=HTMLResponse)
def search_all(request: Request, q: str | None = None, conn: sqlite3.Connection = Depends(db.get_db)):
    """One search box, two result groups: catalogue datasets first, then research papers."""
    q = (q or "").strip()[:200]
    datasets = papers_res = None
    if q:
        # Same parsing as /datasets, so "See all" lands on exactly the same result set.
        params = dataclasses.replace(search_params(QueryParams({"q": q})), page=1, per_page=GROUP_SIZE)
        datasets = repo.search(conn, params)
        papers_res = papers.search(conn, q=q, page=1, per_page=GROUP_SIZE)
    return templates.TemplateResponse(request, "search_all.html", {
        "q": q, "datasets": datasets, "papers_res": papers_res,
    })
