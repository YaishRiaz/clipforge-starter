"""SQLite connection and schema (PRD section 15).

One file, WAL mode. Migrations are numbered and applied in order using
PRAGMA user_version, so later phases add a migration instead of editing one.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

MIGRATIONS: list[str] = [
    # 1 - initial schema
    """
    CREATE TABLE campaigns (
        id INTEGER PRIMARY KEY,
        platform TEXT NOT NULL,              -- whop | vyro | own | other
        external_id TEXT,
        name TEXT NOT NULL,
        creator TEXT,
        category TEXT,
        rate_per_1k REAL,
        clip_cap REAL,
        min_views INTEGER,
        budget_left REAL,
        deadline TEXT,
        allowed_platforms TEXT,              -- JSON list
        country_ok INTEGER,                  -- 1 yes, 0 no, NULL unknown
        brief_url TEXT,
        brief_text TEXT,
        rules_json TEXT,
        status TEXT NOT NULL DEFAULT 'found'
            CHECK (status IN ('found','active','ended','skipped')),
        score REAL,
        score_reason TEXT,
        first_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
        joined_at TEXT,
        ended_at TEXT,
        UNIQUE (platform, external_id)
    );

    CREATE TABLE sources (
        id INTEGER PRIMARY KEY,
        campaign_id INTEGER NOT NULL REFERENCES campaigns(id),
        creator TEXT,
        title TEXT,
        url TEXT,
        path TEXT,
        duration REAL,
        published_at TEXT,
        language TEXT,
        summary TEXT,
        chapters_json TEXT,
        transcript_path TEXT,
        status TEXT NOT NULL DEFAULT 'new'
            CHECK (status IN ('new','held','downloaded','tagged','mined','done','failed','deleted')),
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        last_clip_posted_at TEXT,
        UNIQUE (campaign_id, url)
    );

    CREATE TABLE moments (
        id INTEGER PRIMARY KEY,
        source_id INTEGER NOT NULL REFERENCES sources(id),
        start REAL NOT NULL,
        "end" REAL NOT NULL,
        description TEXT,
        hook TEXT,
        title TEXT,
        score REAL,
        rubric_category TEXT,
        status TEXT NOT NULL DEFAULT 'candidate'
            CHECK (status IN ('candidate','selected','dropped','clipped'))
    );

    CREATE TABLE clips (
        id INTEGER PRIMARY KEY,
        moment_id INTEGER NOT NULL REFERENCES moments(id),
        path TEXT,
        thumb_path TEXT,
        package_path TEXT,
        meta_json TEXT,
        compliance_ok INTEGER,
        compliance_notes TEXT,
        status TEXT NOT NULL DEFAULT 'ready'
            CHECK (status IN ('ready','approved','posted','submitted','rejected','held')),
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );

    CREATE TABLE posts (
        id INTEGER PRIMARY KEY,
        clip_id INTEGER NOT NULL REFERENCES clips(id),
        publisher TEXT,
        publisher_post_id TEXT,
        video_id TEXT,
        url TEXT,
        posted_at TEXT,
        submit_deadline TEXT,
        submitted_at TEXT,
        campaign_review_status TEXT NOT NULL DEFAULT 'pending'
            CHECK (campaign_review_status IN ('pending','approved','rejected'))
    );

    CREATE TABLE stats (
        post_id INTEGER NOT NULL REFERENCES posts(id),
        captured_at TEXT NOT NULL DEFAULT (datetime('now')),
        views INTEGER,
        likes INTEGER,
        comments INTEGER,
        PRIMARY KEY (post_id, captured_at)
    );

    CREATE TABLE payouts (
        id INTEGER PRIMARY KEY,
        campaign_id INTEGER REFERENCES campaigns(id),
        amount_usd REAL NOT NULL,
        fee_usd REAL DEFAULT 0,
        received_at TEXT NOT NULL,
        note TEXT
    );

    CREATE TABLE jobs (
        id INTEGER PRIMARY KEY,
        type TEXT NOT NULL,
        ref_id INTEGER,
        payload_json TEXT NOT NULL DEFAULT '{}',
        status TEXT NOT NULL DEFAULT 'queued'
            CHECK (status IN ('queued','running','done','failed')),
        stage TEXT,                          -- last COMPLETED stage
        priority INTEGER NOT NULL DEFAULT 0,
        attempts INTEGER NOT NULL DEFAULT 0,
        last_error TEXT,
        run_after REAL NOT NULL DEFAULT 0,   -- epoch seconds
        heartbeat_at REAL,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    );
    CREATE INDEX jobs_pick ON jobs (status, run_after, priority DESC, id);
    CREATE INDEX jobs_ref ON jobs (type, ref_id);

    CREATE TABLE kv (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    """,
    # 2 - Phase 1 ingest: why a source is held, how much disk it takes, and
    # one row per file on disk. NULL paths stay distinct in SQLite, which is
    # what we want: several sources can be waiting to download at once.
    """
    ALTER TABLE sources ADD COLUMN held_reason TEXT;
    ALTER TABLE sources ADD COLUMN bytes INTEGER;
    CREATE UNIQUE INDEX sources_path ON sources (campaign_id, path);
    """,
    # 3 - PRD section 5: a source longer than MAX_SOURCE_HOURS is held "until
    # Yaish approves it". This records that he did, so re-running the job does
    # not hold it again. Most of a Twitch streamer's VODs are 4-7 hours, so
    # this is the normal path for campaign footage, not an edge case.
    """
    ALTER TABLE sources ADD COLUMN length_approved INTEGER NOT NULL DEFAULT 0;
    """,
    # 4 - audio-first ingest. A 5h45 Twitch VOD is 18.6 GB at 1080p60 but only
    # 519 MB as audio, and the transcript, the summary, the chapters and every
    # Phase 2 signal need the audio, not the pixels. Fetching only the chosen
    # moments later is not an option: `yt-dlp --download-sections` on Twitch HLS
    # streamed for 10 minutes without writing a usable byte (measured 12 Nov
    # 2026), because it has to read from the start to reach the seek point.
    # So a source can be ingested as audio to decide whether it is worth the
    # video at all. Phase 3 must re-fetch the video for a source still marked
    # 'audio' before it can cut anything.
    """
    ALTER TABLE sources ADD COLUMN media_kind TEXT NOT NULL DEFAULT 'video';
    """,
    # 5 - campaign discovery: what the platforms' public lists showed, so the
    # dashboard can filter it. Joining stays Yaish's click; `state` records what he
    # did with each row, and `campaign_id` links the row once he joined.
    """
    CREATE TABLE discovered (
        id INTEGER PRIMARY KEY,
        source TEXT NOT NULL,
        external_id TEXT NOT NULL,
        title TEXT NOT NULL,
        brand TEXT,
        category TEXT,
        platforms_json TEXT NOT NULL DEFAULT '[]',
        rate_min REAL,
        rate_max REAL,
        budget_total REAL,
        budget_left REAL,
        requires_application INTEGER NOT NULL DEFAULT 0,
        locked INTEGER NOT NULL DEFAULT 0,
        ended INTEGER NOT NULL DEFAULT 0,
        gaming INTEGER NOT NULL DEFAULT 0,
        url TEXT NOT NULL,
        image TEXT,
        ends_at TEXT,
        min_views INTEGER,
        state TEXT NOT NULL DEFAULT 'new'
            CHECK (state IN ('new','watching','hidden','joined')),
        campaign_id INTEGER REFERENCES campaigns(id),
        first_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
        last_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE (source, external_id)
    );
    """,
    # 6 - remember that a source came from a channel Yaish approved, so the clip
    # stage can fetch its picture later (the link itself is not in the brief)
    """
    ALTER TABLE sources ADD COLUMN via_channel INTEGER NOT NULL DEFAULT 0;
    UPDATE sources SET via_channel=1 WHERE media_kind='audio' AND length_approved=1;
    """,
    # 7 - where each post's viewers are, from the channel's own YouTube analytics
    """
    CREATE TABLE geo (
        post_id INTEGER NOT NULL REFERENCES posts(id),
        country TEXT NOT NULL,
        views INTEGER NOT NULL,
        captured_at TEXT NOT NULL DEFAULT (datetime('now')),
        PRIMARY KEY (post_id, country)
    );
    """,
    # 8 - a joined campaign's remaining budget over time, to see how fast it drains
    """
    CREATE TABLE budget_log (
        discovered_id INTEGER NOT NULL REFERENCES discovered(id),
        captured_at TEXT NOT NULL DEFAULT (datetime('now')),
        budget_left REAL NOT NULL
    );
    CREATE INDEX budget_log_by_campaign ON budget_log (discovered_id, captured_at);
    """,
    # 9 - clipping a stream while it is live: the VOD still being written, and the moments
    #     found from its chat before the whole VOD could be mined
    """
    ALTER TABLE sources ADD COLUMN live INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE moments ADD COLUMN live INTEGER NOT NULL DEFAULT 0;
    """,
    # 10 - Yaish's own posting order for approved clips (NULL = the automatic order)
    """
    ALTER TABLE clips ADD COLUMN post_rank INTEGER;
    """,
    # 11 - the streamer and game spans of a source (from Twitch), and the game of each moment,
    #      so a clip can use that streamer's and that game's taste book and rubric
    """
    ALTER TABLE sources ADD COLUMN games_json TEXT;
    ALTER TABLE moments ADD COLUMN game TEXT;
    """,
]


def connect(db_path: Path | str) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # isolation_level=None: we manage transactions explicitly.
    conn = sqlite3.connect(str(path), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def migrate(conn: sqlite3.Connection) -> int:
    """Apply pending migrations. Returns the schema version afterwards."""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for number, sql in enumerate(MIGRATIONS, start=1):
        if number <= version:
            continue
        # executescript commits any open transaction first, then runs the batch.
        conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version={number};\nCOMMIT;")
        version = number
    return version


def open_db(db_path: Path | str) -> sqlite3.Connection:
    conn = connect(db_path)
    migrate(conn)
    return conn


def kv_get(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def kv_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO kv (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=datetime('now')",
        (key, value),
    )
