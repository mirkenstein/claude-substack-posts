-- ============================================================================
-- Substack Database Schema
-- ============================================================================
-- Database: substack
-- Schema:   substack
-- Purpose:  Store Substack posts, comments, and extracted/classified links
-- ============================================================================

CREATE SCHEMA IF NOT EXISTS substack;

-- ============================================================================
-- Table: publications
-- ============================================================================
CREATE TABLE substack.publications (
    id              BIGINT PRIMARY KEY,
    subdomain       VARCHAR(255) NOT NULL UNIQUE,
    name            VARCHAR(500) NOT NULL,
    custom_domain   VARCHAR(255),
    logo_url        TEXT,
    hero_text       TEXT,
    loaded_at       TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================================================
-- Table: authors
-- ============================================================================
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

-- ============================================================================
-- Table: posts
-- ============================================================================
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
    loaded_at           TIMESTAMPTZ DEFAULT NOW(),

    CONSTRAINT unique_publication_slug UNIQUE (publication_id, slug)
);

-- ============================================================================
-- Table: post_tags
-- ============================================================================
CREATE TABLE substack.post_tags (
    post_id     BIGINT NOT NULL REFERENCES substack.posts(id) ON DELETE CASCADE,
    tag         VARCHAR(255) NOT NULL,
    PRIMARY KEY (post_id, tag)
);

-- ============================================================================
-- Table: comments
-- ============================================================================
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

-- ============================================================================
-- Table: post_links  (links extracted from post HTML content)
-- ============================================================================
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

-- ============================================================================
-- Table: comment_links  (links extracted from comment bodies)
-- ============================================================================
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

-- ============================================================================
-- Table: load_status  (tracks which JSON files have been loaded)
-- ============================================================================
CREATE TABLE substack.load_status (
    file_path       TEXT PRIMARY KEY,
    post_id         BIGINT,
    loaded_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    status          VARCHAR(20) NOT NULL,
    error_message   TEXT
);

-- ============================================================================
-- Indexes
-- ============================================================================

-- Posts
CREATE INDEX idx_posts_publication   ON substack.posts(publication_id);
CREATE INDEX idx_posts_author        ON substack.posts(primary_author_id);
CREATE INDEX idx_posts_date          ON substack.posts(post_date DESC);
CREATE INDEX idx_posts_slug          ON substack.posts(slug);
CREATE INDEX idx_posts_audience      ON substack.posts(audience);
CREATE INDEX idx_posts_fts           ON substack.posts
    USING gin(to_tsvector('english', title || ' ' || coalesce(subtitle, '')));

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

-- ============================================================================
-- Views
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
