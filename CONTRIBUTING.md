# Contributing to ClipForge

Thanks for helping. ClipForge is a small, opinionated tool. It aims to stay runnable on an old laptop, honest about what it does, and inside the platforms' rules. These notes keep changes in that spirit.

## Before you start

- **Bugs:** open an issue with the bug template. Include the command, what you expected, what happened, and the log lines. Remove keys, tokens, chat ids and channel ids first.
- **Features:** open an issue (feature template) before writing a lot of code, so we can agree on the shape.
- **Small fixes** (typos, docs, an obvious bug with a test) can go straight to a pull request.

## Development setup

```bash
git clone https://github.com/YaishRiaz/clipforge-starter.git clipforge
cd clipforge
bash scripts/setup.sh            # or: powershell -ExecutionPolicy Bypass -File scripts/setup.ps1
.venv/bin/python -m pytest -q    # Windows: .venv\Scripts\python -m pytest -q
```

The tests need `ffmpeg` and `ffprobe` on PATH. They need no API keys and no network: every external call is passed in as a fake.

## Rules the code follows

1. **Every pipeline step is a job.** Register it in `clipforge/core/handlers.py` as idempotent stages. Running a stage twice must be harmless. Long stages call `ctx.beat()`.
2. **The machine is small:** 8 GB RAM, no GPU, one heavy job at a time, peak memory under 4 GB, no local LLM, no Docker. Prefer streaming and section downloads to whole files.
3. **Schema changes** are a new entry at the end of `MIGRATIONS` in `clipforge/core/db.py`. Never edit an applied migration.
4. **Secrets** come from `.env` through `clipforge/core/config.py`. Never print, log, commit, or put them in test fixtures.
5. **The brief wins.** Anything that changes how a clip looks or what footage is used must respect `BriefRules` (`clipforge/ingest/rules.py`) and the compliance check (`clipforge/edit/compliance.py`).
6. **Responsible-use limits are not optional.** See the README. We will not merge changes that:
   - ingest footage a joined campaign has not approved;
   - reuse footage across campaigns;
   - post without approval in review mode;
   - add multi-account tricks, fake engagement or detection evasion;
   - automate a platform's UI, or use a platform's private/internal API.

   Public, documented endpoints only.
7. **A failure must not hide.** If something cannot be done (a missing key, a blocked endpoint), log why and hold the item for the operator rather than guessing.

## Style

- Python 3.11+, type hints on public functions, `from __future__ import annotations`.
- Docstrings and comments say *why*, in plain sentences.
- Keep modules focused. A new footage source belongs in `clipforge/ingest/`, a new layout or overlay in `clipforge/edit/`.
- Tests go in `tests/test_<area>.py`, named for the behaviour (`test_a_brief_that_bans_graphics_turns_emoji_off`).

## How changes get in

`main` is protected, so nobody can change the code there directly:

1. Fork the repository, make a branch, and open a pull request against `main`.
2. On a first contribution, a maintainer approves the CI run before the tests start.
3. CI must pass on Python 3.11 and 3.13, and the branch must be up to date with `main`.
4. The code owner (see `.github/CODEOWNERS`) must approve the pull request. A new push after approval needs a fresh approval, and every review conversation must be resolved.
5. The maintainer squash-merges it. Force-pushes to `main` and deleting `main` are blocked.

## Pull requests

- One topic per PR. Explain what changes for the operator, and how you tested it.
- `python -m pytest -q` must pass locally and in CI.
- Add an entry under "Unreleased" in [CHANGELOG.md](CHANGELOG.md).
- By contributing you agree your work is released under the project's [MIT License](LICENSE).

## Good first contributions

- A layout file for a streamer's facecam (`layouts/<creator>.json`), with a test.
- More reaction-emoji triggers (`clipforge/edit/emoji.py`).
- Dashboard pages and text: clearer wording, mobile layout fixes.
- New publishers (TikTok, Instagram) through their **official** APIs only.
- Docs for a platform setup that is not covered yet.
