-- ============================================================
-- Twitter Tables for substack schema
-- ============================================================
CREATE SCHEMA twitter;
-- 1. twitter_users
CREATE TABLE twitter.twitter_users (
    user_id         text        NOT NULL,
    username        text        NOT NULL,
    display_name    text,
    bio             text,
    website         text,
    location        text,
    join_date       timestamptz,
    verified        boolean     DEFAULT false,
    followers       integer,
    following       integer,
    total_tweets    integer,
    total_likes     integer,
    avatar_url      text,
    banner_url      text,
    substack_author_handle text,
    scraped_at      timestamptz DEFAULT now(),
    notes           text,

    CONSTRAINT twitter_users_pkey PRIMARY KEY (user_id)
);

CREATE INDEX idx_twitter_users_username
    ON twitter.twitter_users USING btree (username);

CREATE INDEX idx_twitter_users_substack
    ON twitter.twitter_users USING btree (substack_author_handle)
    WHERE substack_author_handle IS NOT NULL;


-- 2. twitter_tweets
CREATE TABLE twitter.twitter_tweets (
    tweet_id            text        NOT NULL,
    url                 text        NOT NULL,
    user_id             text,
    username            text        NOT NULL,
    display_name        text,
    content             text,
    tweet_date          timestamptz NOT NULL,
    is_reply            boolean     DEFAULT false,
    is_quote            boolean     DEFAULT false,
    is_retweet          boolean     DEFAULT false,
    is_pinned           boolean     DEFAULT false,
    replying_to         text[],
    parent_tweet_id     text,
    conversation_root_id text,
    views               integer     DEFAULT 0,
    likes               integer     DEFAULT 0,
    reply_count         integer     DEFAULT 0,
    retweets            integer     DEFAULT 0,
    quote_count         integer     DEFAULT 0,
    links               text[],
    image_urls          text[],
    video_urls          text[],
    card_url            text,
    card_title          text,
    card_description    text,
    quoted_tweet_id     text,
    quoted_username     text,
    quoted_text         text,
    parent_username     text,
    parent_text         text,
    is_primary_author   boolean     DEFAULT false,
    scrape_source       text,
    verified            boolean     DEFAULT false,
    loaded_at           timestamptz DEFAULT now(),
    card_domain         text,
    card_image_url      text,
    quoted_tweet_url    text,
    quoted_links        text[],
    quoted_image_urls   text[],
    parent_tweet_url    text,
    parent_links        text[],
    parent_image_urls   text[],
    parent_likes        integer,
    parent_views        integer,
    is_main_tweet       boolean,
    is_root_thread      boolean,
    raw_json            jsonb,

    CONSTRAINT twitter_tweets_pkey PRIMARY KEY (tweet_id)
);

CREATE INDEX idx_tweets_username
    ON twitter.twitter_tweets USING btree (username);

CREATE INDEX idx_tweets_date
    ON twitter.twitter_tweets USING btree (tweet_date);

CREATE INDEX idx_tweets_primary
    ON twitter.twitter_tweets USING btree (is_primary_author, tweet_date);

CREATE INDEX idx_tweets_conversation
    ON twitter.twitter_tweets USING btree (conversation_root_id)
    WHERE conversation_root_id IS NOT NULL;

CREATE INDEX idx_tweets_parent
    ON twitter.twitter_tweets USING btree (parent_tweet_id)
    WHERE parent_tweet_id IS NOT NULL;

CREATE INDEX idx_tweets_quoted
    ON twitter.twitter_tweets USING btree (quoted_tweet_id)
    WHERE quoted_tweet_id IS NOT NULL;

CREATE INDEX idx_tweets_type
    ON twitter.twitter_tweets USING btree (is_reply, is_quote, is_retweet);

CREATE INDEX idx_tweets_content_fts
    ON twitter.twitter_tweets USING gin (to_tsvector('english', COALESCE(content, '')));

CREATE INDEX idx_tweets_raw_json
    ON twitter.twitter_tweets USING gin (raw_json jsonb_path_ops);


-- 3. twitter_scrape_log
CREATE SEQUENCE IF NOT EXISTS twitter.twitter_scrape_log_id_seq;

CREATE TABLE twitter.twitter_scrape_log (
    id                  integer     NOT NULL DEFAULT nextval('twitter.twitter_scrape_log_id_seq'),
    scrape_source       text        NOT NULL,
    source_file         text,
    account_scraped     text        NOT NULL,
    date_range_start    date,
    date_range_end      date,
    total_tweets        integer,
    primary_tweets      integer,
    context_tweets      integer,
    embedded_tweets     integer,
    loaded_at           timestamptz DEFAULT now(),
    notes               text,

    CONSTRAINT twitter_scrape_log_pkey PRIMARY KEY (id)
);

ALTER SEQUENCE twitter.twitter_scrape_log_id_seq OWNED BY twitter.twitter_scrape_log.id;
