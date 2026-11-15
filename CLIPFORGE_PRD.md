# ClipForge — Product Requirements Document

**Owner:** Yaish
**Build agent:** Claude Code
**Written:** 12 Nov 2026
**Go-live:** 19 Nov 2026 (GTA 6 launch day)
**Target machine:** one old laptop, 8 GB RAM, no dedicated GPU, left on 24/7

---

## 0. How to use this document (instructions for Claude Code)

You are building ClipForge end to end from this document. Work through the phases in section 12 in order. Each phase has acceptance checks; do not start the next phase until they pass.

Rules for how you work:

1. **Verify before you build on a fact.** Every item marked `VERIFY` is something that was true in outside write-ups in November 2026 but that you must confirm against the vendor's own current documentation before writing code against it. If it turns out false, stop and tell Yaish, with the alternative you recommend.
2. **Benchmark before you choose settings.** The laptop is slow. Phase 0 measures it. Use measured numbers, not the estimates in this document.
3. **Stop and ask Yaish for these, never do them yourself:** creating accounts, typing passwords, accepting terms of service, joining a campaign, connecting a social account, entering payout or bank details, approving OAuth screens. Open the page, tell him what to click, and wait.
4. **Secrets** live in a `.env` file that Yaish fills in. Never print them, never commit them, never paste them into chat. Ship a `.env.example`.
5. **Keep a `PROGRESS.md`** at the repo root: what is done, what is blocked, what Yaish must do next. Update it at the end of every work session so a fresh session can resume.
6. **Hard limits** are in section 11. They are not negotiable and not something to work around.
7. **`docs/verification.md` overrides this document** where the two differ. It records what was checked against vendor documentation and the design changes that followed.

---

## 1. What ClipForge is

A local tool that turns permitted long-form video into packaged YouTube Shorts and tracks what they earn.

The business model is **paid clipping campaigns**: creators and brands fund a campaign, give clippers approved footage and a brief, and pay per 1,000 verified views on clips posted to the clipper's own channel. This is the reason ClipForge only ingests campaign footage and Yaish's own recordings: it is both the permission and the revenue.

The loop:

1. Discover open campaigns and show them to Yaish.
2. Yaish joins the ones he wants (his click, on the platform's own page).
3. ClipForge pulls that campaign's approved footage.
4. It finds the best moments, cuts vertical clips, adds a hook and captions.
5. It packages each clip with title, description and tags that follow the brief.
6. It posts to the channel, then gets the link submitted to the campaign.
7. It tracks views and estimated earnings on a dashboard.

Second content track: **Yaish's own GTA 6 gameplay recordings**, dropped into a watched folder.

---

## 2. Facts that shape the design

| Fact | Consequence |
|---|---|
| Videos uploaded through an unaudited YouTube API project are locked private. `VERIFY` | Do not upload with our own Google Cloud project. Post through an already-audited scheduler service (section 8). |
| Shorts that re-upload someone else's footage with nothing added get reduced reach; the reused-content policy applies even with permission. `VERIFY` | Every clip must carry added value: a written hook, context captions, a reason-to-watch title. Never post a bare cut. |
| Campaign footage is licensed only for making clips for that campaign. `VERIFY` | Tag every source file with its campaign. Never reuse footage across campaigns or after a campaign ends. |
| Content Rewards (Whop) asks for the link to be submitted within 30 minutes of posting; Vyro counts views from submission, not from publish. `VERIFY` | Submission must happen immediately after posting. A post that is not submitted is a failed job. |
| Clips are reviewed by the campaign's own team against the written brief; third-party tool watermarks commonly cause rejection. `VERIFY` | Parse the brief into machine-checkable rules. No watermark, no tool branding. Run a brief-compliance check before posting. |
| Rates, eligibility and payout methods vary by campaign and country. | Before clipping for a campaign, confirm Sri Lanka is eligible and a payout method works there. PayPal has historically not supported receiving money in Sri Lanka, so check bank transfer and other options first. `VERIFY` |
| Laptop: 8 GB RAM, CPU only. | One heavy job at a time, cloud LLM, lightest local models, short encodes. See section 9. |

---

## 3. Users and surfaces

One user: Yaish. Two surfaces:

- **Dashboard** at `http://localhost:8787`, also reachable from his phone on home Wi-Fi (bind to LAN, protect with a single password from `.env`).
- **Phone notifications** via a Telegram bot (free, no server needed) for: new campaign found, clips ready, post published, submission needed, job failed.

---

## 4. Module A — Campaign discovery

**Goal:** a "Campaigns" page listing open campaigns worth joining.

Sources, in order of preference:

1. An official API or feed from the platform, if one covers campaign listings. `VERIFY` for Whop / Content Rewards and Vyro.
2. Campaign alert emails or Discord alerts the platforms send, parsed from a dedicated inbox.
3. Reading the public discover pages in Yaish's own logged-in browser session, at a slow polite rate (no more than once every 30 minutes per platform), only if the platform's terms allow automated reading. `VERIFY` the terms. If they forbid it, use sources 1 and 2 only and say so in `PROGRESS.md`.

For each campaign store: platform, name, creator or brand, rate per 1,000 views, per-clip cap, minimum views, budget remaining, deadline, allowed posting platforms, category, country eligibility, link to the brief, date first seen.

**Scoring.** Rank campaigns by: rate, budget remaining, days left, whether YouTube Shorts is allowed, whether Sri Lanka is eligible, and fit with the gaming/GTA niche. Show the score and why.

**Card actions:**

- **Open & join** — opens the real campaign page in Yaish's browser. He reads the brief and joins there. ClipForge does not join for him: joining means accepting that campaign's terms, which is his decision.
- **I've joined** — marks the campaign active. (If the platform exposes "my campaigns", detect this automatically instead.)
- **Skip** — hides it.

Once a campaign is active, everything downstream is automatic.

---

## 5. Module B — Brief parsing and ingest

When a campaign becomes active:

1. Fetch the brief text. Use the LLM to extract a structured rule set: approved source footage (links, folders, channels, specific videos), clip length limits, required hashtags or mentions, required disclosure text, banned content or edits, minimum share of the frame showing the creator, caption requirements, allowed platforms. Save as JSON and show it on the campaign page so Yaish can correct it.
2. Download only the approved source footage named in the brief (shared drive folders, provided files, or the listed videos). Cap resolution at 1080p. Skip anything longer than a configurable limit (default 3 hours) until Yaish approves it.
3. Poll active campaigns for newly added footage every 30 minutes.

**Tagging.** Every source video gets a record: campaign, creator, title, original URL, duration, publish date, language, a 3–5 sentence summary of what happens, and a chapter list with timestamps. This is the "this is the YouTuber, this is the video, this is how long, this is what happens" requirement.

**Own-gameplay track.** A watched folder `inbox/own/`. Anything dropped there is ingested with `campaign = own`, no brief rules, and goes through the same pipeline.

**Disk hygiene.** Delete source files 7 days after their last clip is posted, or immediately when a campaign ends. Keep a free-space floor (default 15 GB); pause downloads below it.

---

## 6. Module C — Moment finding

Goal: pick the 3–8 best moments per source video, each 15–60 seconds (or as the brief dictates).

**Step 1 — transcript.** Use captions that come with the footage if present. Otherwise transcribe locally with `faster-whisper` (`base` model, int8, CPU). Optional setting: send audio to a hosted transcription API instead, if Yaish adds a key; this is far faster than the laptop.

**Step 2 — cheap signals, computed locally.**
- Audio energy peaks (shouting, laughter, crashes).
- Speech rate spikes.
- Scene-change density.
- For streams with chat replays available in the provided footage: chat message rate.

**Step 3 — LLM scoring.** Send the transcript with timestamps plus the signal peaks to a small, cheap Claude model through the API. Ask for candidate moments with: start, end, a one-line description, a hook line for the first frame, a title, a virality score 0–100, and which rubric category it fits. The laptop never runs an LLM locally.

**Step 4 — rubric.** Scoring uses a rubric file per niche.
- `rubrics/general.json` — setup-and-payoff, surprise, conflict, big reaction, quotable line, stakes stated early.
- `rubrics/gta.json` — built in Phase 3 by pulling titles, view counts and durations of top-performing GTA 5 Shorts through the read-only YouTube Data API (metadata only, no downloads), having the LLM cluster them into moment types (police chases, physics glitches, NPC chaos, stunts, heist fails, funny dialogue, first-time reactions), and weighting each type by how well it performed. GTA 6 clips are scored against this.
- Feedback loop: once a week, re-weight rubrics using our own clips' view counts.

**Step 5 — dedupe and select.** Drop overlapping candidates, enforce brief rules, keep the top N.

---

## 7. Module D — Editing and packaging

For each selected moment, produce one folder `out/<campaign>/<clip_id>/` containing:

- `clip.mp4` — 1080×1920, 30 fps, H.264, AAC, under 60 s unless the brief says otherwise.
- `thumb.jpg` — the first frame.
- `meta.json` — title, description, tags, hashtags, disclosure line, campaign id, source video, timestamps, score.
- `package.zip` — all of the above, for the dashboard's one-click download.

**Editing rules:**

- **Reframe to vertical** without a GPU: sample one frame per second, detect faces with a lightweight CPU detector, smooth the crop path. For gameplay with a facecam, use a stacked layout (facecam on top, gameplay below). For pure gameplay, centre crop with a slow follow on motion.
- **First frame is the thumbnail.** Burn the hook line in large text for the first 1.5–2 seconds so the first frame reads as a thumbnail.
- **Captions** burned in from the transcript, word-timed, two lines max, high contrast, inside the safe area (clear of the Shorts UI at the bottom and right).
- **Cut point discipline:** start on the first word of the hook sentence, not mid-breath; end on the payoff, not after it.
- **No watermark, no tool branding.**
- **Brief compliance check** runs last: length, required tags, disclosure, creator screen-share, banned items. A clip that fails is held with the reason shown, not posted.

**Captions and titles** are written per clip by the LLM: a title under 70 characters that states the stakes, a 1–2 line description, the brief's required tags plus up to 3 relevant ones, and the disclosure text if required.

---

## 8. Module E — Publishing and submission

**Posting.** Use a scheduler service whose YouTube integration is already audited, through its API, so posts go public without our own audit. First choice: Buffer's API (reported free on the free plan, YouTube supported). `VERIFY`: that it accepts a video file or URL for YouTube Shorts, sets title/description/visibility, and returns the published video ID. Fallbacks to evaluate if it does not: PostPeer, Zernio, Publer, Postiz. Wrap whichever is chosen behind a `Publisher` interface so it can be swapped.

In parallel, tell Yaish to submit the YouTube API compliance audit for a project of our own, as the long-term route.

**Review mode (default for the first 7 days).** Clips land in a "Ready" queue. Yaish taps **Approve** on the dashboard or in Telegram; ClipForge posts. This is the one-click flow.

**Auto mode (switch on after week one).** Clips with a score above a threshold and a passing compliance check post without approval.

**Pacing.** Start at 3 posts per day, spaced at least 3 hours apart, at times set in config. Raise by one per day each week to a ceiling of 8. This respects YouTube's per-channel upload limits and its policy against mass-produced, repetitive uploads. Never post two clips of the same moment.

**Manual fallback.** If the publisher is down, send the package to Telegram with the title and description as copy-ready text, so Yaish can post from the YouTube app in under a minute.

**Submission to the campaign.** Immediately after a post is public:
1. Record the video URL.
2. If the platform offers an API for submissions, use it. `VERIFY`.
3. Otherwise send Yaish a Telegram message with the link and a button that opens the campaign's submit page, and start a 25-minute countdown with a reminder at 15 minutes. Mark the job "awaiting submission" until he confirms.
4. Only automate the submit form in his browser if the platform's terms permit automated submissions. `VERIFY`, and ask Yaish before enabling.

---

## 9. Running on the slow laptop

Design constraints, all mandatory:

- **One heavy job at a time.** A single worker process pulls from a job queue stored in SQLite. Transcription and encoding never overlap.
- **Peak memory under 4 GB.** Stream files; never load a whole video into memory.
- **Seek, don't scan.** When cutting, seek to the segment before decoding. Never re-encode the full source.
- **Encode settings:** x264 `veryfast`, CRF 23, 30 fps. Benchmark in Phase 0 and adjust.
- **Low priority.** Run the worker at reduced CPU priority so the laptop stays usable.
- **Resumable.** Every job is idempotent and records its stage. After a crash, power cut or reboot, the worker resumes from the last completed stage.
- **Always on.** Install as a service that starts at boot (detect the OS and use the native mechanism). Disable sleep on AC power. Add a watchdog that restarts the worker if it stalls for 20 minutes and sends a Telegram alert.
- **Thermals.** If CPU temperature is readable and exceeds a configurable limit, pause between jobs.
- **Daily budget.** From the Phase 0 benchmark, compute how many source-minutes the laptop can process per day and show it on the dashboard. If active campaigns exceed it, process by campaign score order.

Estimates to replace with measurements: local transcription roughly 1–3× real time; one 45-second clip encode roughly 1–3 minutes.

---

## 10. Module F — Stats and dashboard

**Stats collection.** Every 6 hours, read public statistics (views, likes, comments) for every posted video using the YouTube Data API with a read-only API key. Reading public stats does not need the audit. Store a time series.

**Earnings.**
- *Estimated* = min(views × rate ÷ 1000, per-clip cap), zero if under the campaign's minimum views, and only counted for clips with status "approved" (shown separately as "pending" otherwise).
- *Actual* = payouts Yaish records, or read from the platform if it exposes them. `VERIFY`.
- Show platform fees separately if the platform deducts them.

**Dashboard pages:**

1. **Overview** — views today / 7 days / 30 days, estimated vs actual earnings in USD and LKR, clips posted, approval rate, top 5 clips.
2. **Campaigns** — discover list, active list, per-campaign views, earnings, budget left, deadline, approval rate.
3. **Queue** — every job and its stage, with errors.
4. **Ready** — clips awaiting approval: preview, title, score, Approve / Edit / Reject, Download package.
5. **Posted** — every clip with its link, submission status, views over time.
6. **Learn** — which moment types, hooks and lengths perform best; this feeds the rubric weights.
7. **Settings** — posting times, daily cap, auto-mode threshold, disk floor, review vs auto.

---

## 11. Hard limits

1. Ingest only (a) footage a joined campaign names as approved and (b) Yaish's own recordings. No other creator's videos, ever.
2. Never use footage outside the campaign it was licensed for.
3. No evasion of platform detection: no browser fingerprint spoofing, no proxies or VPN rotation, no multiple channels to dodge limits, no driving YouTube Studio with a bot.
4. No fake engagement: no view bots, no purchased views, no self-refresh loops. Campaigns pay on verified views and fraud forfeits earnings and the account.
5. Follow each brief's disclosure requirement exactly.
6. Claude Code does not create accounts, type passwords, accept terms, join campaigns, or enter payout details. Yaish does.
7. If any `VERIFY` item fails, stop that module and report; do not improvise around it.

---

## 12. Build phases

**Phase 0 — Setup and benchmark (night of 12 Nov)**
- Detect OS, CPU, RAM, free disk. Install Python 3.11+, ffmpeg, dependencies.
- Create repo, `.env.example`, `PROGRESS.md`, SQLite schema, job queue, worker skeleton.
- Benchmark: transcribe a 10-minute sample, encode a 45-second vertical clip. Record timings and compute the daily budget.
- Check every `VERIFY` item against current vendor docs. Write results to `docs/verification.md`.
- Give Yaish his checklist (section 13).
- *Accept:* worker runs a dummy job end to end and survives a forced restart; verification file complete.

**Phase 1 — Ingest and tagging (13 Nov)**
- Brief parser, source downloader, own-footage watched folder, tagging and summaries.
- *Accept:* one real campaign brief parsed into rules; one source video ingested with summary and chapters.

**Phase 2 — Moments (14 Nov)**
- Transcript, local signals, LLM scoring with `general.json`.
- *Accept:* 5 ranked moments with hooks and titles from a real video, inside the memory limit.

**Phase 3 — Editing and GTA rubric (15 Nov)**
- Reframe, hook frame, captions, packaging, compliance check. Build `gta.json`.
- *Accept:* a finished `package.zip` that Yaish watches on his phone and approves for quality.

**Phase 4 — Dashboard and notifications (16 Nov)**
- All dashboard pages, Telegram bot, review flow, package download.
- *Accept:* Yaish approves a clip from his phone.

**Phase 5 — Publishing, submission, stats (17 Nov)**
- Publisher integration, pacing, submission flow, stats collector, earnings.
- *Accept:* one clip posted public through the publisher, submitted to its campaign, and showing views on the dashboard.

**Phase 6 — Campaign discovery (17–18 Nov)**
- Discovery sources, scoring, campaign cards.
- *Accept:* Campaigns page lists live campaigns with correct rates and deadlines.

**Phase 7 — Soak test (18 Nov)**
- Run 24 hours unattended on 10 source videos. Fix failures. Install as a boot service, watchdog on.
- *Accept:* 24 hours with no stalled jobs; reboot test passes.

**Go-live — 19 Nov**
- Review mode on, 3 posts per day. GTA 6 own-footage folder active.

If time runs short, cut in this order: Learn page, auto mode, automated discovery (Yaish can paste campaign links by hand). Do not cut the compliance check, submission flow, or resumability.

---

## 13. Yaish's checklist (things only he can do)

1. Create the YouTube channel; set name, handle, banner.
2. Create accounts on Whop (Content Rewards) and Vyro; connect the YouTube channel to each.
3. Check each platform's payout options for Sri Lanka before clipping. Set up the one that works.
4. Create a Buffer account, connect the YouTube channel, generate an API key, put it in `.env`.
5. Create an Anthropic API key; put it in `.env`. Set a monthly spend limit.
6. Create a Google Cloud project, enable YouTube Data API, create a read-only API key; put it in `.env`. Submit the compliance audit form for future direct uploads.
7. Create a Telegram bot with BotFather; put the token and his chat ID in `.env`.
8. Join 2–3 campaigns to start (not ten); pick ones that allow YouTube Shorts and fit gaming.
9. Decide how GTA 6 gameplay gets recorded and into `inbox/own/`.

---

## 14. Tech stack

- **Language:** Python 3.11+
- **Storage:** SQLite (single file, WAL mode)
- **Web:** FastAPI + server-rendered templates with HTMX; no front-end build step
- **Media:** ffmpeg, faster-whisper (CPU, int8), OpenCV for face detection
- **LLM:** Anthropic API, smallest current Claude model for scoring and copy; a larger one only for brief parsing
- **Scheduling:** in-process scheduler plus the SQLite job queue
- **Notifications:** Telegram Bot API
- **Publishing:** scheduler API behind a `Publisher` interface
- **Packaging:** runs natively; no Docker (saves RAM on this machine)

Suggested layout:

```
clipforge/
  app/            dashboard (routes, templates)
  core/           db, queue, config, models
  discover/       campaign sources and scoring
  ingest/         brief parser, downloader, tagger
  moments/        transcript, signals, scoring
  edit/           reframe, captions, hook frame, packager, compliance
  publish/        publisher interface, pacing, submission
  stats/          collector, earnings
  rubrics/        general.json, gta.json
  inbox/own/      drop own recordings here
  out/            finished packages
  docs/           verification.md
  PROGRESS.md
  .env.example
```

---

## 15. Data model (minimum)

- `campaigns` — id, platform, name, creator, rate_per_1k, clip_cap, min_views, budget_left, deadline, allowed_platforms, country_ok, brief_url, rules_json, status (found / active / ended / skipped), score
- `sources` — id, campaign_id, creator, title, url, path, duration, published_at, summary, chapters_json, status
- `moments` — id, source_id, start, end, description, hook, title, score, rubric_category, status
- `clips` — id, moment_id, path, thumb_path, meta_json, compliance_ok, compliance_notes, status (ready / approved / posted / submitted / rejected / held)
- `posts` — id, clip_id, video_id, url, posted_at, submitted_at, campaign_review_status
- `stats` — post_id, captured_at, views, likes, comments
- `payouts` — id, campaign_id, amount_usd, received_at, note
- `jobs` — id, type, ref_id, stage, attempts, last_error, updated_at

---

## 16. Success measures (first 30 days)

- Pipeline uptime above 95% unattended.
- At least 3 clips posted per day from day one.
- Campaign approval rate above 70%.
- Every post submitted inside its window.
- First payout received, confirming the Sri Lanka payout route works. Earnings arrive in USD, so each payout is foreign currency coming into Sri Lanka rather than leaving it.
