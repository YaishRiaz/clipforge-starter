"""Settings, loaded from the environment and an optional .env file.

Stdlib only so Phase 0 runs on a bare Python install.
Secrets are read here and never logged or printed.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


def repo_root() -> Path:
    override = os.environ.get("CLIPFORGE_HOME")
    if override:
        return Path(override).resolve()
    return Path(__file__).resolve().parents[2]


def package_root() -> Path:
    """Where the code lives, ignoring CLIPFORGE_HOME.

    CLIPFORGE_HOME relocates *data*. Files that ship with the repo, such as the
    rubrics, must still be found when it points somewhere else.
    """
    return Path(__file__).resolve().parents[2]


def load_env_file(path: Path) -> None:
    """Parse KEY=VALUE lines. Real environment variables win over the file."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    root: Path
    data_dir: Path
    db_path: Path
    sources_dir: Path
    inbox_own: Path
    out_dir: Path
    rubrics_dir: Path

    # dashboard
    dashboard_host: str
    dashboard_port: int
    dashboard_password: str

    # external services (all optional until the phase that needs them)
    anthropic_api_key: str
    youtube_api_key: str
    youtube_channel_id: str
    youtube_oauth_client_id: str
    youtube_oauth_client_secret: str
    youtube_refresh_token: str
    telegram_bot_token: str
    telegram_chat_id: str
    buffer_api_key: str
    buffer_channel_id: str
    transcription_api_key: str

    # LLM: the laptop never runs a model locally (PRD section 14).
    # Provider is swappable so a free tier can stand in for paid API credit.
    llm_provider: str
    llm_base_url: str
    groq_api_key: str
    gemini_api_key: str
    llm_model_brief: str
    llm_model_scoring: str

    # pipeline limits
    disk_floor_gb: float
    max_source_hours: float
    source_retention_days: int
    max_height: int
    allow_brief_fetch: bool

    # Phase 2 transcription. No automatic failover between backends: where
    # licensed campaign audio may go is a decision, not a runtime fallback.
    transcribe_backend: str
    transcribe_chunk_seconds: int
    whisper_model: str
    moments_per_source: int
    sources_per_day: int
    clip_buffer: int
    max_transcript_chars: int
    llm_reasoning_effort: str
    summary_windows: int

    # posting
    posts_per_day: int
    posts_per_day_ceiling: int
    min_gap_hours: float
    review_mode: bool
    auto_score_threshold: int

    # worker
    poll_seconds: float
    max_attempts: int
    retry_backoff_seconds: float
    stall_minutes: int
    cpu_temp_limit_c: float
    memory_limit_mb: float

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.sources_dir, self.inbox_own, self.out_dir):
            d.mkdir(parents=True, exist_ok=True)

    @property
    def llm_api_key(self) -> str:
        """The key for the provider in use. Never logged or printed."""
        keys = {"groq": self.groq_api_key, "gemini": self.gemini_api_key}
        return keys.get(self.llm_provider, self.anthropic_api_key)

    @property
    def llm_key_name(self) -> str:
        names = {"groq": "GROQ_API_KEY", "gemini": "GEMINI_API_KEY"}
        return names.get(self.llm_provider, "ANTHROPIC_API_KEY")

    def missing_for(self, phase: int) -> list[str]:
        """Names of settings a phase needs that are still empty."""
        need: dict[int, list[str]] = {
            1: ["llm_api_key"],
            3: ["youtube_api_key"],
            4: ["dashboard_password", "telegram_bot_token", "telegram_chat_id"],
            5: ["youtube_oauth_client_id", "youtube_oauth_client_secret", "youtube_refresh_token",
                "youtube_channel_id"],
        }
        out: list[str] = []
        for p, names in need.items():
            if p <= phase:
                for n in names:
                    if getattr(self, n):
                        continue
                    out.append(self.llm_key_name if n == "llm_api_key" else n.upper())
        return out


def set_env_value(path: Path, key: str, value: str) -> None:
    """Write KEY=VALUE into a .env file (replacing an existing line) and the live environment."""
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    lines = [ln for ln in text.splitlines() if not ln.startswith(f"{key}=")]
    lines.append(f"{key}={value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.environ[key] = value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    root = repo_root()
    load_env_file(root / ".env")
    env = os.environ.get
    data_dir = Path(env("CLIPFORGE_DATA_DIR", str(root / "data"))).resolve()
    return Settings(
        root=root,
        data_dir=data_dir,
        db_path=Path(env("CLIPFORGE_DB", str(data_dir / "clipforge.db"))).resolve(),
        sources_dir=data_dir / "sources",
        inbox_own=root / "inbox" / "own",
        out_dir=root / "out",
        rubrics_dir=Path(env("RUBRICS_DIR", str(package_root() / "rubrics"))).resolve(),
        dashboard_host=env("DASHBOARD_HOST", "0.0.0.0"),
        dashboard_port=_int("DASHBOARD_PORT", 8787),
        dashboard_password=env("DASHBOARD_PASSWORD", ""),
        anthropic_api_key=env("ANTHROPIC_API_KEY", ""),
        youtube_api_key=env("YOUTUBE_API_KEY", ""),
        youtube_channel_id=env("YOUTUBE_CHANNEL_ID", ""),
        youtube_oauth_client_id=env("YOUTUBE_OAUTH_CLIENT_ID", ""),
        youtube_oauth_client_secret=env("YOUTUBE_OAUTH_CLIENT_SECRET", ""),
        youtube_refresh_token=env("YOUTUBE_REFRESH_TOKEN", ""),
        telegram_bot_token=env("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=env("TELEGRAM_CHAT_ID", ""),
        buffer_api_key=env("BUFFER_API_KEY", ""),
        buffer_channel_id=env("BUFFER_CHANNEL_ID", ""),
        transcription_api_key=env("TRANSCRIPTION_API_KEY", ""),
        llm_provider=env("LLM_PROVIDER", "anthropic").strip().lower(),
        llm_base_url=env("LLM_BASE_URL", "").rstrip("/"),
        groq_api_key=env("GROQ_API_KEY", ""),
        gemini_api_key=env("GEMINI_API_KEY", ""),
        llm_model_brief=env("LLM_MODEL_BRIEF", ""),
        llm_model_scoring=env("LLM_MODEL_SCORING", ""),
        disk_floor_gb=_float("DISK_FLOOR_GB", 15.0),
        max_source_hours=_float("MAX_SOURCE_HOURS", 3.0),
        source_retention_days=_int("SOURCE_RETENTION_DAYS", 7),
        max_height=_int("MAX_HEIGHT", 1080),
        allow_brief_fetch=env("ALLOW_BRIEF_FETCH", "0") not in ("0", "false", "False", ""),
        transcribe_backend=env("TRANSCRIBE_BACKEND", "groq").strip().lower(),
        transcribe_chunk_seconds=_int("TRANSCRIBE_CHUNK_SECONDS", 600),
        whisper_model=env("WHISPER_MODEL", "base"),
        moments_per_source=_int("MOMENTS_PER_SOURCE", 8),
        sources_per_day=_int("SOURCES_PER_DAY", 2),
        clip_buffer=_int("CLIP_BUFFER", 6),
        max_transcript_chars=_int("MAX_TRANSCRIPT_CHARS", 9_000),
        llm_reasoning_effort=env("LLM_REASONING_EFFORT", "low").strip().lower(),
        summary_windows=_int("SUMMARY_WINDOWS", 12),
        posts_per_day=_int("POSTS_PER_DAY", 3),
        posts_per_day_ceiling=_int("POSTS_PER_DAY_CEILING", 6),
        min_gap_hours=_float("MIN_GAP_HOURS", 2.0),
        review_mode=env("REVIEW_MODE", "1") not in ("0", "false", "False"),
        auto_score_threshold=_int("AUTO_SCORE_THRESHOLD", 80),
        poll_seconds=_float("WORKER_POLL_SECONDS", 5.0),
        max_attempts=_int("WORKER_MAX_ATTEMPTS", 3),
        retry_backoff_seconds=_float("WORKER_RETRY_BACKOFF_SECONDS", 60.0),
        stall_minutes=_int("WORKER_STALL_MINUTES", 20),
        cpu_temp_limit_c=_float("CPU_TEMP_LIMIT_C", 85.0),
        memory_limit_mb=_float("MEMORY_LIMIT_MB", 4096.0),
    )
