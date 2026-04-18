# Telegram Data Ingestion Commands

## Load Channel Export

```bash
# Load a Telegram Desktop JSON export into PostgreSQL
python telegram/load_telegram.py telegram/ChatExport_2026-02-28/result.json --language ru

# Multiple channels
python telegram/load_telegram.py channel1/result.json channel2/result.json --language ru

# Different database
python telegram/load_telegram.py result.json --database podcasts
```

## Load Audio/Video Transcripts

Transcript filenames are matched to existing `message_media` rows by:
1. Leading message ID prefix: `{msg_id}_{description}_transcript.json` or `{msg_id}_transcript.json`
2. Original media filename: `{file_name}_transcript.json`

```bash
# Dry run (verify matches before loading)
python telegram/load_transcripts.py --dry-run --channel dnevnik_shturmovika \
    --dir "~/Downloads/Telegram Desktop/03172026-dnevnik-shturmovika/audio-exports/"

# Load all transcripts from a directory
python telegram/load_transcripts.py --channel dnevnik_shturmovika \
    --dir "~/Downloads/Telegram Desktop/03172026-dnevnik-shturmovika/audio-exports/"

# Load from Papirusvtelege channel
python telegram/load_transcripts.py --channel Papirusvtelege \
    --dir telegram/sample_transcript/

# Load batch subfolder
python telegram/load_transcripts.py --channel Papirusvtelege \
    --dir telegram/sample_transcript/batch/

# Single file
python telegram/load_transcripts.py "telegram/sample_transcript/Intleopap Final New В ТГ_transcript.json"

# --channel accepts username, channel name, or numeric ID
python telegram/load_transcripts.py --channel 1877852926 --dir transcripts/
python telegram/load_transcripts.py --channel "Дневник штурмовика" --dir transcripts/
```

## Schema Setup

```bash
# Create/update telegram schema (idempotent)
PGPASSWORD=postgres psql -h localhost -U postgres -d substack -f telegram/create_telegram_tables.sql
```

## Transcription (WhisperX)

```bash
# Batch transcribe OGG files with diarization
for ogg in ~/Downloads/Telegram\ Desktop/03172026-dnevnik-shturmovika/audio-exports/*.ogg; do
    echo "=== Transcribing: $ogg ==="
    python transcribe/transcribe_interview.py "$ogg" --model large-v3 --num-speakers 2 --hf-token "$HUGGING_FACE_HUB_TOKEN"
    echo ""
done
```
