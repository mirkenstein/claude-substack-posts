# Bug Report: First Chapter Dropped When `Chapters:` Header Has No Trailing Newline

## Summary

The chapter extraction pipeline drops the first chapter (0:00) when the YouTube description formats the `Chapters:` header without a newline before the first timestamp entry. This results in missing transcript content and an off-by-one error in chapter numbering.

## Root Cause

The chapter parsing logic expects a newline between `Chapters:` and the first timestamp. When the description uses `Chapters:0:00 ...` (no newline), the parser fails to capture the first chapter line.

### Failing format (no newline after `Chapters:`)
```
Chapters:0:00 The Strange Origins of Jeffrey Epstein and His Connection to Bill Barr
9:05 Epstein's Money Laundering
16:53 Did Epstein Belong to Intelligence?
```

### Working format (newline after `Chapters:`)
```
Chapters:
0:00 The Control Grid
7:28 How Biometrics Will Be Used to Control You
```

### Also working (double-zero hour format)
```
Chapters:
00:00 Monologue
12:01 What Is Pizzagate?
```

## Affected Data

Currently only **1 video** is affected in the database, but the bug could hit any future video with the same formatting:

| video_id | title | Expected chapters | Actual chapters | Missing chapter |
|---|---|---|---|---|
| `0BGfo4yiCc8` | Tucker Carlson and Darryl Cooper on the True History of Jeffrey Epstein and Ongoing Cover-Up | 18 | 17 | `0:00 The Strange Origins of Jeffrey Epstein and His Connection to Bill Barr` (0:00–9:05, ~545 seconds of content) |

### Verification queries

```sql
-- Shows the one video where first chapter doesn't start at 0 seconds
SELECT v.video_id, v.title, v.chapter_count, 
       min(c.start_seconds) as first_chapter_start
FROM youtube_videos v
JOIN youtube_chapters c ON v.video_id = c.video_id
WHERE v.has_chapters = true
GROUP BY v.video_id, v.title, v.chapter_count
HAVING min(c.start_seconds) > 0;

-- Compare description chapter list vs actual chapters table
SELECT v.description
FROM youtube_videos v
WHERE v.video_id = '0BGfo4yiCc8';

-- Show the 3 formatting variants across videos
SELECT video_id, title,
       regexp_extract(description, '(Chapters:?[^\n]*\n[^\n]+)', 1) as chapter_header_format
FROM youtube_videos
WHERE video_id IN ('0BGfo4yiCc8', 'BvLz1bI2sXU', 'r6OTnOoGtGk');
```

## Downstream Effects

1. **Missing transcript**: The 0:00–9:05 segment's transcript is not in `youtube_chapters` at all — it was never extracted
2. **Wrong `chapter_count`**: `youtube_videos.chapter_count` = 17 (should be 18)
3. **Wrong `chapter_number` indexing**: All chapters are numbered 0–16 instead of 0–17; chapter 0 in the table is actually the second chapter from the description
4. **Missing embedding**: No `transcript_embedding` exists for the lost chapter, so semantic search can't find content from that segment
5. **FTS index gap**: The FTS index `fts_main_youtube_chapters` also lacks this content

## Fix Requirements

### 1. Fix the chapter line parser

The regex or string split that extracts chapter lines from the description must handle all three observed formats:

```
Chapters:0:00 Title Here        ← no newline, first chapter on same line
Chapters:\n0:00 Title Here      ← newline, single-digit hour
Chapters:\n00:00 Title Here     ← newline, double-digit hour
```

Suggested approach: after finding the `Chapters:` marker, strip it and treat the remainder as chapter lines. Don't assume a newline delimiter between the header and the first entry.

```python
# Example fix sketch
raw = description_text.split("Chapters:")[-1]  # everything after "Chapters:"
lines = [line.strip() for line in raw.strip().splitlines() if line.strip()]
# Now parse timestamp + title from each line
```

### 2. Backfill the affected video

After fixing the parser, re-process video `0BGfo4yiCc8`:
- Extract the missing chapter: title="The Strange Origins of Jeffrey Epstein and His Connection to Bill Barr", start_seconds=0, chapter_number=0
- Re-number existing chapters 0→1, 1→2, ..., 16→17
- Extract/assign the transcript for the 0:00–9:05 segment
- Generate the transcript embedding
- Update `youtube_videos.chapter_count` from 17 → 18
- Rebuild the FTS index

### 3. Add a validation check

After chapter extraction, add an assertion:

```python
if has_chapters and chapters[0].start_seconds != 0:
    logger.warning(f"First chapter for {video_id} starts at {chapters[0].start_seconds}s, not 0. "
                   f"Possible parsing bug — check description format.")
```

## How to Verify the Fix

```sql
-- After fix: first chapter should start at 0 for all videos with chapters
SELECT v.video_id, v.title, min(c.start_seconds) as first_start
FROM youtube_videos v
JOIN youtube_chapters c ON v.video_id = c.video_id
WHERE v.has_chapters = true
GROUP BY v.video_id, v.title
HAVING min(c.start_seconds) > 0;
-- Expected: 0 rows

-- After fix: chapter count should match
SELECT v.video_id, v.chapter_count, count(*) as actual
FROM youtube_videos v
JOIN youtube_chapters c ON v.video_id = c.video_id
WHERE v.video_id = '0BGfo4yiCc8'
GROUP BY v.video_id, v.chapter_count;
-- Expected: chapter_count = 18, actual = 18

-- After fix: first chapter title should match description
SELECT chapter_number, chapter_title, start_seconds
FROM youtube_chapters
WHERE video_id = '0BGfo4yiCc8'
ORDER BY chapter_number
LIMIT 3;
-- Expected: 
--   0 | The Strange Origins of Jeffrey Epstein and His Connection to Bill Barr | 0
--   1 | Epstein's Money Laundering | 545
--   2 | Did Epstein Belong to Intelligence? | 1013
```
