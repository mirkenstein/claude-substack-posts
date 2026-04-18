-- ═══════════════════════════════════════════════════════════
-- External Articles Schema
-- Unified storage for scraped articles from external sources
-- cited in Substack publications (TopWar, LiveJournal, WSJ, etc.)
-- ═══════════════════════════════════════════════════════════

CREATE SCHEMA IF NOT EXISTS external;

-- ───────────────────────────────────────────────────────────
-- Sources registry
-- ───────────────────────────────────────────────────────────
CREATE TABLE external.sources (
    id              serial PRIMARY KEY,
    domain          text NOT NULL UNIQUE,       -- 'topwar.ru', 'livejournal.com', 'wsj.com'
    name            text,                        -- 'Military Review (TopWar)', 'Wall Street Journal'
    language        text DEFAULT 'ru',           -- 'ru', 'en', 'mixed'
    notes           text,
    created_at      timestamptz DEFAULT now()
);

-- Sources are auto-created by the loader (load_external.py) on first article insert.
-- No seed data needed — ensure_source() handles INSERT ON CONFLICT DO NOTHING.

-- ───────────────────────────────────────────────────────────
-- Articles — unified across all sources
-- ───────────────────────────────────────────────────────────
CREATE TABLE external.articles (
    id              bigserial PRIMARY KEY,
    source_id       int NOT NULL REFERENCES external.sources(id),

    -- Identity
    source_article_id text,                      -- original article ID from source (e.g. TopWar numeric ID)
    url             text,                        -- canonical URL (may be null for 404 recoveries)
    slug            text,                        -- extracted from URL or filename
    file_path       text,                        -- local scrape file path

    -- Content
    title           text,
    subtitle        text,
    author          text,                        -- author name or handle (nullable)
    publish_date    timestamptz,                 -- normalized from various formats
    text            text,                        -- plain text content (GIN-indexed)

    -- Metrics (source-specific, all nullable)
    views           int,
    likes           int,
    comment_count   int,                         -- from header/metadata

    -- Structured data (JSONB for flexibility)
    tags            jsonb DEFAULT '[]'::jsonb,   -- array of category/tag strings from source
    image_urls      jsonb DEFAULT '[]'::jsonb,   -- array of image URL strings
    external_links  jsonb DEFAULT '[]'::jsonb,   -- array of {url, text} objects
    internal_links  jsonb DEFAULT '[]'::jsonb,   -- array of {url, text} objects

    -- Recovery metadata (for 404/archive articles)
    recovery_source text,                        -- 'wayback', 'livejournal_mirror', 'substack_translation', null
    is_deleted      boolean DEFAULT false,        -- true if original URL returns 404

    -- Raw data preservation
    raw_json        jsonb,                       -- full original JSON for anything we didn't extract

    -- Housekeeping
    created_at      timestamptz DEFAULT now(),
    updated_at      timestamptz DEFAULT now(),

    -- Dedup
    UNIQUE (source_id, url),
    UNIQUE (source_id, file_path)
);

-- Full-text search index on article text
CREATE INDEX idx_ext_articles_text_fts
    ON external.articles
    USING GIN (to_tsvector('russian', COALESCE(text, '')));

-- Also English FTS for WSJ/Nation articles
CREATE INDEX idx_ext_articles_text_fts_en
    ON external.articles
    USING GIN (to_tsvector('english', COALESCE(text, '')));

-- Title search
CREATE INDEX idx_ext_articles_title_fts
    ON external.articles
    USING GIN (to_tsvector('russian', COALESCE(title, '') || ' ' || COALESCE(subtitle, '')));

-- Lookup indexes
CREATE INDEX idx_ext_articles_source      ON external.articles (source_id);
CREATE INDEX idx_ext_articles_date        ON external.articles (publish_date);
CREATE INDEX idx_ext_articles_author      ON external.articles (author);
CREATE INDEX idx_ext_articles_domain_url  ON external.articles (source_id, url);
CREATE INDEX idx_ext_articles_deleted     ON external.articles (is_deleted) WHERE is_deleted = true;
CREATE INDEX idx_ext_articles_source_aid ON external.articles (source_id, source_article_id) WHERE source_article_id IS NOT NULL;

-- JSONB indexes for link analysis
CREATE INDEX idx_ext_articles_ext_links   ON external.articles USING GIN (external_links);

-- ───────────────────────────────────────────────────────────
-- Comments — unified across sources with comments
-- ───────────────────────────────────────────────────────────
CREATE TABLE external.comments (
    id                  bigserial PRIMARY KEY,
    article_id          bigint NOT NULL REFERENCES external.articles(id) ON DELETE CASCADE,

    -- Identity
    source_comment_id   text,                    -- original comment ID from source (TopWar has these, LJ doesn't)
    username            text,                    -- commenter name/handle
    comment_date        text,                    -- kept as text since formats vary wildly across sources
    comment_date_parsed timestamptz,             -- normalized version (nullable, best-effort)

    -- Content
    body                text NOT NULL,

    -- Metrics
    rating              text,                    -- TopWar: '+21', '-3', etc. Nullable for sources without ratings.

    -- Dedup
    content_hash        text GENERATED ALWAYS AS (md5(COALESCE(username, '') || body)) STORED,

    -- Housekeeping
    created_at          timestamptz DEFAULT now(),
    updated_at          timestamptz DEFAULT now(),

    UNIQUE (article_id, content_hash)
);

-- Full-text search on comment body
CREATE INDEX idx_ext_comments_body_fts
    ON external.comments
    USING GIN (to_tsvector('russian', body));

CREATE INDEX idx_ext_comments_body_fts_en
    ON external.comments
    USING GIN (to_tsvector('english', body));

-- Lookup indexes
CREATE INDEX idx_ext_comments_article     ON external.comments (article_id);
CREATE INDEX idx_ext_comments_user        ON external.comments (username);
CREATE INDEX idx_ext_comments_source_id   ON external.comments (source_comment_id) WHERE source_comment_id IS NOT NULL;

-- ───────────────────────────────────────────────────────────
-- Cross-reference view: articles with source info
-- ───────────────────────────────────────────────────────────
CREATE OR REPLACE VIEW external.articles_with_source AS
SELECT
    a.*,
    s.domain,
    s.name AS source_name,
    s.language,
    (SELECT COUNT(*) FROM external.comments c WHERE c.article_id = a.id) AS loaded_comment_count
FROM external.articles a
JOIN external.sources s ON s.id = a.source_id;

-- ───────────────────────────────────────────────────────────
-- Substack cross-reference view
-- Links external articles to Substack posts that cite them
-- ───────────────────────────────────────────────────────────
CREATE OR REPLACE VIEW external.substack_citations AS
SELECT
    a.id AS article_id,
    a.title AS article_title,
    a.url AS article_url,
    s.domain AS article_domain,
    pl.post_id,
    pl.anchor_text,
    pwd.title AS substack_title,
    pwd.post_date AS substack_date,
    pwd.subdomain AS substack_publication,
    pwd.author_name AS substack_author
FROM external.articles a
JOIN external.sources s ON s.id = a.source_id
JOIN substack.post_links pl ON pl.url = a.url
JOIN substack.posts_with_details pwd ON pwd.id = pl.post_id
WHERE a.url IS NOT NULL;
