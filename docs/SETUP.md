# Setting up ClipForge

Everything here is done by you, on your own accounts. Budget about an hour, most of it the YouTube step.

## 0. What you need

- A Windows, macOS or Linux machine that can stay on (Windows has helpers for auto-start).
- Python 3.11 or newer, Git, and `ffmpeg` (with `ffprobe`) on your PATH. Check with `ffmpeg -version`.
- A YouTube channel of your own, and the Google account that owns it.
- A Telegram account on your phone.

## 1. Get the code and install

```
git clone https://github.com/YaishRiaz/clipforge-starter.git clipforge
cd clipforge
```

Windows: `powershell -ExecutionPolicy Bypass -File scripts/setup.ps1`
macOS/Linux: `bash scripts/setup.sh`

This makes `.venv`, installs `requirements.txt`, copies `.env.example` to `.env` and creates the database. Check it worked:

```
.venv\Scripts\python -m pytest -q        # Windows (use .venv/bin/python on macOS/Linux)
```

All later commands are `python -m clipforge ...` with the venv's python.

## 2. Fill in `.env` (never commit it)

Open `.env` in an editor. Set these:

| Setting | Where it comes from |
|---|---|
| `DASHBOARD_PASSWORD` | Make one up. It protects the dashboard. |
| `LLM_PROVIDER` + `GROQ_API_KEY` | Free key from console.groq.com (provider `groq`). `gemini` or `anthropic` also work. |
| `YOUTUBE_API_KEY`, `YOUTUBE_CHANNEL_ID` | Step 4 |
| `YOUTUBE_OAUTH_CLIENT_ID`, `YOUTUBE_OAUTH_CLIENT_SECRET` | Step 4 |
| `TELEGRAM_BOT_TOKEN` | Step 3 |

Leave the rest at their defaults. `REVIEW_MODE=1` means nothing posts until you tap Approve. Keep it that way at the start.

Check the LLM key: `python -m clipforge llm-check`.

## 3. Telegram bot (phone review)

1. In Telegram, message **@BotFather**, send `/newbot`, follow the prompts, copy the token into `TELEGRAM_BOT_TOKEN`.
2. Send any message to your new bot.
3. Run `python -m clipforge telegram-setup`. It finds your chat id, writes `TELEGRAM_CHAT_ID` and sends a test message. Only that chat can control the bot.

## 4. YouTube (uploads and view counts)

In the Google Cloud console, signed in as the account that owns your channel:

1. Create a project. Enable **YouTube Data API v3**.
2. **Credentials, create API key** and put it in `YOUTUBE_API_KEY` (read-only stats). Your channel id (starts `UC`, from YouTube Studio, Settings, Channel, Advanced) goes in `YOUTUBE_CHANNEL_ID`.
3. **OAuth consent screen**: external, add your own Google account as a **test user**.
4. **Credentials, create OAuth client ID**, type **Desktop app**, and put the values in `YOUTUBE_OAUTH_CLIENT_ID` and `YOUTUBE_OAUTH_CLIENT_SECRET`.
5. Run `python -m clipforge youtube-auth` and approve the consent screen in the browser. The refresh token is saved to `.env`.

While the consent screen is in "Testing" the token lasts **7 days**; if uploads start failing, run `youtube-auth` again.

## 5. Campaigns

1. Find a campaign on the platform and read the brief. Decide whether you want it.
2. **Join it yourself** on the platform (you accept its terms).
3. Add it in the dashboard (Campaigns page) or with `campaign-add`, paste the brief, then run `campaign-joined <id>` once you have joined.
4. `parse-brief <id>`, then open the campaign page and **check the parsed rules** against the brief. Correct anything wrong (`campaign-rules <id>`).
5. Only footage the brief names is ingested. A creator's whole channel is only taken if you approve it with `campaign-rules <id> --add-channel <url>`.

Brief-specific things are enforced by the compliance check: an overlay with the streamer's handle, required hashtags, a creator's name, a disclosure line, a facecam. Check these rules on the campaign page:

- **Time windows** (`source_windows`): only part of a VOD is approved ("from 45:50 onwards").
- **Footage start and game** (`footage_after`, `footage_games`): only a given day's stream, or only a given game. The live watcher and the channel scan skip everything else.
- **Opening time** (`post_from`): nothing posts before the campaign opens.
- **Paid promotion** (`paid_promotion`): uploads get YouTube's "includes paid promotion" label.

The facecam is found automatically for Twitch and Kick footage. A `layouts/<creator>.json` box is only needed to pin an exact position.

## 5b. Live streams (Twitch and Kick)

When a channel a campaign approves goes live, `serve` notices within two minutes:

- **Twitch:** chat is read anonymously, without an account. A burst of reactions becomes a moment, cut from the VOD being written.
- **Kick:** chat is not read. Kick only offers it through its developer API, which needs an app on your Kick account. The stream is taken in 20-minute stretches instead, and each is scored on loudness and speech rate.

When the stream ends, the whole VOD is mined as usual. The number of live clips per stream is a setting on the dashboard (default 4).

## 5c. Edit style: taste books and rubrics

- `python -m clipforge taste-build <niche>` watches the top Shorts of a niche and writes a **draft** taste book with evidence. It measures font, size, colours, words per caption, banner, emoji, zoom and pace.
- Books for a streamer (`streamer-<name>`) or a game (`game-<name>`) are built automatically for footage you clip, at most two a day. Each build costs about 650 YouTube API units.
- Drafts arrive in Telegram with **Read / Approve / Reject** buttons. Nothing is used until you approve it.
- At clip time the books are layered (niche, then game, then streamer), and then the brief removes anything it bans.
- Reaction emoji come from `assets/emoji` (Twemoji, CC-BY 4.0). They are already in the repository; `emoji-fetch` downloads them again if missing.

## 5d. Priority campaigns

From release day, a campaign that names GTA VI posts before everything else and its jobs run first. Two kv keys control this:

- `priority_gta6_from` moves the start time.
- `campaign_priority_<id>` set to `1` or `0` forces any campaign on or off.

Use it for any launch where early clips get the views.

## 6. Run it

```
python -m clipforge supervise
```

Open `http://localhost:8787`. To use the dashboard from your phone on the same Wi-Fi, use the address `serve` prints and allow the firewall prompt. Away from home you need something like Tailscale.

Windows, to start at logon and never sleep on mains power:

```
python -m clipforge install-service
python -m clipforge power-setup
```

## 7. Daily loop

1. A clip arrives in Telegram. Watch it on your phone.
2. Tap **Approve** or **Reject**. Reply to the clip with a note ("cut the last 3 seconds") to have it remade.
3. Approved clips post to YouTube at spaced slots. Telegram sends the link and a button for the campaign's submit page. Submit it yourself.
4. For TikTok and Instagram Reels, Telegram also sends the video and a caption for each platform. Post those by hand.
5. Views appear on the dashboard and are collected every few hours.

## Safety checklist

- `.env` is git-ignored. Run `git status` before every commit and make sure no `.env`, token, key, database or video is listed.
- If a key ever leaks, revoke it at the provider (BotFather `/revoke`, Groq console, Google Cloud) and put the new one in `.env`.
- Use your own channel and your own accounts. Do not share a channel, a refresh token or a bot with anyone else.
