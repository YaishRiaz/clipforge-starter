"""Finding and loading a niche's taste book from `tastebook/`.

A book is `<niche>.json` (what the renderer applies) beside `<niche>.md` (the
evidence and reasoning, for people). Books built by analysis start as
`status: draft` and are ignored until approved; hand-written ones are `active`.
"""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from ..core import db
from ..core.config import package_root


def books_dir() -> Path:
    return package_root() / "tastebook"


def slug(text: str | None) -> str:
    return re.sub(r"[^a-z0-9-]+", "", (text or "").lower()).strip("-")


def niche_for(conn: sqlite3.Connection, campaign, moment) -> str:
    """An official trailer has its own book; else an explicit override, else the campaign's category."""
    if moment is not None and moment["rubric_category"] == "official_trailer":
        return "trailer"
    explicit = db.kv_get(conn, f"taste_niche_{campaign['id']}")
    if explicit:
        return slug(explicit)
    return slug(campaign["category"]) or "general"


def load(niche: str, *, directory: Path | None = None, include_draft: bool = False) -> dict[str, Any] | None:
    """The niche's book, else `general`'s, else None. Drafts are skipped unless asked for."""
    base = directory or books_dir()
    for name in dict.fromkeys([slug(niche), "general"]):
        path = base / f"{name}.json"
        if not path.is_file():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        data.setdefault("niche", name)
        if data.get("status", "active") == "active" or include_draft:
            return data
    return None


def approve(niche: str, *, directory: Path | None = None) -> Path:
    """Make the niche's draft the live book (the book it replaces is kept as `<niche>.prior.json`)."""
    base = directory or books_dir()
    name = slug(niche)
    path, draft = base / f"{name}.json", base / f"{name}.draft.json"
    if draft.is_file():
        if path.is_file():
            (base / f"{name}.prior.json").write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        data = json.loads(draft.read_text(encoding="utf-8"))
        md = base / f"{name}.draft.md"
        if md.is_file():
            md.replace(base / f"{name}.md")
        draft.unlink()
    else:
        data = json.loads(path.read_text(encoding="utf-8"))
    data["status"] = "active"
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def approve_with_rubric(niche: str, rubrics_dir: Path, *, directory: Path | None = None) -> list[str]:
    """Make a draft book live, and the same subject's rubric draft with it. Returns what went live."""
    live = [approve(niche, directory=directory).name]
    draft = Path(rubrics_dir) / f"{slug(niche)}.draft.json"
    if draft.is_file():
        draft.replace(Path(rubrics_dir) / f"{slug(niche)}.json")
        live.append(f"rubrics/{slug(niche)}.json")
    return live


def set_aside(niche: str, rubrics_dir: Path, *, directory: Path | None = None) -> None:
    """Turn a draft down. It is kept as `<niche>.rejected.*` (never deleted), is not used, and is
    not rebuilt on its own; `taste-build` can still build it again on request."""
    base = directory or books_dir()
    name = slug(niche)
    moved = False
    for ext in ("json", "md"):
        p = base / f"{name}.draft.{ext}"
        if p.is_file():
            p.replace(base / f"{name}.rejected.{ext}")
            moved = True
    r = Path(rubrics_dir) / f"{name}.draft.json"
    if r.is_file():
        r.replace(Path(rubrics_dir) / f"{name}.rejected.json")
    if not moved:
        raise FileNotFoundError(f"no draft for '{name}'")


def listing(*, directory: Path | None = None) -> list[dict]:
    """Every book: {name, status: live|draft, path, md}."""
    base = directory or books_dir()
    out = []
    for p in sorted(base.glob("*.json")):
        stem = p.stem
        if stem.endswith((".prior", ".rejected")):
            continue
        draft = stem.endswith(".draft")
        name = stem[:-6] if draft else stem
        md = base / (f"{name}.draft.md" if draft else f"{name}.md")
        out.append({"name": name, "status": "draft" if draft else "live", "path": p,
                    "md": md if md.is_file() else None})
    return out


def draft_path(niche: str, *, directory: Path | None = None) -> Path:
    return (directory or books_dir()) / f"{slug(niche)}.draft.json"


def _merge(base: dict, top: dict) -> dict:
    out = dict(base)
    for k, v in top.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def _exact(name: str, directory: Path | None = None) -> dict[str, Any] | None:
    """One approved book by exact name, with no fallback to `general`."""
    path = (directory or books_dir()) / f"{slug(name)}.json"
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if data.get("status", "active") == "active" else None


def for_clip(conn: sqlite3.Connection, campaign, moment, *,
             directory: Path | None = None) -> dict[str, Any] | None:
    """The niche's book, with the game's book laid over it and the streamer's over that.

    Each layer only changes what it measured: a streamer book that says nothing about zoom
    leaves the game's or the niche's zoom in place. Only approved books are used.
    """
    niche = niche_for(conn, campaign, moment)
    book = load(niche, directory=directory)
    if niche == "trailer" or moment is None:
        return book
    from . import subjects

    try:
        streamer, game = subjects.for_moment(conn, moment)
    except Exception:
        return book
    used = [book["niche"]] if book else []
    style = dict((book or {}).get("style") or {})
    for key in (game, streamer):
        layer = _exact(key, directory) if key else None
        if layer and layer.get("style"):
            style = _merge(style, layer["style"])
            used.append(slug(key))
    if not used:
        return None
    return {**(book or {}), "style": style, "niche": " + ".join(used), "status": "active"}
