"""Phone notifications and one-tap review through a Telegram bot (PRD section 3).

Only the configured chat is ever obeyed. The bot token is part of every API URL,
so no error raised here may carry the URL: failures are reported by type only.
"""
from __future__ import annotations

import logging
import re
import socket
import sqlite3
from pathlib import Path

from ..core import db
from ..core.config import Settings
from ..publish import crosspost, flow
from . import redo, review

log = logging.getLogger("clipforge.telegram")

API = "https://api.telegram.org"
VIDEO_LIMIT = 49 * 1024 * 1024        # the Bot API refuses uploads over 50 MB


class TelegramError(RuntimeError):
    pass


def configured(settings: Settings) -> bool:
    return bool(settings.telegram_bot_token and settings.telegram_chat_id)


def call(settings: Settings, method: str, *, files=None, http_timeout: float = 60.0, **params) -> dict:
    import httpx

    if not settings.telegram_bot_token:
        raise TelegramError("TELEGRAM_BOT_TOKEN is empty in .env")
    try:
        r = httpx.post(f"{API}/bot{settings.telegram_bot_token}/{method}", data=params,
                       files=files, timeout=http_timeout)
        body = r.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise TelegramError(f"{method}: {type(exc).__name__}") from None
    if not body.get("ok"):
        raise TelegramError(f"{method}: {body.get('description', r.status_code)}")
    return body["result"]


def discover_chat(settings: Settings) -> str | None:
    """The chat id of whoever last messaged the bot, or None if nobody has."""
    for upd in reversed(call(settings, "getUpdates", http_timeout=30.0)):
        msg = upd.get("message") or (upd.get("callback_query") or {}).get("message")
        if msg:
            return str(msg["chat"]["id"])
    return None


def lan_address(settings: Settings) -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            ip = s.getsockname()[0]
    except OSError:
        ip = "localhost"
    return f"http://{ip}:{settings.dashboard_port}"


def _keyboard(rows: list[list[tuple[str, str]]]) -> str:
    import json

    def button(text: str, data: str) -> dict:
        return {"text": text, "url": data} if data.startswith("https://") else {
            "text": text, "callback_data": data}

    return json.dumps({"inline_keyboard": [[button(t, d) for t, d in row] for row in rows]})


def send(settings: Settings, text: str, buttons: list[list[tuple[str, str]]] | None = None) -> None:
    params = {"chat_id": settings.telegram_chat_id, "text": text[:4000]}
    if buttons:
        params["reply_markup"] = _keyboard(buttons)
    call(settings, "sendMessage", **params)


def clip_caption(row: sqlite3.Row) -> str:
    meta = review.meta_of(row)
    lines = [f"Clip {row['id']}  score {row['score']:.0f}", meta.get("title", ""), "",
             meta.get("description", "")]
    warns = [n for n in (row["compliance_notes"] or "").splitlines() if n.strip()]
    if warns:
        lines += ["", *[w[:160] for w in warns]]
    return "\n".join(lines)[:1000]


def send_clip(settings: Settings, row: sqlite3.Row) -> None:
    buttons = [[("Approve", f"a:{row['id']}"), ("Reject", f"r:{row['id']}")]]
    path = Path(row["path"] or "")
    if path.is_file() and path.stat().st_size <= VIDEO_LIMIT:
        with path.open("rb") as fh:
            call(settings, "sendVideo", files={"video": (path.name, fh, "video/mp4")},
                 http_timeout=300.0, chat_id=settings.telegram_chat_id, caption=clip_caption(row),
                 supports_streaming="true", reply_markup=_keyboard(buttons))
    else:
        send(settings, clip_caption(row) + "\n\n(video not attached; open the dashboard)", buttons)


def sweep(conn: sqlite3.Connection, settings: Settings) -> int:
    """Announce new ready clips and failed jobs once each. Returns messages sent."""
    if not configured(settings):
        return 0
    sent = 0
    for r in review.by_status(conn, "ready"):
        key = f"tg_clip_{r['id']}"
        if db.kv_get(conn, key):
            continue
        send_clip(settings, review.get(conn, r["id"]))
        db.kv_set(conn, key, "1")
        sent += 1
    for j in conn.execute("SELECT id, type, ref_id, last_error FROM jobs WHERE status='failed'"):
        key = f"tg_job_{j['id']}"
        if db.kv_get(conn, key):
            continue
        send(settings, f"Job {j['id']} ({j['type']}) failed: {(j['last_error'] or '')[:300]}")
        if j["type"] == flow.JOB and j["ref_id"]:
            send_manual(settings, review.get(conn, j["ref_id"]))
        db.kv_set(conn, key, "1")
        sent += 1
    return sent + _sweep_posts(conn, settings)


def send_manual(settings: Settings, row: sqlite3.Row | None) -> None:
    """Publisher is down: hand over the package and copy-ready text to post by hand."""
    if row is None:
        return
    path = Path(row["path"] or "")
    if path.is_file() and path.stat().st_size <= VIDEO_LIMIT:
        with path.open("rb") as fh:
            call(settings, "sendVideo", files={"video": (path.name, fh, "video/mp4")},
                 http_timeout=300.0, chat_id=settings.telegram_chat_id,
                 caption=f"Post this one by hand (clip {row['id']}).")
    send(settings, flow.copy_ready(row))


def _post_buttons(conn: sqlite3.Connection, post: sqlite3.Row) -> list[list[tuple[str, str]]]:
    rows = [[("Open video", post["url"])]]
    page = flow.submit_page(conn, post["id"])
    if page and page.startswith("https://"):
        rows.append([("Open submit page", page)])
    rows.append([("I submitted it", f"s:{post['id']}")])
    return rows


def _send_kit(conn: sqlite3.Connection, settings: Settings, clip_id: int) -> int:
    """Video and per-platform captions to post by hand on TikTok and Instagram."""
    items = crosspost.kit(conn, clip_id)
    row = review.get(conn, clip_id)
    if not items or row is None:
        return 0
    path = Path(row["path"] or "")
    if path.is_file() and path.stat().st_size <= VIDEO_LIMIT:
        with path.open("rb") as fh:
            call(settings, "sendVideo", files={"video": (path.name, fh, "video/mp4")},
                 http_timeout=300.0, chat_id=settings.telegram_chat_id,
                 caption=f"Cross-post clip {clip_id} by hand: " + ", ".join(n for n, _ in items))
    for name, text in items:
        send(settings, f"{name} caption (clip {clip_id}):\n\n{text}"[:4000])
    return 1 + len(items)


def _sweep_posts(conn: sqlite3.Connection, settings: Settings) -> int:
    """New posts, then the 15-minute and 25-minute submission reminders."""
    sent = 0
    for p in conn.execute("SELECT * FROM posts WHERE url IS NOT NULL ORDER BY id"):
        if not db.kv_get(conn, f"tg_post_{p['id']}"):
            send(settings, f"Posted clip {p['clip_id']}: {p['url']}\nSubmit it to the campaign within "
                 f"{flow.SUBMIT_WINDOW_MIN} minutes.", _post_buttons(conn, p))
            db.kv_set(conn, f"tg_post_{p['id']}", "1")
            sent += 1
        if not db.kv_get(conn, f"tg_kit_{p['clip_id']}") and                 db.kv_get(conn, "setting_crosspost", "1") == "1":
            sent += _send_kit(conn, settings, p["clip_id"])
            db.kv_set(conn, f"tg_kit_{p['clip_id']}", "1")
        if p["submitted_at"] or not p["posted_at"]:
            continue
        age = flow.minutes_since(p["posted_at"])
        if age >= 15 and not db.kv_get(conn, f"tg_remind_{p['id']}"):
            send(settings, f"Reminder: clip {p['clip_id']} is not submitted yet "
                 f"({flow.SUBMIT_WINDOW_MIN - int(age)} min left).", _post_buttons(conn, p))
            db.kv_set(conn, f"tg_remind_{p['id']}", "1")
            sent += 1
        if age >= flow.SUBMIT_WINDOW_MIN and not db.kv_get(conn, f"tg_late_{p['id']}"):
            send(settings, f"Clip {p['clip_id']}: the submission window has passed. Submit "
                 "anyway; the campaign may still accept it.", _post_buttons(conn, p))
            db.kv_set(conn, f"tg_late_{p['id']}", "1")
            sent += 1
    return sent


def _clip_id_of(msg: dict) -> int | None:
    """The clip a message is about: the one replied to, or 'clip 23: ...' typed out."""
    quoted = msg.get("reply_to_message") or {}
    for text in (quoted.get("caption"), quoted.get("text"), msg.get("text")):
        m = re.search(r"[Cc]lip (\d+)", text or "")
        if m and (text is not msg.get("text") or re.match(r"\s*[Cc]lip \d+\s*[:,-]", text)):
            return int(m.group(1))
    return None


def _redo_from_note(conn: sqlite3.Connection, settings: Settings, msg: dict, note: str) -> None:
    clip_id = _clip_id_of(msg)
    if clip_id is None:
        send(settings, "Reply to a clip, or start with 'clip 23:', to say what to fix.")
        return
    try:
        edit = redo.interpret(conn, settings, clip_id, note)
        what = redo.describe(edit)
        redo.apply(conn, settings, clip_id, edit)
    except (redo.RedoError, review.ReviewError) as exc:
        send(settings, f"Clip {clip_id}: {exc}")
        return
    extra = f"\nCannot do: {edit.unsupported}" if edit.unsupported else ""
    send(settings, f"Remaking clip {clip_id}: {what}. The new version comes back here for "
         f"approval; clip {clip_id} is closed.{extra}")


def _status_text(conn: sqlite3.Connection, settings: Settings) -> str:
    n = {r["status"]: r["n"] for r in conn.execute(
        "SELECT status, COUNT(*) n FROM clips GROUP BY status")}
    q = {r["status"]: r["n"] for r in conn.execute(
        "SELECT status, COUNT(*) n FROM jobs GROUP BY status")}
    return (f"Clips: {n or 'none'}\nJobs: {q or 'none'}\nDashboard: {lan_address(settings)}")


def handle(conn: sqlite3.Connection, settings: Settings, update: dict) -> None:
    cb = update.get("callback_query")
    msg = update.get("message")
    chat = str(((cb or {}).get("message") or msg or {}).get("chat", {}).get("id", ""))
    if chat != str(settings.telegram_chat_id):
        return                                  # not Yaish: ignore silently
    if cb:
        action, _, raw = (cb.get("data") or "").partition(":")
        try:
            clip_id = int(raw)
            if action == "a":
                review.approve(conn, clip_id)
                answer = f"Clip {clip_id} approved; posts {flow.next_slot_text(conn, settings)}"
            elif action == "s":
                flow.mark_submitted(conn, clip_id)
                answer = "Marked as submitted"
            elif action == "r":
                review.reject(conn, clip_id, "rejected from Telegram")
                answer = f"Clip {clip_id} rejected"
            else:
                return
        except (ValueError, review.ReviewError) as exc:
            answer = str(exc)[:190]
        call(settings, "answerCallbackQuery", callback_query_id=cb["id"], text=answer)
        try:
            call(settings, "editMessageReplyMarkup", chat_id=settings.telegram_chat_id,
                 message_id=cb["message"]["message_id"], reply_markup='{"inline_keyboard":[]}')
        except TelegramError:
            pass
        send(settings, answer)
        return
    raw_text = (msg.get("text") or "").strip()
    text = raw_text.lower()
    if raw_text and not text.startswith("/"):
        _redo_from_note(conn, settings, msg, raw_text)
        return
    if text.startswith("/ready"):
        rows = review.by_status(conn, "ready")
        if not rows:
            send(settings, "Nothing waiting for review.")
        for r in rows[:5]:
            send_clip(settings, review.get(conn, r["id"]))
    elif text.startswith("/status"):
        send(settings, _status_text(conn, settings))
    else:
        send(settings, "Commands: /ready (clips to review), /status. Reply to a clip with what to "
             "fix (for example \"cut the last 3 seconds\") and it is remade.")


def poll_once(conn: sqlite3.Connection, settings: Settings, *, wait: int = 20) -> int:
    offset = int(db.kv_get(conn, "tg_offset", "0") or 0)
    updates = call(settings, "getUpdates", http_timeout=wait + 10, offset=offset, timeout=wait)
    for u in updates:
        try:
            handle(conn, settings, u)
        except TelegramError as exc:
            log.warning("telegram: %s", exc)
        db.kv_set(conn, "tg_offset", str(u["update_id"] + 1))
    return len(updates)
