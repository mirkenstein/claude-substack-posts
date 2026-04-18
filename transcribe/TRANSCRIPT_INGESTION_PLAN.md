# Podcast Transcript Ingestion Plan

## Overview

Ingest podcast transcripts (from the "Worst of All Worlds" / WOAW podcast and potentially other audio sources) into the existing Substack database infrastructure. Two storage layers: Postgres for structured data, Weaviate for semantic search.

The transcripts come as JSON files with speaker-diarized segments (speaker ID, start/end timestamps, text). A companion speaker map identifies speakers by name.

---

## INPUT FORMAT

Each transcript arrives as two items:

### 1. Transcript JSON
```json
{
  "speaker_map": {
    "SPEAKER_00": "Marco",
    "SPEAKER_01": "Rolo",
    "SPEAKER_02": "Slavsquat",
    "Unknown": "Speaker 4"
  },
  "segments": [
    {
      "start": 2.68,
      "end": 90.04,
      "speaker": "SPEAKER_01",
      "text": "Welcome back, everybody..."
    }
  ]
}
```

### 2. Episode Metadata (provided manually or inferred)
- **Episode title** (e.g., "Kherson Withdrawal — WOAW #6")
- **Episode date** (e.g., "2022-11-10")
- **Matched post ID** in `substack.posts` if one exists (e.g., `83709910`)
- **Publication** (e.g., "anti-empire" or "woaw")

---

## STEP 1: Postgres — Store Full Transcript as Post Content

### Goal
The WOAW episodes already exist as rows in `substack.posts` but many have empty or near-empty `content_html`. We want to populate them with the full transcript text.

### Instructions

1. **Check the existing post** by its `id` (post ID). Confirm it exists and has little/no content:
   ```sql
   SELECT id, title, post_date, LENGTH(content_html) AS html_len
   FROM substack.posts WHERE id = <post_id>;
   ```

2. **Build the full transcript text blob.** From the JSON segments, concatenate all turns in order into a single text block. Format each turn as:
   ```
   **[Speaker Name] [HH:MM:SS]:**
   Text of what they said.

   ```
   Use the `speaker_map` to resolve speaker IDs to names. Convert `start` time (float seconds) to `HH:MM:SS` format.

3. **Wrap in basic HTML** (since the column is `content_html`):
   ```html
   <div class="transcript">
   <p><strong>[Marco] [00:03:51]:</strong></p>
   <p>Yeah, I saw what you wrote on it...</p>
   ...
   </div>
   ```

4. **UPDATE the existing post row:**
   ```sql
   UPDATE substack.posts
   SET content_html = '<full transcript HTML>'
   WHERE id = <post_id>;
   ```

5. **If no matching post exists** (e.g., for a newly discovered episode), INSERT a new row. You'll need to match the schema of `substack.posts`. Key fields:
   - `id` — use the episode's Substack post ID if known, or generate one
   - `publication_id` — use the anti-empire publication ID (`SELECT id FROM substack.publications WHERE subdomain = 'anti-empire'`)
   - `primary_author_id` — use Marko Marjanović's author ID (since he hosted on Anti-Empire)
   - `title`, `subtitle`, `post_date`, `slug`
   - `content_html` — the full transcript
   - `audience` — typically `'everyone'`
   - `canonical_url`

### Quality Filtering

Some ASR segments are garbled (hallucinated text, foreign characters, gibberish). Before building the text blob:
- Flag segments where the text contains non-Latin/Cyrillic unicode blocks (e.g., Japanese characters)
- Flag segments that are very short AND make no contextual sense
- Include garbled segments in the full text blob but wrap them in a marker: `<span class="garbled">[inaudible/garbled]</span>` — this preserves the timeline without polluting search

---

## STEP 2: Postgres — Create and Populate `transcript_lines` Table

### Goal
Store each individual speaker turn as a separate row for speaker-level queries and analysis.

### Create the Table

```sql
CREATE TABLE IF NOT EXISTS substack.transcript_lines (
    id SERIAL PRIMARY KEY,
    episode_post_id BIGINT NOT NULL REFERENCES substack.posts(id),
    turn_index INTEGER NOT NULL,
    speaker TEXT NOT NULL,
    start_time FLOAT NOT NULL,
    end_time FLOAT NOT NULL,
    content TEXT NOT NULL,
    word_count INTEGER,
    quality TEXT DEFAULT 'clean' CHECK (quality IN ('clean', 'garbled', 'short')),
    
    UNIQUE (episode_post_id, turn_index)
);

-- Indexes for common query patterns
CREATE INDEX idx_transcript_lines_episode ON substack.transcript_lines(episode_post_id);
CREATE INDEX idx_transcript_lines_speaker ON substack.transcript_lines(speaker);
CREATE INDEX idx_transcript_lines_content_search ON substack.transcript_lines USING gin(to_tsvector('english', content));
```

### Populate the Table

For each segment in the transcript JSON:

1. Resolve `speaker` ID to name using `speaker_map`
2. Compute `word_count` as simple whitespace split count
3. Determine `quality`:
   - `'garbled'` — if the text contains non-Latin/Cyrillic characters, obvious hallucination patterns, or is contextually nonsensical gibberish
   - `'short'` — if word_count <= 3 (e.g., "Right.", "Yeah.", "Fair enough.")
   - `'clean'` — everything else
4. INSERT:
   ```sql
   INSERT INTO substack.transcript_lines 
       (episode_post_id, turn_index, speaker, start_time, end_time, content, word_count, quality)
   VALUES
       (<post_id>, 0, 'Rolo', 2.68, 90.04, 'Welcome back, everybody...', 157, 'clean'),
       (<post_id>, 1, 'Rolo', 93.66, 114.88, 'Kherson is going really well...', 52, 'garbled'),
       ...
   ```

### Verification Queries

After ingestion, verify with:
```sql
-- Turn count and speaker breakdown
SELECT speaker, COUNT(*) AS turns, SUM(word_count) AS total_words, 
       AVG(word_count)::int AS avg_words
FROM substack.transcript_lines
WHERE episode_post_id = <post_id>
GROUP BY speaker ORDER BY total_words DESC;

-- Check quality distribution
SELECT quality, COUNT(*) FROM substack.transcript_lines
WHERE episode_post_id = <post_id>
GROUP BY quality;
```

---

## STEP 3: Weaviate — Chunk and Insert Full Transcript

### Goal
Get the full transcript into the `SubstackPostEngRu` collection so it's discoverable via semantic and keyword search alongside blog posts.

### Instructions

1. **Use the same chunking approach** already established for SubstackPostEngRu. Check the existing collection schema:
   ```
   collection: SubstackPostEngRu
   ```
   It should have properties like: `postId`, `title`, `subtitle`, `content`, `chunkNumber`, `totalChunks`, `chunkTokens`, `postDate`, `authorName`, `publicationName`, `subdomain`, `audience`, `canonicalUrl`, `slug`, `wordcount`, `commentCount`, `restacks`.

2. **Prepare the plain text** version of the transcript (same content as the HTML blob but stripped of tags). This is what gets chunked.

3. **Chunk the text** into segments of approximately 500-700 tokens each. Use overlap of ~50-100 tokens between chunks to preserve context across chunk boundaries. Since this is a conversation, try to break chunks at speaker transitions when possible (i.e., don't split mid-sentence within a speaker's turn).

4. **Insert each chunk** as a Weaviate object in `SubstackPostEngRu` with:
   - `postId`: the post ID (as string)
   - `title`: episode title
   - `subtitle`: episode subtitle
   - `content`: the chunk text
   - `chunkNumber`: 0, 1, 2, ...
   - `totalChunks`: total count
   - `chunkTokens`: approximate token count for this chunk
   - `postDate`: episode date in ISO format
   - `authorName`: "Marko Marjanović" (primary host on Anti-Empire)
   - `publicationName`: "Anti-Empire" (or "The Worst of All Worlds")
   - `subdomain`: "anti-empire"
   - `audience`: "everyone"
   - `canonicalUrl`: the post URL
   - `slug`: the post slug
   - `wordcount`: total transcript word count
   - `commentCount`: from the post record
   - `restacks`: from the post record

5. **DO NOT put individual transcript lines into Weaviate.** The per-speaker granularity lives only in Postgres. Weaviate gets the full conversational flow for semantic discovery.

---

## STEP 4: Handling Multiple Episodes

When processing a batch of transcripts:

1. **Match each transcript to its existing post.** The WOAW episodes in the database are:

   | Post ID    | Title | Date |
   |------------|-------|------|
   | 84805944   | WOAW #1 — Deep Thoughts About Ukraine | 2022-09-23 |
   | 84809249   | WOAW #2 — Liman Was a Good Retreat | 2022-10-03 |
   | 83709910   | WOAW #6 — Kherson Withdrawal | 2022-11-10 |
   | 84477662   | WOAW #7 — Does the Kremlin Want Out? | 2022-11-14 |
   | 88175981   | WOAW #8 — Second Wave of Mobilization | 2022-12-03 |
   | 88973737   | WOAW #9 — Lets Just Look at Results | 2022-12-08 |
   | 83741179   | Kherson Withdrawal Podcast (unnumbered) | 2022-11-10 |
   | 89776413   | Wagner Sledgehammer (unnumbered) | 2022-12-10 |

   Cross-posted on Slavland Chronicles:
   | Post ID    | Title | Date |
   |------------|-------|------|
   | (check DB) | WOAW Episode 2 | 2022-10-04 |
   | (check DB) | WOAW Episode 3 | 2022-10-13 |
   | 79784996   | WOAW Episode 5? | 2022-10-23 |

2. **For each transcript file**, identify which episode it is (by date, content, or filename), find the matching post ID, and run Steps 1-3.

3. **Idempotency**: Before inserting, check if transcript_lines already exist for that episode_post_id. If so, DELETE existing and re-insert (or skip).
   ```sql
   DELETE FROM substack.transcript_lines WHERE episode_post_id = <post_id>;
   ```
   Similarly, for Weaviate, check if chunks for that postId already exist and delete before re-inserting.

---

## STEP 5: Verification & Testing

After ingesting a transcript, verify the full pipeline:

### Postgres Checks
```sql
-- Full transcript stored in posts table
SELECT id, title, LENGTH(content_html) AS html_size 
FROM substack.posts WHERE id = <post_id>;

-- Transcript lines populated
SELECT COUNT(*) AS total_turns, 
       COUNT(DISTINCT speaker) AS speakers,
       SUM(word_count) AS total_words
FROM substack.transcript_lines WHERE episode_post_id = <post_id>;

-- Speaker breakdown
SELECT speaker, COUNT(*) AS turns, SUM(word_count) AS words
FROM substack.transcript_lines 
WHERE episode_post_id = <post_id> AND quality = 'clean'
GROUP BY speaker ORDER BY words DESC;

-- Test a speaker-specific search
SELECT speaker, start_time, substring(content for 200) 
FROM substack.transcript_lines
WHERE episode_post_id = <post_id> 
  AND speaker = 'Marco' 
  AND content ILIKE '%mobilization%';
```

### Weaviate Checks
- Keyword search: `"Kherson withdrawal bridgehead"` in SubstackPostEngRu — should return chunks from the transcript
- Semantic search: `"Russia losing territory because of HIMARS"` — should return relevant chunks
- Verify chunk count matches expected total

---

## NOTES

- **Speaker name normalization**: Always use consistent names across all episodes: `Marco`, `Rolo`, `Slavsquat`. If a guest appears, use their actual name. `Unknown` for unidentified speakers.
- **ASR artifacts**: The transcription tool sometimes hallucinates foreign-language text (Japanese, Spanish, etc.) or produces gibberish during crosstalk or audio glitches. Always mark these as `quality = 'garbled'`.
- **Future expansion**: This same pipeline works for any podcast or audio content. The schema is not WOAW-specific. If Slavsquat interviews someone, or Rurik does a solo episode, it ingests the same way.
- **The `transcript_lines` table can be queried cross-episode**: e.g., "everything Marco ever said about Nabiullina across all episodes" is a single query once multiple episodes are loaded.
