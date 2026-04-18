-- =============================================================================
-- Twitter/X Archive Schema — substack.twitter_*
-- Status: APPLIED to database (tables and views exist, data not yet loaded)
-- =============================================================================
--
-- Purpose: Store X/Twitter timeline scrapes alongside the Substack archive.
-- First dataset: @marmar_ae (Marko Marjanović), June 14 – July 15, 2025.
-- 3,000 tweets: 415 from Marko, 2,585 context (interlocutors + thread ecosystem).
-- Plus ~151 embedded quoted tweets extracted from quotedTweet fields.
--
-- Design principles:
--   - is_primary_author flag separates the studied account from context
--   - parent_text / quoted_text are denormalized INTO each tweet row so that
--     short replies are intelligible without joins
--   - conversation_root_id groups all tweets in a thread
--   - Extensible to future scrapes of other accounts via scrape_source
--
-- =============================================================================

-- 1. USER PROFILES (snapshot at scrape time)
-- Already created: substack.twitter_users
--   PK: user_id (text, X platform ID)
--   Key: username, display_name, bio, website, followers, following
--   Link: substack_author_handle → connects to substack.authors
--   Note: Only 1 user profile in this scrape (Marko's). Others lack the
--         user sub-object in the JSON. Username is always available.

-- 2. TWEETS (core table)  
-- Already created: substack.twitter_tweets
--   PK: tweet_id (text, X platform tweet ID)
--   Content: content (tweet text), tweet_date
--   Type flags: is_reply, is_quote, is_retweet, is_pinned
--   Threading: replying_to (text[]), parent_tweet_id, conversation_root_id
--   Engagement: views, likes, reply_count, retweets, quote_count
--   Embedded media: links (text[]), image_urls, video_urls, card_url/title/description
--   Inline context: quoted_tweet_id/username/text, parent_username/text
--   Research: is_primary_author, scrape_source, verified
--
--   INDEXES:
--     - username, tweet_date, (is_primary_author + tweet_date)
--     - conversation_root_id, parent_tweet_id, quoted_tweet_id
--     - GIN full-text on content
--     - (is_reply, is_quote, is_retweet) for type filtering

-- 3. SCRAPE LOG (ETL tracking)
-- Already created: substack.twitter_scrape_log
--   Tracks each ingestion batch: source file, date range, counts

-- 4. VIEWS

-- twitter_conversations: Thread-level aggregation
--   conversation_root_id, tweet_count, participant_count, primary_author_tweets,
--   first/last tweet, participants array, total_likes/views, root tweet text

-- twitter_marko_with_context: Research workhorse
--   All Marko tweets with inline parent/quoted context
--   Derived tweet_type: original | reply | self_thread | quote_tweet | retweet
--   Includes char_length, links, image_urls, card_title

-- =============================================================================
-- EXAMPLE QUERIES
-- =============================================================================

-- All Marko originals by engagement
-- SELECT marko_text, likes, views FROM substack.twitter_marko_with_context
-- WHERE tweet_type = 'original' ORDER BY likes DESC;

-- Marko's replies WITH the tweet he was responding to
-- SELECT tweet_date, marko_text, parent_username, parent_text
-- FROM substack.twitter_marko_with_context
-- WHERE tweet_type = 'reply' AND parent_text IS NOT NULL
-- ORDER BY likes DESC;

-- Marko's quote tweets with the original
-- SELECT marko_text, quoted_username, quoted_text, likes
-- FROM substack.twitter_marko_with_context
-- WHERE tweet_type = 'quote_tweet' ORDER BY likes DESC;

-- Conversation threads with 5+ tweets
-- SELECT * FROM substack.twitter_conversations
-- WHERE tweet_count >= 5 ORDER BY total_likes DESC;

-- Full-text search across all tweets
-- SELECT username, content, tweet_date
-- FROM substack.twitter_tweets
-- WHERE to_tsvector('english', coalesce(content, '')) @@ plainto_tsquery('english', 'Fordow enrichment bomb')
-- ORDER BY tweet_date;

-- Reconstruct a conversation thread chronologically
-- SELECT tweet_date, username, content, is_primary_author
-- FROM substack.twitter_tweets
-- WHERE conversation_root_id = '1944819613344391186'
-- ORDER BY tweet_date;

-- Who does Marko argue with most?
-- SELECT unnest(replying_to) AS target, count(*) AS replies
-- FROM substack.twitter_tweets
-- WHERE is_primary_author AND is_reply
-- GROUP BY target ORDER BY replies DESC LIMIT 20;
