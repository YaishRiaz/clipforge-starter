# ClipForge

**An open-source, local-first pipeline for clipping-campaign creators.** ClipForge turns footage a paid clipping campaign has approved (and your own recordings) into vertical Shorts. You review them on your phone, and the approved ones post to *your own* YouTube channel on a schedule.

It runs on an ordinary laptop: 8 GB RAM, no GPU, no Docker. The language model and transcription are hosted services (free tiers work), so ffmpeg is the only heavy thing running locally.

[![CI](https://github.com/YaishRiaz/clipforge-starter/actions/workflows/ci.yml/badge.svg)](https://github.com/YaishRiaz/clipforge-starter/actions/workflows/ci.yml)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)
![License: MIT](https://img.shields.io/badge/license-MIT-green)

> **No secrets are in this repository.** Keys, tokens and passwords live in a local `.env` that git ignores. You create it from `.env.example` ([docs/SETUP.md](docs/SETUP.md)). Never commit it, paste it into an issue, or share it.

---

## What it does

```
brief ──► rules ──► approved footage ──► transcript + signals ──► scored moments
                                                                       │
 YouTube ◄── posting queue ◄── your approval (Telegram / dashboard) ◄── 9:16 clip + compliance check
```

1. **You join a campaign yourself** (Content Rewards / Whop, Vyro and others) and paste its brief.
2. **The brief becomes machine-checked rules:** approved footage, time windows, required hashtags, credits and names, length, banned edits, disclosure, paid-promotion label, opening date, and which game or date the footage must show.
3. **Only approved footage is ingested.** That means named VODs, or a whole channel you approve, from YouTube, Twitch or Kick. Long streams are downloaded as audio first, and only the stretch a clip needs is fetched in video.
4. **Moments are found from signals:** loudness, speech rate, scene changes and chat bursts. A language model then scores them against a niche rubric that is re-weighted from what performs.
5. **Each clip is cut to 9:16** with a layout that suits the footage:
   - facecam on top with gameplay below (the facecam is found automatically by a vision model);
   - the whole game under the cam, for chaotic games;
   - the whole frame over a blurred copy, for trailers and produced videos.

   It adds word-by-word captions, a hook, reaction emoji, zoom punches, and swear words masked as `s**t`.
6. **Taste books** set the edit style per niche, game and streamer. A taste book is measured from the top-performing Shorts: font, size, colours, words per caption, banner, emoji, zoom and pace. The campaign brief always overrides it.
7. **A compliance check holds any clip** that breaks the brief: a missing hashtag or credit, wrong length, a blank facecam, and so on.
8. **You approve or reject in Telegram or on the dashboard.** Reply to a clip with a note ("start 3 s earlier") and it is remade.
9. **Approved clips post at spaced slots.** Prime slots get the best clips. You can reorder the queue, and priority campaigns (e.g. a game launch) jump it.
10. **Views are collected every few hours.** The rubric learns from what your own clips did.

**Live clipping:**
- *Twitch:* while a creator a campaign approves is live, chat bursts mark moments. The stretch around each burst is cut from the in-progress VOD.
- *Kick:* no chat is read, so the stream is taken in rolling 20-minute stretches instead.

---

## Responsible use (read this first)

ClipForge is built for **paid clipping campaigns**, where a creator or brand pays clippers to repost approved footage, and for **your own recordings**. The limits below are part of the design. Pull requests that remove them will not be merged.

- **Only footage you have permission to use.** It ingests what a campaign you **joined** approves, plus your own recordings. Footage never crosses campaigns, and nothing recorded before you joined is clipped.
- **One person, one channel.** No extra accounts to get around limits.
- **No fake views or engagement, and no detection evasion.** No bot-driving of YouTube Studio or any platform UI, and no scraping behind logins.
- **Review mode is on by default.** Nothing posts until you tap Approve.
- **Humans do the human steps.** You create accounts, type passwords, accept terms, join campaigns, approve OAuth screens and enter payout details yourself.
- **The brief wins.** If a taste book or a default conflicts with a campaign's rules, the rules apply.

You are responsible for following each platform's terms and each campaign's brief.

---

## Quick start

Full walk-through, covering every account you need: **[docs/SETUP.md](docs/SETUP.md)**.

You need Python 3.11+, git, and `ffmpeg`/`ffprobe` on PATH.

```bash
git clone https://github.com/YaishRiaz/clipforge-starter.git clipforge
cd clipforge

# Windows (PowerShell)
powershell -ExecutionPolicy Bypass -File scripts/setup.ps1
# macOS / Linux
bash scripts/setup.sh
```

The script creates `.venv`, installs the requirements, copies `.env.example` to `.env`, and initialises the database. Fill in `.env`, then run these with the venv's python:

```bash
python -m clipforge llm-check        # is the LLM key good, and which models can it reach?
python -m clipforge telegram-setup   # find your chat id, send a test message
python -m clipforge youtube-auth     # one-time sign-in to your own channel
python -m clipforge emoji-fetch      # (optional) only if assets/emoji is missing
python -m clipforge supervise        # dashboard + worker, restarted if they stall
```

Dashboard: `http://localhost:8787`, protected by the password from `.env`.

---

## Commands

Run as `python -m clipforge <command>`. Add `-h` for options.

| Area | Commands |
|---|---|
| Run | `supervise` (dashboard and worker together), `serve`, `worker`, `status` |
| Campaigns | `campaign-add`, `campaign-joined`, `parse-brief`, `campaign-rules` (add channels, windows, hashtags...), `campaign-hold`, `campaigns` |
| Footage | `ingest-url`, `scan-inbox`, `sources`, `source-release` |
| Moments and clips | `mine`, `moments`, `clip`, `clip-source`, `clips` |
| Style and scoring | `taste-build <niche>`, `taste-approve`, `taste-show`, `rubric-build`, `rubric-reweight`, `emoji-fetch` |
| Accounts | `llm-check`, `telegram-setup`, `youtube-auth` |
| Posting and stats | `publish-now`, `stats-collect` |
| Windows helpers | `install-service`, `uninstall-service`, `service-status`, `power-setup` |
| Machine | `init-db`, `sysinfo`, `benchmark`, `selftest` |

---

## How it is built

- **Every pipeline step is a job** made of idempotent stages, in a SQLite queue (`clipforge/core/queue.py`, `handlers.py`). A crash halfway re-runs only the unfinished stage. Long stages call `ctx.beat()` so the supervisor can tell a slow job from a stuck one.
- **One heavy job at a time,** with peak memory kept under 4 GB.
- **Schema changes are append-only migrations** (`clipforge/core/db.py`). An applied migration is never edited.
- **Secrets only come from `.env`,** read by `clipforge/core/config.py`. They are never logged.

```
clipforge/
  core/       config, database, job queue, worker, supervisor, autopilot, priority
  ingest/     briefs -> rules, sources, downloads, Twitch/Kick channels, live watcher, Kick API
  moments/    transcription, signals (loudness, speech rate, scenes, chat), scoring, rubrics
  edit/       layouts, facecam finder, captions/ASS, emoji, render, compliance, packaging
  taste/      taste books: build (measure top Shorts), style, per-streamer/per-game subjects
  app/        FastAPI dashboard (Jinja), Telegram bot
  publish/    YouTube upload, pacing, posting queue, submission reminders
  stats/      view collection, earnings
  discover/   campaign discovery helpers
layouts/      per-creator facecam boxes (optional; the facecam is also found automatically)
rubrics/      scoring rubrics by niche, streamer and game
tastebook/    edit-style books by niche, streamer and game (+ README)
assets/emoji/ Twemoji PNGs (CC-BY 4.0)
docs/         setup guide, clip-quality SOP, verified platform facts
scripts/      one-time setup scripts
tests/        pytest suite
```

The design document is [CLIPFORGE_PRD.md](CLIPFORGE_PRD.md). It was written for the original operator, so "Yaish" there and in code comments means *whoever runs the install*. [docs/verification.md](docs/verification.md) records platform facts checked against vendor pages; where it disagrees with the PRD, it wins. [docs/clip_sop.md](docs/clip_sop.md) is the clip-quality checklist.

---

## Contributing

Contributions are welcome: new footage sources, layouts, taste-book measurements, publishers, dashboard pages, docs and fixes. Start with **[CONTRIBUTING.md](CONTRIBUTING.md)**. The short version:

- Open an issue first for anything larger than a fix.
- Add tests with the change, and keep `python -m pytest -q` green. CI runs it on every pull request.
- Keep the [responsible-use limits](#responsible-use-read-this-first). Keep secrets out of code, tests and logs.

Please follow the [Code of Conduct](CODE_OF_CONDUCT.md). Report security issues privately, as described in [SECURITY.md](SECURITY.md).

## Licence

ClipForge is released under the [MIT License](LICENSE). Bundled Twemoji graphics are CC-BY 4.0. Dependency licences are listed in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

ClipForge is not affiliated with YouTube, Google, Twitch, Kick, Whop, Content Rewards or any campaign platform or game publisher.
