# Third-party notices

ClipForge's own code is under the [MIT License](LICENSE). It depends on, or ships, the following
third-party work. Each keeps its own licence.

## Shipped in this repository

| What | Where | Licence |
|---|---|---|
| Twemoji 15.1.0 graphics (40 reaction emoji, unmodified PNGs) | `assets/emoji/` | [CC-BY 4.0](https://creativecommons.org/licenses/by/4.0/). Copyright 2019 Twitter, Inc and other contributors. Source: https://github.com/jdecked/twemoji. The full credit is in `assets/emoji/CREDIT.txt` and the licence text in `assets/emoji/LICENSE-GRAPHICS.txt`. |

The emoji also appear in videos ClipForge renders. CC-BY 4.0 asks for attribution "in any reasonable manner". Keeping `CREDIT.txt` with the files covers this repository. If you publish videos with emoji in them, consider crediting Twemoji in your channel's about page.

## Installed by `pip install -r requirements.txt` (not shipped here)

| Package | Licence |
|---|---|
| anthropic | Apache-2.0 |
| av (PyAV) | BSD-3-Clause. Its binary wheels bundle FFmpeg libraries, which are LGPL-2.1 or later. |
| faster-whisper | MIT |
| fastapi | MIT |
| httpx | BSD-3-Clause |
| jinja2 | BSD-3-Clause |
| numpy | BSD-3-Clause (plus 0BSD, MIT, Zlib parts) |
| opencv-python-headless | Apache-2.0 |
| psutil | BSD-3-Clause |
| pydantic (via anthropic and fastapi) | MIT |
| pytest | MIT |
| python-multipart | Apache-2.0 |
| uvicorn | BSD-3-Clause |
| yt-dlp | Unlicense |

## Installed separately by you

| Tool | Licence |
|---|---|
| [FFmpeg](https://ffmpeg.org/legal.html) (`ffmpeg` and `ffprobe` on PATH) | LGPL-2.1+ or GPL-2.0+, depending on the build you install. ClipForge only runs it as a separate program and does not link to it. |
| Whisper model weights (downloaded by faster-whisper on first local use) | MIT (OpenAI Whisper) |
| System fonts named in taste books (Arial Black, Impact, Segoe UI Black, Comic Sans MS) | Your operating system's licence. ClipForge does not ship any fonts; libass uses the ones installed on the machine. |

## Services you connect

Groq, Google Gemini, Anthropic, the YouTube Data API, Telegram, Twitch and Kick all have their own terms. You accept them with your own accounts. Nothing in this repository grants you rights to footage: see "Responsible use" in the [README](README.md).
