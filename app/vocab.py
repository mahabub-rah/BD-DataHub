"""Controlled vocabularies for dataset metadata."""

FORMATS = [
    "PDF", "XLSX", "XLS", "CSV", "JSON", "XML", "HTML",
    "SHP", "GeoJSON", "DOCX", "ZIP", "Dashboard", "Other",
]

# Formats a machine can read without manual extraction.
MACHINE_READABLE = {"XLSX", "XLS", "CSV", "JSON", "XML", "SHP", "GeoJSON"}

GEOGRAPHIC_LEVELS = [
    "National", "Division", "District", "Upazila", "Union", "City Corporation",
    "Station/Point", "Constituency", "Grid/Raster", "Mixed",
]

FREQUENCIES = [
    "Real-time", "Daily", "Weekly", "Monthly", "Quarterly", "Half-yearly",
    "Annual", "Multi-year", "Decennial", "One-time", "Irregular", "Unknown",
]

ORG_TYPES = [
    "Ministry", "Division", "Government Department", "Directorate", "Central Bank",
    "Statutory Body", "Regulatory Commission", "Constitutional Body", "Law Enforcement",
    "Public Utility", "Open Data Portal", "International Organization",
    "Research Institute", "Stock Exchange", "Non-Governmental Organization",
]

# Organisation types that count as official Bangladesh government/public sources.
OFFICIAL_ORG_TYPES = set(ORG_TYPES) - {"International Organization", "Non-Governmental Organization"}

LINK_STATUSES = {
    "ok": "Link working",
    "redirected": "Link redirects",
    "broken": "Link broken",
    "blocked": "Check blocked (robots.txt / access)",
    "error": "Check failed",
    "unchecked": "Not yet checked",
}

LANGUAGES = {"en": "English", "bn": "Bangla"}
