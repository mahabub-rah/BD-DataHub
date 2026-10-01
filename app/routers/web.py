"""Public HTML pages."""

from __future__ import annotations

import json
import sqlite3
from urllib.parse import quote, urlsplit
from xml.sax.saxutils import escape as xml_escape

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse

from .. import citation, db, monitor, papers, preview, repository as repo, serializers
from ..templating import templates
from .params import search_params

router = APIRouter(include_in_schema=False)


def render(request: Request, name: str, **ctx) -> HTMLResponse:
    return templates.TemplateResponse(request, name, ctx)


def _script_json(data) -> str:
    """JSON that is safe inside a <script> element (no </script> break-out)."""
    return (json.dumps(data, ensure_ascii=False)
            .replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026"))


@router.get("/", response_class=HTMLResponse)
def home(request: Request, conn: sqlite3.Connection = Depends(db.get_db)):
    return render(
        request, "home.html",
        stats=repo.stats(conn),
        categories=repo.category_tree(conn),
        recent=repo.recent_datasets(conn, 6),
        paper_count=papers.count(conn),
        top_papers=papers.top(conn, 5),
    )


@router.post("/refresh")
async def refresh(request: Request, conn: sqlite3.Connection = Depends(db.get_db)):
    origin = urlsplit(request.headers.get("origin") or request.headers.get("referer") or "")
    if not origin.netloc or origin.netloc == request.url.netloc:
        monitor.start_web_run(conn)
    # Return to the page the button was pressed on (same-site paths only).
    ref = urlsplit(request.headers.get("referer", ""))
    back = ref.path if ref.path.startswith("/") and not ref.path.startswith("//") else "/"
    if ref.query:
        back += "?" + ref.query
    return RedirectResponse(back, status_code=303)


@router.get("/datasets", response_class=HTMLResponse)
def datasets(request: Request, conn: sqlite3.Connection = Depends(db.get_db)):
    params = search_params(request.query_params)
    res = repo.search(conn, params)
    context = {}
    if len(params.categories) == 1:
        try:
            context["category"] = repo.get_category(conn, params.categories[0])
        except repo.NotFound:
            pass
        if "category" in context:
            # Papers use the top-level categories only, so a subcategory borrows its parent's research.
            cat = context["category"]
            top = cat["parent"] or cat
            context["research"] = {"slug": top["slug"], "name": top["name"],
                                   "papers": papers.for_category(conn, top["slug"], n=5)}
    if len(params.organizations) == 1:
        try:
            context["organization"] = repo.get_organization(conn, params.organizations[0])
        except repo.NotFound:
            pass
    labels = {r["slug"]: r["name"] for r in conn.execute("SELECT slug, name FROM categories")}
    labels |= {r["slug"]: r["acronym"] or r["name"] for r in conn.execute("SELECT slug, name, acronym FROM organizations")}
    return render(request, "search.html", res=res, p=params, sorts=repo.SORTS, labels=labels, **context)


@router.get("/datasets/{slug}", response_class=HTMLResponse)
async def dataset_detail(slug: str, request: Request, show_preview: bool = Query(False, alias="preview"),
                         conn: sqlite3.Connection = Depends(db.get_db)):
    try:
        ds = repo.get_dataset(conn, slug)
    except repo.NotFound:
        raise HTTPException(404, "Dataset not found")
    try:
        repo.record_view(conn, ds["id"])
    except sqlite3.OperationalError:
        pass  # view counts are best-effort; never fail the page over them
    checks = [dict(r) for r in conn.execute(
        "SELECT url, checked_at, status, http_status FROM link_checks WHERE dataset_id=? "
        "ORDER BY id DESC LIMIT 5", (ds["id"],))]
    cites = citation.all_citations(ds)
    previewable = preview.previewable(ds)
    return render(
        request, "dataset.html", ds=ds,
        related=repo.related_datasets(conn, ds, 6),
        citations=cites, styles=citation.STYLES, checks=checks, previewable=previewable,
        preview=await preview.get_preview(conn, ds) if previewable and show_preview else None,
        jsonld=_script_json(serializers.dataset_jsonld(ds, str(request.base_url))),
    )


@router.get("/datasets/{slug}/cite.{ext}")
def dataset_cite_file(slug: str, ext: str, conn: sqlite3.Connection = Depends(db.get_db)):
    style = {"bib": "bibtex", "ris": "ris"}.get(ext)
    if not style:
        raise HTTPException(404)
    try:
        ds = repo.get_dataset(conn, slug)
    except repo.NotFound:
        raise HTTPException(404)
    return PlainTextResponse(citation.cite(ds, style), headers={
        "Content-Disposition": f'attachment; filename="{slug}.{ext}"'})


@router.get("/organizations", response_class=HTMLResponse)
def organizations(request: Request, q: str | None = None, type: str | None = None,
                  conn: sqlite3.Connection = Depends(db.get_db)):
    orgs = repo.list_organizations(conn, type or None, q or None)
    return render(request, "organizations.html", orgs=orgs, q=q or "", org_type=type or "")


@router.get("/organizations/{slug}", response_class=HTMLResponse)
def organization_detail(slug: str, request: Request, conn: sqlite3.Connection = Depends(db.get_db)):
    try:
        org = repo.get_organization(conn, slug)
    except repo.NotFound:
        raise HTTPException(404, "Organization not found")
    res = repo.search(conn, repo.SearchParams(organizations=[slug], sort="title", per_page=100))
    return render(request, "organization.html", org=org, res=res)


@router.get("/categories", response_class=HTMLResponse)
def categories(request: Request, conn: sqlite3.Connection = Depends(db.get_db)):
    return render(request, "categories.html", categories=repo.category_tree(conn))


@router.get("/categories/{slug}")
def category_detail(slug: str):
    return RedirectResponse(f"/datasets?category={quote(slug, safe='')}", status_code=307)


@router.get("/about", response_class=HTMLResponse)
def about(request: Request, conn: sqlite3.Connection = Depends(db.get_db)):
    return render(request, "about.html", stats=repo.stats(conn))


@router.get("/healthz", response_class=PlainTextResponse)
def healthz(conn: sqlite3.Connection = Depends(db.get_db)):
    """Health check for the host: the app is up and its database answers."""
    conn.execute("SELECT 1 FROM datasets LIMIT 1").fetchone()
    return "ok"


@router.get("/robots.txt", response_class=PlainTextResponse)
def robots(request: Request):
    return f"User-agent: *\nSitemap: {str(request.base_url).rstrip('/')}/sitemap.xml\n"


SITEMAP_LIMIT = 50_000  # the sitemaps.org maximum for one file


@router.get("/sitemap.xml")
def sitemap(request: Request, conn: sqlite3.Connection = Depends(db.get_db)):
    base = str(request.base_url).rstrip("/")
    urls = [f"{base}/", f"{base}/datasets", f"{base}/organizations", f"{base}/categories", f"{base}/about",
            f"{base}/papers", f"{base}/research"]
    urls += [f"{base}/datasets/{r[0]}" for r in conn.execute("SELECT slug FROM datasets WHERE is_published=1")]
    # Organizations without a published dataset have an empty page, so they stay out.
    urls += [f"{base}/organizations/{r[0]}" for r in conn.execute(
        "SELECT o.slug FROM organizations o WHERE EXISTS "
        "(SELECT 1 FROM datasets d WHERE d.organization_id = o.id AND d.is_published = 1)")]
    urls += [f"{base}/research/{slug}" for slug, _, n in papers.category_counts(conn) if n]
    urls += [f"{base}/papers/{r[0]}" for r in conn.execute("SELECT openalex_id FROM papers ORDER BY id")]
    urls = urls[:SITEMAP_LIMIT]
    body = "".join(f"<url><loc>{xml_escape(u)}</loc></url>" for u in urls)
    return PlainTextResponse(
        f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{body}</urlset>',
        media_type="application/xml")
