"""Command line: python -m clipforge <command>"""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
from pathlib import Path

from .core import db, queue
from .core.config import get_settings
from .core.worker import Worker, heartbeat_age


def _conn():
    settings = get_settings()
    settings.ensure_dirs()
    return settings, db.open_db(settings.db_path)


def cmd_init_db(_: argparse.Namespace) -> int:
    settings, conn = _conn()
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    print(f"database ready at {settings.db_path} (schema v{version})")
    return 0


def cmd_worker(args: argparse.Namespace) -> int:
    settings, conn = _conn()
    if not args.drain:
        from .core import autopilot

        autopilot.ensure_scheduled(conn)
    finished = Worker(conn, settings).run(drain=args.drain)
    if args.drain:
        print(f"queue drained; {finished} job(s) finished")
    return 0


def cmd_selftest(args: argparse.Namespace) -> int:
    _, conn = _conn()
    payload: dict = {"sleep": args.sleep}
    if args.crash_at:
        payload["crash_at"] = args.crash_at
    if args.fail_at:
        payload["fail_at"] = args.fail_at
    job_id = queue.enqueue(conn, "selftest", payload=payload)
    print(f"queued selftest job {job_id}")
    return 0


def cmd_status(_: argparse.Namespace) -> int:
    settings, conn = _conn()
    age = heartbeat_age(settings)
    if age is None:
        worker = "never started"
    elif age > settings.stall_minutes * 60:
        worker = f"STALLED (last heartbeat {age / 60:.0f} min ago)"
    elif age > max(60.0, settings.poll_seconds * 4):
        worker = f"not running (last heartbeat {age:.0f}s ago)"
    else:
        worker = "running"
    print(f"worker: {worker}")
    print("jobs:  ", ", ".join(f"{k}={v}" for k, v in queue.counts(conn).items()))
    rows = conn.execute(
        "SELECT id, type, status, stage, attempts, last_error FROM jobs "
        "WHERE status IN ('running','failed') OR id IN (SELECT id FROM jobs ORDER BY id DESC LIMIT 5) "
        "ORDER BY id DESC LIMIT 15"
    ).fetchall()
    for r in rows:
        err = (r["last_error"] or "").strip().splitlines()[-1:] or [""]
        print(f"  #{r['id']:<4} {r['type']:<12} {r['status']:<8} stage={r['stage'] or '-':<10} "
              f"attempts={r['attempts']} {err[0][:80]}")
    missing = settings.missing_for(5)
    if missing:
        print("not yet set in .env:", ", ".join(missing))
    return 0


def cmd_sysinfo(_: argparse.Namespace) -> int:
    from .tools.sysinfo import collect

    print(json.dumps(collect(get_settings().root), indent=2))
    return 0


def cmd_benchmark(args: argparse.Namespace) -> int:
    from .tools.benchmark import run

    media = Path(args.media).resolve() if args.media else None
    if media is not None and not media.is_file():
        print(f"not a file: {media}", file=sys.stderr)
        return 2
    result = run(get_settings().root, media, args.minutes, args.start)
    print(json.dumps(result, indent=2))
    print("\nsaved to docs/benchmark.json")
    return 0


# --- Phase 1: campaigns, briefs, ingest -----------------------------------

def cmd_campaign_add(args: argparse.Namespace) -> int:
    _, conn = _conn()
    brief_text = Path(args.brief_file).read_text(encoding="utf-8") if args.brief_file else None
    cur = conn.execute(
        "INSERT INTO campaigns (platform, external_id, name, creator, category, rate_per_1k, "
        "clip_cap, min_views, brief_url, brief_text, status) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,'found') "
        "ON CONFLICT (platform, external_id) DO UPDATE SET "
        "name=excluded.name, brief_text=COALESCE(excluded.brief_text, campaigns.brief_text), "
        "brief_url=COALESCE(excluded.brief_url, campaigns.brief_url)",
        (args.platform, args.external_id, args.name, args.creator, args.category,
         args.rate, args.clip_cap, args.min_views, args.brief_url, brief_text),
    )
    row = conn.execute(
        "SELECT id, status FROM campaigns WHERE platform=? AND external_id=?",
        (args.platform, args.external_id),
    ).fetchone()
    print(f"campaign {row['id']} saved as '{row['status']}'"
          f"{' with brief text' if brief_text else ' (no brief text yet)'}")
    if row["status"] == "found":
        print("Nothing is ingested until you join it on the platform, then run:")
        print(f"  python -m clipforge campaign-joined {row['id']}")
    return 0 if cur else 0


def cmd_campaign_joined(args: argparse.Namespace) -> int:
    """Record that Yaish joined the campaign himself. ClipForge never joins one."""
    _, conn = _conn()
    row = conn.execute("SELECT id, name, status FROM campaigns WHERE id=?",
                       (args.campaign_id,)).fetchone()
    if row is None:
        print(f"no campaign {args.campaign_id}", file=sys.stderr)
        return 2
    conn.execute(
        "UPDATE campaigns SET status='active', joined_at=COALESCE(joined_at, datetime('now')) "
        "WHERE id=?", (args.campaign_id,),
    )
    joined = conn.execute("SELECT joined_at FROM campaigns WHERE id=?",
                          (args.campaign_id,)).fetchone()["joined_at"]
    print(f"campaign {row['id']} ({row['name']}) is active, joined_at={joined}")
    print("Only footage its brief names may be ingested, and only clips made from now on.")
    return 0


def cmd_parse_brief(args: argparse.Namespace) -> int:
    _, conn = _conn()
    job_id = queue.enqueue(conn, "parse_brief", ref_id=args.campaign_id)
    print(f"queued parse_brief job {job_id} for campaign {args.campaign_id}")
    return 0


def cmd_ingest_url(args: argparse.Namespace) -> int:
    from .ingest import sources

    _, conn = _conn()
    try:
        source_id = sources.register(
            conn, args.campaign_id, url=args.url,
            media_kind="audio" if args.audio_only else "video",
        )
    except sources.NotApproved as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 3
    job_id = queue.enqueue(conn, "ingest_source", ref_id=source_id)
    print(f"source {source_id} registered; queued ingest_source job {job_id}")
    return 0


def cmd_scan_inbox(args: argparse.Namespace) -> int:
    settings, conn = _conn()
    payload = {"repeat_seconds": args.repeat_minutes * 60} if args.repeat_minutes else {}
    job_id = queue.enqueue(conn, "scan_inbox", payload=payload, dedupe=False)
    print(f"queued scan_inbox job {job_id} for {settings.inbox_own}")
    return 0


def cmd_campaigns(_: argparse.Namespace) -> int:
    from .ingest import briefs

    _, conn = _conn()
    rows = conn.execute(
        "SELECT id, platform, name, status, joined_at, rules_json FROM campaigns ORDER BY id"
    ).fetchall()
    if not rows:
        print("no campaigns yet")
        return 0
    for r in rows:
        rules = briefs.load_rules(conn, r["id"])
        approved = len(rules.approved_source_urls) if rules else 0
        print(f"  #{r['id']:<3} {r['platform']:<6} {r['status']:<8} "
              f"{(r['name'] or '')[:34]:<34} "
              f"brief={'parsed' if rules else 'not parsed':<10} approved_links={approved}")
        if rules and rules.uncertain:
            for note in rules.uncertain:
                print(f"        ? {note}")
    return 0


def cmd_sources(_: argparse.Namespace) -> int:
    _, conn = _conn()
    rows = conn.execute(
        "SELECT s.id, s.campaign_id, s.status, s.duration, s.title, s.held_reason, "
        "s.summary IS NOT NULL AS tagged, s.transcript_path IS NOT NULL AS has_transcript "
        "FROM sources s ORDER BY s.id"
    ).fetchall()
    if not rows:
        print("no sources yet")
        return 0
    for r in rows:
        mins = f"{r['duration'] / 60:.1f}m" if r["duration"] else "-"
        print(f"  #{r['id']:<4} c{r['campaign_id']:<3} {r['status']:<11} {mins:>7} "
              f"transcript={'y' if r['has_transcript'] else 'n'} "
              f"summary={'y' if r['tagged'] else 'n'}  {(r['title'] or '')[:40]}")
        if r["held_reason"]:
            print(f"        held: {r['held_reason']}")
    return 0


def cmd_llm_check(args: argparse.Namespace) -> int:
    """Prove the configured provider and key work, without printing the key."""
    from .core import llm
    from .ingest.rules import BriefRules

    settings = get_settings()
    print(f"provider: {settings.llm_provider}")
    if settings.llm_provider != "anthropic":
        print(f"endpoint: {llm.base_url(settings)}")
    print(f"{settings.llm_key_name}: {'set' if settings.llm_api_key else 'NOT SET'}")
    if not settings.llm_api_key:
        print(file=sys.stderr)
        print(f"Add {settings.llm_key_name} to .env, then run this again.",
              file=sys.stderr)
        return 2

    brief = llm.model_for(settings, "brief")
    scoring = llm.model_for(settings, "scoring")
    print(f"brief model:   {brief}")
    print(f"scoring model: {scoring}")

    try:
        models = llm.available_models(settings)
    except (llm.LLMUnavailable, llm.LLMBadAnswer) as exc:
        print(file=sys.stderr)
        print(f"could not list models: {exc}", file=sys.stderr)
        return 3
    print()
    print(f"{len(models)} model(s) the key can reach:")
    for name in models:
        print(f"  {name}")

    missing = [m for m in {brief, scoring} if models and m not in models]
    if missing:
        print(file=sys.stderr)
        print(f"NOT AVAILABLE: {', '.join(sorted(missing))}", file=sys.stderr)
        print("Pick ids from the list above and set them in .env:", file=sys.stderr)
        print("  LLM_MODEL_BRIEF=<a capable model, for parsing briefs>", file=sys.stderr)
        print("  LLM_MODEL_SCORING=<a small fast model, one call per moment>",
              file=sys.stderr)
        return 3

    if args.no_call:
        return 0
    print()
    print("sending one tiny real request to check the key end to end...")
    try:
        rules = llm.parse(
            BriefRules,
            "You extract clipping-campaign rules. Record only what the brief states.",
            "Brief: Clips must be 20-60 seconds long. Approved footage: "
            "https://youtu.be/dQw4w9WgXcQ",
            model=scoring, max_tokens=2000, settings=settings,
        )
    except (llm.LLMUnavailable, llm.LLMBadAnswer) as exc:
        print(f"the call failed: {exc}", file=sys.stderr)
        return 3
    print(f"  parsed clip length: {rules.clip_min_seconds}-{rules.clip_max_seconds}s")
    print(f"  approved links found: {len(rules.approved_source_urls)}")
    print()
    print("provider works.")
    return 0


def cmd_campaign_rules(args: argparse.Namespace) -> int:
    """Show or correct a campaign's parsed rules by hand.

    The parser is a first draft (PRD section 5 step 1: "show it on the campaign
    page so Yaish can correct it"). Adding an approved source here is Yaish
    asserting that the campaign permits that footage, which is a decision only
    he can make -- the model is never allowed to widen what is approved.
    """
    from .core import llm
    from .ingest import briefs
    from .ingest.rules import BriefRules, channel_root, normalize_url

    _, conn = _conn()
    rules = briefs.load_rules(conn, args.campaign_id)
    if rules is None:
        print(f"campaign {args.campaign_id} has no parsed rules; run parse-brief first",
              file=sys.stderr)
        return 2

    data = rules.model_dump()
    changed: list[str] = []

    for url in args.add_source or []:
        existing = {normalize_url(u) for u in data["approved_source_urls"]}
        if normalize_url(url) in existing:
            print(f"already approved: {url}")
            continue
        data["approved_source_urls"].append(url)
        changed.append(f"approved footage += {url}")

    for url in args.remove_source or []:
        target = normalize_url(url)
        kept = [u for u in data["approved_source_urls"] if normalize_url(u) != target]
        if len(kept) == len(data["approved_source_urls"]):
            print(f"not in the approved list: {url}", file=sys.stderr)
        else:
            data["approved_source_urls"] = kept
            changed.append(f"approved footage -= {url}")

    for url in args.add_channel or []:
        if channel_root(url) in {channel_root(u) for u in data["approved_channels"]}:
            print(f"already approved: {url}")
            continue
        data["approved_channels"].append(channel_root(url))
        changed.append(f"approved channel += {channel_root(url)} (every VOD on it)")

    for url in args.remove_channel or []:
        kept = [u for u in data["approved_channels"] if channel_root(u) != channel_root(url)]
        if len(kept) == len(data["approved_channels"]):
            print(f"not an approved channel: {url}", file=sys.stderr)
        else:
            data["approved_channels"] = kept
            changed.append(f"approved channel -= {url}")

    for pair in args.set or []:
        if "=" not in pair:
            print(f"--set wants FIELD=VALUE, got {pair!r}", file=sys.stderr)
            return 2
        field, _, raw = pair.partition("=")
        field = field.strip()
        if field not in BriefRules.model_fields:
            print(f"no such field {field!r}. Fields: "
                  f"{', '.join(sorted(BriefRules.model_fields))}", file=sys.stderr)
            return 2
        try:                      # let JSON give us numbers, booleans, null and lists
            value = json.loads(raw)
        except json.JSONDecodeError:
            value = raw
        data[field] = value
        changed.append(f"{field} = {value!r}")

    if args.clear_uncertain and data["uncertain"]:
        changed.append(f"cleared {len(data['uncertain'])} uncertain note(s)")
        data["uncertain"] = []

    if changed:
        try:
            updated = BriefRules.model_validate(data)
        except Exception as exc:
            print("that would not be a valid rule set:", file=sys.stderr)
            print(str(exc), file=sys.stderr)
            return 2
        briefs.save_rules(conn, args.campaign_id, updated)
        rules = updated
        for line in changed:
            print(f"  {line}")
        print()

    row = conn.execute("SELECT name, status, joined_at FROM campaigns WHERE id=?",
                       (args.campaign_id,)).fetchone()
    print(f"campaign {args.campaign_id}: {row['name']}  [{row['status']}, "
          f"joined {row['joined_at'] or 'never'}]")
    print(json.dumps(rules.model_dump(), indent=1, ensure_ascii=False))
    if not rules.approved_source_urls:
        print()
        print("No approved footage links, so NOTHING can be ingested for this campaign.")
        print("If the brief names the footage in words only, add the specific videos")
        print("it permits yourself:")
        print(f"  python -m clipforge campaign-rules {args.campaign_id} "
              f"--add-source <url>")
    return 0


def cmd_source_release(args: argparse.Namespace) -> int:
    """Approve a held source so the pipeline carries on with it.

    PRD section 5: anything over MAX_SOURCE_HOURS is held "until Yaish approves
    it". Most of a Twitch streamer's VODs are 4-7 hours, so this is the normal
    path for campaign footage rather than an edge case.
    """
    from .ingest import sources

    _, conn = _conn()
    try:
        row = sources.release(conn, args.source_id)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    hours = (row["duration"] or 0) / 3600
    print(f"source {row['id']} released: {row['title'] or row['url'] or row['path']}")
    if row["duration"]:
        print(f"  {hours:.1f} h, length approved despite the "
              f"{get_settings().max_source_hours:g} h cap")
    job_id = queue.enqueue(conn, "ingest_source", ref_id=row["id"])
    print(f"  queued ingest_source job {job_id}; run the worker to continue")
    return 0


def cmd_mine(args: argparse.Namespace) -> int:
    """Queue Phase 2 for a source: transcript, summary, signals, moments."""
    _, conn = _conn()
    row = conn.execute("SELECT id, status, title FROM sources WHERE id=?",
                       (args.source_id,)).fetchone()
    if row is None:
        print(f"no source {args.source_id}", file=sys.stderr)
        return 2
    if row["status"] == "held":
        from .ingest import sources

        # A provider hold ("cannot summarise: ...") means ingest already passed
        # the hard limits, so resuming is just mining again. Any other hold is
        # a gate that only `source-release` may lift.
        full = conn.execute("SELECT held_reason, path FROM sources WHERE id=?",
                            (row["id"],)).fetchone()
        reason = full["held_reason"] or ""
        if reason.startswith("cannot ") and full["path"]:
            sources.mark(conn, row["id"], "downloaded", held_reason=None)
            print(f"source {row['id']} was held ({reason[:90]}); resuming")
            row = conn.execute("SELECT id, status, title FROM sources WHERE id=?",
                               (row["id"],)).fetchone()
    if row["status"] == "held":
        print(f"source {row['id']} is held; release it first:", file=sys.stderr)
        print(f"  python -m clipforge source-release {row['id']}", file=sys.stderr)
        return 2
    job_id = queue.enqueue(conn, "mine_source", ref_id=row["id"])
    print(f"queued mine_source job {job_id} for source {row['id']} "
          f"({row['title'] or 'untitled'})")
    return 0


def cmd_moments(args: argparse.Namespace) -> int:
    _, conn = _conn()
    where = "WHERE m.source_id=?" if args.source_id else ""
    params = (args.source_id,) if args.source_id else ()
    rows = conn.execute(
        'SELECT m.id, m.source_id, m.start, m."end", m.score, m.rubric_category, '
        "m.status, m.hook, m.title FROM moments m "
        + where + " ORDER BY m.score DESC, m.start", params
    ).fetchall()
    if not rows:
        print("no moments yet")
        return 0
    for r in rows:
        length = r["end"] - r["start"]
        print(f"  #{r['id']:<4} s{r['source_id']:<3} {r['score']:>5.1f} "
              f"{r['start']:>8.1f}s +{length:>5.1f}s {r['status']:<10} "
              f"{r['rubric_category']}")
        print(f"        hook:  {r['hook']}")
        print(f"        title: {r['title']}")
    return 0


def _requeue_clip(conn, moment_id: int) -> None:
    """Forget a clip that was held before it was made, so a retry starts clean."""
    conn.execute("DELETE FROM clips WHERE moment_id=? AND path IS NULL", (moment_id,))
    marker = Path(get_settings().data_dir) / "work" / f"clip-{moment_id}" / "blocked.txt"
    marker.unlink(missing_ok=True)


def cmd_rubric_build(args: argparse.Namespace) -> int:
    from .moments import rubric_build

    settings = get_settings()
    path = settings.rubrics_dir / f"{args.niche}.json"
    try:
        out = rubric_build.build(path, settings, write=not args.dry_run)
    except Exception as exc:
        print(f"rubric build failed: {exc}", file=sys.stderr)
        return 2
    for c in out["categories"]:
        ev = out["evidence"].get(c["id"])
        print(f"{c['weight']:.2f}  {c['id']:24s} " + (f"n={ev['n']} median views={ev['median_views']:,}" if ev else "(kept)"))
    print("dry run, nothing written" if args.dry_run else f"wrote {path} (version {out['version']})")
    return 0


def cmd_telegram_setup(_: argparse.Namespace) -> int:
    from .app import telegram as tg
    from .core.config import package_root

    settings = get_settings()
    try:
        me = tg.call(settings, "getMe")
        chat = settings.telegram_chat_id or tg.discover_chat(settings)
    except tg.TelegramError as exc:
        print(f"telegram: {exc}", file=sys.stderr)
        return 2
    print(f"bot: @{me.get('username')}")
    if not chat:
        print("no chat found yet: open the bot in Telegram, press Start, send it any message, "
              "then run this again", file=sys.stderr)
        return 2
    if not settings.telegram_chat_id:
        env = package_root() / ".env"
        text = env.read_text(encoding="utf-8") if env.is_file() else ""
        lines = [ln for ln in text.splitlines() if not ln.startswith("TELEGRAM_CHAT_ID=")]
        lines.append(f"TELEGRAM_CHAT_ID={chat}")
        env.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.environ["TELEGRAM_CHAT_ID"] = str(chat)
        get_settings.cache_clear()
        settings = get_settings()
    try:
        tg.send(settings, "ClipForge is connected. Send /ready to review clips, /status for the queue.")
    except tg.TelegramError as exc:
        print(f"telegram: {exc}", file=sys.stderr)
        return 2
    print("test message sent")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import threading
    import time

    import uvicorn

    from .app import telegram as tg
    from .app.web import create_app
    from .publish import flow
    from .stats import collect

    settings, _ = _conn()
    try:
        app = create_app(settings)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    def bot() -> None:
        log = logging.getLogger("clipforge.telegram")
        conn = db.open_db(settings.db_path)
        while True:
            try:
                flow.tick(conn, settings)
                tg.sweep(conn, settings)
                if collect.due(conn):
                    _, alerts = collect.collect(conn, settings)
                    for a in alerts:
                        tg.send(settings, a)
                tg.poll_once(conn, settings)
            except Exception as exc:                 # keep the bot alive; never log the token
                log.warning("telegram loop: %s", type(exc).__name__ if not isinstance(
                    exc, tg.TelegramError) else exc)
                time.sleep(30)

    if tg.configured(settings) and not args.no_bot:
        threading.Thread(target=bot, daemon=True, name="telegram").start()
        print("telegram bot running")
    else:
        print("telegram bot off (no token/chat id, or --no-bot)")
    print(f"dashboard: http://localhost:{settings.dashboard_port}   phone (same Wi-Fi): "
          f"{tg.lan_address(settings)}")
    uvicorn.run(app, host=settings.dashboard_host, port=settings.dashboard_port,
                log_level="warning")
    return 0


def cmd_youtube_auth(_: argparse.Namespace) -> int:
    from .core.config import package_root, set_env_value
    from .publish import youtube

    settings = get_settings()
    try:
        token = youtube.authorize(settings)
    except youtube.YouTubeError as exc:
        print(f"youtube-auth: {exc}", file=sys.stderr)
        return 2
    set_env_value(package_root() / ".env", "YOUTUBE_REFRESH_TOKEN", token)
    get_settings.cache_clear()
    print("authorised: refresh token saved to .env")
    return 0


def cmd_publish_now(args: argparse.Namespace) -> int:
    from .publish import flow

    _, conn = _conn()
    try:
        flow.enqueue_now(conn, args.clip)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"queued upload of clip {args.clip}; the worker will pick it up")
    return 0


def cmd_supervise(_: argparse.Namespace) -> int:
    from .app import telegram as tg
    from .core import supervisor as sv

    settings, _conn_unused = _conn()
    logs = settings.data_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    sv.trim_log(logs / "supervisor.log")
    handler = logging.FileHandler(logs / "supervisor.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.getLogger().addHandler(handler)
    lock = sv.acquire_lock()
    if lock is None:
        print("another supervisor is already running", file=sys.stderr)
        return 1

    def notify(text: str) -> None:
        if tg.configured(settings):
            tg.send(settings, text)

    sup = sv.Supervisor(settings, sv.default_children(settings), notify=notify,
                        heartbeat_age=lambda: heartbeat_age(settings))
    try:
        notify("ClipForge started (dashboard, bot and worker).")
    except Exception as exc:
        logging.getLogger("clipforge.supervisor").warning("start alert failed: %s", type(exc).__name__)
    sup.run()
    return 0


def cmd_install_service(_: argparse.Namespace) -> int:
    from .core import service

    ok, out = service.install(Path(__file__).resolve().parents[1])
    print(out or ("installed" if ok else "failed"))
    if ok:
        print("ClipForge now starts at logon. For a restart with nobody at the keyboard, "
              "turn on Windows auto-login for this account.")
    return 0 if ok else 2


def cmd_uninstall_service(_: argparse.Namespace) -> int:
    from .core import service

    ok, out = service.uninstall()
    print(out)
    return 0 if ok else 2


def cmd_service_status(_: argparse.Namespace) -> int:
    from .core import service

    print(service.status())
    return 0


def cmd_power_setup(_: argparse.Namespace) -> int:
    from .core import service

    bad = 0
    for cmd, code in service.power_setup():
        print(("ok    " if code == 0 else f"FAILED({code}) ") + " ".join(cmd))
        bad += code != 0
    return 2 if bad else 0


def cmd_stats_collect(_: argparse.Namespace) -> int:
    from .stats import collect

    settings, conn = _conn()
    try:
        n, alerts = collect.collect(conn, settings)
    except Exception as exc:
        print(f"stats: {exc}", file=sys.stderr)
        return 2
    print(f"{n} video(s) updated")
    for a in alerts:
        print("ALERT:", a)
    return 0


def cmd_clip(args: argparse.Namespace) -> int:
    """Queue Phase 3 for one moment: fetch, render, package."""
    _, conn = _conn()
    row = conn.execute("SELECT id, status FROM moments WHERE id=?", (args.moment_id,)).fetchone()
    if row is None:
        print(f"no moment {args.moment_id}", file=sys.stderr)
        return 2
    if args.redo:
        made = conn.execute("SELECT status FROM clips WHERE moment_id=? AND path IS NOT NULL",
                            (row["id"],)).fetchall()
        if any(m["status"] not in ("ready", "held") for m in made):
            print("that clip was approved or posted; refusing to remake it", file=sys.stderr)
            return 2
        conn.execute("DELETE FROM clips WHERE moment_id=?", (row["id"],))
        shutil.rmtree(Path(get_settings().data_dir) / "work" / f"clip-{row['id']}",
                      ignore_errors=True)
        conn.execute("UPDATE moments SET status='candidate' WHERE id=?", (row["id"],))
    _requeue_clip(conn, row["id"])
    job_id = queue.enqueue(conn, "make_clip", ref_id=row["id"])
    print(f"queued make_clip job {job_id} for moment {row['id']}; "
          f"run: python -m clipforge worker --drain")
    return 0


def cmd_clip_source(args: argparse.Namespace) -> int:
    """Queue the best N unclipped moments of a source."""
    _, conn = _conn()
    rows = conn.execute(
        "SELECT id FROM moments WHERE source_id=? AND status='candidate' "
        "ORDER BY score DESC LIMIT ?", (args.source_id, args.top)).fetchall()
    if not rows:
        print("no candidate moments for that source", file=sys.stderr)
        return 2
    for r in rows:
        _requeue_clip(conn, r["id"])
        queue.enqueue(conn, "make_clip", ref_id=r["id"])
    print(f"queued {len(rows)} make_clip job(s); run: python -m clipforge worker --drain")
    return 0


def cmd_clips(args: argparse.Namespace) -> int:
    """List finished clips and why any are held."""
    _, conn = _conn()
    rows = conn.execute(
        "SELECT c.id, c.moment_id, c.status, c.path, c.package_path, c.compliance_notes, "
        "m.title, m.score FROM clips c JOIN moments m ON m.id=c.moment_id ORDER BY c.id").fetchall()
    if not rows:
        print("no clips yet")
        return 0
    for r in rows:
        print(f"  clip {r['id']:<4} moment {r['moment_id']:<4} {r['status']:<9} "
              f"score {r['score']:>5.1f}  {r['title']}")
        if r["package_path"]:
            print(f"        {r['package_path']}")
        for line in (r["compliance_notes"] or "").splitlines():
            print(f"        {line}")
    return 0


def cmd_campaign_hold(args: argparse.Namespace) -> int:
    """Pause a campaign: autopilot stops sourcing and clipping it. --resume undoes it."""
    _, conn = _conn()
    row = conn.execute("SELECT name, status, joined_at FROM campaigns WHERE id=?",
                       (args.campaign_id,)).fetchone()
    if row is None:
        print(f"no campaign {args.campaign_id}", file=sys.stderr)
        return 2
    if args.resume:
        if not row["joined_at"]:
            print("never joined, nothing to resume", file=sys.stderr)
            return 2
        conn.execute("UPDATE campaigns SET status='active' WHERE id=?", (args.campaign_id,))
        print(f"{row['name']}: active again")
    else:
        conn.execute("UPDATE campaigns SET status='skipped' WHERE id=?", (args.campaign_id,))
        print(f"{row['name']}: on hold")
    return 0


def cmd_rubric_reweight(args: argparse.Namespace) -> int:
    """Re-weight a rubric from our own posted clips' views (needs settled posts)."""
    from .moments import reweight, rubric as rubric_mod

    settings = get_settings()
    _, conn = _conn()
    path = settings.rubrics_dir / f"{args.niche}.json"
    rub = rubric_mod.load(path)
    perf = reweight.own_views(conn)
    changes = reweight.propose(rub, perf)
    for c in changes:
        views = f"n={c.n} median={c.median_views:,}" if c.n else "no posts"
        mark = "" if c.new == c.old else "  <-"
        print(f"{c.old:.2f} -> {c.new:.2f}  {c.id:24s} {views}{mark}")
    if not any(c.new != c.old for c in changes):
        print("nothing to change: too few settled posts per moment type "
              f"(need {reweight.MIN_POSTS} in at least two types)")
        return 0
    if args.dry_run:
        print("dry run, nothing written")
        return 0
    out = reweight.apply(path, changes)
    print(f"wrote {path} (version {out['version']}), old file kept as {path.stem}.v{out['version'] - 1}.json")
    return 0


def main(argv: list[str] | None = None) -> int:
    # Model-written text carries characters the Windows console code page cannot
    # encode (a real reply contained U+2011). Printing one would otherwise raise
    # UnicodeEncodeError, and from Phase 7 this runs as a service where the code
    # page is whatever the service host gives it.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            pass
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # httpx logs every request URL at INFO, and a Telegram bot token is part of its URL.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    parser = argparse.ArgumentParser(prog="clipforge")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="create or migrate the database").set_defaults(fn=cmd_init_db)

    p = sub.add_parser("worker", help="run the job worker")
    p.add_argument("--drain", action="store_true", help="exit when the queue is empty")
    p.set_defaults(fn=cmd_worker)

    p = sub.add_parser("selftest", help="queue a dummy three-stage job")
    p.add_argument("--sleep", type=float, default=0.0, help="seconds each stage takes")
    p.add_argument("--crash-at", choices=["a", "b", "c"], help="hard-exit once in this stage")
    p.add_argument("--fail-at", choices=["a", "b", "c"], help="raise an error in this stage")
    p.set_defaults(fn=cmd_selftest)

    sub.add_parser("status", help="worker and queue status").set_defaults(fn=cmd_status)
    sub.add_parser("sysinfo", help="describe this machine").set_defaults(fn=cmd_sysinfo)

    p = sub.add_parser("benchmark", help="time encoding and transcription on this machine")
    p.add_argument("--media", help="a real video with speech (recommended)")
    p.add_argument("--minutes", type=float, default=10.0, help="minutes of audio to transcribe")
    p.add_argument("--start", type=float, default=0.0,
                   help="seek this many seconds into the media first; use it to skip a "
                        "silent intro, which would make the transcription timing meaningless")
    p.set_defaults(fn=cmd_benchmark)

    p = sub.add_parser("campaign-add", help="save a campaign Yaish found (does not join it)")
    p.add_argument("--platform", required=True, choices=["whop", "vyro", "other"])
    p.add_argument("--external-id", required=True, help="the campaign's id on the platform")
    p.add_argument("--name", required=True)
    p.add_argument("--creator")
    p.add_argument("--category")
    p.add_argument("--rate", type=float, help="USD per 1,000 views")
    p.add_argument("--clip-cap", type=float, help="USD cap per clip")
    p.add_argument("--min-views", type=int)
    p.add_argument("--brief-url")
    p.add_argument("--brief-file", help="a text file with the brief pasted into it")
    p.set_defaults(fn=cmd_campaign_add)

    p = sub.add_parser("campaign-joined",
                       help="record that Yaish joined this campaign on the platform")
    p.add_argument("campaign_id", type=int)
    p.set_defaults(fn=cmd_campaign_joined)

    p = sub.add_parser("parse-brief", help="queue parsing of a campaign's brief into rules")
    p.add_argument("campaign_id", type=int)
    p.set_defaults(fn=cmd_parse_brief)

    p = sub.add_parser("ingest-url", help="ingest one approved source video")
    p.add_argument("campaign_id", type=int)
    p.add_argument("url")
    p.add_argument("--audio-only", action="store_true",
                   help="fetch just the audio. A 5 h Twitch VOD is 18.6 GB as video and "
                        "519 MB as audio, and the transcript, summary, chapters and every "
                        "Phase 2 signal need only the audio. Phase 3 re-fetches the video "
                        "for the moments worth cutting")
    p.set_defaults(fn=cmd_ingest_url)

    p = sub.add_parser("scan-inbox", help="ingest new recordings from inbox/own/")
    p.add_argument("--repeat-minutes", type=float, default=0,
                   help="keep rescanning this often (PRD: every 30 minutes)")
    p.set_defaults(fn=cmd_scan_inbox)

    sub.add_parser("campaigns", help="list campaigns and their parsed rules").set_defaults(
        fn=cmd_campaigns)
    sub.add_parser("sources", help="list source footage and its status").set_defaults(
        fn=cmd_sources)

    p = sub.add_parser("llm-check",
                       help="check the LLM provider, key and models (sends one tiny request)")
    p.add_argument("--no-call", action="store_true",
                   help="only list models; do not send a request")
    p.set_defaults(fn=cmd_llm_check)

    p = sub.add_parser("campaign-rules",
                       help="show or correct a campaign's parsed brief rules")
    p.add_argument("campaign_id", type=int)
    p.add_argument("--add-source", action="append", metavar="URL",
                   help="approve one source video or folder (repeatable). Only for footage "
                        "the brief permits: this is your assertion, not the parser's")
    p.add_argument("--remove-source", action="append", metavar="URL",
                   help="withdraw approval for a source (repeatable)")
    p.add_argument("--add-channel", action="append", metavar="URL",
                   help="approve EVERY video on a channel, for briefs that approve a "
                        "creator's whole output. Your assertion, not the parser's")
    p.add_argument("--remove-channel", action="append", metavar="URL",
                   help="withdraw approval for a channel (repeatable)")
    p.add_argument("--set", action="append", metavar="FIELD=VALUE",
                   help='correct a field, e.g. --set min_views=2000 or '
                        '--set clip_max_seconds=null (repeatable)')
    p.add_argument("--clear-uncertain", action="store_true",
                   help="drop the parser's uncertain notes once you have reviewed them")
    p.set_defaults(fn=cmd_campaign_rules)

    p = sub.add_parser("source-release",
                       help="approve a held source (e.g. longer than MAX_SOURCE_HOURS)")
    p.add_argument("source_id", type=int)
    p.set_defaults(fn=cmd_source_release)

    p = sub.add_parser("mine", help="transcribe, summarise and find moments in a source")
    p.add_argument("source_id", type=int)
    p.set_defaults(fn=cmd_mine)

    p = sub.add_parser("moments", help="list candidate moments, best first")
    p.add_argument("source_id", type=int, nargs="?", help="only this source")
    p.set_defaults(fn=cmd_moments)

    p = sub.add_parser("clip", help="make a finished clip from one moment")
    p.add_argument("moment_id", type=int)
    p.add_argument("--redo", action="store_true",
                   help="throw away an unposted clip and make it again")
    p.set_defaults(fn=cmd_clip)

    p = sub.add_parser("clip-source", help="make clips from a source's best moments")
    p.add_argument("source_id", type=int)
    p.add_argument("--top", type=int, default=3)
    p.set_defaults(fn=cmd_clip_source)

    p = sub.add_parser("campaign-hold", help="pause a campaign (autopilot leaves it alone)")
    p.add_argument("campaign_id", type=int)
    p.add_argument("--resume", action="store_true")
    p.set_defaults(fn=cmd_campaign_hold)

    p = sub.add_parser("rubric-reweight", help="re-weight a rubric from our own clips' views")
    p.add_argument("niche", nargs="?", default="gta")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_rubric_reweight)

    p = sub.add_parser("rubric-build", help="re-weight a niche rubric from top YouTube Shorts")
    p.add_argument("niche", nargs="?", default="gta")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_rubric_build)

    sub.add_parser("telegram-setup", help="find your Telegram chat id and send a test message"
                   ).set_defaults(fn=cmd_telegram_setup)

    sub.add_parser("youtube-auth", help="one-time browser sign-in so ClipForge can upload"
                   ).set_defaults(fn=cmd_youtube_auth)
    p = sub.add_parser("publish-now", help="upload one approved clip now, ignoring the pacing slot")
    p.add_argument("clip", type=int)
    p.set_defaults(fn=cmd_publish_now)
    sub.add_parser("stats-collect", help="read views for every posted video now"
                   ).set_defaults(fn=cmd_stats_collect)

    sub.add_parser("supervise", help="run serve and worker, restart them, watch for stalls"
                   ).set_defaults(fn=cmd_supervise)
    sub.add_parser("install-service", help="start the supervisor at logon (Windows scheduled task)"
                   ).set_defaults(fn=cmd_install_service)
    sub.add_parser("uninstall-service", help="remove the logon task").set_defaults(
        fn=cmd_uninstall_service)
    sub.add_parser("service-status", help="show the logon task").set_defaults(fn=cmd_service_status)
    sub.add_parser("power-setup", help="never sleep or react to the lid on mains power"
                   ).set_defaults(fn=cmd_power_setup)

    p = sub.add_parser("serve", help="run the dashboard and the Telegram bot")
    p.add_argument("--no-bot", action="store_true")
    p.set_defaults(fn=cmd_serve)

    sub.add_parser("clips", help="list finished clips and any compliance holds").set_defaults(
        fn=cmd_clips)

    args = parser.parse_args(argv)
    return int(args.fn(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
