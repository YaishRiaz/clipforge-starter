# Changelog

All notable changes are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## Unreleased

## 0.2.0 - 2026-11-18

First public, open-source release (MIT).

### Added
- **Kick support:** live status, VOD listing and VOD playlists from Kick's public endpoints. Kick VODs can be cut while the stream is still live. Live Kick streams are taken in rolling 20-minute stretches.
- **Live clipping on Twitch:** anonymous read-only chat. A burst of chat reactions becomes a moment, cut from the in-progress VOD.
- **Brief gates:**
  - `footage_after` and `footage_games` (only a given day's stream, or only a given game);
  - `post_from` (no posting before a campaign opens);
  - `paid_promotion` (YouTube's paid-promotion label);
  - `required_caption`, `required_names_any` and `source_windows`.
- **Priority campaigns:** campaigns for a launch (e.g. GTA VI from release day) post first and jump the job queue.
- **Taste books per niche, game and streamer,** measured from top Shorts (font, size, colours, words per caption, banner, emoji, zoom, pace) and layered at clip time. Approve them from Telegram.
- **Rubrics per streamer and game,** blended with the niche rubric.
- **Reaction emoji** (Twemoji, CC-BY 4.0), chosen from the spoken words.
- **Profanity masking** in captions and titles (`s**t`).
- **Layouts:** `game_full` (facecam over the whole game) and `fit` (whole frame over a blur). The facecam is found automatically for every stream, and boxes that swallow a panel are trimmed.
- **Posting:** a reorderable "Up next" queue on the dashboard. The streamer's channel is credited in the post text.
- **Open-source files:** licence, notices, contributing guide, code of conduct, security policy, CI.

### Fixed
- The "up next" time skipped a posting slot that had passed unused.
- A Telegram button that had sat too long ("query is too old") no longer aborts its action.

## 0.1.0 - 2026-11-15

Private starter: the pipeline from brief to posted Short, the dashboard, the Telegram review bot, the YouTube upload, view stats, and setup scripts.
