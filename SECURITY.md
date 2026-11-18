# Security policy

## Reporting a vulnerability

Please **do not open a public issue** for a security problem. Instead:

1. Use GitHub's **"Report a vulnerability"** button on the repository's *Security* tab (private advisory), or
2. Contact the maintainer privately through their GitHub profile.

Include what is affected, how to reproduce it, and what an attacker could do. You will get an acknowledgement within a week. Fixes are credited unless you ask otherwise.

## Scope

In scope:

- Leaks of secrets from `.env` into logs, the dashboard, Telegram messages or rendered files.
- Dashboard authentication bypass. The dashboard binds to the LAN behind a password.
- Telegram bot commands accepted from a chat other than the configured `TELEGRAM_CHAT_ID`.
- Path traversal or command injection through briefs, URLs, file names or captions handed to ffmpeg or yt-dlp.

Out of scope: problems in third-party services, and issues that need an attacker who already controls your machine or your `.env`.

## Keeping your install safe

- `.env` is git-ignored. Run `git status` before every commit, and never paste `.env` content into issues or logs.
- If a key leaks, revoke it at the provider (BotFather `/revoke`, Groq/Gemini/Anthropic console, Google Cloud), then put the new one in `.env`.
- Do not expose the dashboard to the internet directly. Use your home Wi-Fi, or a private network such as Tailscale.
