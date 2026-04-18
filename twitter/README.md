```shell


python3 load_twitter_all.py \
    --db "postgresql://postgres:postgres@localhost:5432/substack" \
    --d1 ./data/dataset_twitter-scraper_2026-02-20_05-29-55-458.json \
    --d2 ./data/dataset_twitter-scraper_2026-02-20_06-48-42-608.json \
    --d3 ./data/dataset_twitter-scraper_2026-02-20_07-47-30-127.json

```

```shell
# Dry run first:
python3 load_twitter_apify.py --db "postgresql://postgres:postgres@localhost:5432/substack" --dry-run \
    .data/2_feeds_json/dataset_twitter-scraper_2026-02-20_21-30-06-203.json
 # Real load + backfill threading on existing rows:
python3 load_twitter_apify.py --db "postgresql://..." --backfill \
    threads-feed-2024.json main-feed-2024.json

python3 load_twitter_apify.py --db "postgresql://postgres:postgres@localhost:5432/substack" --backfill    ./data/2_feeds_json/threads-dataset_twitter-scraper_2026-02-20_21-31-54-013.json 
python3 load_twitter_apify.py --db "postgresql://postgres:postgres@localhost:5432/substack" --backfill     ./data/2_feeds_json/dataset_twitter-scraper_2026-02-20_21-30-06-203.json

```

```shell
Backfilling threading data on existing rows...
  Updated 11469 existing rows with threading data.

=======================================================
LOAD COMPLETE
=======================================================
Before:             14384 tweets
After:              14384 tweets (+0 new)
Marko tweets:       2548
User profiles:      1
Date range:         2015-04-26 10:16:00+00:00 → 2025-12-26 02:04:00+00:00
Threading backfill: 11469 rows updated

By source:
  apify_threads-dataset_twitter-scraper_2026-02-20_21-31-54-013: 7536
  apify_threads-dataset_twitter-scraper_2026-02-20_21-31-54-013_embedded_quoted: 198
  marmar_ae_d1_conversation: 3151
  marmar_ae_d2_timeline: 1174
  marmar_ae_d3_hybrid: 2325

```
 python3 load_twitter_apify.py --db "postgresql://postgres:postgres@localhost:5432/substack" --backfill    ./data/2_feeds_json/threads-dataset_twitter-scraper_2026-02-20_21-31-54-013.json 
Processing 1 file(s)...
  threads-dataset_twitter-scraper_2026-02-20_21-31-54-013.json (apify_threads-dataset_twitter-scraper_2026-02-20_21-31-54-013):
    11981 main + 597 embedded_qt + 0 embedded_parent = 12578 rows (2203 Marko, 0 skipped dupes)

Total rows to insert: 12578
Total unique IDs seen: 12578
User profiles: 0

Connecting to database...
Existing tweets in DB: 6650
Inserting 12578 tweets in batches of 100...
  500/12578 (3%)
  1000/12578 (7%)
  1500/12578 (11%)
  2000/12578 (15%)
  2500/12578 (19%)
  3000/12578 (23%)
  3500/12578 (27%)
  4000/12578 (31%)
  4500/12578 (35%)
  5000/12578 (39%)
  5500/12578 (43%)
  6000/12578 (47%)
  6500/12578 (51%)
  7000/12578 (55%)
  7500/12578 (59%)
  8000/12578 (63%)
  8500/12578 (67%)
  9000/12578 (71%)
  9500/12578 (75%)
  10000/12578 (79%)
  10500/12578 (83%)
  11000/12578 (87%)
  11500/12578 (91%)
  12000/12578 (95%)
  12500/12578 (99%)
  12578/12578 (100%)

Backfilling threading data on existing rows...
  Updated 11671 existing rows with threading data.

=======================================================
LOAD COMPLETE
=======================================================
Before:             6650 tweets
After:              14384 tweets (+7734 new)
Marko tweets:       2548
User profiles:      1
Date range:         2015-04-26 10:16:00+00:00 → 2025-12-26 02:04:00+00:00
Threading backfill: 11671 rows updated

By source:
  apify_threads-dataset_twitter-scraper_2026-02-20_21-31-54-013: 7536
  apify_threads-dataset_twitter-scraper_2026-02-20_21-31-54-013_embedded_quoted: 198
  marmar_ae_d1_conversation: 3151
  marmar_ae_d2_timeline: 1174
  marmar_ae_d3_hybrid: 2325
...

Key differences from the original script:

Accepts any number of files as positional args (no --d1/d2/d3)
Extracts embedded parent tweets (replyToTweet) as standalone rows too
Retweet handling — correctly marks Marko as primary author even when username is the original author
--backfill flag — UPDATEs existing rows with threading data (parent_tweet_id, conversation_root_id, replying_to, etc.) using COALESCE (only fills NULLs)
Auto-generates scrape_source label from filename

When your scrapes finish with more data, just run the same command with the additional files appended.