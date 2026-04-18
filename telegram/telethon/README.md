# Telegram Telethon Poller

Incremental Telegram channel ingestion via [Telethon](https://github.com/LonamiWebs/Telethon) (MTProto). Replaces the manual Telegram Desktop export workflow with a watermark-driven poller that writes into the same `telegram` PostgreSQL schema as `telegram/load_telegram.py`.

## Scripts

- **`poll_channel.py`** — fetch new messages per channel, upsert into Postgres, download small photos (≤ 8 MB).
- **`fetch_media.py`** — on-demand downloader for videos / large documents that the poller skips.

## One-time setup

1. **Install Telethon** (already in the project venv):
   ```bash
   pip install telethon
   ```

2. **Get API credentials** from https://my.telegram.org → *API development tools*. Create an app (Platform: Desktop). You get:
   - `api_id` — numeric
   - `api_hash` — 32-char hex

   One app per Telegram account; credentials persist.

3. **Export env vars** (add to `~/.bashrc` or a sourced `.env`):
   ```bash
   export TELEGRAM_API_ID=34438842
   export TELEGRAM_API_HASH=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
   ```

4. **First run authenticates interactively** — prompts for phone, login code (delivered in your Telegram app, not SMS), and 2FA password if enabled. The session is saved to `telegram/telethon/.session/poller.session` and subsequent runs are non-interactive.

   Session file holds the auth token — treat it like a password, do **not** commit it. Add to `.gitignore`:
   ```
   telegram/telethon/.session/
   ```

## Usage

### Polling channels

```bash
# Smoke test (10 messages, no photo download)
python telegram/telethon/poll_channel.py strelkov_i --limit 10 --no-media

# Small test with photo download
python telegram/telethon/poll_channel.py @KvachkovV --limit 20

# Full incremental catch-up for a channel
python telegram/telethon/poll_channel.py @KvachkovV

# Multiple channels in one run
python telegram/telethon/poll_channel.py strelkov_i @KvachkovV another_channel

# Russian-language channels
python telegram/telethon/poll_channel.py @KvachkovV --language ru

# Backfill in chunks (safer for very large histories)
python telegram/telethon/poll_channel.py @KvachkovV --limit 500
# repeat until "+0 messages"
```

Channel references accept `username`, `@username`, or full `https://t.me/username` URLs.

**Flags:**
- `--limit N` — cap messages per channel per run
- `--full` — ignore watermark, refetch from the beginning (upserts, so it's safe)
- `--no-media` — skip photo downloads, metadata only
- `--language ru` — set on the channel row (only applied on first insert)
- `--database substack` — target database (default `substack`)

**What it does:**
- Reads `MAX(messages.id)` per channel and passes it as `min_id` to `iter_messages(reverse=True)`, so each run fetches strictly newer messages
- Upserts into `telegram.channels`, `telegram.messages`, `telegram.message_media`, `telegram.message_links`, `telegram.message_reactions`
- Downloads photos ≤ 8 MB to `telegram/media/{username}/photos/{msg_id}.jpg`
- Videos, audio, and documents: metadata row inserted, file_path left NULL (fetch separately when needed)
- Commits every 200 messages; re-runnable after interruption

### Fetching videos / large media on demand

```bash
# Explicit message IDs
python telegram/telethon/fetch_media.py --channel @KvachkovV --ids 12345,12346

# All un-downloaded videos for a channel via SQL → stdin
psql -d substack -Atc "
  SELECT message_id
  FROM telegram.message_media
  WHERE channel_id = (SELECT id FROM telegram.channels WHERE username = 'KvachkovV')
    AND media_type = 'video_file'
    AND file_path IS NULL
  ORDER BY message_id DESC
  LIMIT 5
" | python telegram/telethon/fetch_media.py --channel @KvachkovV --stdin
```

Files are routed by MIME type into `telegram/media/{username}/{videos,audio,documents}/`. Original filenames are preserved when present (`{msg_id}_{original_name}`). Updates `message_media.file_path` after successful download. `--force` re-downloads existing files.

## Checking results

```bash
# New messages since a watermark
psql -d substack -c "
  SELECT id, posted_at, LEFT(text_plain, 60)
  FROM telegram.messages
  WHERE channel_id = (SELECT id FROM telegram.channels WHERE username = 'KvachkovV')
  ORDER BY id DESC LIMIT 10;
"

# Media download coverage per channel
psql -d substack -c "
  SELECT media_type, COUNT(*) AS total, COUNT(file_path) AS downloaded
  FROM telegram.message_media
  WHERE channel_id = (SELECT id FROM telegram.channels WHERE username = 'KvachkovV')
  GROUP BY media_type;
"
```

## Troubleshooting

**`ApiIdInvalidError: The api_id/api_hash combination is invalid`** — almost always means the env vars didn't reach the Python process, not that the credentials are wrong. Check what Python actually sees:

```bash
python -c "import os; print(repr(os.getenv('TELEGRAM_API_ID')), repr(os.getenv('TELEGRAM_API_HASH')))"
```

Expected output (values as strings, no stray quotes or whitespace):

```
'34438842' 'xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx'
```

If either prints `None`, you exported in a different shell than the one running the script (common with PyCharm run configs — set the env vars in *Run → Edit Configurations → Environment variables*, or run from the terminal where you exported). If the `repr` shows embedded quotes or spaces, fix the `export` line.

After fixing env vars, also delete any stale session file from the failed attempt before retrying:

```bash
rm -f telegram/telethon/.session/poller.session*
```

## Operational notes

- **Transient handshake errors** like `Attempt 1 at new auth_key failed: Step 3 invalid new nonce hash` are normal — Telethon retries automatically and the run continues. Don't delete the session file in response.
- **Clock drift** can break MTProto. If auth keeps failing, check `timedatectl status` → `System clock synchronized: yes`.
- **Flood-wait**: Telethon auto-sleeps when Telegram rate-limits. For very large first backfills, prefer `--limit 500` in a loop so a single flood-wait doesn't block a multi-hour run.
- **Watermark is per-channel**, stored as `MAX(telegram.messages.id)`. Running on an already-exported channel is safe — it resumes from the newest imported message.
- **Re-ingesting a Desktop export afterward**: `load_telegram.py` uses `ON CONFLICT DO UPDATE` on messages, so the two loaders coexist. The poller is the canonical source going forward.
- **Scheduling**: once stable, put `poll_channel.py <channels...>` on cron. Re-runs are cheap no-ops when nothing is new.

## Downloading history that predates the watermark

The watermark only advances forward. To backfill older messages that were never in your Desktop export, temporarily lower `MAX(id)` or use `--full`:

```bash
# Re-scan from the very beginning (upserts existing rows, slow)
python telegram/telethon/poll_channel.py @KvachkovV --full --limit 1000
```

For a true gap-fill (older than your earliest known message), `iter_messages(max_id=<earliest_known_id>)` is the right primitive — not currently exposed as a flag; add one if this use case comes up.
