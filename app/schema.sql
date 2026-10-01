-- BD DataHub schema (SQLite)

CREATE TABLE IF NOT EXISTS categories (
    id          INTEGER PRIMARY KEY,
    slug        TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    parent_id   INTEGER REFERENCES categories(id) ON DELETE CASCADE,
    description TEXT,
    icon        TEXT,
    sort_order  INTEGER NOT NULL DEFAULT 0,
    UNIQUE (parent_id, name)
);

-- Alternative names that resolve to a canonical (sub)category, e.g. "CPI" → "Inflation & Prices".
CREATE TABLE IF NOT EXISTS category_aliases (
    alias       TEXT NOT NULL PRIMARY KEY COLLATE NOCASE,
    category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS organizations (
    id          INTEGER PRIMARY KEY,
    slug        TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    name_bn     TEXT,
    acronym     TEXT,
    org_type    TEXT,
    parent      TEXT,
    website     TEXT,
    description TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS datasets (
    id                  INTEGER PRIMARY KEY,
    slug                TEXT NOT NULL UNIQUE,
    title               TEXT NOT NULL,
    organization_id     INTEGER NOT NULL REFERENCES organizations(id),
    description         TEXT NOT NULL DEFAULT '',
    source_url          TEXT NOT NULL,
    download_url        TEXT,
    geographic_coverage TEXT NOT NULL DEFAULT 'Bangladesh',
    geographic_level    TEXT,
    temporal_start      INTEGER,
    temporal_end        INTEGER,
    update_frequency    TEXT NOT NULL DEFAULT 'Unknown',
    license             TEXT,
    languages           TEXT NOT NULL DEFAULT '["en"]',   -- JSON array
    -- verification
    verification_status TEXT NOT NULL DEFAULT 'unverified', -- verified | unverified | needs_review | deprecated
    verified_at         TEXT,
    verified_by         TEXT,
    evidence_url        TEXT,
    evidence_note       TEXT,
    -- source monitoring
    link_status         TEXT NOT NULL DEFAULT 'unchecked',  -- ok | redirected | broken | blocked | error | unchecked
    http_status         INTEGER,
    link_checked_at     TEXT,
    content_fingerprint TEXT,
    source_changed_at   TEXT,
    -- bookkeeping
    is_published        INTEGER NOT NULL DEFAULT 1,
    view_count          INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_datasets_org ON datasets(organization_id);
CREATE INDEX IF NOT EXISTS ix_datasets_updated ON datasets(updated_at);

CREATE TABLE IF NOT EXISTS dataset_categories (
    dataset_id  INTEGER NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
    PRIMARY KEY (dataset_id, category_id)
);

CREATE TABLE IF NOT EXISTS dataset_formats (
    dataset_id INTEGER NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    format     TEXT NOT NULL,
    PRIMARY KEY (dataset_id, format)
);

CREATE TABLE IF NOT EXISTS dataset_tags (
    dataset_id INTEGER NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    tag        TEXT NOT NULL,
    PRIMARY KEY (dataset_id, tag)
);

CREATE TABLE IF NOT EXISTS link_checks (
    id          INTEGER PRIMARY KEY,
    dataset_id  INTEGER NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    url         TEXT NOT NULL,
    checked_at  TEXT NOT NULL DEFAULT (datetime('now')),
    status      TEXT NOT NULL,
    http_status INTEGER,
    final_url   TEXT,
    fingerprint TEXT,
    changed     INTEGER NOT NULL DEFAULT 0,
    elapsed_ms  INTEGER,
    error       TEXT
);
CREATE INDEX IF NOT EXISTS ix_link_checks_dataset ON link_checks(dataset_id, checked_at);

CREATE TABLE IF NOT EXISTS audit_log (
    id        INTEGER PRIMARY KEY,
    at        TEXT NOT NULL DEFAULT (datetime('now')),
    actor     TEXT NOT NULL,
    action    TEXT NOT NULL,
    entity    TEXT NOT NULL,
    entity_id TEXT,
    detail    TEXT
);

-- Cached first-rows previews of machine-readable download files (app.preview).
CREATE TABLE IF NOT EXISTS dataset_previews (
    dataset_id INTEGER PRIMARY KEY REFERENCES datasets(id) ON DELETE CASCADE,
    fetched_at TEXT NOT NULL,
    status     TEXT NOT NULL,        -- ok | unsupported | error | blocked
    kind       TEXT,
    columns    TEXT,
    rows       TEXT,
    truncated  INTEGER NOT NULL DEFAULT 0,
    note       TEXT
);

-- Full-text index; rowid = datasets.id. Maintained by app.repository.reindex().
CREATE VIRTUAL TABLE IF NOT EXISTS datasets_fts USING fts5(
    title, description, tags, organization, categories,
    tokenize = 'unicode61 remove_diacritics 2'
);

-- Research papers from OpenAlex (seed/papers/*.json). Rebuilt from the seed by app.papers.load().
CREATE TABLE IF NOT EXISTS papers (
    id               INTEGER PRIMARY KEY,
    openalex_id      TEXT NOT NULL UNIQUE,              -- e.g. W2741809807
    doi              TEXT,                              -- bare, e.g. 10.1038/nature15393
    title            TEXT NOT NULL,
    authors          TEXT NOT NULL DEFAULT '[]',        -- JSON array of display names
    year             INTEGER,
    publication_date TEXT,
    venue            TEXT,
    abstract         TEXT,
    is_oa            INTEGER,
    oa_url           TEXT,
    landing_url      TEXT,
    topics           TEXT NOT NULL DEFAULT '[]',        -- JSON array
    cited_by_count   INTEGER NOT NULL DEFAULT 0,
    language         TEXT,
    harvested_at     TEXT
);
CREATE INDEX IF NOT EXISTS ix_papers_year ON papers(year);
CREATE INDEX IF NOT EXISTS ix_papers_cited ON papers(cited_by_count);
CREATE INDEX IF NOT EXISTS ix_papers_venue ON papers(venue);

CREATE TABLE IF NOT EXISTS paper_topics (
    paper_id INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    topic    TEXT NOT NULL,
    PRIMARY KEY (paper_id, topic)
);
CREATE INDEX IF NOT EXISTS ix_paper_topics_topic ON paper_topics(topic);

-- Papers in the site's top-level categories, derived by app.papers.categorize() from OpenAlex topics.
-- rank 0 is the category of the paper's primary topic.
CREATE TABLE IF NOT EXISTS paper_categories (
    paper_id    INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
    rank        INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (paper_id, category_id)
);
CREATE INDEX IF NOT EXISTS ix_paper_categories_category ON paper_categories(category_id);

-- Full-text index; rowid = papers.id.
CREATE VIRTUAL TABLE IF NOT EXISTS papers_fts USING fts5(
    title, abstract, authors, venue, topics,
    tokenize = 'unicode61 remove_diacritics 2'
);

-- What app.seed.sync() last loaded: one row per seed group ("catalog", "papers") with a fingerprint
-- of its files (names, sizes, modification times), so an unchanged seed is skipped on boot.
CREATE TABLE IF NOT EXISTS seed_state (
    name        TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    rows        INTEGER,
    loaded_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
