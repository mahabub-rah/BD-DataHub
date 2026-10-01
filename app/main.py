"""BD DataHub — FastAPI application.

Run locally:  uvicorn app.main:app --reload
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.gzip import GZipMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import db, seed
from .config import settings
from .routers import papers as paper_pages, web
from .templating import templates

log = logging.getLogger("bd_data_hub")


@asynccontextmanager
async def lifespan(app: FastAPI):
    with db.session() as conn:
        # Every start: create missing tables, then load whatever in seed/ changed since the last start
        # (everything on a fresh database, nothing when the seed is unchanged).
        report = seed.sync(conn)
    for err in report["errors"]:
        log.warning("seed sync: %s", err)
    yield


app = FastAPI(
    title="BD DataHub",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
if settings.allowed_hosts:
    # Generated absolute URLs (sitemap, JSON-LD) come from the Host header, so pin it in production.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)
app.add_middleware(GZipMiddleware, minimum_size=1000)
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")

app.include_router(web.router)
app.include_router(paper_pages.router)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    return response


def _error_page(request: Request, status: int, detail: str):
    return templates.TemplateResponse(request, "error.html", {"status": status, "detail": detail}, status_code=status)


@app.exception_handler(StarletteHTTPException)
async def http_error(request: Request, exc: HTTPException):
    return _error_page(request, exc.status_code, exc.detail)


@app.exception_handler(404)
async def not_found(request: Request, exc):
    return _error_page(request, 404, "Page not found")
