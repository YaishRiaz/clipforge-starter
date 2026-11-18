"""Job type registry.

Importing this module registers every job type with the worker. A job type is a
list of named stages; each stage must be idempotent, because a stage that was
interrupted runs again from the top after a restart.

`selftest` proves the queue, the stage bookkeeping and crash recovery without
any media.
"""
from __future__ import annotations

import logging
import os
import time

from .worker import Ctx, register

log = logging.getLogger("clipforge.handlers")


# --------------------------------------------------------------------------
# selftest (Phase 0)
# --------------------------------------------------------------------------

def _selftest_dir(ctx: Ctx):
    d = ctx.settings.data_dir / "selftest" / str(ctx.job_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _make_stage(name: str):
    def stage(ctx: Ctx) -> None:
        d = _selftest_dir(ctx)
        # One line per execution, so tests can count how often a stage ran.
        with open(d / f"{name}.txt", "a", encoding="utf-8") as fh:
            fh.write(f"{time.time()}\n")

        # Test hook: die hard, once, in the middle of the named stage.
        marker = d / "crashed"
        if ctx.payload.get("crash_at") == name and not marker.exists():
            marker.write_text("1", encoding="utf-8")
            os._exit(9)

        if ctx.payload.get("fail_at") == name:
            raise RuntimeError(f"selftest: forced failure in stage {name}")

        slept = 0.0
        total = float(ctx.payload.get("sleep", 0))
        while slept < total:
            time.sleep(min(0.2, total - slept))
            slept += 0.2
            ctx.beat()

    return stage


register("selftest", [(n, _make_stage(n)) for n in ("a", "b", "c")])


# --------------------------------------------------------------------------
# Phase 1: brief parsing and ingest
# --------------------------------------------------------------------------

def _source_row(ctx: Ctx):
    row = ctx.conn.execute("SELECT * FROM sources WHERE id=?", (ctx.ref_id,)).fetchone()
    if row is None:
        raise RuntimeError(f"no source {ctx.ref_id}")
    return row


def stage_parse_brief(ctx: Ctx) -> None:
    from ..ingest import briefs
    from ..ingest.rules import BriefRules
    from . import llm

    campaign_id = ctx.ref_id
    if campaign_id is None:
        raise RuntimeError("parse_brief needs a campaign id as ref_id")
    try:
        rules = briefs.parse_brief(ctx.conn, campaign_id, settings=ctx.settings)
    except llm.LLMUnavailable as exc:
        # Not the job's fault and retrying will not help: a human must act.
        log.error("campaign %s brief not parsed: %s", campaign_id, exc)
        raise
    if not rules.approved_source_urls and not rules.approved_source_notes:
        log.warning(
            "campaign %s: the brief named no approved footage. Nothing can be ingested "
            "for it until Yaish corrects the rules on the campaign page.", campaign_id,
        )
    if rules.uncertain:
        log.warning("campaign %s: parser was unsure about %d item(s): %s",
                    campaign_id, len(rules.uncertain), "; ".join(rules.uncertain[:5]))
    assert isinstance(rules, BriefRules)


register("parse_brief", [("parse", stage_parse_brief)])


def stage_download(ctx: Ctx) -> None:
    from pathlib import Path

    from ..ingest import download, sources

    row = _source_row(ctx)
    if row["status"] == "held":
        return
    if row["path"] and Path(row["path"]).is_file():
        return                      # own footage, or an earlier run of this stage
    if not row["url"]:
        sources.hold(ctx.conn, row["id"], "no url and no file on disk")
        return

    sources.check_disk(ctx.settings)
    dest = ctx.settings.sources_dir / str(row["campaign_id"])
    try:
        got = download.fetch(row["url"], dest, ctx.settings,
                             allow_long=bool(row["length_approved"]),
                             audio_only=row["media_kind"] == "audio", beat=ctx.beat)
    except download.TooLong as exc:
        sources.hold(ctx.conn, row["id"], str(exc))
        return
    sources.mark(
        ctx.conn, row["id"], "downloaded",
        path=str(got.path), duration=got.duration, published_at=got.published_at,
        title=row["title"] or got.title, creator=row["creator"] or got.creator,
        language=got.language,
    )


def stage_probe(ctx: Ctx) -> None:
    from pathlib import Path

    from ..ingest import sources, tagger

    row = _source_row(ctx)
    if row["status"] == "held":
        return
    path = Path(row["path"]) if row["path"] else None
    if path is None or not path.is_file():
        sources.hold(ctx.conn, row["id"], "the source file is not on disk")
        return
    info = tagger.probe_into(ctx.conn, row["id"], path)
    cap = ctx.settings.max_source_hours * 3600
    if info.duration > cap and not row["length_approved"]:
        sources.hold(
            ctx.conn, row["id"],
            f"{info.duration / 3600:.1f} h is over the {ctx.settings.max_source_hours:g} h cap "
            f"(MAX_SOURCE_HOURS); approve it with: "
            f"python -m clipforge source-release {row['id']}",
        )
        return
    if not info.has_audio:
        log.warning("source %s has no audio track; there will be no transcript", row["id"])
    if row["media_kind"] == "audio" and info.has_video:
        log.info("source %s was fetched as audio but carries video too", row["id"])
    elif row["media_kind"] == "audio":
        log.info("source %s is audio only; Phase 3 must fetch the video before cutting",
                 row["id"])


def stage_captions(ctx: Ctx) -> None:
    from pathlib import Path

    from ..ingest import captions as caps
    from ..ingest import tagger

    row = _source_row(ctx)
    if row["status"] == "held" or not row["path"]:
        return
    path = Path(row["path"])
    if not path.is_file():
        return
    dest = tagger.transcript_path(ctx.settings, row["id"])
    if dest.is_file():
        return                      # an earlier run of this stage already wrote it
    ctx.beat()
    segments = caps.from_media(path, ctx.settings.data_dir / "work")
    if not segments:
        log.info("source %s came with no captions; Phase 2 will transcribe it", row["id"])
        return
    caps.save(segments, dest, source="supplied captions")
    ctx.conn.execute("UPDATE sources SET transcript_path=? WHERE id=?", (str(dest), row["id"]))


def stage_tag(ctx: Ctx) -> None:
    from ..core import llm
    from ..ingest import captions as caps
    from ..ingest import sources, tagger

    row = _source_row(ctx)
    if row["status"] == "held":
        return
    if row["summary"]:
        return                      # already tagged
    if not row["transcript_path"]:
        log.info(
            "source %s has no transcript yet, so it has no summary; it stays 'downloaded' "
            "until Phase 2 transcribes it", row["id"],
        )
        return
    ctx.beat()
    segments = caps.load(row["transcript_path"])
    try:
        tags = tagger.describe(
            segments, title=row["title"], duration=row["duration"], settings=ctx.settings
        )
    except llm.LLMUnavailable as exc:
        sources.hold(ctx.conn, row["id"], f"cannot summarise: {exc}")
        return
    except llm.LLMRequestTooLarge as exc:
        sources.hold(ctx.conn, row["id"],
                     f"cannot summarise: {exc}. Lower MAX_TRANSCRIPT_CHARS in .env.")
        return
    tagger.save_tags(ctx.conn, row["id"], tags)
    sources.mark(ctx.conn, row["id"], "tagged")
    log.info("source %s tagged: %d chapters", row["id"], len(tags.chapters))


register("ingest_source", [
    ("download", stage_download),
    ("probe", stage_probe),
    ("captions", stage_captions),
    ("tag", stage_tag),
])


def stage_scan_inbox(ctx: Ctx) -> None:
    from . import queue
    from ..ingest import watcher

    new = watcher.scan(ctx.conn, ctx.settings.inbox_own)
    for source_id in new:
        queue.enqueue(ctx.conn, "ingest_source", ref_id=source_id)
    if new:
        log.info("queued %d own recording(s) for ingest", len(new))

    # The queue doubles as the scheduler until Phase 4 brings a real one.
    repeat = float(ctx.payload.get("repeat_seconds") or 0)
    if repeat > 0:
        queue.enqueue(
            ctx.conn, "scan_inbox", payload=dict(ctx.payload),
            delay_seconds=repeat, dedupe=False, priority=-10,
        )


register("scan_inbox", [("scan", stage_scan_inbox)])


def stage_autopilot(ctx: Ctx) -> None:
    from . import autopilot, queue

    # Schedule the next pass first so one bad pass cannot end the loop.
    repeat = float(ctx.payload.get("repeat_seconds") or 0)
    if repeat > 0:
        queue.enqueue(ctx.conn, "autopilot", payload=dict(ctx.payload),
                      delay_seconds=repeat, dedupe=False, priority=-10)
    autopilot.run(ctx.conn, ctx.settings)


register("autopilot", [("run", stage_autopilot)])


# --------------------------------------------------------------------------
# Phase 2: transcript, signals, moments
# --------------------------------------------------------------------------

def _require_media(ctx: Ctx):
    """The source row and its file, or None when the job cannot proceed."""
    from pathlib import Path

    from ..ingest import sources

    row = _source_row(ctx)
    if row["status"] == "held":
        return None, None
    path = Path(row["path"]) if row["path"] else None
    if path is None or not path.is_file():
        sources.hold(ctx.conn, row["id"], "the source file is not on disk")
        return None, None
    return row, path


def stage_transcribe(ctx: Ctx) -> None:
    """Transcribe, unless captions already came with the footage."""
    from ..ingest import captions as caps
    from ..ingest import sources, tagger
    from ..moments import transcribe as tr

    row, path = _require_media(ctx)
    if row is None:
        return
    dest = tagger.transcript_path(ctx.settings, row["id"])
    if row["transcript_path"] and dest.is_file():
        return                      # supplied captions, or an earlier run
    ctx.beat()
    # A persistent work dir: chunk checkpoints have to outlive a crash or a
    # rate limit, or a 35-request transcription restarts from zero.
    work = ctx.settings.data_dir / "work" / f"transcribe-{row['id']}"
    heard, shift = path, 0.0
    from ..ingest import briefs as briefs_mod

    rules = briefs_mod.load_rules(ctx.conn, row["campaign_id"])
    win = rules.window_for(row["url"]) if rules else None
    if win:
        # The brief approves only part of this video: listen to that part alone, which also
        # saves most of the transcription quota on a long stream. Times are shifted back after.
        from ..core import media

        shift = win[0]
        heard = work / "window.m4a"
        work.mkdir(parents=True, exist_ok=True)
        if not heard.is_file():
            end = min(win[1], row["duration"] or win[1]) if win[1] else (row["duration"] or None)
            cut = ["-ss", f"{shift:.2f}"] + (["-t", f"{end - shift:.2f}"] if end else [])
            media.run([media._tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y", *cut,
                       "-i", str(path), "-vn", "-c:a", "aac", "-b:a", "64k", "-ac", "1", str(heard)])
    try:
        segments, backend = tr.transcribe(heard, ctx.settings, work_dir=work,
                                          beat=ctx.beat)
    except tr.TranscriptionUnavailable as exc:
        # A missing key or backend needs a human, not three more attempts.
        sources.hold(ctx.conn, row["id"], f"cannot transcribe: {exc}")
        return
    if not segments:
        sources.hold(ctx.conn, row["id"],
                     "transcription produced nothing; is there speech in this audio?")
        return
    if shift:
        segments = [caps.Segment(s.start + shift, s.end + shift, s.text) for s in segments]
    caps.save(segments, dest, source=backend)
    ctx.conn.execute("UPDATE sources SET transcript_path=? WHERE id=?",
                     (str(dest), row["id"]))
    log.info("source %s transcribed by %s: %d segment(s), %d words",
             row["id"], backend, len(segments), caps.word_count(segments))


def stage_summarise(ctx: Ctx) -> None:
    """Summary and chapters, now that a transcript exists (PRD section 5)."""
    from ..core import llm
    from ..ingest import captions as caps
    from ..ingest import sources, tagger

    row = _source_row(ctx)
    if row["status"] == "held" or row["summary"] or not row["transcript_path"]:
        return
    ctx.beat()
    segments = caps.load(row["transcript_path"])
    try:
        tags = tagger.describe(segments, title=row["title"], duration=row["duration"],
                               settings=ctx.settings, beat=ctx.beat)
    except llm.LLMUnavailable as exc:
        sources.hold(ctx.conn, row["id"], f"cannot summarise: {exc}")
        return
    except llm.LLMQuotaExhausted as exc:
        # Not the source's fault and not worth retrying today.
        sources.hold(ctx.conn, row["id"], f"cannot summarise: {exc}")
        return
    except (llm.LLMRequestTooLarge, llm.LLMBadAnswer) as exc:
        # The summary is the human-readable record (PRD section 5). The moments
        # are what the pipeline exists for, and they come from signal peaks over
        # the whole transcript rather than from this. So a record that will not
        # come out must not stop the scoring that follows it: holding here would
        # throw away the transcription and signals work already paid for.
        log.warning("source %s has no summary: %s. Carrying on to the moments; "
                    "run `mine` again later to retry the record.", row["id"], exc)
        return
    tagger.save_tags(ctx.conn, row["id"], tags)
    sources.mark(ctx.conn, row["id"], "tagged")
    log.info("source %s summarised: %d chapter(s)", row["id"], len(tags.chapters))


def _peaks_path(ctx: Ctx, source_id: int):
    return ctx.settings.data_dir / "signals" / f"{source_id}.json"


def _chat_peaks(ctx: Ctx, row) -> list:
    """Where the stream's chat reacted. Only Twitch VODs have chat to read; any failure is no signal."""
    from ..moments import chat

    vod = chat.vod_id(row["url"])
    if vod is None or not row["duration"]:
        return []
    try:
        msgs = chat.fetch(vod, ctx.settings.data_dir / "chat" / f"{row['id']}.json",
                         duration=row["duration"], beat=ctx.beat)
        found = chat.find_peaks(msgs, row["duration"])
    except Exception as exc:
        from ..ingest import live

        kept = live.chat_log(ctx.settings, row["url"])
        if not kept.is_file():
            log.warning("source %s: no chat signal (%s)", row["id"], exc)
            return []
        msgs = chat.load(kept)                 # read while the stream was live
        found = chat.find_peaks(msgs, row["duration"])
    log.info("source %s: %d chat message(s), %d reaction burst(s)", row["id"], len(msgs), len(found))
    return found


MAX_SCENE_SCAN_PIXELS = 3_800_000          # about 1440p; a 4K file decodes slower than real time here


def _cheap_to_scan(path) -> bool:
    """Scene changes need every frame decoded. Past 1440p this laptop cannot keep up, and the
    loudness, speech and chat signals carry a talking video well enough without them."""
    from ..core import media

    try:
        p = media.probe(path)
    except Exception:
        return True
    if (p.width or 0) * (p.height or 0) > MAX_SCENE_SCAN_PIXELS:
        log.info("%s is %dx%d: skipping the scene-change scan", path.name, p.width, p.height)
        return False
    return True


def stage_signals(ctx: Ctx) -> None:
    """Loudness, speech rate and scene changes, then the peaks worth reading."""
    import json

    from ..ingest import captions as caps
    from ..moments import signals as sig

    row, path = _require_media(ctx)
    if row is None:
        return
    dest = _peaks_path(ctx, row["id"])
    if dest.is_file():
        return                      # an earlier run of this stage
    ctx.beat()

    loudness = sig.loudness_series(path, beat=ctx.beat)
    found = list(sig.peaks(loudness, z=1.8, min_gap=20.0, top_n=40))

    if row["transcript_path"]:
        segments = caps.load(row["transcript_path"])
        rate = sig.speech_rate_series(segments, duration=row["duration"])
        found += sig.peaks(rate, z=1.8, min_gap=20.0, top_n=20)

    if row["media_kind"] == "video" and _cheap_to_scan(path):
        ctx.beat()
        changes = sig.scene_change_times(path, beat=ctx.beat)
        if changes and row["duration"]:
            density = sig.density(changes, window=10.0, duration=row["duration"])
            found += sig.peaks(density, z=2.0, min_gap=20.0, top_n=20)

    found += _chat_peaks(ctx, row)

    found.sort(key=lambda p: p.start)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(
        [{"start": p.start, "end": p.end, "value": p.value, "z": p.z, "signal": p.signal,
          "note": p.note} for p in found], indent=1), encoding="utf-8")
    log.info("source %s: %d peak(s) across the signals", row["id"], len(found))


SHORT_SOURCE_SECONDS = 180.0


def _short_official_cuts(row, rules, score_mod) -> list:
    """Cut an official trailer by its own chapters when no signal peaks exist.

    A trailer has no crowd or chat to spike. Its chapters (each a distinct piece of
    the trailer) and the whole trailer are the only honest cuts, so those are the
    candidates, each kept to the brief's length rules. Hooks stay empty: a trailer
    brief bans exaggerated hooks.
    """
    import json

    duration = row["duration"]
    if not duration or duration > SHORT_SOURCE_SECONDS:
        return []
    low, high = score_mod._length_rule(rules)
    try:
        chapters = json.loads(row["chapters_json"] or "[]")
    except ValueError:
        chapters = []
    bounds = [float(c["start"]) for c in chapters] + [float(duration)]
    spans = [(bounds[i], bounds[i + 1], chapters[i].get("title") or "")
             for i in range(len(chapters))]
    spans.append((0.0, float(duration), row["title"] or ""))
    out = []
    for start, end, title in spans:
        if end - start < low:
            continue
        end = min(end, start + high)
        cand = score_mod.Candidate(
            start=start, end=end, description=title or "Official trailer", hook="",
            title=title or (row["title"] or "Official trailer"),
        )
        out.append(score_mod.Scored(candidate=cand, score=30.0 if end - start < duration - 1 else 25.0,
                                    rubric_category="official_trailer"))
    return out


def stage_score(ctx: Ctx) -> None:
    """Ask for candidate moments around each peak and record them."""
    import json

    from ..core import llm
    from ..ingest import briefs
    from ..ingest import captions as caps
    from ..ingest import sources
    from ..moments import rubric as rubric_mod
    from ..moments import score as score_mod
    from ..moments.signals import Peak

    row = _source_row(ctx)
    if row["status"] == "held" or not row["transcript_path"]:
        return
    existing = ctx.conn.execute(
        "SELECT COUNT(*) AS n FROM moments WHERE source_id=? AND live=0", (row["id"],)
    ).fetchone()["n"]
    if existing:
        return                      # an earlier run of this stage

    peaks_file = _peaks_path(ctx, row["id"])
    if not peaks_file.is_file():
        log.warning("source %s has no signals file; run the signals stage", row["id"])
        return
    peaks = [Peak(**p) for p in json.loads(peaks_file.read_text(encoding="utf-8"))]
    segments = caps.load(row["transcript_path"])
    campaign = sources.campaign_of(ctx.conn, row["id"])
    rules = briefs.load_rules(ctx.conn, row["campaign_id"])
    if rules is not None and rules.window_for(row["url"]):
        peaks = [p for p in peaks if rules.inside_window(row["url"], p.start, p.end)]
    from ..taste import subjects

    info = subjects.of_source(ctx.conn, row)
    lo, hi = 0.0, float(row["duration"] or 0)
    win = rules.window_for(row["url"]) if rules else None
    if win:
        lo, hi = win[0], win[1] or hi
    main_game = subjects.game_at(info["games"], lo, hi or 1e9)
    rubric = rubric_mod.for_subjects(
        ctx.settings.rubrics_dir, campaign["category"] if campaign else None,
        subjects.streamer_key(info.get("streamer")), subjects.game_key(main_game))

    ctx.beat()
    try:
        chosen = score_mod.propose(
            segments, peaks, rubric, rules=rules, duration=row["duration"],
            settings=ctx.settings, beat=ctx.beat,
        )
    except (llm.LLMUnavailable, llm.LLMQuotaExhausted) as exc:
        sources.hold(ctx.conn, row["id"], f"cannot score moments: {exc}")
        return

    if not chosen:
        chosen = _short_official_cuts(row, rules, score_mod)
    if rules is not None and rules.source_windows:
        inside = [c for c in chosen if rules.inside_window(row["url"], c.start, c.end)]
        if len(inside) != len(chosen):
            log.info("source %s: %d moment(s) outside the brief's allowed stretch were dropped",
                     row["id"], len(chosen) - len(inside))
        chosen = inside

    from ..ingest import live as live_mod
    from ..moments import chat as chat_mod

    # Moments already clipped while the stream was live are not offered twice.
    early = ctx.conn.execute('SELECT start, "end" FROM moments WHERE source_id=? AND live=1',
                             (row["id"],)).fetchall()
    chosen = [c for c in chosen
              if not any(live_mod._overlaps(c.start, c.end, e["start"], e["end"]) for e in early)]

    chat_peaks = [p for p in peaks if p.signal == "chat"]
    if chat_peaks and chat_mod.boost(chosen, chat_peaks):
        log.info("source %s: chat reacted to some of the moments; their scores were raised", row["id"])

    # PRD section 6 step 5: keep the top N. A five-hour stream can yield dozens
    # of passable moments, and posting is capped at 3-8 a day, so the rest are
    # recorded as 'dropped' rather than deleted: the Learn page (PRD section 10)
    # needs to know what was passed over, not just what was chosen.
    keep = ctx.settings.moments_per_source
    ranked = sorted(chosen, key=lambda s: s.score, reverse=True)
    for rank, item in enumerate(ranked):
        status = "candidate" if rank < keep else "dropped"
        ctx.conn.execute(
            'INSERT INTO moments (source_id, start, "end", description, hook, title, '
            "score, rubric_category, status, game) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (row["id"], item.start, item.end, item.candidate.description,
             item.candidate.hook, item.candidate.title, item.score,
             item.rubric_category, status, subjects.game_at(info["games"], item.start, item.end)),
        )
    sources.mark(ctx.conn, row["id"], "mined")
    log.info("source %s: %d moment(s) found, best %d kept, top score %.1f",
             row["id"], len(ranked), min(keep, len(ranked)),
             max((c.score for c in ranked), default=0.0))


def stage_live_moment(ctx: Ctx) -> None:
    """A burst of live chat: listen to that stretch of the stream and clip what is there."""
    from ..ingest import live

    live.make_moment(ctx.conn, ctx.settings, ctx.payload, beat=ctx.beat)


register("live_moment", [("find", stage_live_moment)])


register("mine_source", [
    ("transcribe", stage_transcribe),
    ("summarise", stage_summarise),
    ("signals", stage_signals),
    ("score", stage_score),
])


# --------------------------------------------------------------------------
# Phase 3: one moment becomes one finished clip
# --------------------------------------------------------------------------

def _clip_work(ctx: Ctx):
    d = ctx.settings.data_dir / "work" / f"clip-{ctx.ref_id}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _moment_ctx(ctx: Ctx):
    """The moment, its source and campaign rows."""
    moment = ctx.conn.execute("SELECT * FROM moments WHERE id=?", (ctx.ref_id,)).fetchone()
    if moment is None:
        raise RuntimeError(f"no moment {ctx.ref_id}")
    source = ctx.conn.execute("SELECT * FROM sources WHERE id=?",
                              (moment["source_id"],)).fetchone()
    campaign = ctx.conn.execute("SELECT * FROM campaigns WHERE id=?",
                                (source["campaign_id"],)).fetchone()
    return moment, source, campaign


def _block_clip(ctx: Ctx, work, reason: str) -> None:
    """Record a clip that cannot be made, so it shows up held instead of vanishing."""
    (work / "blocked.txt").write_text(reason, encoding="utf-8")
    ctx.conn.execute("DELETE FROM clips WHERE moment_id=? AND path IS NULL", (ctx.ref_id,))
    ctx.conn.execute(
        "INSERT INTO clips (moment_id, status, compliance_ok, compliance_notes) "
        "VALUES (?, 'held', 0, ?)", (ctx.ref_id, f"FAIL source: {reason}"))
    log.warning("moment %s cannot be clipped: %s", ctx.ref_id, reason)


def stage_clip_fetch(ctx: Ctx) -> None:
    """Decide the cut, and get the footage for just that stretch."""
    import json
    from pathlib import Path

    from ..core import media
    from ..edit import cut, hls
    from ..ingest import briefs
    from ..ingest import captions as caps
    from ..ingest import sources

    work = _clip_work(ctx)
    if (work / "blocked.txt").is_file() or (work / "plan.json").is_file():
        return
    moment, source, campaign = _moment_ctx(ctx)
    rules = briefs.load_rules(ctx.conn, source["campaign_id"])

    ok, why = sources.campaign_may_ingest(ctx.conn, source["campaign_id"])
    if not ok:
        return _block_clip(ctx, work, why)

    segments = caps.load(source["transcript_path"]) if source["transcript_path"] else []
    min_s = rules.clip_min_seconds if rules else None
    override = work / "override.json"
    if override.is_file():                      # Yaish's own trim: use it as given
        fixed = json.loads(override.read_text(encoding="utf-8"))
        start, end = float(fixed["start"]), float(fixed["end"])
    else:
        start, end = cut.snap(segments, moment["start"], moment["end"], min_seconds=min_s)

    if rules is not None and not rules.inside_window(source["url"], start, end):
        return _block_clip(ctx, work, "the brief only approves part of this video, and this moment is outside it")

    local = Path(source["path"]) if source["path"] else None
    if local is not None and local.is_file() and media.probe(local).has_video:
        plan = {"start": start, "end": end, "offset": 0.0, "footage": str(local), "keep": True}
    else:
        url = source["url"]
        if not url:
            return _block_clip(ctx, work, "the source has no picture and no URL to fetch it from")
        approved = bool(rules) and (rules.approves_url(url) or (
            bool(source["via_channel"]) and bool(rules.approved_channels)))
        if campaign["platform"] != "own" and not approved:
            return _block_clip(ctx, work, f"the brief does not approve {url}")
        sources.check_disk(ctx.settings)
        try:
            got = hls.fetch_section(url, max(0.0, start - 1.0), end + 1.0, work / "section.mp4",
                                    max_height=ctx.settings.max_height, beat=ctx.beat)
        except hls.SectionError as exc:
            return _block_clip(ctx, work, f"cannot fetch the footage: {exc}")
        plan = {"start": start, "end": end, "offset": got.offset,
                "footage": str(got.path), "keep": False}
    (work / "plan.json").write_text(json.dumps(plan), encoding="utf-8")


def _clip_style(ctx: Ctx, work, campaign, moment, rules, lay):
    """This clip's edit style: the niche's taste book, then the brief takes things away.

    A taste-book failure must never stop a clip, so any problem falls back to the plain default.
    """
    import json

    from ..taste import book as book_mod
    from ..taste import style as style_mod

    try:
        niche_book = book_mod.for_clip(ctx.conn, campaign, moment)
        resolved = style_mod.resolve(niche_book, rules, fit=lay.fit or lay.game_full)
    except Exception as exc:
        log.warning("taste book unusable for moment %s, using the plain style: %s", ctx.ref_id, exc)
        resolved = style_mod.resolve(None, rules, fit=lay.fit or lay.game_full)
    (work / "style.json").write_text(resolved.model_dump_json(indent=2), encoding="utf-8")
    return resolved


def _clip_zooms(ctx: Ctx, work, plan, duration, st) -> list:
    """Punch-in moments from the clip's own audio; empty when zoom is off or cannot be measured."""
    from pathlib import Path

    from ..core import media
    from ..edit import words as words_mod
    from ..taste import zoom as zoom_mod

    if not st.zoom.enabled:
        return []
    try:
        audio = work / "clip-audio.flac"
        if not audio.is_file():
            ss = max(0.0, plan["start"] - plan["offset"])
            words_mod.extract_audio(Path(plan["footage"]), ss=ss, duration=duration, dest=audio)
        return zoom_mod.plan(audio, duration, st.zoom)
    except (media.MediaError, OSError) as exc:
        log.warning("zoom skipped for moment %s: %s", ctx.ref_id, exc)
        return []


def _clip_layout(ctx: Ctx, work, moment, source, campaign, plan, *, search: bool):
    """The creator's layout, corrected for where the camera is in THIS stretch of footage.

    A streamer moves the webcam between streams and scenes, so the layout file's box is only a
    first guess. The search result is kept in the work dir; `search=False` only reads it.
    """
    import json
    from dataclasses import replace
    from pathlib import Path

    from ..edit import camfind
    from ..edit import layout as layout_mod
    from ..edit.layout import Box

    produced = campaign["platform"] != "own" and not layout_mod.is_stream(source["url"])
    from . import db as db_mod

    game_full = db_mod.kv_get(ctx.conn, f"layout_full_{campaign['id']}") == "1"
    lay = layout_mod.for_moment(source["creator"] or campaign["creator"], moment, campaign["id"],
                                produced=produced, game_full=game_full)
    # A stream nearly always has a facecam somewhere, so it is looked for even when the
    # streamer has no saved layout yet; other footage is searched only when its layout says so.
    if lay.fit or (lay.facecam is None and not lay.facecam_required
                   and not layout_mod.is_stream(source["url"])):
        return lay
    cache = work / "camfind.json"
    got = None
    if cache.is_file():
        got = json.loads(cache.read_text(encoding="utf-8"))
    elif search:
        override = work / "override.json"
        wide = override.is_file() and bool(json.loads(override.read_text(encoding="utf-8")).get("recam"))
        d = camfind.locate(Path(plan["footage"]), ss=max(0.0, plan["start"] - plan["offset"]),
                           duration=plan["end"] - plan["start"], settings=ctx.settings, work=work,
                           known=lay.facecam, points=camfind.WIDE_POINTS if wide else camfind.POINTS)
        got = {"kind": d.kind, "box": list(d.box.__dict__.values()) if d.box else None, "note": d.note}
        if d.kind != "unknown":
            cache.write_text(json.dumps(got), encoding="utf-8")
    if not got or got["kind"] == "unknown":
        return lay
    if got["kind"] == "overlay" and got["box"]:
        return replace(lay, facecam=Box(*got["box"]), cam_note=got["note"])
    if got["kind"] == "full_screen":
        return replace(lay, facecam=None, cam_full=True, cam_note=got["note"])
    return replace(lay, facecam=None, cam_note="no facecam is visible in this part of the stream")


def _clip_tighten(ctx: Ctx, work, plan, words, duration, ss, rules):
    """Jump cuts when the style asks for them: (words, duration, footage, ss, plan) on the new clock.

    Filler lines and long silences are cut out of the footage into work/tight.mkv; the choice is
    kept in work/cuts.json so a rerun cuts the same way. Any failure leaves the clip uncut.
    """
    import json
    from pathlib import Path

    from ..core import media
    from ..taste import lines as lines_mod

    same = (words, duration, Path(plan["footage"]), ss, plan)
    try:
        f = work / "cuts.json"
        if f.is_file():
            keep = json.loads(f.read_text(encoding="utf-8")).get("keep")
        else:
            lo = rules.clip_min_seconds if rules and rules.clip_min_seconds else 5.0
            keep = lines_mod.plan(words, duration, ctx.settings, min_seconds=lo)
            kept = sum(e - s for s, e in keep) if keep else duration
            f.write_text(json.dumps({"keep": keep, "removed_seconds": round(duration - kept, 2)}), encoding="utf-8")
        if not keep:
            return same
        tight = work / "tight.mkv"
        if not tight.is_file():
            ctx.beat()
            part = work / "tight.partial.mkv"
            lines_mod.render_tight(Path(plan["footage"]), ss=ss, keep=keep, dest=part)
            part.replace(tight)
        (work / "clip-audio.flac").unlink(missing_ok=True)     # zoom peaks must be measured on the cut audio
        return (lines_mod.remap(words, keep), sum(e - s for s, e in keep), tight, 0.0,
                {**plan, "footage": str(tight), "start": 0.0, "offset": 0.0})
    except (media.MediaError, OSError, ValueError) as exc:
        log.warning("jump cuts skipped for moment %s: %s", ctx.ref_id, exc)
        return same


def stage_clip_render(ctx: Ctx) -> None:
    """Burn the hook, captions and handle into the vertical frame."""
    import json
    from pathlib import Path

    from ..edit import meta as meta_mod
    from ..edit import render, subs
    from ..edit import words as words_mod
    from ..ingest import briefs
    from ..ingest import captions as caps

    work = _clip_work(ctx)
    if (work / "blocked.txt").is_file() or (work / "clip.mp4").is_file():
        return
    moment, source, campaign = _moment_ctx(ctx)
    plan = json.loads((work / "plan.json").read_text(encoding="utf-8"))
    rules = briefs.load_rules(ctx.conn, source["campaign_id"])
    lay = _clip_layout(ctx, work, moment, source, campaign, plan, search=True)
    start, end = plan["start"], plan["end"]
    duration = end - start

    segments = caps.load(source["transcript_path"]) if source["transcript_path"] else []
    ss = max(0.0, start - plan["offset"])
    words = words_mod.for_clip(Path(plan["footage"]), ss=ss, duration=duration,
                               settings=ctx.settings, work=work)
    if words is None:
        words = subs.words_in_window(segments, start, end)
    handle = meta_mod.handle_from(rules, lay.handle)
    resolved = _clip_style(ctx, work, campaign, moment, rules, lay)
    st = resolved.style
    if lay.game_bottom:
        st.captions.margin_v = max(60, min(st.captions.margin_v, subs.OUT_H - lay.game_bottom - 420))
    burned = subs.has_burned_subs(plan["footage"])
    footage = Path(plan["footage"])
    if st.cuts.trim_lines and words and not burned:
        words, duration, footage, ss, plan = _clip_tighten(ctx, work, plan, words, duration, ss, rules)
    ass = subs.build_ass(hook=moment["hook"] or "", handle=handle,
                         events=[] if burned else subs.caption_events(words, st.captions.words_per_event),
                         duration=duration,
                         cam_height=lay.cam_height, style=st, words=None if burned else words,
                         fit=lay.fit, game_bottom=lay.game_bottom)
    ass_path = work / "subs.ass"
    ass_path.write_text(ass, encoding="utf-8")
    (work / "words.txt").write_text(" ".join(w[2] for w in words), encoding="utf-8")

    ctx.beat()
    zooms = _clip_zooms(ctx, work, plan, duration, st)
    tmp = work / "clip.partial.mp4"
    pops = []
    if st.emoji.enabled and words and not burned:
        from ..edit import emoji as emoji_mod

        # Beside the caption line (margin_v up from the bottom), at the right edge.
        pops = emoji_mod.pick(words, max_events=st.emoji.max_events, duration=duration)
        (work / "emoji.json").write_text(json.dumps([[p.name, p.start] for p in pops]), encoding="utf-8")
    render.render(footage, ss=ss,
                  duration=duration, layout=lay, ass_path=ass_path, dest=tmp,
                  zooms=zooms, zoom_scale=st.zoom.scale,
                  pops=pops, pop_y=max(80, subs.OUT_H - st.captions.margin_v - 160))
    tmp.replace(work / "clip.mp4")


def _streamer_of(ctx: Ctx, source) -> str | None:
    try:
        from ..taste import subjects

        return subjects.of_source(ctx.conn, source).get("streamer") or source["creator"]
    except Exception:  # noqa: BLE001 - a credit line must never stop a clip
        return source["creator"]


def stage_clip_finish(ctx: Ctx) -> None:
    """Title and tags, the brief check, then the package."""
    import json
    import shutil
    from pathlib import Path

    from ..core import media
    from ..edit import compliance
    from ..edit import meta as meta_mod
    from ..edit import package, render
    from ..ingest import briefs

    work = _clip_work(ctx)
    if (work / "blocked.txt").is_file() or not (work / "clip.mp4").is_file():
        return
    moment, source, campaign = _moment_ctx(ctx)
    done = ctx.conn.execute(
        "SELECT id FROM clips WHERE moment_id=? AND package_path IS NOT NULL", (ctx.ref_id,)
    ).fetchone()
    if done:
        return

    plan = json.loads((work / "plan.json").read_text(encoding="utf-8"))
    rules = briefs.load_rules(ctx.conn, source["campaign_id"])
    lay = _clip_layout(ctx, work, moment, source, campaign, plan, search=False)
    words_file = work / "words.txt"
    spoken = words_file.read_text(encoding="utf-8") if words_file.is_file() else ""

    ctx.beat()
    fields = meta_mod.finish(meta_mod.draft(moment, spoken, settings=ctx.settings,
                                          creator=source["creator"] or campaign["creator"], rules=rules),
                             rules=rules, handle=meta_mod.handle_from(rules, lay.handle),
                             credit=meta_mod.stream_credit(source["url"], _streamer_of(ctx, source)))
    probe = media.probe(work / "clip.mp4")
    findings = compliance.check(
        rules=rules, layout=lay, meta=fields, duration=probe.duration, width=probe.width,
        height=probe.height, has_audio=probe.has_audio, overlay_burned=bool(fields["handle"]) or bool(lay.logo),
        hook=moment["hook"] or "", spoken=spoken,
        cam_blank=render.cam_band_blank(work / "clip.mp4", lay))
    from ..edit import framecheck

    ctx.beat()
    seen = framecheck.check(work / "clip.mp4", settings=ctx.settings, work=work,
                            cam_expected=lay.facecam is not None and not lay.fit)
    findings += [compliance.Finding("WARN", "frame_check", f"{s}; look at the clip before approving") for s in seen]
    ok, notes = compliance.summary(findings)

    ctx.conn.execute("DELETE FROM clips WHERE moment_id=? AND path IS NULL", (ctx.ref_id,))
    cur = ctx.conn.execute(
        "INSERT INTO clips (moment_id, status, compliance_ok, compliance_notes) VALUES (?,?,?,?)",
        (ctx.ref_id, "ready" if ok else "held", int(ok), notes))
    clip_id = cur.lastrowid
    dest = package.folder(ctx.settings.out_dir, campaign["name"], clip_id)
    shutil.copyfile(work / "clip.mp4", dest / "clip.mp4")
    render.thumbnail(dest / "clip.mp4", dest / "thumb.jpg")
    meta = {**fields, "campaign_id": campaign["id"], "moment_id": moment["id"],
            "source_id": source["id"], "source_url": source["url"],
            "source_start": plan["start"], "source_end": plan["end"],
            "duration": round(probe.duration, 2), "score": moment["score"],
            "hook": moment["hook"], "compliance": [str(f) for f in findings]}
    style_file = work / "style.json"
    if style_file.is_file():
        meta["taste"] = json.loads(style_file.read_text(encoding="utf-8"))
    cuts_file = work / "cuts.json"
    if cuts_file.is_file():
        cut = json.loads(cuts_file.read_text(encoding="utf-8"))
        if cut.get("keep"):
            meta["jump_cuts"] = {"removed_seconds": cut.get("removed_seconds"), "parts": len(cut["keep"])}
    zip_path = package.write(dest, clip=dest / "clip.mp4", thumb=dest / "thumb.jpg", meta=meta)
    ctx.conn.execute(
        "UPDATE clips SET path=?, thumb_path=?, package_path=?, meta_json=? WHERE id=?",
        (str(dest / "clip.mp4"), str(dest / "thumb.jpg"), str(zip_path),
         json.dumps(meta, ensure_ascii=False), clip_id))
    ctx.conn.execute("UPDATE moments SET status='clipped' WHERE id=?", (ctx.ref_id,))

    if not plan.get("keep"):
        Path(plan["footage"]).unlink(missing_ok=True)
    log.info("moment %s -> clip %s (%s): %s", ctx.ref_id, clip_id,
             "ready" if ok else "HELD", dest)


register("make_clip", [
    ("fetch", stage_clip_fetch),
    ("render", stage_clip_render),
    ("finish", stage_clip_finish),
])


# --------------------------------------------------------------------------
# Phase 5: publishing
# --------------------------------------------------------------------------

def _publish_stage(name: str):
    def stage(ctx: Ctx) -> None:
        from ..publish import flow

        getattr(flow, f"stage_{name}")(ctx)

    return stage


register("publish_clip", [(n, _publish_stage(n)) for n in ("upload", "record", "verify")])


# --------------------------------------------------------------------------
# Taste book: watch a niche's top Shorts and write a draft book
# --------------------------------------------------------------------------

def stage_taste_build(ctx: Ctx) -> None:
    from ..taste import build

    niche = str(ctx.payload.get("niche") or "").strip()
    if not niche:
        raise ValueError("taste_build needs a niche in its payload")
    queries = tuple(ctx.payload.get("queries") or ()) or None
    build.build(ctx.settings, niche, beat=ctx.beat, queries=queries)
    subject = ctx.payload.get("subject")
    if subject and queries:
        # The same subject's rubric: which kinds of moment its top Shorts are, weighted by
        # views. Written as a draft; approving the taste book makes both live.
        from ..moments import rubric_build

        try:
            rubric_build.build(ctx.settings.rubrics_dir / f"{niche}.draft.json", ctx.settings,
                               queries=queries, subject=subject, name=niche,
                               base=ctx.settings.rubrics_dir / "general.json")
        except Exception as exc:
            log.warning("rubric for %s not built: %s", niche, exc)
    # The Telegram bot announces the draft, with Read / Approve / Reject buttons.
    log.info("taste book draft for %s is ready", niche)


register("taste_build", [("build", stage_taste_build)])
