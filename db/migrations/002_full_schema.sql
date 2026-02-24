-- ============================================================================
-- Substack Database Schema — Full
-- ============================================================================
-- Database: substack
-- Schemas:  substack, external
-- Created:  2026-02-23
--
-- This is the complete schema as if creating from scratch.
-- Includes all tables, indexes, and views.
-- ============================================================================

-- ============================================================================
-- SCHEMA: substack
-- Core Substack data: posts, comments, links, media, transcripts
-- ============================================================================

CREATE SCHEMA IF NOT EXISTS substack;

-- ──────────────────────────────────────────────────────────────────────────────
-- Table: publications
-- ──────────────────────────────────────────────────────────────────────────────
CREATE TABLE substack.publications (
    id              BIGINT PRIMARY KEY,
    subdomain       VARCHAR(255) NOT NULL UNIQUE,
    name            VARCHAR(500) NOT NULL,
    custom_domain   VARCHAR(255),
    logo_url        TEXT,
    hero_text       TEXT,
    loaded_at       TIMESTAMPTZ DEFAULT NOW()
);

-- ──────────────────────────────────────────────────────────────────────────────
-- Table: authors
-- ──────────────────────────────────────────────────────────────────────────────
CREATE TABLE substack.authors (
    id                  BIGINT PRIMARY KEY,
    name                VARCHAR(255) NOT NULL,
    handle              VARCHAR(255),
    photo_url           TEXT,
    bio                 TEXT,
    twitter_screen_name VARCHAR(255),
    bestseller_tier     INTEGER,
    loaded_at           TIMESTAMPTZ DEFAULT NOW()
);

-- ──────────────────────────────────────────────────────────────────────────────
-- Table: posts
-- ──────────────────────────────────────────────────────────────────────────────
CREATE TABLE substack.posts (
    id                  BIGINT PRIMARY KEY,
    publication_id      BIGINT NOT NULL REFERENCES substack.publications(id),
    primary_author_id   BIGINT REFERENCES substack.authors(id),
    slug                VARCHAR(500) NOT NULL,
    title               TEXT NOT NULL,
    subtitle            TEXT,
    canonical_url       TEXT NOT NULL,
    post_date           TIMESTAMPTZ NOT NULL,
    updated_at          TIMESTAMPTZ,
    type                VARCHAR(50) NOT NULL DEFAULT 'newsletter',
    audience            VARCHAR(50),
    is_paywalled        BOOLEAN DEFAULT false,
    wordcount           INTEGER,
    restacks            INTEGER DEFAULT 0,
    comment_count       INTEGER DEFAULT 0,
    cover_image         TEXT,
    description         TEXT,
    reactions           JSONB,
    content_html        TEXT,
    podcast_url         TEXT,
    content_text        TEXT,                        -- plain text extracted from content_html
    loaded_at           TIMESTAMPTZ DEFAULT NOW(),

    CONSTRAINT unique_publication_slug UNIQUE (publication_id, slug)
);

-- ──────────────────────────────────────────────────────────────────────────────
-- Table: post_tags
-- ──────────────────────────────────────────────────────────────────────────────
CREATE TABLE substack.post_tags (
    post_id     BIGINT NOT NULL REFERENCES substack.posts(id) ON DELETE CASCADE,
    tag         VARCHAR(255) NOT NULL,
    PRIMARY KEY (post_id, tag)
);

-- ──────────────────────────────────────────────────────────────────────────────
-- Table: comments
-- ──────────────────────────────────────────────────────────────────────────────
CREATE TABLE substack.comments (
    id                  BIGINT PRIMARY KEY,
    post_id             BIGINT NOT NULL REFERENCES substack.posts(id) ON DELETE CASCADE,
    user_id             BIGINT,
    author_name         VARCHAR(255),
    author_handle       VARCHAR(255),
    body                TEXT,
    body_json           JSONB,
    ancestor_path       TEXT NOT NULL DEFAULT '',
    parent_comment_id   BIGINT,
    depth               INTEGER GENERATED ALWAYS AS (
                            CASE
                                WHEN ancestor_path = '' THEN 0
                                ELSE array_length(string_to_array(ancestor_path, '.'), 1)
                            END
                        ) STORED,
    date                TIMESTAMPTZ NOT NULL,
    edited_at           TIMESTAMPTZ,
    deleted             BOOLEAN DEFAULT false,
    reactions           JSONB,
    reaction_count      INTEGER DEFAULT 0,
    is_valuable         BOOLEAN DEFAULT false,
    loaded_at           TIMESTAMPTZ DEFAULT NOW()
);

-- ──────────────────────────────────────────────────────────────────────────────
-- Table: post_links  (links extracted from post HTML content)
-- ──────────────────────────────────────────────────────────────────────────────
CREATE TABLE substack.post_links (
    id                  BIGSERIAL PRIMARY KEY,
    post_id             BIGINT NOT NULL REFERENCES substack.posts(id) ON DELETE CASCADE,
    url                 TEXT NOT NULL,
    domain              TEXT NOT NULL,
    source_category     VARCHAR(50) NOT NULL,
    position_in_post    INTEGER NOT NULL,
    anchor_text         TEXT,
    loaded_at           TIMESTAMPTZ DEFAULT NOW()
);

-- ──────────────────────────────────────────────────────────────────────────────
-- Table: comment_links  (links extracted from comment bodies)
-- ──────────────────────────────────────────────────────────────────────────────
CREATE TABLE substack.comment_links (
    id                  BIGSERIAL PRIMARY KEY,
    comment_id          BIGINT NOT NULL REFERENCES substack.comments(id) ON DELETE CASCADE,
    post_id             BIGINT NOT NULL REFERENCES substack.posts(id) ON DELETE CASCADE,
    url                 TEXT NOT NULL,
    domain              TEXT NOT NULL,
    source_category     VARCHAR(50) NOT NULL,
    anchor_text         TEXT,
    loaded_at           TIMESTAMPTZ DEFAULT NOW()
);

-- ──────────────────────────────────────────────────────────────────────────────
-- Table: post_media  (images, audio, cover images from posts)
-- ──────────────────────────────────────────────────────────────────────────────
CREATE TABLE substack.post_media (
    media_id            SERIAL PRIMARY KEY,
    post_id             BIGINT NOT NULL REFERENCES substack.posts(id),
    media_type          TEXT NOT NULL,                -- 'image', 'audio', 'cover_image'
    source_url          TEXT NOT NULL,
    local_path          TEXT,                         -- filled after download
    position_in_post    INTEGER,                      -- order in HTML (images)
    alt_text            TEXT,
    caption             TEXT,
    width               INTEGER,
    height              INTEGER,
    file_size_bytes     BIGINT,
    image_format        TEXT,
    duration_seconds    DOUBLE PRECISION,             -- for audio
    downloaded_at       TIMESTAMPTZ,

    -- LLM image analysis results
    image_description   TEXT,                         -- general description of image content
    image_data          JSONB,                        -- structured data: extracted_text, entities,
                                                      -- chart_description, chart_data, source, date_depicted
    image_category      TEXT,                         -- screenshot, tweet, meme, chart, table, photo, etc.
    analysis_model      TEXT,                         -- model used: 'gemma3:27b', 'claude-haiku-4-5-20251001'
    analyzed_at         TIMESTAMPTZ,
    analysis_error      TEXT,

    UNIQUE(post_id, source_url)
);

-- ──────────────────────────────────────────────────────────────────────────────
-- Table: transcript_lines  (podcast transcriptions)
-- ──────────────────────────────────────────────────────────────────────────────
CREATE TABLE substack.transcript_lines (
    id                  SERIAL PRIMARY KEY,
    episode_post_id     BIGINT NOT NULL REFERENCES substack.posts(id) ON DELETE CASCADE,
    turn_index          INTEGER NOT NULL,
    speaker             TEXT NOT NULL,
    start_time          DOUBLE PRECISION NOT NULL,
    end_time            DOUBLE PRECISION NOT NULL,
    content             TEXT NOT NULL,
    word_count          INTEGER,
    quality             TEXT DEFAULT 'clean',

    UNIQUE(episode_post_id, turn_index)
);

-- ──────────────────────────────────────────────────────────────────────────────
-- Table: youtube_references  (YouTube videos linked from posts)
-- ──────────────────────────────────────────────────────────────────────────────
CREATE TABLE substack.youtube_references (
    id                  SERIAL PRIMARY KEY,
    post_id             BIGINT REFERENCES substack.posts(id),
    video_id            VARCHAR NOT NULL,
    relationship_type   VARCHAR NOT NULL,             -- 'embed', 'link', 'mentioned'
    notes               TEXT,
    created_at          TIMESTAMP DEFAULT NOW()
);

-- ──────────────────────────────────────────────────────────────────────────────
-- Table: youtube_video_links  (relationships between YouTube videos)
-- ──────────────────────────────────────────────────────────────────────────────
CREATE TABLE substack.youtube_video_links (
    id                  SERIAL PRIMARY KEY,
    source_video_id     VARCHAR NOT NULL,
    target_video_id     VARCHAR NOT NULL,
    relationship_type   VARCHAR NOT NULL,             -- 'related', 'same_channel', etc.
    notes               TEXT,
    created_at          TIMESTAMP DEFAULT NOW()
);

-- ──────────────────────────────────────────────────────────────────────────────
-- Table: load_status  (tracks which JSON files have been loaded)
-- ──────────────────────────────────────────────────────────────────────────────
CREATE TABLE substack.load_status (
    file_path       TEXT PRIMARY KEY,
    post_id         BIGINT,
    loaded_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    status          VARCHAR(20) NOT NULL,
    error_message   TEXT
);


-- ============================================================================
-- Indexes: substack schema
-- ============================================================================

-- Posts
CREATE INDEX idx_posts_publication   ON substack.posts(publication_id);
CREATE INDEX idx_posts_author        ON substack.posts(primary_author_id);
CREATE INDEX idx_posts_date          ON substack.posts(post_date DESC);
CREATE INDEX idx_posts_slug          ON substack.posts(slug);
CREATE INDEX idx_posts_audience      ON substack.posts(audience);
CREATE INDEX idx_posts_fts           ON substack.posts
    USING GIN(to_tsvector('english', title || ' ' || COALESCE(subtitle, '')));

-- Comments
CREATE INDEX idx_comments_post       ON substack.comments(post_id);
CREATE INDEX idx_comments_date       ON substack.comments(date DESC);
CREATE INDEX idx_comments_ancestor   ON substack.comments(ancestor_path);
CREATE INDEX idx_comments_parent     ON substack.comments(parent_comment_id);
CREATE INDEX idx_comments_valuable   ON substack.comments(is_valuable) WHERE is_valuable = true;

-- Post links
CREATE INDEX idx_post_links_post     ON substack.post_links(post_id);
CREATE INDEX idx_post_links_domain   ON substack.post_links(domain);
CREATE INDEX idx_post_links_category ON substack.post_links(source_category);

-- Comment links
CREATE INDEX idx_comment_links_comment  ON substack.comment_links(comment_id);
CREATE INDEX idx_comment_links_post     ON substack.comment_links(post_id);
CREATE INDEX idx_comment_links_domain   ON substack.comment_links(domain);
CREATE INDEX idx_comment_links_category ON substack.comment_links(source_category);

-- Post media
CREATE INDEX idx_post_media_post_id          ON substack.post_media(post_id);
CREATE INDEX idx_post_media_type             ON substack.post_media(media_type);
CREATE INDEX idx_post_media_not_downloaded   ON substack.post_media(media_type)
    WHERE downloaded_at IS NULL;

-- Transcript lines
CREATE INDEX idx_transcript_lines_episode    ON substack.transcript_lines(episode_post_id);
CREATE INDEX idx_transcript_lines_speaker    ON substack.transcript_lines(speaker);
CREATE INDEX idx_transcript_lines_content_search ON substack.transcript_lines
    USING GIN(to_tsvector('english', content));

-- YouTube references
CREATE INDEX idx_youtube_refs_post_id        ON substack.youtube_references(post_id);
CREATE INDEX idx_youtube_refs_video_id       ON substack.youtube_references(video_id);

-- YouTube video links
CREATE INDEX idx_yt_links_source             ON substack.youtube_video_links(source_video_id);
CREATE INDEX idx_yt_links_target             ON substack.youtube_video_links(target_video_id);


-- ============================================================================
-- Views: substack schema
-- ============================================================================

-- Posts with publication and author details
CREATE VIEW substack.posts_with_details AS
SELECT
    p.id,
    p.title,
    p.slug,
    p.subtitle,
    p.canonical_url,
    p.post_date,
    p.type,
    p.audience,
    p.is_paywalled,
    p.wordcount,
    p.comment_count,
    p.restacks,
    p.reactions,
    pub.name       AS publication_name,
    pub.subdomain,
    a.name         AS author_name,
    a.handle       AS author_handle
FROM substack.posts p
JOIN substack.publications pub ON p.publication_id = pub.id
LEFT JOIN substack.authors a ON p.primary_author_id = a.id;

-- Link statistics by domain (post content)
CREATE VIEW substack.link_stats_by_domain AS
SELECT
    domain,
    source_category,
    COUNT(*)                AS link_count,
    COUNT(DISTINCT post_id) AS post_count
FROM substack.post_links
GROUP BY domain, source_category
ORDER BY link_count DESC;

-- Comment link statistics by domain
CREATE VIEW substack.comment_link_stats AS
SELECT
    domain,
    source_category,
    COUNT(*)                   AS link_count,
    COUNT(DISTINCT comment_id) AS comment_count,
    COUNT(DISTINCT post_id)    AS post_count
FROM substack.comment_links
GROUP BY domain, source_category
ORDER BY link_count DESC;

-- Valuable comments with post context
CREATE VIEW substack.valuable_comments AS
SELECT
    c.id            AS comment_id,
    c.post_id,
    p.title         AS post_title,
    p.slug          AS post_slug,
    c.author_name,
    c.body,
    c.date,
    c.reaction_count,
    c.depth,
    (SELECT COUNT(*) FROM substack.comment_links cl WHERE cl.comment_id = c.id) AS link_count
FROM substack.comments c
JOIN substack.posts p ON c.post_id = p.id
WHERE c.is_valuable = true
ORDER BY c.reaction_count DESC, c.date DESC;


-- ============================================================================
-- SCHEMA: external
-- External articles from sources cited in Substack publications
-- ============================================================================

CREATE SCHEMA IF NOT EXISTS external;

-- ──────────────────────────────────────────────────────────────────────────────
-- Table: sources  (registry of external domains)
-- ──────────────────────────────────────────────────────────────────────────────
CREATE TABLE external.sources (
    id              SERIAL PRIMARY KEY,
    domain          TEXT NOT NULL UNIQUE,
    name            TEXT,
    language        TEXT DEFAULT 'ru',
    notes           TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- Seed known sources
INSERT INTO external.sources (domain, name, language, notes) VALUES
    ('topwar.ru',          'Military Review (TopWar)',  'ru', 'Most popular Russian military/national security news portal.'),
    ('livejournal.com',    'LiveJournal',               'ru', 'Articles from various LJ accounts cited in Substack posts/comments.'),
    ('katyusha.org',       'Katyusha',                  'ru', 'Russian conservative/Orthodox news outlet.'),
    ('wsj.com',            'Wall Street Journal',       'en', 'Paywalled; scraped from archive.ph or direct access.'),
    ('thenation.com',      'The Nation',                'en', 'Includes landmark "Harvard Boys Do Russia" piece.'),
    ('topcor.ru',          'TopCor',                    'ru', 'Russian analytical portal.'),
    ('washingtonpost.com', 'Washington Post',           'en', 'Scraped from archive.ph or direct access.')
ON CONFLICT (domain) DO NOTHING;

-- ──────────────────────────────────────────────────────────────────────────────
-- Table: articles  (unified external articles)
-- ──────────────────────────────────────────────────────────────────────────────
CREATE TABLE external.articles (
    id              BIGSERIAL PRIMARY KEY,
    source_id       INT NOT NULL REFERENCES external.sources(id),

    -- Identity
    source_article_id TEXT,
    url             TEXT,
    slug            TEXT,
    file_path       TEXT,

    -- Content
    title           TEXT,
    subtitle        TEXT,
    author          TEXT,
    publish_date    TIMESTAMPTZ,
    text            TEXT,

    -- Metrics
    views           INT,
    likes           INT,
    comment_count   INT,

    -- Structured data
    image_urls      JSONB DEFAULT '[]'::JSONB,
    external_links  JSONB DEFAULT '[]'::JSONB,
    internal_links  JSONB DEFAULT '[]'::JSONB,

    -- Recovery metadata
    recovery_source TEXT,                           -- 'wayback', 'livejournal_mirror', 'substack_translation'
    is_deleted      BOOLEAN DEFAULT false,

    -- Raw data preservation
    raw_json        JSONB,

    -- Housekeeping
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW(),

    UNIQUE (source_id, url),
    UNIQUE (source_id, file_path)
);

-- ──────────────────────────────────────────────────────────────────────────────
-- Table: comments  (external article comments)
-- ──────────────────────────────────────────────────────────────────────────────
CREATE TABLE external.comments (
    id                  BIGSERIAL PRIMARY KEY,
    article_id          BIGINT NOT NULL REFERENCES external.articles(id) ON DELETE CASCADE,

    source_comment_id   TEXT,
    username            TEXT,
    comment_date        TEXT,                        -- kept as text (formats vary across sources)
    comment_date_parsed TIMESTAMPTZ,                 -- normalized (best-effort)

    body                TEXT NOT NULL,
    rating              TEXT,                        -- e.g. TopWar: '+21', '-3'

    created_at          TIMESTAMPTZ DEFAULT NOW()
);


-- ============================================================================
-- Indexes: external schema
-- ============================================================================

-- Articles: full-text search (Russian + English)
CREATE INDEX idx_ext_articles_text_fts     ON external.articles
    USING GIN (to_tsvector('russian', COALESCE(text, '')));
CREATE INDEX idx_ext_articles_text_fts_en  ON external.articles
    USING GIN (to_tsvector('english', COALESCE(text, '')));
CREATE INDEX idx_ext_articles_title_fts    ON external.articles
    USING GIN (to_tsvector('russian', COALESCE(title, '') || ' ' || COALESCE(subtitle, '')));

-- Articles: lookup
CREATE INDEX idx_ext_articles_source       ON external.articles(source_id);
CREATE INDEX idx_ext_articles_date         ON external.articles(publish_date);
CREATE INDEX idx_ext_articles_author       ON external.articles(author);
CREATE INDEX idx_ext_articles_domain_url   ON external.articles(source_id, url);
CREATE INDEX idx_ext_articles_deleted      ON external.articles(is_deleted) WHERE is_deleted = true;
CREATE INDEX idx_ext_articles_source_aid   ON external.articles(source_id, source_article_id)
    WHERE source_article_id IS NOT NULL;

-- Articles: JSONB
CREATE INDEX idx_ext_articles_ext_links    ON external.articles USING GIN (external_links);

-- Comments: full-text search (Russian + English)
CREATE INDEX idx_ext_comments_body_fts     ON external.comments
    USING GIN (to_tsvector('russian', body));
CREATE INDEX idx_ext_comments_body_fts_en  ON external.comments
    USING GIN (to_tsvector('english', body));

-- Comments: lookup
CREATE INDEX idx_ext_comments_article      ON external.comments(article_id);
CREATE INDEX idx_ext_comments_user         ON external.comments(username);
CREATE INDEX idx_ext_comments_source_id    ON external.comments(source_comment_id)
    WHERE source_comment_id IS NOT NULL;


-- ============================================================================
-- Views: external schema
-- ============================================================================

-- Articles with source info
CREATE OR REPLACE VIEW external.articles_with_source AS
SELECT
    a.*,
    s.domain,
    s.name AS source_name,
    s.language,
    (SELECT COUNT(*) FROM external.comments c WHERE c.article_id = a.id) AS loaded_comment_count
FROM external.articles a
JOIN external.sources s ON s.id = a.source_id;

-- Cross-reference: external articles cited in Substack posts
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
