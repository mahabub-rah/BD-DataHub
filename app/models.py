"""Pydantic models for validating catalog records."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from . import vocab

CURRENT_YEAR = date.today().year


def _check_url(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    if not value.lower().startswith(("http://", "https://")):
        raise ValueError("must be an http(s) URL")
    return value.replace(" ", "%20")


class OrganizationIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    slug: str | None = None
    name: str = Field(min_length=2, max_length=200)
    name_bn: str | None = None
    acronym: str | None = Field(default=None, max_length=30)
    org_type: str | None = None
    parent: str | None = None
    website: str | None = None
    description: str | None = None

    @field_validator("website")
    @classmethod
    def _url(cls, v):
        return _check_url(v)

    @field_validator("org_type")
    @classmethod
    def _org_type(cls, v):
        if v and v not in vocab.ORG_TYPES:
            raise ValueError(f"org_type must be one of {vocab.ORG_TYPES}")
        return v or None


class DatasetIn(BaseModel):
    """Metadata record for one dataset. Used by the seed loader and harvester."""

    model_config = ConfigDict(extra="ignore")

    slug: str | None = None
    title: str = Field(min_length=3, max_length=300)
    organization: str = Field(description="Organization slug")
    description: str = Field(default="", max_length=5000)
    source_url: str
    download_url: str | None = None
    categories: list[str] = Field(default_factory=list, description="Top-level category names or slugs")
    subcategories: list[str] = Field(default_factory=list)
    formats: list[str] = Field(default_factory=list)
    geographic_coverage: str = "Bangladesh"
    geographic_level: str | None = None
    temporal_start: int | None = Field(default=None, ge=1800, le=CURRENT_YEAR + 10)
    temporal_end: int | None = Field(default=None, ge=1800, le=CURRENT_YEAR + 10)
    update_frequency: str = "Unknown"
    license: str | None = None
    languages: list[str] = Field(default_factory=lambda: ["en"])
    tags: list[str] = Field(default_factory=list)
    verification_status: Literal["verified", "unverified", "needs_review", "deprecated"] = "unverified"
    evidence_url: str | None = None
    evidence_note: str | None = None
    checked_at: str | None = None
    verified_by: str | None = None
    is_published: bool = True

    @field_validator("source_url", "download_url", "evidence_url")
    @classmethod
    def _urls(cls, v):
        return _check_url(v)

    @field_validator("formats")
    @classmethod
    def _formats(cls, v):
        lookup = {f.lower(): f for f in vocab.FORMATS}
        out = []
        for f in v:
            key = f.strip().lower()
            if key not in lookup:
                raise ValueError(f"unknown format {f!r}; allowed: {vocab.FORMATS}")
            if lookup[key] not in out:
                out.append(lookup[key])
        return out

    @field_validator("update_frequency")
    @classmethod
    def _freq(cls, v):
        lookup = {f.lower(): f for f in vocab.FREQUENCIES}
        key = (v or "Unknown").strip().lower()
        if key not in lookup:
            raise ValueError(f"update_frequency must be one of {vocab.FREQUENCIES}")
        return lookup[key]

    @field_validator("geographic_level")
    @classmethod
    def _level(cls, v):
        if not v:
            return None
        lookup = {f.lower(): f for f in vocab.GEOGRAPHIC_LEVELS}
        if v.strip().lower() not in lookup:
            raise ValueError(f"geographic_level must be one of {vocab.GEOGRAPHIC_LEVELS}")
        return lookup[v.strip().lower()]

    @field_validator("tags", "subcategories")
    @classmethod
    def _clean_list(cls, v):
        seen, out = set(), []
        for item in v:
            item = " ".join(str(item).split())
            if item and item.lower() not in seen:
                seen.add(item.lower())
                out.append(item)
        return out

    @field_validator("languages")
    @classmethod
    def _langs(cls, v):
        out = [x.strip().lower() for x in v if x and x.strip()]
        return out or ["en"]

    @model_validator(mode="after")
    def _years(self):
        if self.temporal_start and self.temporal_end and self.temporal_start > self.temporal_end:
            raise ValueError("temporal_start must not be after temporal_end")
        return self

