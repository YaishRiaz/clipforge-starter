"""Test isolation.

`config.load_env_file` fills gaps in `os.environ` with `setdefault`, which is
right in production (a child process should inherit what the CLI was given) but
means an unguarded test reads the developer's real `.env`. Once `.env` existed
on this machine, three tests started asserting against whatever happened to be
configured there.

So every test gets a throwaway CLIPFORGE_HOME, which is where `.env` is looked
for, and a clean slate for everything a provider decision depends on. A test
that wants one of these sets it itself.
"""
from __future__ import annotations

import pytest

from clipforge.core.config import get_settings

# Anything that would change which provider, model or key a test sees.
MANAGED = (
    "LLM_PROVIDER", "LLM_MODEL_BRIEF", "LLM_MODEL_SCORING", "LLM_BASE_URL",
    "ANTHROPIC_API_KEY", "GROQ_API_KEY", "GEMINI_API_KEY", "TRANSCRIPTION_API_KEY",
    "YOUTUBE_API_KEY", "YOUTUBE_CHANNEL_ID", "YOUTUBE_OAUTH_CLIENT_ID",
    "YOUTUBE_OAUTH_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN",
    "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
    "BUFFER_API_KEY", "BUFFER_CHANNEL_ID",
    "DASHBOARD_PASSWORD", "DISK_FLOOR_GB", "MAX_SOURCE_HOURS", "ALLOW_BRIEF_FETCH",
)


@pytest.fixture(autouse=True)
def isolate_settings(tmp_path_factory, monkeypatch):
    """No test reads the real .env, and no real key reaches a test."""
    home = tmp_path_factory.mktemp("clipforge-home")
    monkeypatch.setenv("CLIPFORGE_HOME", str(home))
    for name in MANAGED:
        monkeypatch.delenv(name, raising=False)
    # The free-disk floor protects a real install; a test machine (a CI runner has about
    # 14 GB free) must not fail for lack of space it never uses. The floor's own test sets it.
    monkeypatch.setenv("DISK_FLOOR_GB", "0")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
