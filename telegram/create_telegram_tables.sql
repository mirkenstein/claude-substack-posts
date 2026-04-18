-- Telegram channel data schema
-- Source: Telegram Desktop JSON exports
-- Usage: psql -d substack -f telegram/create_telegram_tables.sql

CREATE SCHEMA IF NOT EXISTS telegram;

-- ═══════════════════════════════════════════════════════════
-- Core tables
-- ═══════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS telegram.channels (
    id            BIGINT PRIMARY KEY,
    name          TEXT NOT NULL,
    type          TEXT NOT NULL,
    username      TEXT,
    language      TEXT,
    description   TEXT,
    imported_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS telegram.messages (
    channel_id                 BIGINT NOT NULL REFERENCES telegram.channels(id),
    id                         BIGINT NOT NULL,
    PRIMARY KEY (channel_id, id),
    type                       TEXT NOT NULL,
    posted_at                  TIMESTAMPTZ NOT NULL,
    posted_unixtime            BIGINT,
    edited_at                  TIMESTAMPTZ,
    edited_unixtime            BIGINT,
    from_name                  TEXT,
    from_id                    TEXT,
    forwarded_from             TEXT,
    forwarded_from_id          TEXT,
    text_plain                 TEXT,
    actor                      TEXT,
    actor_id                   TEXT,
    action                     TEXT,
    action_target_message_id   BIGINT,
    loaded_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS telegram.message_media (
    id                  SERIAL PRIMARY KEY,
    channel_id          BIGINT NOT NULL,
    message_id          BIGINT NOT NULL,
    FOREIGN KEY (channel_id, message_id)
        REFERENCES telegram.messages(channel_id, id),
    media_type          TEXT NOT NULL,
    mime_type           TEXT,
    file_name           TEXT,
    file_size           BIGINT,
    file_path           TEXT,
    width               INT,
    height              INT,
    duration_seconds    INT,
    thumbnail_path      TEXT,
    thumbnail_file_size BIGINT,
    UNIQUE (channel_id, message_id, media_type)
);

CREATE TABLE IF NOT EXISTS telegram.message_links (
    id           SERIAL PRIMARY KEY,
    channel_id   BIGINT NOT NULL,
    message_id   BIGINT NOT NULL,
    FOREIGN KEY (channel_id, message_id)
        REFERENCES telegram.messages(channel_id, id),
    link_text    TEXT,
    href         TEXT NOT NULL,
    entity_type  TEXT,
    UNIQUE (channel_id, message_id, href, link_text)
);

CREATE TABLE IF NOT EXISTS telegram.message_entities (
    id           SERIAL PRIMARY KEY,
    channel_id   BIGINT NOT NULL,
    message_id   BIGINT NOT NULL,
    FOREIGN KEY (channel_id, message_id)
        REFERENCES telegram.messages(channel_id, id),
    position     INT NOT NULL,
    entity_type  TEXT NOT NULL,
    text         TEXT
);

CREATE TABLE IF NOT EXISTS telegram.message_reactions (
    id            SERIAL PRIMARY KEY,
    channel_id    BIGINT NOT NULL,
    message_id    BIGINT NOT NULL,
    FOREIGN KEY (channel_id, message_id)
        REFERENCES telegram.messages(channel_id, id),
    emoji         TEXT NOT NULL,
    reaction_type TEXT NOT NULL DEFAULT 'emoji',
    count         INT  NOT NULL,
    UNIQUE (channel_id, message_id, emoji)
);

-- ═══════════════════════════════════════════════════════════
-- Transcript tables (future use)
-- ═══════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS telegram.media_transcripts (
    id               SERIAL PRIMARY KEY,
    channel_id       BIGINT NOT NULL REFERENCES telegram.channels(id),
    media_id         INT NOT NULL REFERENCES telegram.message_media(id),
    transcript_type  TEXT NOT NULL,
    language         TEXT,
    transcript_text  TEXT,
    speaker_map      JSONB,
    transcribed_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    model_version    TEXT
);

CREATE TABLE IF NOT EXISTS telegram.media_transcript_segments (
    id              SERIAL PRIMARY KEY,
    transcript_id   INT NOT NULL REFERENCES telegram.media_transcripts(id),
    segment_index   INT NOT NULL,
    start_seconds   NUMERIC(8,2),
    end_seconds     NUMERIC(8,2),
    speaker         TEXT,
    text            TEXT NOT NULL
);

-- ═══════════════════════════════════════════════════════════
-- Indexes
-- ═══════════════════════════════════════════════════════════

CREATE INDEX IF NOT EXISTS idx_messages_posted_at
    ON telegram.messages (channel_id, posted_at);

CREATE INDEX IF NOT EXISTS idx_messages_forwarded_from_id
    ON telegram.messages (forwarded_from_id)
    WHERE forwarded_from_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_messages_fts
    ON telegram.messages
    USING GIN (to_tsvector('russian', COALESCE(text_plain, '')));

CREATE INDEX IF NOT EXISTS idx_media_type
    ON telegram.message_media (channel_id, media_type);

CREATE INDEX IF NOT EXISTS idx_reactions_message
    ON telegram.message_reactions (channel_id, message_id);

CREATE INDEX IF NOT EXISTS idx_links_href
    ON telegram.message_links (href);

CREATE INDEX IF NOT EXISTS idx_transcripts_media_id
    ON telegram.media_transcripts (media_id);
