# Auto-Chaptering Pipeline for Strateg Divannogo Legiona Videos

## Objective

Build a Python script that generates chapter markers for Russian-language YouTube livestreams and video essays. The pipeline reads timestamped transcript segments from PostgreSQL, sends a compressed representation to the Claude API for topic boundary detection, maps the results back to exact segment timestamps, and outputs structured chapter data.

---

## Data Architecture

### Source: `youtube.transcript_segments`

```
schema: youtube
table: transcript_segments

columns:
  id              serial PRIMARY KEY
  video_id        varchar NOT NULL  -- FK to video_transcripts
  segment_index   integer NOT NULL
  start_seconds   float NOT NULL
  end_seconds     float NOT NULL
  text            text NOT NULL
  created_at      timestamp

unique constraint: (video_id, segment_index)
```

Each segment is ~5 seconds of ASR-transcribed Russian speech. A 5-hour livestream has ~6,000 segments. A 20-minute scripted video has ~700 segments.

### Target: `youtube.video_chapters`

```
schema: youtube
table: video_chapters

columns:
  id              serial PRIMARY KEY
  video_id        varchar NOT NULL  -- FK to video_transcripts
  position        integer NOT NULL  -- chapter sequence number (1-indexed)
  start_seconds   float NOT NULL    -- exact timestamp from transcript_segments
  title           text NOT NULL     -- chapter title
  created_at      timestamp

unique constraint: (video_id, position)
```

### Reference: `youtube.video_transcripts`

```
schema: youtube
table: video_transcripts

columns:
  video_id        varchar PRIMARY KEY
  channel_name    varchar
  title           varchar
  upload_date     date
  transcript      text              -- flat text blob (used as fallback if no segments)
  processing_status varchar
```

Filter: `channel_name = 'Strateg_Divannogo_Legiona'`

Total: 150 videos, 147 with transcripts. Currently only 1 video (`3a5PLSu-yxE`) has segments loaded. The script should work per-video as more segments are ingested.

---

## Pipeline Steps

### Step 1: Determine Video Category and Sampling Rate

Query the transcript length to determine the video category:

```sql
SELECT video_id, title, LENGTH(transcript) as chars
FROM youtube.video_transcripts
WHERE video_id = :video_id;
```

Apply adaptive sampling:

| Category | Transcript Chars | Sampling | Rationale |
|----------|-----------------|----------|-----------|
| Short    | < 30,000        | Every segment (no sampling) | ~700 segments, ~20K chars — fits easily in one API call |
| Medium   | 30,000–60,000   | Every 2nd segment | ~1,300 segments → ~650 rows, ~30K chars |
| Long     | 60,000–100,000  | Every 3rd segment | ~2,500 segments → ~830 rows, ~30K chars |
| Marathon | > 100,000       | Every 5th segment | ~4,700 segments → ~940 rows, ~30K chars |

The goal is to keep the skeleton in the **20–40K character range** regardless of video length. This fits comfortably in a single Claude API call.

Video count by category in the corpus: 22 short, 28 medium, 7 long, 92 marathon.

### Step 2: Build Timestamped Skeleton

Query segments at the determined sampling rate:

```sql
SELECT segment_index, start_seconds, text
FROM youtube.transcript_segments
WHERE video_id = :video_id
  AND segment_index % :sampling_rate = 0
ORDER BY segment_index;
```

Format as a timestamped document for the LLM:

```
[00:00:08] Так пошёл. Да, пошёл же.
[00:00:48] Всем привет. Всем салюта.
[00:01:08] ночь с 23 на 24 февраля 2022 года я спал
[00:01:38] очень тревожно. То есть я ворочился,
...
```

Use `HH:MM:SS` format derived from `start_seconds` for human readability in the prompt. Keep the raw `start_seconds` float for database writes.

### Step 3: Call Claude API for Chapter Detection

Use model `claude-sonnet-4-5-20250929` (best balance of cost and quality for this task).

**System prompt:**

```
You are analyzing a timestamped transcript skeleton of a Russian-language YouTube video 
to identify chapter boundaries. The video is from the channel "Стратег Диванного Легиона" 
(Sofa Strategist) — a Kremlin-critical military-political analyst who produces both 
scripted video essays (15-30 min) and long-form livestreams (3-6 hours).

Your task:
1. Read the timestamped transcript and identify major topic transitions.
2. For each chapter, provide the approximate start timestamp and a concise Russian title.
3. Also provide a brief English summary (1 sentence) for each chapter.

Rules:
- Aim for 5-10 chapters for short videos (<30 min), 10-25 for long livestreams.
- Only chapter on SUBSTANTIVE topic blocks. Ignore:
  - Greetings to individual chat members
  - Donation/superchat reads (unless they trigger a substantive discussion)
  - Brief tangential jokes or personal anecdotes (< 2 minutes)
  - Technical difficulties (audio checks, stream setup)
- The opening segment should always be Chapter 1.
- For livestreams: the host takes questions from live chat. Group Q&A sections 
  as single chapters unless a question triggers a major standalone analysis (5+ minutes).
- Titles should be descriptive and specific, not generic. 
  Good: "Почему Иран не создал ядерное оружие"
  Bad: "Обсуждение Ирана"
- If the video is a scripted essay (short, structured, no chat interaction), 
  chapters should follow the essay's argument structure.

Respond ONLY with a JSON array. No preamble, no markdown fences, no explanation.
Each element: {"start_time": "HH:MM:SS", "start_seconds": <float>, "title_ru": "<string>", "summary_en": "<string>"}
```

**User prompt:**

```
Video title: {video_title}
Upload date: {upload_date}
Duration: {duration_formatted}
Transcript type: {scripted_essay | livestream}  # determined by category/duration

--- TRANSCRIPT ---
{formatted_skeleton}
```

Determine `transcript_type` heuristically: if duration < 45 minutes OR transcript < 60K chars, classify as `scripted_essay`; otherwise `livestream`. This affects how aggressively the model chapters (fewer chapters for essays, more for streams).

### Step 4: Parse Response and Map to Exact Segments

Parse the JSON response. For each proposed chapter, find the nearest actual segment:

```sql
SELECT segment_index, start_seconds, text
FROM youtube.transcript_segments
WHERE video_id = :video_id
  AND start_seconds >= :proposed_seconds - 15
  AND start_seconds <= :proposed_seconds + 15
ORDER BY ABS(start_seconds - :proposed_seconds)
LIMIT 1;
```

Use the matched segment's `start_seconds` as the chapter's precise timestamp.

### Step 5: Optional Refinement Pass

For higher accuracy on chapter boundaries, do a second targeted API call. For each proposed boundary at time T, pull the FULL (non-sampled) segments in a ±30 second window:

```sql
SELECT segment_index, start_seconds, text
FROM youtube.transcript_segments
WHERE video_id = :video_id
  AND start_seconds BETWEEN :T - 30 AND :T + 30
ORDER BY segment_index;
```

Send this dense window to Claude with a focused prompt:

```
Here is a 60-second transcript window around a proposed chapter boundary.
The proposed new chapter topic is: "{title_ru}"
The preceding chapter topic was: "{prev_title_ru}"

Find the exact segment where the topic transition occurs.
Respond with ONLY the start_seconds value of that segment.
```

This corrects for the ±25 second imprecision from sampling. Skip this step if speed matters more than precision.

### Step 6: Output

Output a JSON array ready for the ingestion script:

```json
[
  {
    "video_id": "3a5PLSu-yxE",
    "position": 1,
    "start_seconds": 8.16,
    "title": "Вступление — параллель с 24 февраля 2022"
  },
  {
    "video_id": "3a5PLSu-yxE",
    "position": 2,
    "start_seconds": 351.32,
    "title": "Китай и спутниковое целеуказание для Ирана"
  }
]
```

---

## Configuration

```python
# Database
DB_CONNECTION = "postgresql://..."  # or use environment variable

# API
ANTHROPIC_API_KEY = "..."  # environment variable
MODEL = "claude-sonnet-4-5-20250929"
MAX_TOKENS = 4096

# Sampling thresholds (transcript char counts)
SAMPLING_THRESHOLDS = {
    30_000:  1,   # short: every segment
    60_000:  2,   # medium: every 2nd
    100_000: 3,   # long: every 3rd
}
DEFAULT_SAMPLING = 5  # marathon: every 5th

# Chapter count targets
CHAPTER_TARGETS = {
    "scripted_essay": (5, 10),   # min, max
    "livestream": (10, 25),
}

# Video type classification
SCRIPTED_ESSAY_MAX_DURATION_SECONDS = 2700  # 45 minutes
SCRIPTED_ESSAY_MAX_CHARS = 60_000
```

---

## CLI Interface

```bash
# Chapter a single video
python chapter_video.py --video-id 3a5PLSu-yxE

# Chapter a single video, skip refinement pass
python chapter_video.py --video-id 3a5PLSu-yxE --no-refine

# Chapter all videos that have segments but no chapters yet
python chapter_video.py --all-pending

# Dry run — print proposed chapters without writing to DB
python chapter_video.py --video-id 3a5PLSu-yxE --dry-run

# Override video type detection
python chapter_video.py --video-id fLdDHadJYj4 --type scripted_essay
```

---

## Validation Reference: Iran Stream (3a5PLSu-yxE)

This video has been manually analyzed. Use these known topic boundaries to validate the pipeline output:

| ~Seconds | ~Time | Expected Topic |
|----------|-------|---------------|
| 8 | 0:00:08 | Opening — parallel to Feb 24 2022 sleepless night |
| 350 | 0:05:50 | China Mizar Vision satellite imagery of US bases |
| 700 | 0:11:40 | Pakistan-Afghanistan war + Iranian PVO invisible |
| 850 | 0:14:10 | Managed conflict signs — Iran hitting empty bases |
| 1073 | 0:17:53 | "Who's next after Iran?" — Cuba, Algeria, Russia |
| 1400 | 0:23:20 | Missing Axis of Resistance — Kata'ib Hezbollah, Ansar Allah |
| 1800 | 0:30:00 | Iran 90M population, ground invasion analysis |
| 2440 | 0:40:40 | US force composition — 3 carrier groups, air-only |
| 2783 | 0:46:23 | Khamenei's responsibility — no aviation, no nukes |
| 3100 | 0:51:40 | Uranium vs plutonium — technical deep-dive |
| 3600 | 1:00:00 | Extended chat Q&A begins |
| 10200 | 2:50:00 | Pantsir vs drones, FPV loss ratios |
| 10800 | 3:00:00 | Why Russia won't build a Starlink analog |
| 17227 | 4:47:07 | Votkinsk ICBM factory strike — Flamingo or Tomahawk? |
| 17500 | 4:51:40 | 100 years of Russian leaders — all condemned |
| 18400 | 5:06:40 | China vs US future war — FPV, BKI, humanoid robots |
| 20300 | 5:38:20 | Sign-off |

## Validation Reference: SMO 4-Year Summary (fLdDHadJYj4)

Short scripted essay (~24K chars). Expected ~7 chapters following the argument structure:

| ~Topic | Description |
|--------|-------------|
| Strategic blunders Phase 1 | Sumy vs Donbas — attacking the strongest wall |
| FPV revolution Phase 2 | Transition from maneuver to technology-driven war |
| Ukrainian drone ecosystem | Baba Yaga, Vampire, relays, Delta/Krapiva systems |
| Starlink as decisive weapon | Second only to nuclear weapons |
| Russian weapons audit | UMPK (85% miss), TOS-1, Geran on transformers, Iskander waste |
| Five questions about Starlink | Why no Russian analog, why block Telegram simultaneously |
| Conclusion | 95% caused by Russian leadership, AFU command deserves no credit |

---

## Edge Cases

1. **No segments loaded yet:** If `transcript_segments` has no rows for a `video_id`, fall back to the flat `transcript` blob from `video_transcripts`. Chunk it into ~5000 char windows, estimate timestamps proportionally from total duration, and run the same pipeline. Mark these chapters as `approximate = true` for later refinement.

2. **Very short videos (<10 min):** May only need 2-4 chapters. Set minimum to 2.

3. **Garbled ASR segments:** Some segments contain foreign character hallucinations or gibberish (common in Russian ASR). The LLM should ignore these. They appear as nonsensical strings mixed into otherwise coherent Russian text.

4. **Chat-dominated sections:** In marathon livestreams, long stretches (sometimes 60+ minutes) are pure Q&A with rapid topic cycling. Group these as single "Q&A" chapters rather than trying to chapter each 2-minute answer. Only break out a sub-chapter if a question triggers a sustained analytical monologue (5+ minutes on one topic).

5. **Profanity filtering:** Strateg uses extensive profanity. The transcripts contain `[\xa0__\xa0]` markers where YouTube's ASR censored words. These are normal and should not affect chaptering.

6. **Duplicate content:** Some videos are re-uploads. Check `video_chapters` for existing entries before processing: `SELECT COUNT(*) FROM youtube.video_chapters WHERE video_id = :video_id`. Skip if chapters already exist (use `--force` flag to overwrite).

---

## Scaling Plan

Priority order for processing:

1. **`3a5PLSu-yxE`** — Iran stream (has segments, use as validation)
2. **`fLdDHadJYj4`** — 4-year SMO summary (needs segments loaded first, then chapter)
3. **10 Rolo-cited videos** — highest research value, findable via:
   ```sql
   SELECT DISTINCT yr.video_id, vt.title, vt.upload_date
   FROM substack.youtube_references yr
   JOIN youtube.video_transcripts vt ON vt.video_id = yr.video_id
   WHERE yr.relationship_type IN ('embedded_cited', 'text_mention_contextual', 'comment_linked')
     AND vt.channel_name = 'Strateg_Divannogo_Legiona'
   ORDER BY vt.upload_date;
   ```
4. **Remaining 140 videos** — batch process

Each video requires one API call for chaptering (+ optional refinement calls). At ~$0.01-0.03 per video for Sonnet, the full corpus costs under $5.
