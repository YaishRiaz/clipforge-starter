# ClipForge

A local Python pipeline that turns **footage a paid clipping campaign has approved** (and your own recordings) into short vertical videos, lets you review them from your phone, posts the approved ones to **your own** YouTube channel, and tracks views.

It runs on an ordinary laptop (8 GB RAM, no GPU). The language model and transcription are hosted, so nothing heavy runs locally except ffmpeg.

> **No secrets are in this repository.** All keys, tokens and passwords live in a local `.env` file that git ignores. You create it yourself from `.env.example` (see [docs/SETUP.md](docs/SETUP.md)). Never commit it, paste it into an issue, or share it.

## What it does

1. You find a campaign (Whop, Vyro, etc.), read the brief and **join it yourself** on the platform.
2. ClipForge reads the brief into rules (length, hashtags, overlay, approved footage, banned edits).
3. It downloads only the footage the brief approves, transcribes it, and scores the best moments.
4. It cuts a 9:16 clip: facecam on top (when the creator has one), burned-in captions, a hook line, the required overlay.
5. A compliance check holds any clip that breaks the brief.
6. You approve clips in Telegram or on the dashboard. Approved clips are uploaded to YouTube at spaced-out times.
7. For TikTok and Instagram Reels you get a ready kit in Telegram (video plus caption) and post by hand.
8. Views are collected every few hours and shown on the dashboard.

## Hard limits (they are part of the design, do not remove them)

- Ingest only footage a campaign you have **joined** approves, plus your own recordings. Never reuse footage across campaigns. Never clip before the join date.
- One person, one channel. No extra accounts to get around limits.
- No fake views or engagement. No detection evasion. No bot-driving of YouTube Studio or any platform UI.
- Nothing posts without your approval while review mode is on (the default).
- Account creation, passwords, accepting terms, joining campaigns, OAuth consent and payout details are always done by **you**, by hand.

## Quick start

Full walk-through with every account you need: **[docs/SETUP.md](docs/SETUP.md)**.

Needs Python 3.11+, git and ffmpeg on PATH.

```
git clone <this repo>
cd clipforge

# Windows (PowerShell)
powershell -ExecutionPolicy Bypass -File scripts/setup.ps1
# macOS / Linux
bash scripts/setup.sh
```

The script creates `.venv`, installs the requirements, creates `.env` from `.env.example` and initialises the database. Then edit `.env`, and (using the venv's python):

```
python -m clipforge llm-check        # is the LLM key good?
python -m clipforge telegram-setup   # finds your chat id and sends a test message
python -m clipforge youtube-auth     # one-time YouTube sign-in (your own channel)
python -m clipforge supervise        # runs the dashboard + worker and restarts them
```

Dashboard: `http://localhost:8787` (password from `.env`).

## Commands

Run as `python -m clipforge <command>`.

| Command | What it does |
|---|---|
| `init-db` | Create or migrate the database |
| `supervise` | Run the dashboard and the worker together, restart them, watch for stalls |
| `serve` / `worker` | The two halves separately (both must run for posting) |
| `campaign-add`, `campaign-joined`, `parse-brief`, `campaign-rules`, `campaigns` | Register a campaign, record that you joined, read its brief, see its rules |
| `ingest-url`, `scan-inbox`, `sources`, `source-release` | Bring in approved footage |
| `mine`, `moments`, `clip`, `clip-source`, `clips` | Find moments and cut clips |
| `llm-check`, `telegram-setup`, `youtube-auth` | One-time checks and sign-ins |
| `publish-now`, `stats-collect` | Upload one clip now, read views now |
| `install-service`, `power-setup` | Windows: start at logon, never sleep on mains power |
| `status`, `sysinfo`, `benchmark` | Health and machine checks |

## Facecam layouts

Streamers with a facecam need a small file in `layouts/<creator>.json` telling ClipForge where the cam is in the stream (see `layouts/skiimasktimmy.json`). Open a frame of the stream, read off the cam rectangle in source pixels, and set `facecam`. A rendered clip whose cam band is black fails the compliance check, so a wrong box gets caught instead of posted. Creators with no layout file get a plain centre crop, and official trailers are shown whole.

## Folder layout

```
clipforge/     the code (core, ingest, moments, edit, app, publish, stats, discover)
layouts/       per-creator facecam boxes
rubrics/       scoring rubrics by niche
docs/          setup guide, clip SOP, verified platform facts
scripts/       one-time setup scripts
tests/         pytest suite (python -m pytest -q)
inbox/own/     drop your own recordings here (git ignored)
assets/logos/  official campaign logos, <campaign id>.png (git ignored)
data/ out/     database, working files and finished clips (git ignored)
```

The full specification is in [CLIPFORGE_PRD.md](CLIPFORGE_PRD.md) (written for the original owner, so "Yaish" in it and in code comments means whoever operates the install: you). [docs/verification.md](docs/verification.md) records which platform facts were checked against vendor pages and wins where the two differ. [docs/clip_sop.md](docs/clip_sop.md) is the clip-quality checklist.

## Tests

```
python -m pytest -q
```
