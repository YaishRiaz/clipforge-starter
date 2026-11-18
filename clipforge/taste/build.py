"""Turn measurements of reference Shorts into a draft taste book.

The book is written as `status: draft`: nothing in it is applied to a clip
until Yaish runs `taste-approve <niche>`. Every rule in the markdown says how
many references support it and cites video + second, and says whether it was
measured, looked at in frames, or only interpreted. Unknown stays unknown.
"""
from __future__ import annotations

import json
import logging
import statistics
from collections import Counter
from datetime import date
from pathlib import Path

from ..core.config import Settings
from . import analyze, book as book_mod, refs
from .style import Style

log = logging.getLogger("clipforge.taste.build")

COLOURS = {"white": "#FFFFFF", "yellow": "#FFE000", "black": "#000000", "red": "#FF3B30",
           "green": "#39FF14", "blue": "#00B4FF", "orange": "#FF9500", "pink": "#FF3DA5"}
MARGIN_BY_POSITION = {"bottom": 200, "lower_third": 330, "middle": 780}
# Fonts every Windows install has, standing in for the look the references use.
FONT_BY_STYLE = {"heavy_block": "Arial Black", "condensed": "Impact", "rounded": "Comic Sans MS",
                 "standard_bold": "Segoe UI Black"}
SIZE_BY_LABEL = {"small": 56, "medium": 68, "large": 84}
SUPPORTED_GRAPHICS = {"text_banner", "emoji"}  # a title banner, and reaction emoji (Twemoji)
EMOJI_SHARE = 0.25                             # emoji go on when at least this share of references use them


def niche_dir(settings: Settings, niche: str) -> Path:
    d = settings.data_dir / "taste" / niche
    d.mkdir(parents=True, exist_ok=True)
    return d


def _med(values: list[float], default: float = 0.0) -> float:
    return statistics.median(values) if values else default


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _link(video_id: str, t: float | None = None) -> str:
    return f"https://www.youtube.com/shorts/{video_id}" + (f"?t={int(t)}" if t else "")


def aggregate(niche: str, videos: list[dict], analyses: dict[str, dict]) -> tuple[dict, str]:
    """(book json, evidence markdown) from the references and what was measured on them."""
    done = [(v, analyses[v["id"]]) for v in videos if v["id"] in analyses]
    seen = [(v, a["looked"]) for v, a in done if a.get("looked")]
    n, nl = len(done), len(seen)
    rules: list[dict] = []

    def rule(text, kind, support, total, evidence):
        unique = list({(e["video"], e["note"]): e for e in evidence}.values())
        rules.append({"rule": text, "kind": kind, "support": f"{support} of {total}",
                      "evidence": unique[:4]})

    cpt = [a["cuts_per_10s"] for _, a in done]
    shots = [a["median_shot_s"] for _, a in done]
    wps = [a["words_per_s"] for _, a in done if a.get("words_per_s")]
    cut_ev = [{"video": _link(v["id"], a["cuts"][0]), "t": a["cuts"][0], "note": "first hard cut"}
              for v, a in done if a["cuts"]]
    pace = {"cuts_per_10s": round(_med(cpt), 2), "median_shot_s": round(_med(shots), 2),
            "words_per_s": round(_med(wps), 2) if wps else None,
            "peaks_per_10s": round(_med([len(a["loud_peaks"]) / a["seconds"] * 10 for _, a in done]), 2)}
    rule(f"Median {pace['cuts_per_10s']} hard cuts per 10 s, median shot {pace['median_shot_s']} s",
         "measured", n, n, cut_ev)
    if wps:
        rule(f"Speech runs at a median {pace['words_per_s']} words per second", "measured", len(wps), n, [])

    style: dict = {"captions": {}, "hook": {}, "zoom": {}, "cuts": {}}
    unsupported: list[str] = []

    if nl:
        caps = [(v, r) for v, r in seen if r["captions"]["present"]]
        share_caps = len(caps) / nl
        rule("Burned-in captions are used", "looked", len(caps), nl,
             [{"video": _link(v["id"], a_t), "t": a_t, "note": f"{r['captions']['position']}, {r['captions']['colour']}"}
              for v, r in caps for a_t in [analyses[v["id"]]["frame_times"][0]]])
        if caps and share_caps >= 0.5:
            wos = [r["captions"]["words_on_screen"] for _, r in caps if r["captions"]["words_on_screen"]]
            med_w = round(_med(wos, 2))
            if 1 <= med_w <= 6:
                style["captions"]["words_per_event"] = int(med_w)
                rule(f"Captions show about {med_w} words at a time", "looked", len(wos), len(caps), [])
            else:
                unsupported.append(f"captions show {med_w} words at once in the references; "
                                   "ClipForge keeps 1-6 word captions")
            fonts = Counter(r["captions"].get("font_style", "unknown") for _, r in caps)
            fonts.pop("unknown", None)
            if fonts:
                look, count = fonts.most_common(1)[0]
                if look in FONT_BY_STYLE and count / len(caps) >= 0.4:
                    style["captions"]["font"] = FONT_BY_STYLE[look]
                    rule(f"Caption font looks {look.replace('_', ' ')} (rendered as {FONT_BY_STYLE[look]})",
                         "looked", count, len(caps), [])
                elif look not in FONT_BY_STYLE:
                    unsupported.append(f"captions are mostly {look} type; no matching font is installed")
            sizes = Counter(r["captions"].get("size", "unknown") for _, r in caps)
            sizes.pop("unknown", None)
            if sizes:
                label, count = sizes.most_common(1)[0]
                if count / len(caps) >= 0.4:
                    style["captions"]["size"] = SIZE_BY_LABEL[label] if label in SIZE_BY_LABEL else 66
                    rule(f"Captions are {label}", "looked", count, len(caps), [])
            pos = Counter(r["captions"]["position"] for _, r in caps).most_common(1)[0]
            if pos[0] in MARGIN_BY_POSITION:
                style["captions"]["margin_v"] = MARGIN_BY_POSITION[pos[0]]
                rule(f"Captions sit at the {pos[0].replace('_', ' ')}", "looked", pos[1], len(caps), [])
            else:
                unsupported.append(f"captions sit at the {pos[0]} in most references; not supported")
            hl = [(v, r) for v, r in caps if r["captions"]["highlight_word"]]
            if len(hl) / len(caps) >= 0.4:
                style["captions"]["highlight"] = "#FFE000"
                style["captions"]["pop"] = True
                rule("The spoken word is highlighted", "looked", len(hl), len(caps),
                     [{"video": _link(v["id"], analyses[v["id"]]["frame_times"][0]),
                       "t": analyses[v["id"]]["frame_times"][0], "note": r["captions"]["colour"]} for v, r in hl])
            col = Counter(r["captions"]["colour"].lower() for _, r in caps if r["captions"]["colour"]).most_common(1)
            if col and col[0][0] in COLOURS:
                style["captions"]["color"] = COLOURS[col[0][0]]
            up = sum(1 for _, r in caps if r["captions"]["uppercase"])
            style["captions"]["uppercase"] = up / len(caps) >= 0.5
        elif share_caps < 0.5:
            unsupported.append("fewer than half the references show captions; ClipForge keeps its default captions")
        hooks = [(v, r) for v, r in seen if r["hook_title"]]
        rule("A large hook title is on screen from the first frame", "looked", len(hooks), nl,
             [{"video": _link(v["id"], 0.4), "t": 0.4, "note": r["hook_position"]} for v, r in hooks])
        style["hook"]["enabled"] = len(hooks) / nl >= 0.3
        if hooks:
            stays = [(v, r) for v, r in hooks if r.get("title_stays")]
            if len(stays) / len(hooks) >= 0.4:
                style["hook"]["banner"] = True
                rule("The title stays on screen as a banner", "looked", len(stays), len(hooks), [])
            boxed = [(v, r) for v, r in hooks if r.get("title_box")]
            if len(boxed) / len(hooks) >= 0.4:
                style["hook"]["box"] = True
                rule("The title sits on a solid box", "looked", len(boxed), len(hooks), [])
            if style["captions"].get("font"):
                style["hook"]["font"] = style["captions"]["font"]
        gfx = Counter(k for _, r in seen for k in {g["kind"] for g in r["graphics"]})
        for kind, count in gfx.most_common(3):
            ev = [{"video": _link(v["id"], g["at_seconds"]), "t": g["at_seconds"], "note": kind}
                  for v, r in seen for g in r["graphics"] if g["kind"] == kind]
            rule(f"Added {kind.replace('_', ' ')} graphics appear", "looked", count, nl, ev)
            if kind == "emoji" and count / nl >= EMOJI_SHARE:
                style["emoji"] = {"enabled": True, "max_events": 3}
                continue
            if kind in SUPPORTED_GRAPHICS and style["hook"].get("banner"):
                continue
            unsupported.append(f"{kind} graphics are in the references; the renderer has no support for them yet")
    else:
        unsupported.append("no frame could be inspected (vision model unavailable); caption and graphics "
                           "rules were left at defaults")

    fast = pace["cuts_per_10s"] >= 2.0
    style["zoom"] = {"enabled": fast, "min_gap": round(_clamp(pace["median_shot_s"], 2.5, 8.0), 1),
                     "max_events": int(_clamp(round(pace["cuts_per_10s"]), 2, 6)),
                     "scale": 1.10, "seconds": 0.7}
    tight = pace["cuts_per_10s"] >= 3.0
    style["cuts"] = {"trim_lines": tight}
    interpretation = (
        f"Punch-in zoom is {'ON' if fast else 'OFF'}: the references cut {pace['cuts_per_10s']} times per "
        f"10 s ({'a fast edit' if fast else 'a slow edit'}). A zoom is a stand-in for that pace; zooms "
        "themselves were not measured. "
        + (f"Jump cuts are ON: at {pace['cuts_per_10s']} cuts per 10 s the references are tightly edited, so "
           "filler lines and long silences are cut from our clips." if tight else
           "Jump cuts are OFF: the references are not cut tightly enough to justify trimming lines."))
    Style.model_validate(style)           # a derived style the renderer would reject must not be written

    unknown = ["Zoom and punch-in use in the references (frame sampling cannot tell a zoom from a camera move).",
               "Music, sound effects and sound design (only loudness peaks were measured).",
               "Which edits were made by the original creator and which were later additions."]
    book = {"niche": niche, "status": "draft", "source": "measured", "built": date.today().isoformat(),
            "sample": {"videos": n, "inspected_in_frames": nl},
            "pace": pace, "rules": rules, "unsupported": unsupported, "unknown": unknown, "style": style}

    md = [f"# Taste book: {niche} (DRAFT)", "",
          f"Built {book['built']} from {n} top recent Shorts ({nl} inspected in frames). Not applied until "
          f"`python -m clipforge taste-approve {niche}`. The campaign brief always overrides this book.", "",
          "## Sample", "", "| Short | Views | Seconds | Cuts/10 s |", "|---|---|---|---|"]
    md += [f"| {_link(v['id'])} | {v['views']:,} | {a['seconds']} | {a['cuts_per_10s']} |" for v, a in done]
    for kind, title in (("measured", "Measured (numbers from ffmpeg / Whisper)"),
                        ("looked", "Looked at (vision model over frames, each cited)")):
        md += ["", f"## {title}", ""]
        for r in (r for r in rules if r["kind"] == kind):
            md.append(f"- **{r['rule']}** ({r['support']})")
            md += [f"  - {e['video']} at {e['t']} s: {e['note']}" for e in r["evidence"]]
    md += ["", "## Interpretation (judgement, not measured)", "", f"- {interpretation}", "",
           "## Not supported by the renderer yet", ""] + [f"- {u}" for u in unsupported or ["(none)"]]
    md += ["", "## Unknown", ""] + [f"- {u}" for u in unknown]
    return book, "\n".join(md) + "\n"


def build(settings: Settings, niche: str, *, n: int = 12, queries=None, vision=analyze.look,
          beat=lambda: None, directory: Path | None = None) -> dict:
    """Find, watch and measure references, then write the draft book beside any live one
    (`<niche>.draft.json`; the live book keeps applying until approval). Resumable: videos already
    measured are read from the cache, and the reference file is deleted once measured."""
    base = niche_dir(settings, niche)
    listing = base / "videos.json"
    if listing.is_file():
        videos = json.loads(listing.read_text(encoding="utf-8"))
    else:
        videos = refs.find(settings, niche, n=n, queries=queries)
        listing.write_text(json.dumps(videos, indent=1), encoding="utf-8")
    analyses: dict[str, dict] = {}
    for v in videos:
        beat()
        cache = base / "analysis" / f"{v['id']}.json"
        try:
            if not cache.is_file():
                path = refs.fetch(v["url"], refs.refs_dir(settings, niche))
                analyze.analyze(path, settings, cache=cache, vision=vision)
                path.unlink(missing_ok=True)
            analyses[v["id"]] = json.loads(cache.read_text(encoding="utf-8"))
        except Exception as exc:
            log.warning("reference %s skipped: %s", v["id"], exc)
    if len(analyses) < 3:
        raise RuntimeError(f"only {len(analyses)} reference Shorts could be measured; need at least 3")
    data, md = aggregate(niche, videos, analyses)
    out = directory or book_mod.books_dir()
    (out / f"{niche}.draft.json").write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    (out / f"{niche}.draft.md").write_text(md, encoding="utf-8")
    return data


def summary(niche: str, data: dict) -> str:
    """The Telegram notice: what the draft says and how to approve it."""
    s = data["style"]
    zoom = "zoom on" if s["zoom"]["enabled"] else "no zoom"
    cap = s["captions"]
    bits = [f"{data['pace']['cuts_per_10s']} cuts per 10 s", zoom]
    if cap.get("highlight"):
        bits.append("highlighted caption word")
    if cap.get("words_per_event"):
        bits.append(f"{cap['words_per_event']}-word captions")
    return (f"Taste book draft for '{niche}' is ready ({data['sample']['videos']} Shorts watched): "
            + ", ".join(bits) + f". Read tastebook/{niche}.draft.md, then run: "
            f"python -m clipforge taste-approve {niche}. Nothing changes until you approve. "
            "A campaign's brief still overrides it.")


def queue_missing(conn, settings: Settings, *, directory: Path | None = None) -> list[str]:
    """Queue one build per niche that has a joined campaign but no book and no earlier attempt."""
    from ..core import db, queue

    base = directory or book_mod.books_dir()
    queued: list[str] = []
    for camp in conn.execute("SELECT id, category FROM campaigns WHERE status='active' "
                             "AND joined_at IS NOT NULL AND platform != 'own'").fetchall():
        niche = book_mod.niche_for(conn, camp, None)
        if niche in ("general", "trailer") or niche in queued:
            continue
        if (base / f"{niche}.json").is_file() or (base / f"{niche}.draft.json").is_file():
            continue
        if db.kv_get(conn, f"taste_build_{niche}"):
            continue
        db.kv_set(conn, f"taste_build_{niche}", "queued")
        queue.enqueue(conn, "taste_build", payload={"niche": niche}, priority=-20, dedupe=False)
        queued.append(niche)
    queued += queue_subjects(conn, base)
    return queued


MAX_SUBJECT_BUILDS = 2       # per day: one build spends about 650 of the 10,000 daily YouTube API units


def queue_subjects(conn, base: Path) -> list[str]:
    """One taste book and rubric build per streamer and game seen in a joined campaign's footage."""
    import json

    from ..core import db, queue
    from . import subjects

    import time

    # Streamers first (every clip of theirs uses the book), then games by hours played.
    names: dict[str, str] = {}
    weight: dict[str, float] = {}
    for row in conn.execute(
            "SELECT s.games_json FROM sources s JOIN campaigns k ON k.id=s.campaign_id "
            "WHERE k.status='active' AND k.joined_at IS NOT NULL AND s.games_json IS NOT NULL").fetchall():
        info = json.loads(row["games_json"])
        key = subjects.streamer_key(info.get("streamer"))
        if key:
            names.setdefault(key, info["streamer"])
            weight[key] = 1e12
        for start, end, game in info.get("games") or []:
            key = subjects.game_key(game)
            if key:
                names.setdefault(key, game)
                weight[key] = weight.get(key, 0.0) + (end - start)
    done_today = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE type='taste_build' AND created_at >= ? "
        "AND payload_json LIKE '%\"subject\"%'", (time.time() - 86400,)).fetchone()[0]
    queued: list[str] = []
    for key in sorted(names, key=lambda k: -weight[k]):
        name = names[key]
        if done_today + len(queued) >= MAX_SUBJECT_BUILDS:
            break
        if (base / f"{key}.json").is_file() or (base / f"{key}.draft.json").is_file():
            continue
        if db.kv_get(conn, f"taste_build_{key}"):
            continue
        db.kv_set(conn, f"taste_build_{key}", "queued")
        queue.enqueue(conn, "taste_build", priority=-20, dedupe=False, payload={
            "niche": key, "subject": name, "queries": list(subjects.queries(key, name))})
        queued.append(key)
    return queued
