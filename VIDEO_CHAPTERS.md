# Video Chapters Pipeline

How YouTube video chapters are parsed, assembled, chunked, and stored across all backends.

## Source Data (PostgreSQL)

Three tables in the `youtube` schema provide the raw material:

### `youtube.video_transcripts`

Video metadata and full transcript text. The `description` field is the source for chapter parsing.

| Column | Type | Notes |
|---|---|---|
| `video_id` | VARCHAR | YouTube video ID (PK) |
| `title` | VARCHAR | Video title |
| `description` | TEXT | Raw YouTube description — source for chapter timestamp extraction |
| `channel_name` | VARCHAR | Channel name |
| `url` | TEXT | YouTube URL |
| `upload_date` | DATE | Publication date |
| `transcript` | TEXT | Full concatenated transcript (used by fixed-window chunking scripts, not chapter scripts) |

### `youtube.video_chapters`

Structured chapter markers extracted from video descriptions.

| Column | Type | Notes |
|---|---|---|
| `video_id` | VARCHAR | FK to video_transcripts |
| `position` | INTEGER | Zero-based chapter number (0, 1, 2, ...) |
| `start_seconds` | INTEGER | Chapter start time in seconds |
| `title` | VARCHAR | Chapter title text |

Unique constraint: `(video_id, position, start_seconds, title)` with `ON CONFLICT DO NOTHING`.

### `youtube.transcript_segments`

Atomic timed transcript segments with sub-second precision.

| Column | Type | Notes |
|---|---|---|
| `video_id` | VARCHAR | FK to video_transcripts |
| `segment_index` | INTEGER | Order within video |
| `start_seconds` | FLOAT | Segment start time (e.g., 8.16, 182.64) |
| `end_seconds` | FLOAT | Segment end time |
| `text` | TEXT | Segment content |
| `created_at` | TIMESTAMPTZ | Used as watermark for incremental uploads |

### Relationship

Segments are grouped into chapters by matching `segment.start_seconds >= chapter.start_seconds` (before the next chapter boundary). The per-segment timestamps are used only during assembly and are **not preserved** in any downstream backend.

## Chapter Parsing

### How Chapters Are Populated

Chapter data originates as unstructured timestamps in `video_transcripts.description`. The function `parse_chapters_from_description()` (in `weaviate/upload_videos_chapters.py` and `motherduck/upload_video_chapters.py`) extracts them with a regex:

```
(?:^|\n)\s*(?:[Cc]hapters?:\s*)?(?:(\d+):)?(\d{1,2}):(\d{2})\s+(.+)
```

Matches patterns like:
- `0:00 Introduction`
- `1:02:17 The Main Topic`
- `Chapters: 7:28 Some Topic`

### Bootstrap Strategy

Chapter parsing runs **inside the upload scripts**, not as a separate ETL step. Before chunking, the scripts query for videos that have segments but no chapters, parse any found timestamps, and `INSERT INTO youtube.video_chapters ... ON CONFLICT DO NOTHING`. This is idempotent — subsequent runs skip videos that already have chapters.

This is documented in CLAUDE.md as an exception to the normal pattern where PostgreSQL is populated by separate ETL scripts.

### Known Bug

When a description has `Chapters:0:00 Title` with **no newline** between the header and first timestamp, the regex fails to match the first chapter. Currently affects 1 video (`0BGfo4yiCc8`) — 17 of 18 chapters captured, missing 545 seconds of content. See `motherduck/fix-chapter-parsing-bug.md`.

## Segment-to-Chapter Assembly

All backends use the same grouping logic. Each segment is assigned to the latest chapter whose `start_seconds <= segment.start_seconds`:

```python
for seg in segments:
    for chapter in reversed(chapter_entries):
        if seg.start_seconds >= chapter.start_seconds:
            chapter.texts.append(seg.text)
            break
```

The segment texts within each chapter are joined with spaces:

```python
text = " ".join(entry["texts"]).strip()
```

**Individual segment timestamps (`start_seconds`, `end_seconds`) are discarded at this point.** The only timestamp preserved downstream is the chapter-level `start_seconds`.

## Chunking Strategies

This is where the backends diverge significantly.

### Weaviate: Sub-Chunked Chapters

Weaviate is the only backend that breaks chapters into smaller pieces for search precision.

**For videos WITH chapters** (`chunkMethod="chapter_exact"`):

1. **Group segments by chapter** — as described above
2. **Merge small chapters** — if a chapter has < 500 tokens, merge it forward into the next chapter. The merged title becomes `"Chapter A / Chapter B"`. The earlier chapter's start time and number are kept.
3. **Sub-chunk large chapters** — if a chapter has > 1,000 tokens, split with 1,000-token windows and 250-token overlap. Small trailing sub-chunks (< 500 tokens) merge back into the previous sub-chunk.
4. **Result**: 1–N chunks per chapter depending on chapter length

**For videos WITHOUT chapters** (`chunkMethod="fixed_window"`):

1. Concatenate all segments into one full transcript
2. Chunk with 1,000-token windows, 250-token overlap
3. Zero chapter metadata (empty title, chapter_number=0, start_time=0)

**Chunking settings** (from `weaviate/config.py`):
- `CHUNK_SIZE = 1000` tokens
- `OVERLAP = 250` tokens
- `MIN_CHUNK_SIZE = 500` tokens

### MotherDuck: One Row Per Chapter

No sub-chunking. Each chapter produces exactly one row. Long chapters are stored in full but truncated at embedding time.

**For videos WITH chapters**: One row per chapter with complete transcript text.

**For videos WITHOUT chapters**: Single "Full Transcript" row with `chapter_number=0`, `chapter_title="Full Transcript"`.

### ChromaDB: One Row Per Chapter

Same approach as MotherDuck — one row per chapter, no sub-chunking. However, the document text is truncated before storage (see Data Fidelity below).

## Chunking Comparison

| Aspect | Weaviate | MotherDuck | ChromaDB |
|---|---|---|---|
| **Sub-chunking large chapters** | Yes (1,000-token windows) | No | No |
| **Merging small chapters** | Yes (< 500 tokens merged forward) | No | No |
| **Rows per chapter** | 1–N (depends on size) | 1 | 1 |
| **Chunk size** | 1,000 tokens | N/A (whole chapter) | N/A (whole chapter) |
| **Overlap** | 250 tokens | N/A | N/A |
| **Min chunk size** | 500 tokens | N/A | N/A |
| **No-chapter fallback** | Fixed-window chunks | Single "Full Transcript" row | Single "Full Transcript" row |
| **Chunk tracking** | `chunkNumber`, `totalChunks`, `chunkMethod` | None | None |

## Data Fidelity: Raw Text vs. Embedded Text

| Backend | Raw chapter text stored | What gets embedded | Text loss? |
|---|---|---|---|
| **Weaviate** | Sub-chunked (~1,000 tokens each) | Same sub-chunks (vectorizer embeds the stored `transcript` property) | Text is split, not truncated — full content preserved across chunks |
| **MotherDuck** | **Full chapter text** (`transcript` VARCHAR column, no truncation) | `LEFT(transcript, 32000)` chars (~8K tokens) for `embedding()` | **No loss** — raw text fully preserved, only embedding is truncated |
| **ChromaDB** | **Truncated to 7,500 tokens** (client-side `truncate_to_tokens()` applied to document before upsert) | Same truncated text + Jina `truncate=True` server-side safety net | **Text lost** — tail of long chapters not stored anywhere in ChromaDB |

**Key insight**: MotherDuck is the only backend that stores the complete raw chapter text regardless of length. ChromaDB loses text beyond 7,500 tokens because the truncation is applied to the document itself, not just the embedding input. Weaviate preserves all text but distributed across multiple sub-chunks.

## Schemas Per Backend

### Weaviate: `VideoChapterChunkPodcasts` / `VideoChapterChunkSubstack`

| Property | Type | Vectorized | Notes |
|---|---|---|---|
| `transcript` | TEXT | Yes | Chunk content (sub-chunked for large chapters) |
| `description` | TEXT | No | Full video description |
| `videoId` | TEXT | No | YouTube video ID |
| `videoTitle` | TEXT | No | Video title |
| `channelName` | TEXT | No | Filterable |
| `videoUrl` | TEXT | No | |
| `uploadDate` | DATE | No | |
| `playlistName` | TEXT | No | Filterable |
| `chapterTitle` | TEXT | No | Chapter name (may be merged: "A / B") |
| `chapterNumber` | INT | No | Position in video |
| `chapterStartTime` | NUMBER | No | Chapter start in seconds |
| `chunkMethod` | TEXT | No | `"chapter_exact"` or `"fixed_window"` |
| `chunkNumber` | INT | No | Position within video's chunks |
| `totalChunks` | INT | No | Total chunks for this video |
| `chunkTokens` | INT | No | Token count for this chunk |

Embedding: Jina AI v3 (`jina-embeddings-v3`), 1,024 dimensions. Reranker: Jina v3.
UUID: Deterministic `uuid5(NAMESPACE_DNS, "chapter-video-{videoId}-ch{chapterNumber}-{chunkNumber}")`.

### MotherDuck: `youtube_chapters` + `youtube_videos`

**`youtube_chapters`** — one row per chapter:

| Column | Type | Notes |
|---|---|---|
| `video_id` | VARCHAR | YouTube video ID |
| `chapter_number` | INTEGER | Position in video |
| `chapter_title` | VARCHAR | Chapter name |
| `start_seconds` | INTEGER | Chapter start time |
| `source_database` | VARCHAR | `"podcasts"` or `"substack"` |
| `transcript` | VARCHAR | **Full chapter text** (no truncation) |
| `transcript_tokens` | INTEGER | Token count |
| `transcript_embedding` | FLOAT[512] | Embedded from `LEFT(transcript, 32000)` |

**`youtube_videos`** — one row per video (catalog):

| Column | Type | Notes |
|---|---|---|
| `video_id` | VARCHAR | YouTube video ID |
| `title` | VARCHAR | Video title |
| `description` | VARCHAR | Full description |
| `channel_name` | VARCHAR | |
| `url` | VARCHAR | |
| `upload_date` | DATE | |
| `playlist_name` | VARCHAR | |
| `source_database` | VARCHAR | `"podcasts"` or `"substack"` |
| `chapter_count` | INTEGER | Number of chapters |
| `has_chapters` | BOOLEAN | |
| `description_embedding` | FLOAT[512] | Embedded from `title + LEFT(description, 20000)` |

Embedding: OpenAI `text-embedding-3-small`, 512 dimensions (MotherDuck built-in `embedding()` function).

### ChromaDB: `youtube_chapters`

Stored as document + metadata per record:

| Field | Storage | Notes |
|---|---|---|
| `document` | ChromaDB document | Chapter transcript, **truncated to 7,500 tokens** |
| `videoId` | metadata | YouTube video ID |
| `chapterNumber` | metadata | Position in video |
| `chapterTitle` | metadata | Chapter name |
| `startSeconds` | metadata | Chapter start time (integer) |
| `videoTitle` | metadata | |
| `channelName` | metadata | |
| `videoUrl` | metadata | |
| `uploadDate` | metadata | ISO string |
| `playlistName` | metadata | |
| `sourceDatabase` | metadata | `"podcasts"` or `"substack"` |
| `hasChapters` | metadata | Boolean |
| `transcriptTokens` | metadata | Token count (of original, pre-truncation) |

ID format: `chapter-{sourceDatabase}-{videoId}-ch{chapterNumber}`.
Embedding: Jina AI v3 (`jina-embeddings-v3`), 1,024 dimensions, `truncate=True`.

## Embedding Model Comparison

| Backend | Model | Dimensions | Truncation for embedding | Embedding location |
|---|---|---|---|---|
| Weaviate | Jina AI v3 | 1,024 | N/A (sub-chunked, fits naturally) | Server-side vectorizer |
| MotherDuck | OpenAI `text-embedding-3-small` | 512 | `LEFT(transcript, 32000)` chars | Server-side `embedding()` SQL function |
| ChromaDB | Jina AI v3 | 1,024 | 7,500 tokens client + `truncate=True` server | Client-side via Jina API |

## Row Counts

| Backend | Collection/Table | Rows | Videos | Notes |
|---|---|---|---|---|
| Weaviate | `VideoChapterChunkPodcasts` | ~3,384 | ~243 | Sub-chunked |
| Weaviate | `VideoChapterChunkSubstack` | ~9,108 | ~147 | Sub-chunked |
| MotherDuck | `youtube_chapters` | 6,086 | 428 | 1 row per chapter |
| MotherDuck | `youtube_videos` | 428 | 428 | Catalog table |
| ChromaDB | `youtube_chapters` | 6,086 | 428 | 1 row per chapter |

Weaviate has ~12,492 total chapter chunks vs 6,086 in MotherDuck/ChromaDB. The difference is entirely from sub-chunking large chapters.

## Incremental Upload / Watermarking

| Backend | Strategy | Watermark |
|---|---|---|
| Weaviate | File-based watermark, filters on `transcript_segments.created_at` | `.last_upload_videos_chapters_{database}` |
| MotherDuck | Full reload (`CREATE OR REPLACE TABLE`), rebuilds from both DBs each run | None |
| ChromaDB | Full reload with upsert (deterministic IDs) | None |

## Data Flow Summary

```
                    youtube.video_transcripts.description
                                    |
                    parse_chapters_from_description()
                                    |
                                    v
                        youtube.video_chapters
                    (video_id, position, start_seconds, title)
                                    |
                                    +--- group with transcript_segments
                                    |    (assign segments to chapters by timestamp)
                                    |
                                    v
                        Chapter text blobs
                    (segment texts joined, timestamps discarded)
                                    |
                +-------------------+-------------------+
                |                   |                   |
                v                   v                   v
            Weaviate            MotherDuck          ChromaDB
                |                   |                   |
        merge small (<500t)     store as-is         store as-is
        sub-chunk large (>1000t)    |               truncate to 7500t
                |                   |                   |
                v                   v                   v
        1-N chunks/chapter      1 row/chapter       1 row/chapter
        ~12,492 total           6,086 total         6,086 total
                |                   |                   |
        Jina AI v3 (1024d)      embedding()         Jina AI v3 (1024d)
        server-side             LEFT(text, 32000)   truncate=True
```

## TODO

### ChromaDB: Store full transcript in metadata

Currently ChromaDB's `document` field contains the truncated chapter text (7,500 tokens max). The full text should also be stored in metadata as `fullTranscript` so that retrieval returns complete chapter content even when the embedding was generated from truncated text. This matches MotherDuck's approach where the full `transcript` column is preserved despite embedding truncation.

### Evaluate embedded databases outside this project

The end goal of the embedded databases (LanceDB, ChromaDB) is **full portability** — a self-contained vector search solution that doesn't require Docker or external services. The truncated embeddings for chapters are acceptable. The next step is to evaluate the three embedded solutions (Weaviate Embedded, LanceDB, ChromaDB) in a separate project to compare:

- Search quality (semantic, FTS, hybrid, reranking)
- Query API ergonomics
- MCP server integration (chroma-mcp, lancedb tooling)
- Portability and deployment (single directory, no network dependencies)
- Data management (backup, migration, schema evolution)

### Fix chapter parsing bug

The regex in `parse_chapters_from_description()` fails on `Chapters:0:00 Title` (no newline). See `motherduck/fix-chapter-parsing-bug.md` for details. Affects 1 video currently.

### Preserve segment timestamps

No backend currently preserves the per-segment timestamps from `transcript_segments`. For future use cases (click-to-timestamp, segment-level search), consider storing segment boundaries alongside the text. This would require a schema change in all backends.
