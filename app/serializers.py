"""Convert internal records into schema.org markup."""

from __future__ import annotations

from typing import Any


def dataset_jsonld(d: dict, base_url: str) -> dict:
    """schema.org/Dataset markup so search engines (e.g. Google Dataset Search) can index entries."""
    base = base_url.rstrip("/")
    org = d["organization"]
    ld: dict[str, Any] = {
        "@context": "https://schema.org",
        "@type": "Dataset",
        "name": d["title"],
        "description": d["description"] or d["title"],
        "url": f"{base}/datasets/{d['slug']}",
        "sameAs": d["source_url"],
        "keywords": d["tags"] + [c["name"] for c in d["categories"]],
        "isAccessibleForFree": True,
        "creator": {"@type": "Organization", "name": org["name"], **({"url": org["website"]} if org["website"] else {})},
        "publisher": {"@type": "Organization", "name": org["name"]},
        "spatialCoverage": {"@type": "Place", "name": d["geographic_coverage"] or "Bangladesh"},
        "inLanguage": d["languages"],
    }
    if d["temporal_start"] or d["temporal_end"]:
        ld["temporalCoverage"] = f"{d['temporal_start'] or '..'}/{d['temporal_end'] or '..'}"
    if d["license"]:
        ld["license"] = d["license"]
    if d["download_url"]:
        ld["distribution"] = [{"@type": "DataDownload", "contentUrl": d["download_url"],
                               "encodingFormat": d["formats"][0] if d["formats"] else None}]
    return ld
