"""Citation-ready references for datasets and research papers (APA 7, MLA 9, Chicago, Harvard, BibTeX, RIS)."""

from __future__ import annotations

import re
from datetime import date

STYLES = {
    "apa": "APA 7th",
    "mla": "MLA 9th",
    "chicago": "Chicago",
    "harvard": "Harvard",
    "bibtex": "BibTeX",
    "ris": "RIS (Zotero / Mendeley / EndNote)",
}

_MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August",
           "September", "October", "November", "December"]
_MLA_MONTHS = ["Jan.", "Feb.", "Mar.", "Apr.", "May", "June", "July", "Aug.", "Sept.", "Oct.", "Nov.", "Dec."]


def _year(ds: dict) -> str:
    if ds.get("temporal_end"):
        return str(ds["temporal_end"])
    return "n.d."


def _coverage(ds: dict) -> str:
    s, e = ds.get("temporal_start"), ds.get("temporal_end")
    if s and e and s != e:
        return f"{s}–{e}"
    return str(s or e or "")


def _bib_escape(text: str) -> str:
    return re.sub(r"([&%$#_{}])", r"\\\1", text or "")


def cite(ds: dict, style: str, accessed: date | None = None) -> str:
    if "openalex_id" in ds:  # a research paper (app.papers), not a dataset
        return cite_paper(ds, style, accessed)
    accessed = accessed or date.today()
    org = ds["organization"]
    publisher = org["name"]
    author = publisher + (f" ({org['acronym']})" if org.get("acronym") and style in {"apa", "harvard"} else "")
    title = ds["title"]
    url = ds["source_url"]
    year = _year(ds)
    cov = _coverage(ds)
    acc_long = f"{accessed.day} {_MONTHS[accessed.month - 1]} {accessed.year}"
    acc_mla = f"{accessed.day} {_MLA_MONTHS[accessed.month - 1]} {accessed.year}"
    fmt_word = "Data set"

    if style == "apa":
        # APA 7 omits the publisher when it is the same as the author; retrieval date because data changes.
        return f"{author}. ({year}). *{title}* [{fmt_word}]. Retrieved {acc_long}, from {url}"
    if style == "mla":
        # MLA 9 skips the author when it is also the publisher.
        year_part = f", {year}" if year != "n.d." else ""
        return f"*{title}*. {publisher}{year_part}, {url}. Accessed {acc_mla}."
    if style == "chicago":
        return f"{publisher}. *{title}*. {'Dataset, ' + cov + '. ' if cov else 'Dataset. '}{publisher}, {year}. {url} (accessed {_MONTHS[accessed.month - 1]} {accessed.day}, {accessed.year})."
    if style == "harvard":
        return f"{author} ({year}) *{title}* [Dataset]. Available at: {url} (Accessed: {acc_long})."
    if style == "bibtex":
        # The slug is unique per dataset and stable, so the key is too (BibTeX-safe characters only).
        key = re.sub(r"[^a-z0-9-]+", "-", ds["slug"].lower()).strip("-") or f"dataset{ds['id']}"
        lines = [
            f"@misc{{{key},",
            f"  author       = {{{{{_bib_escape(publisher)}}}}},",
            f"  title        = {{{{{_bib_escape(title)}}}}},",
            f"  howpublished = {{\\url{{{url}}}}},",
            f"  publisher    = {{{_bib_escape(publisher)}}},",
        ]
        if year != "n.d.":
            lines.append(f"  year         = {{{year}}},")
        lines += [
            f"  note         = {{Dataset{', coverage ' + cov if cov else ''}. Accessed {accessed.isoformat()}}},",
            f"  urldate      = {{{accessed.isoformat()}}}",
            "}",
        ]
        return "\n".join(lines)
    if style == "ris":
        lines = ["TY  - DATA", f"AU  - {publisher}", f"TI  - {title}", f"PB  - {publisher}"]
        if year != "n.d.":
            lines.append(f"PY  - {year}")
        lines += [f"UR  - {url}", f"Y2  - {accessed.isoformat()}"]
        if ds.get("description"):
            lines.append(f"AB  - {ds['description'][:1000]}")
        for kw in ds.get("tags", [])[:10]:
            lines.append(f"KW  - {kw}")
        lines.append("ER  - ")
        return "\n".join(lines)
    raise ValueError(f"Unknown citation style {style!r}")


_GROUP_AUTHOR = re.compile(r"\b(team|group|collaborat\w*|consortium|investigators|committee|network|"
                           r"working|study|initiative|organi[sz]ation|on behalf|programme|program)\b", re.IGNORECASE)


def _name_parts(name: str) -> tuple[str, str]:
    """Split an OpenAlex display name ("Mohammad A. Rahman") into (surname, given names).

    Group authors ("the WHO Leishmaniasis Control Team", "GBD 2017 Collaborators") are kept whole.
    """
    parts = name.replace(",", " ").split()
    if len(parts) < 2 or _GROUP_AUTHOR.search(name):
        return name.strip(), ""
    return parts[-1], " ".join(parts[:-1])


def _initials(given: str) -> str:
    return " ".join(f"{p[0]}." for p in re.split(r"[\s.-]+", given) if p and p[0].isalpha())


def _join(names: list[str], last_sep: str) -> str:
    if len(names) <= 1:
        return "".join(names)
    if len(names) == 2:
        return f"{names[0]}{last_sep}{names[1]}"
    return ", ".join(names[:-1]) + f",{last_sep}{names[-1]}"


def _apa_authors(authors: list[str]) -> str:
    """APA 7 author list: "Surname, I." each; a comma before "&" (also with two authors); 21 or more
    authors give the first 19, an ellipsis and the last one; the list always ends with a period,
    so a group author reads "The WHO ACTION Trials Collaborators. (2020)."
    """
    names = [f"{s}, {_initials(g)}".rstrip(", ") for s, g in map(_name_parts, authors)]
    if not names:
        return ""
    if len(names) == 1:
        who = names[0]
    elif len(names) >= 21:
        who = ", ".join(names[:19]) + ", . . . " + names[-1]
    else:
        who = ", ".join(names[:-1]) + ", & " + names[-1]
    return who if who.endswith(".") else who + "."


def cite_paper(p: dict, style: str, accessed: date | None = None) -> str:
    """Reference for a research paper from app.papers (fields straight from OpenAlex; missing parts are left out)."""
    accessed = accessed or date.today()
    authors = p.get("authors") or []
    title = (p.get("title") or "").rstrip(".")
    venue = p.get("venue")
    year = str(p["year"]) if p.get("year") else "n.d."
    doi = (p.get("doi") or "").removeprefix("https://doi.org/") or None
    url = f"https://doi.org/{doi}" if doi else (p.get("landing_url") or p.get("oa_url") or "")
    acc_long = f"{accessed.day} {_MONTHS[accessed.month - 1]} {accessed.year}"

    if style == "apa":
        who = _apa_authors(authors)
        head = f"{who} ({year}). {title}." if who else f"{title}. ({year})."
        return f"{head}{f' *{venue}*.' if venue else ''}{f' {url}' if url else ''}"
    if style == "harvard":
        names = [f"{s}, {_initials(g)}".rstrip(", ") for s, g in map(_name_parts, authors)]
        who = _join(names, " and ") if names else ""
        head = f"{who} ({year}) '{title}'" if who else f"'{title}' ({year})"
        return f"{head}{f', *{venue}*' if venue else ''}.{f' Available at: {url} (Accessed: {acc_long}).' if url else ''}"
    if style == "mla":
        if not authors:
            who = ""
        else:
            s, g = _name_parts(authors[0])
            first = f"{s}, {g}".rstrip(", ")
            who = first if len(authors) == 1 else (f"{first}, and {authors[1]}" if len(authors) == 2 else f"{first}, et al")
        parts = [f"{who}." if who else "", f'"{title}."', f"*{venue}*," if venue else "",
                 f"{year}," if year != "n.d." else "", f"{url}." if url else ""]
        return " ".join(x for x in parts if x).rstrip(",").replace(",.", ".")
    if style == "chicago":
        who = _join(authors, " and ")
        return (f"{who + '. ' if who else ''}{year}. \"{title}.\"{f' *{venue}*.' if venue else ''}"
                f"{f' {url}.' if url else ''}")
    if style == "bibtex":
        first = re.sub(r"[^a-z]", "", _name_parts(authors[0])[0].lower()) if authors else "anon"
        key = f"{first}{year if year != 'n.d.' else ''}_{p['openalex_id'].lower()}"
        lines = [f"@article{{{key},"]
        if authors:
            lines.append(f"  author  = {{{_bib_escape(' and '.join(f'{s}, {g}'.rstrip(', ') for s, g in map(_name_parts, authors)))}}},")
        lines.append(f"  title   = {{{{{_bib_escape(title)}}}}},")
        if venue:
            lines.append(f"  journal = {{{_bib_escape(venue)}}},")
        if year != "n.d.":
            lines.append(f"  year    = {{{year}}},")
        if doi:
            lines.append(f"  doi     = {{{doi}}},")
        if url:
            lines.append(f"  url     = {{{url}}},")
        lines[-1] = lines[-1].rstrip(",")
        lines.append("}")
        return "\n".join(lines)
    if style == "ris":
        lines = ["TY  - JOUR", *[f"AU  - {s}, {g}".rstrip(", ") for s, g in map(_name_parts, authors)], f"TI  - {title}"]
        if venue:
            lines.append(f"T2  - {venue}")
        if year != "n.d.":
            lines.append(f"PY  - {year}")
        if doi:
            lines.append(f"DO  - {doi}")
        if url:
            lines.append(f"UR  - {url}")
        if p.get("abstract"):
            lines.append(f"AB  - {p['abstract'][:2000]}")
        for kw in (p.get("topics") or [])[:10]:
            lines.append(f"KW  - {kw}")
        lines.append("ER  - ")
        return "\n".join(lines)
    raise ValueError(f"Unknown citation style {style!r}")


def all_citations(ds: dict, accessed: date | None = None) -> dict[str, str]:
    return {key: cite(ds, key, accessed) for key in STYLES}
