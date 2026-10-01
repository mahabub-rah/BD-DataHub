# Seed catalog specification

The seed catalog is the list of datasets that BD DataHub ships with. **Every entry must be
verified against the live source.** Do not fabricate datasets, URLs, years, or formats.

## Verification rule

A dataset may be added only if a page on the publisher's own site (or an official portal the
publisher uses) was actually loaded and shows that the dataset/publication/table exists. Record:

- `evidence_url` – the exact URL that was loaded and shows the dataset
- `evidence_note` – one sentence quoting or describing what the page shows (e.g. the table title,
  report name, or download link text)
- `checked_at` – the ISO date of the check

If a field cannot be confirmed from the page (e.g. coverage years, license), use `null` rather
than guessing. If a site could not be loaded, list it under `unreachable` instead of inventing
entries from memory.

## File format

Each source group is one JSON file in `seed/sources/` with this shape:

```json
{
  "organizations": [
    {
      "slug": "bbs",
      "name": "Bangladesh Bureau of Statistics",
      "name_bn": "বাংলাদেশ পরিসংখ্যান ব্যুরো",
      "acronym": "BBS",
      "org_type": "Government Department",
      "parent": "Statistics and Informatics Division, Ministry of Planning",
      "website": "https://bbs.gov.bd",
      "description": "National statistical agency of Bangladesh."
    }
  ],
  "datasets": [
    {
      "slug": "bbs-population-housing-census-2022",
      "title": "Population and Housing Census 2022",
      "organization": "bbs",
      "description": "Plain-language description of what the dataset contains (2–4 sentences).",
      "source_url": "https://… page where users find the dataset",
      "download_url": "https://… direct file link, or null",
      "categories": ["Population & Demographics"],
      "subcategories": ["Census"],
      "formats": ["PDF", "XLSX"],
      "geographic_coverage": "Bangladesh",
      "geographic_level": "District",
      "temporal_start": 2022,
      "temporal_end": 2022,
      "update_frequency": "Decennial",
      "license": null,
      "languages": ["en", "bn"],
      "tags": ["census", "population", "households"],
      "evidence_url": "https://…",
      "evidence_note": "Page lists 'Population and Housing Census 2022 – National Report (Volume I)' with PDF download.",
      "checked_at": "2026-09-27"
    }
  ],
  "unreachable": [
    {"url": "https://…", "note": "Timed out / 403 / DNS failure"}
  ]
}
```

### Allowed values

- `org_type`: Ministry, Division, Government Department, Directorate, Central Bank,
  Statutory Body, Regulatory Commission, Constitutional Body, Law Enforcement, Public Utility,
  Open Data Portal, International Organization, Research Institute, Stock Exchange
- `categories` (one or more, exact spelling):
  Economy & Finance, Population & Demographics, Health, Education, Agriculture, Environment,
  Climate & Weather, Crime & Justice, Transport, Labour & Employment, Industry & Business,
  Trade & Commerce, Government & Public Administration, Geography & GIS, Energy,
  Infrastructure, Social Protection, Tourism, Science & Technology, Other
- `subcategories` (optional list of free-text strings, e.g. "Inflation", "Balance of Payments",
  "Rainfall", "Flood Forecasting", "Elections", "Tax Revenue", "Census"). Use short title-case
  labels; the platform will attach each to the dataset's first category.
- `formats`: PDF, XLSX, XLS, CSV, JSON, XML, HTML, SHP, GeoJSON, DOCX, ZIP, Dashboard, Other
- `geographic_level`: National, Division, District, Upazila, Union, City Corporation,
  Station/Point, Constituency, Grid/Raster, Mixed
- `update_frequency`: Real-time, Daily, Weekly, Monthly, Quarterly, Half-yearly, Annual,
  Multi-year, Decennial, One-time, Irregular, Unknown
- `formats` must reflect what is actually offered on the page (HTML tables → HTML; a web
  dashboard → Dashboard).
