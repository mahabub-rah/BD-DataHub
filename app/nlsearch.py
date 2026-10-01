"""Lightweight natural-language query understanding.

Turns a query such as "monthly rainfall data for Sylhet 2010-2020 in csv" into
free-text terms plus structured filters (years, formats, frequency, level),
and builds a synonym-expanded SQLite FTS5 expression. Fully deterministic and
offline, so it works without any external AI service.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

STOPWORDS = {
    "a", "an", "the", "of", "for", "in", "on", "at", "to", "from", "by", "with", "and", "or",
    "about", "data", "dataset", "datasets", "database", "info", "information", "find", "show",
    "me", "i", "need", "want", "looking", "get", "give", "list", "all", "any", "some",
    "bangladesh", "bangladeshi", "bd", "wise", "level", "between", "since", "until", "till",
    "is", "are", "what", "where", "which", "how", "latest", "recent", "download", "please",
    "তথ্য", "ডেটা", "বাংলাদেশ", "বাংলাদেশের",
}

# Each entry expands a user word into equivalent phrases found in metadata.
SYNONYMS: dict[str, list[str]] = {
    "inflation": ["inflation", "cpi", "consumer price"],
    "cpi": ["cpi", "consumer price", "inflation"],
    "prices": ["price", "prices", "cpi"],
    "gdp": ["gdp", "gross domestic product", "national accounts"],
    "economy": ["economy", "economic"],
    "rain": ["rainfall", "rain", "precipitation"],
    "rainfall": ["rainfall", "rain", "precipitation"],
    "weather": ["weather", "meteorological", "forecast"],
    "temperature": ["temperature", "weather"],
    "population": ["population", "census", "demographic"],
    "census": ["census", "population"],
    "crime": ["crime", "offence", "police"],
    "election": ["election", "electoral", "voter", "constituency"],
    "elections": ["election", "electoral", "voter"],
    "voter": ["voter", "electoral roll", "election"],
    "remittance": ["remittance", "remittances", "wage earner"],
    "school": ["school", "education", "institution"],
    "schools": ["school", "education", "institution"],
    "hospital": ["hospital", "health facility", "health"],
    "electricity": ["electricity", "power", "generation"],
    "power": ["power", "electricity", "generation"],
    "unemployment": ["unemployment", "labour force", "employment"],
    "jobs": ["employment", "labour force"],
    "labor": ["labour", "labor"],
    "poverty": ["poverty", "household income", "hies"],
    "flood": ["flood", "water level", "river"],
    "floods": ["flood", "water level", "river"],
    "river": ["river", "water level", "hydrology"],
    "air": ["air quality", "aqi", "air"],
    "pollution": ["pollution", "air quality", "aqi"],
    "tax": ["tax", "revenue"],
    "revenue": ["revenue", "tax"],
    "fdi": ["fdi", "foreign direct investment"],
    "investment": ["investment", "fdi"],
    "export": ["export", "exports"],
    "exports": ["export", "exports"],
    "import": ["import", "imports"],
    "trade": ["trade", "export", "import"],
    "covid": ["covid", "coronavirus"],
    "map": ["map", "boundaries", "gis", "shapefile"],
    "maps": ["map", "boundaries", "gis", "shapefile"],
    "boundaries": ["boundaries", "boundary", "administrative"],
    "exchange": ["exchange rate", "exchange"],
    "budget": ["budget", "public finance"],
    "rice": ["rice", "paddy", "crop"],
    "fish": ["fish", "fisheries"],
    "internet": ["internet", "broadband", "telecom"],
    "mobile": ["mobile", "subscriber", "telecom"],
    "refugee": ["refugee", "rohingya"],
    "rohingya": ["rohingya", "refugee"],
    "road": ["road", "roads", "highway"],
    "roads": ["road", "roads", "highway"],
    "gas": ["gas", "natural gas"],
    # Bangla → English
    "জনসংখ্যা": ["population", "census"],
    "আদমশুমারি": ["census", "population"],
    "বৃষ্টিপাত": ["rainfall"],
    "আবহাওয়া": ["weather", "meteorological"],
    "মূল্যস্ফীতি": ["inflation", "cpi"],
    "শিক্ষা": ["education"],
    "স্বাস্থ্য": ["health"],
    "কৃষি": ["agriculture"],
    "নির্বাচন": ["election"],
    "অপরাধ": ["crime"],
    "বিদ্যুৎ": ["electricity", "power"],
    "বন্যা": ["flood"],
    "রপ্তানি": ["export"],
    "রেমিট্যান্স": ["remittance"],
    "দারিদ্র্য": ["poverty"],
    "বাজেট": ["budget"],
}

FORMAT_WORDS = {
    "csv": "CSV", "excel": "XLSX", "xlsx": "XLSX", "xls": "XLS", "spreadsheet": "XLSX",
    "pdf": "PDF", "json": "JSON", "xml": "XML", "shapefile": "SHP", "shp": "SHP",
    "geojson": "GeoJSON", "dashboard": "Dashboard",
}

FREQUENCY_WORDS = {
    "realtime": "Real-time", "real-time": "Real-time", "live": "Real-time",
    "daily": "Daily", "weekly": "Weekly", "monthly": "Monthly", "quarterly": "Quarterly",
    "annual": "Annual", "annually": "Annual", "yearly": "Annual",
}

LEVEL_WORDS = {
    "division": "Division", "divisional": "Division", "divisions": "Division",
    "district": "District", "districts": "District", "zila": "District", "zilla": "District",
    "upazila": "Upazila", "upazilas": "Upazila", "thana": "Upazila",
    "union": "Union", "unions": "Union",
    "constituency": "Constituency", "constituencies": "Constituency",
    "station": "Station/Point", "stations": "Station/Point",
}

DIVISIONS = ["dhaka", "chattogram", "chittagong", "rajshahi", "khulna", "barishal", "barisal",
             "sylhet", "rangpur", "mymensingh"]

DISTRICTS = [
    "bagerhat", "bandarban", "barguna", "barishal", "barisal", "bhola", "bogura", "bogra",
    "brahmanbaria", "chandpur", "chapainawabganj", "chattogram", "chittagong", "chuadanga",
    "cox's bazar", "coxs bazar", "cumilla", "comilla", "dhaka", "dinajpur", "faridpur", "feni",
    "gaibandha", "gazipur", "gopalganj", "habiganj", "jamalpur", "jashore", "jessore",
    "jhalokathi", "jhenaidah", "joypurhat", "khagrachhari", "khulna", "kishoreganj",
    "kurigram", "kushtia", "lakshmipur", "lalmonirhat", "madaripur", "magura", "manikganj",
    "meherpur", "moulvibazar", "munshiganj", "mymensingh", "naogaon", "narail",
    "narayanganj", "narsingdi", "natore", "netrokona", "nilphamari", "noakhali", "pabna",
    "panchagarh", "patuakhali", "pirojpur", "rajbari", "rajshahi", "rangamati", "rangpur",
    "satkhira", "shariatpur", "sherpur", "sirajganj", "sunamganj", "sylhet", "tangail",
    "thakurgaon",
]
PLACES = set(DIVISIONS) | set(DISTRICTS)

_YEAR = r"(19[5-9]\d|20[0-4]\d)"
_RANGE_RE = re.compile(rf"\b{_YEAR}\s*(?:-|–|to|until|till)\s*{_YEAR}\b", re.I)
_SINCE_RE = re.compile(rf"\b(?:since|after|from)\s+{_YEAR}\b", re.I)
_BEFORE_RE = re.compile(rf"\b(?:before|until|till|up to)\s+{_YEAR}\b", re.I)
_YEAR_RE = re.compile(rf"\b{_YEAR}\b")
_TOKEN_RE = re.compile(r"[\wঀ-৿'’-]+", re.UNICODE)


@dataclass
class ParsedQuery:
    raw: str
    terms: list[str] = field(default_factory=list)
    year_from: int | None = None
    year_to: int | None = None
    formats: list[str] = field(default_factory=list)
    frequencies: list[str] = field(default_factory=list)
    levels: list[str] = field(default_factory=list)
    places: list[str] = field(default_factory=list)

    @property
    def interpreted(self) -> list[str]:
        """Human-readable list of detected filters (for 'Interpreted as…' chips)."""
        out = []
        if self.year_from or self.year_to:
            if self.year_from == self.year_to:
                out.append(f"Year {self.year_from}")
            else:
                out.append(f"Years {self.year_from or '…'}–{self.year_to or '…'}")
        out += [f"Format: {f}" for f in self.formats]
        out += [f"Frequency: {f}" for f in self.frequencies]
        out += [f"Level: {lvl}" for lvl in self.levels]
        out += [f"Place: {p.title()}" for p in self.places]
        return out

    @property
    def has_filters(self) -> bool:
        return bool(self.year_from or self.year_to or self.formats or self.frequencies
                    or self.levels)


def parse(query: str) -> ParsedQuery:
    q = ParsedQuery(raw=query or "")
    text = " " + (query or "").strip() + " "

    if m := _RANGE_RE.search(text):
        a, b = sorted((int(m.group(1)), int(m.group(2))))
        q.year_from, q.year_to = a, b
        text = text.replace(m.group(0), " ")
    else:
        if m := _SINCE_RE.search(text):
            q.year_from = int(m.group(1))
            text = text.replace(m.group(0), " ")
        if m := _BEFORE_RE.search(text):
            q.year_to = int(m.group(1))
            text = text.replace(m.group(0), " ")
        if not (q.year_from or q.year_to):
            years = [int(y) for y in _YEAR_RE.findall(text)]
            if len(years) == 1:
                q.year_from = q.year_to = years[0]
                text = _YEAR_RE.sub(" ", text)

    lowered = text.lower()
    # Multi-word places first ("cox's bazar").
    for place in sorted(PLACES, key=len, reverse=True):
        if " " in place and place in lowered:
            q.places.append(place)
            lowered = lowered.replace(place, " ")

    tokens = [t.strip("'’-") for t in _TOKEN_RE.findall(lowered)]
    for tok in tokens:
        if not tok:
            continue
        if tok in FORMAT_WORDS:
            if FORMAT_WORDS[tok] not in q.formats:
                q.formats.append(FORMAT_WORDS[tok])
        elif tok in FREQUENCY_WORDS:
            if FREQUENCY_WORDS[tok] not in q.frequencies:
                q.frequencies.append(FREQUENCY_WORDS[tok])
        elif tok in LEVEL_WORDS:
            if LEVEL_WORDS[tok] not in q.levels:
                q.levels.append(LEVEL_WORDS[tok])
        elif tok in STOPWORDS:
            continue
        else:
            if tok in PLACES and tok not in q.places:
                q.places.append(tok)
            if tok not in q.terms:
                q.terms.append(tok)
    return q


def _fts_phrase(text: str, prefix: bool) -> str:
    cleaned = re.sub(r"[^\wঀ-৿\s]", " ", text, flags=re.UNICODE)
    cleaned = " ".join(cleaned.split())
    if not cleaned:
        return ""
    # Prefix-match single words ("rain" → "rainfall"); quote phrases exactly.
    if prefix and " " not in cleaned and len(cleaned) >= 3:
        return f'"{cleaned}"*'
    return f'"{cleaned}"'


def build_fts(terms: list[str], mode: str = "AND") -> str:
    """Build an FTS5 MATCH expression with synonym expansion. mode is AND or OR."""
    groups = []
    for term in terms:
        alts = SYNONYMS.get(term, [term])
        if term not in alts:
            alts = [term, *alts]
        parts = [p for p in (_fts_phrase(a, prefix=True) for a in alts) if p]
        if not parts:
            continue
        groups.append(parts[0] if len(parts) == 1 else "(" + " OR ".join(parts) + ")")
    # FTS5 needs an explicit AND between parenthesised groups.
    joiner = " OR " if mode == "OR" else " AND "
    return joiner.join(groups)
