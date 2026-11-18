"""The dashboard (PRD section 10): one password, served on the LAN.

Plain HTTP on a home network. The session cookie is signed and SameSite=Strict,
and every state-changing request is a POST from the same origin. Files are only
ever served from paths stored in the database, never from a path in the URL.
"""
from __future__ import annotations

import hashlib
import hmac
import time
from pathlib import Path
from urllib.parse import quote, urlparse

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

from ..core import db, queue
from ..core.config import Settings, get_settings
from ..moments import reweight as reweight_mod
from ..moments import rubric as rubric_mod
from . import review, stats_view

COOKIE = "cf_session"
SESSION_SECONDS = 7 * 24 * 3600
MAX_FAILS, FAIL_WINDOW = 5, 600

JOB_LABELS = {
    "ingest_source": "Downloading footage",
    "mine_source": "Finding moments",
    "parse_brief": "Reading brief",
    "make_clip": "Making clip",
    "live_moment": "Checking a live moment",
    "publish_clip": "Posting clip",
    "taste_build": "Building taste book",
}


def _job_subject(conn, job) -> str:
    """What a job row is actually working on, in plain words, for the queue page."""
    rid = job["ref_id"]
    t = job["type"]
    if t in ("make_clip", "publish_clip") and rid is not None:
        m = conn.execute("SELECT hook, source_id FROM moments WHERE id=?", (rid,)).fetchone()
        if m is None:
            return f"moment {rid}"
        k = conn.execute(
            "SELECT k.name FROM sources s JOIN campaigns k ON k.id = s.campaign_id WHERE s.id=?",
            (m["source_id"],)).fetchone()
        return f"{k['name'] if k else 'campaign ?'}: {m['hook'] or '(no hook yet)'}"
    if t in ("ingest_source", "mine_source") and rid is not None:
        s = conn.execute("SELECT url, campaign_id FROM sources WHERE id=?", (rid,)).fetchone()
        if s is None:
            return f"source {rid}"
        k = conn.execute("SELECT name FROM campaigns WHERE id=?", (s["campaign_id"],)).fetchone()
        return f"{k['name'] if k else 'campaign ?'}: {s['url']}"
    if t == "parse_brief" and rid is not None:
        k = conn.execute("SELECT name FROM campaigns WHERE id=?", (rid,)).fetchone()
        return k["name"] if k else f"campaign {rid}"
    if t == "taste_build":
        return queue.payload_of(job).get("niche", "")
    return ""


NOTE_HELP = {
    "banned_edits": "The brief bans these edits. Watch the clip and make sure none are in it",
    "pre_approval": "The campaign team must approve a draft before this is posted",
    "facecam": "Facecam",
}


def _plain_note(note: str) -> dict:
    """'WARN banned_edits: check by eye, the machine cannot: x' -> a title and the detail."""
    body = note.split(" ", 1)[1] if note.split(" ", 1)[0] in ("FAIL", "WARN") and " " in note else note
    code, _, detail = body.partition(":")
    detail = detail.strip().removeprefix("check by eye, the machine cannot:").strip()
    return {"title": NOTE_HELP.get(code.strip(), code.strip().replace("_", " ").capitalize()),
            "detail": detail}


def _campaign_alerts(conn) -> list[dict]:
    """Things about an active campaign Yaish should know: budget nearly gone, Tier 1 short."""
    from ..ingest import briefs as briefs_mod

    budgets, geo = stats_view.budgets(conn), stats_view.geo(conn)["campaigns"]
    out = []
    for k in conn.execute("SELECT id, name FROM campaigns WHERE status='active'").fetchall():
        b, g = budgets.get(k["id"]), geo.get(k["id"])
        if b and b["warn"]:
            out.append({"id": k["id"], "text": f"{k['name']}: {b['warn']}"})
        quota = getattr(briefs_mod.load_rules(conn, k["id"]), "min_tier1_share", None)
        if quota and g and g["tier1"] < quota:
            out.append({"id": k["id"], "text": f"{k['name']}: {g['tier1']:.0%} of viewers are Tier 1, "
                                              f"the brief wants {quota:.0%}"})
    return out


def _live_now(conn) -> list[dict]:
    """Approved channels that are streaming right now, with what the chat watcher has caught."""
    from ..ingest import live

    out = []
    for lv in live.state(conn):
        k = conn.execute("SELECT name FROM campaigns WHERE id=?", (lv["campaign_id"],)).fetchone()
        out.append({**lv, "campaign": k["name"] if k else "", "ago": _ago(lv["since"])})
    return out


def _ago(ts: float | None) -> str:
    if not ts:
        return "never"
    secs = max(0, time.time() - ts)
    if secs < 90:
        return "just now"
    if secs < 3600:
        return f"{int(secs // 60)} min ago"
    if secs < 86400:
        return f"{int(secs // 3600)} h ago"
    return f"{int(secs // 86400)} d ago"


def _describe_job(conn, job) -> dict:
    d = dict(job)
    d["label"] = JOB_LABELS.get(job["type"], job["type"])
    d["what"] = _job_subject(conn, job)
    return d

_env = Environment(loader=FileSystemLoader(str(Path(__file__).parent / "templates")),
                   autoescape=select_autoescape(["html"]))


def _key(password: str) -> bytes:
    return hashlib.sha256(("clipforge-session:" + password).encode()).digest()


def make_token(password: str, now: float | None = None) -> str:
    exp = str(int((now or time.time()) + SESSION_SECONDS))
    return f"{exp}.{hmac.new(_key(password), exp.encode(), hashlib.sha256).hexdigest()}"


def valid_token(password: str, token: str | None, now: float | None = None) -> bool:
    if not password or not token or "." not in token:
        return False
    exp, _, sig = token.partition(".")
    good = hmac.new(_key(password), exp.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, good) and exp.isdigit() and int(exp) > (now or time.time())


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    if not settings.dashboard_password:
        raise RuntimeError("DASHBOARD_PASSWORD is empty in .env; the dashboard will not start "
                           "without one")
    app = FastAPI(title="ClipForge", docs_url=None, redoc_url=None, openapi_url=None)
    fails: dict[str, list[float]] = {}

    def conn():
        settings.ensure_dirs()
        c = db.open_db(settings.db_path)
        try:
            yield c
        finally:
            c.close()

    def authed(request: Request) -> None:
        if not valid_token(settings.dashboard_password, request.cookies.get(COOKIE)):
            raise HTTPException(status_code=303, headers={"Location": "/login"})
        if request.method == "POST":
            origin = request.headers.get("origin")
            if origin and urlparse(origin).netloc != request.headers.get("host"):
                raise HTTPException(status_code=403, detail="cross-site request refused")

    def page(request: Request, name: str, status: int = 200, **ctx) -> HTMLResponse:
        flash = ctx.pop("flash", None) or request.query_params.get("msg")
        html = _env.get_template(name).render(path=request.url.path, flash=flash, **ctx)
        return HTMLResponse(html, status_code=status)

    def back(to: str, msg: str) -> RedirectResponse:
        return RedirectResponse(f"{to}?msg={quote(msg)}", status_code=303)

    # ---- login ---------------------------------------------------------
    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request):
        return page(request, "login.html", error=None)

    @app.post("/login")
    def login(request: Request, password: str = Form("")):
        ip = request.client.host if request.client else "?"
        now = time.time()
        recent = [t for t in fails.get(ip, []) if now - t < FAIL_WINDOW]
        if len(recent) >= MAX_FAILS:
            return page(request, "login.html", status=429,
                        error="Too many attempts. Wait ten minutes.")
        if not hmac.compare_digest(password.encode(), settings.dashboard_password.encode()):
            fails[ip] = recent + [now]
            return page(request, "login.html", status=401, error="Wrong password.")
        fails.pop(ip, None)
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(COOKIE, make_token(settings.dashboard_password), httponly=True,
                        samesite="strict", max_age=SESSION_SECONDS)
        return resp

    @app.post("/logout")
    def logout():
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(COOKIE)
        return resp

    # ---- pages ---------------------------------------------------------
    @app.get("/", response_class=HTMLResponse, dependencies=[Depends(authed)])
    def overview(request: Request, c=Depends(conn)):
        sm = stats_view.settings_map(c)
        ov = stats_view.overview(c, float(sm["lkr_per_usd"]))
        waiting = c.execute("SELECT COUNT(*) FROM clips WHERE status='ready'").fetchone()[0]
        from ..core import autopilot
        from ..publish import flow

        to_submit = [p for p in stats_view.post_rows(c) if not p["submitted_at"]]
        alerts = _campaign_alerts(c)
        failed = c.execute("SELECT COUNT(*) FROM jobs WHERE status='failed' AND type != 'autopilot' "
                           "AND updated_at > ?", (time.time() - 86400,)).fetchone()[0]
        return page(request, "overview.html", ov=ov, waiting=waiting, to_submit=to_submit,
                    alerts=alerts, live=_live_now(c),
                    failed=failed, next_post=flow.next_slot_text(c, settings),
                    daily=stats_view.daily_gains(c), geo=stats_view.geo(c),
                    runway=autopilot.runway(c),
                    autopilot_on=autopilot.enabled(c))

    @app.get("/campaigns", response_class=HTMLResponse, dependencies=[Depends(authed)])
    def campaigns(request: Request, c=Depends(conn)):
        posts = stats_view.post_rows(c)
        from ..ingest import briefs as briefs_mod

        images = stats_view.campaign_images(c)
        geo = stats_view.geo(c)["campaigns"]
        budgets = stats_view.budgets(c)
        rows = []
        for k in c.execute("SELECT * FROM campaigns ORDER BY status='active' DESC, id"):
            mine = [p for p in posts if p["campaign_id"] == k["id"]]
            reviewed = [p for p in mine if p["campaign_review_status"] != "pending"]
            approved = sum(p["campaign_review_status"] == "approved" for p in reviewed)
            rows.append({**dict(k), "views": sum(p["views"] for p in mine),
                         "image": images.get(k["id"]), "counts": stats_view.review_counts(mine),
                         "geo": geo.get(k["id"]), "budget": budgets.get(k["id"]),
                         "quota": getattr(briefs_mod.load_rules(c, k["id"]), "min_tier1_share", None),
                         "earned": sum(p["estimate"] for p in mine
                                       if p["campaign_review_status"] == "approved"),
                         "approval": approved / len(reviewed) if reviewed else None})
        return page(request, "campaigns.html", rows=rows)

    # ---- discover and join ---------------------------------------------
    def _filters(q) -> dict:
        def num(key):
            try:
                return float(q.get(key) or 0)
            except ValueError:
                return 0.0
        return {"source": q.get("source", ""), "gaming": q.get("gaming") == "1",
                "youtube": q.get("youtube", "0" if "f" in q else "1") == "1", "min_rate": num("min_rate"),
                "min_budget": num("min_budget"), "hide_application": q.get("hide_application") == "1",
                "text": (q.get("text") or "").strip(), "state": q.get("state", ""),
                "sort": q.get("sort", "rate")}

    @app.get("/discover", response_class=HTMLResponse, dependencies=[Depends(authed)])
    def discover_page(request: Request, c=Depends(conn)):
        from ..discover import store

        q = request.query_params
        f = _filters(q)
        if not q or set(q) == {"msg"}:                 # first visit: start on gaming
            f["gaming"] = True
        rows = store.query(c, **f)
        total = c.execute("SELECT COUNT(*) FROM discovered WHERE ended=0").fetchone()[0]
        return page(request, "discover.html", rows=rows, f=f, total=total,
                    refreshed=db.kv_get(c, store.KV_REFRESHED))

    @app.post("/discover/refresh", dependencies=[Depends(authed)])
    def discover_refresh(c=Depends(conn)):
        from ..discover import store

        report = store.refresh(c)
        msg = f"Looked at {report['seen']} campaigns, {len(report['new'])} new."
        if report["errors"]:
            msg += " Problems: " + "; ".join(report["errors"])
        return back("/discover", msg)

    @app.post("/discover/{did}/state", dependencies=[Depends(authed)])
    def discover_state(did: int, state: str = Form(...), c=Depends(conn)):
        from ..discover import store

        try:
            store.set_state(c, did, state)
        except ValueError as exc:
            return back("/discover", str(exc))
        return back("/discover", "Saved.")

    @app.get("/discover/{did}", response_class=HTMLResponse, dependencies=[Depends(authed)])
    def discover_join_page(did: int, request: Request, c=Depends(conn)):
        from ..discover import store

        try:
            row = store.get(c, did)
        except ValueError:
            raise HTTPException(status_code=404)
        return page(request, "join.html", d=row)

    @app.post("/discover/{did}/joined", dependencies=[Depends(authed)])
    def discover_joined(did: int, brief: str = Form(""), c=Depends(conn)):
        from ..discover import store

        try:
            cid = store.mark_joined(c, did, brief=brief)
        except ValueError as exc:
            return back("/discover", str(exc))
        have = c.execute("SELECT brief_text FROM campaigns WHERE id=?", (cid,)).fetchone()[0]
        return back(f"/campaigns/{cid}", "Campaign created, reading its brief." if have
                    else "Campaign created. Paste its brief below so clipping can start.")

    # ---- campaign page, footage and autopilot --------------------------
    @app.get("/campaigns/{cid}", response_class=HTMLResponse, dependencies=[Depends(authed)])
    def campaign_page(cid: int, request: Request, c=Depends(conn)):
        from ..ingest import briefs as briefs_mod, rules as rules_mod

        k = c.execute("SELECT * FROM campaigns WHERE id=?", (cid,)).fetchone()
        if k is None:
            raise HTTPException(status_code=404)
        rules = briefs_mod.load_rules(c, cid)
        srcs = c.execute(
            "SELECT s.id, s.url, s.status, "
            "(SELECT COUNT(*) FROM moments m WHERE m.source_id=s.id AND m.status='candidate') AS candidates, "
            "(SELECT COUNT(*) FROM moments m WHERE m.source_id=s.id AND m.status='clipped') AS clipped "
            "FROM sources s WHERE s.campaign_id=? ORDER BY s.id DESC LIMIT 30", (cid,)).fetchall()
        return page(request, "campaign.html", k=k, rules=rules, srcs=srcs,
                    budget=stats_view.budgets(c).get(cid),
                    geo=stats_view.geo(c)["campaigns"].get(cid))

    @app.post("/campaigns/{cid}/brief", dependencies=[Depends(authed)])
    def campaign_brief(cid: int, brief: str = Form(""), c=Depends(conn)):
        from ..discover import store

        text = brief.strip()
        if text.lower().startswith("http"):
            text = store.brief_from_link(text) or ""
        if not text:
            return back(f"/campaigns/{cid}", "Could not read that. Paste the brief text itself.")
        c.execute("UPDATE campaigns SET brief_text=? WHERE id=?", (text, cid))
        queue.enqueue(c, "parse_brief", ref_id=cid)
        return back(f"/campaigns/{cid}", "Brief saved, reading it now.")

    @app.post("/campaigns/{cid}/channel", dependencies=[Depends(authed)])
    def campaign_channel(cid: int, channel: str = Form(""), c=Depends(conn)):
        from ..ingest import briefs as briefs_mod, rules as rules_mod

        rules = briefs_mod.load_rules(c, cid)
        url = channel.strip()
        if rules is None or not url.lower().startswith("http"):
            return back(f"/campaigns/{cid}", "Needs a parsed brief and a channel link.")
        root = rules_mod.channel_root(url)
        if root not in rules.approved_channels:
            rules.approved_channels.append(root)
            briefs_mod.save_rules(c, cid, rules)
        return back(f"/campaigns/{cid}", f"Approved footage from {root}.")

    @app.post("/campaigns/{cid}/scan", dependencies=[Depends(authed)])
    def campaign_scan(cid: int, c=Depends(conn)):
        queue.enqueue(c, "autopilot", dedupe=False, priority=-10)
        return back(f"/campaigns/{cid}", "Looking for new footage now.")

    @app.post("/campaigns/{cid}/hold", dependencies=[Depends(authed)])
    def campaign_hold_route(cid: int, c=Depends(conn)):
        row = c.execute("SELECT name, status FROM campaigns WHERE id=?", (cid,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404)
        c.execute("UPDATE campaigns SET status='skipped' WHERE id=?", (cid,))
        return back(f"/campaigns/{cid}", f"{row['name']}: left. Autopilot and publishing leave it alone now.")

    @app.post("/campaigns/{cid}/resume", dependencies=[Depends(authed)])
    def campaign_resume_route(cid: int, c=Depends(conn)):
        row = c.execute("SELECT name, joined_at FROM campaigns WHERE id=?", (cid,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404)
        if not row["joined_at"]:
            return back(f"/campaigns/{cid}", "Never joined, nothing to resume.")
        c.execute("UPDATE campaigns SET status='active' WHERE id=?", (cid,))
        return back(f"/campaigns/{cid}", f"{row['name']}: active again.")

    @app.post("/autopilot", dependencies=[Depends(authed)])
    def autopilot_set(mode: str = Form(...), c=Depends(conn)):
        if mode == "run":
            queue.enqueue(c, "autopilot", dedupe=False, priority=-10)
            return back("/", "Autopilot pass queued.")
        db.kv_set(c, "setting_autopilot", "off" if mode == "off" else "on")
        return back("/", f"Autopilot {'off' if mode == 'off' else 'on'}.")

    @app.post("/warmup", dependencies=[Depends(authed)])
    def warmup_set(warmup_date: str = Form(""), c=Depends(conn)):
        import datetime as dt

        text = warmup_date.strip()
        if text:
            try:
                dt.date.fromisoformat(text)
            except ValueError:
                return back("/", "Date looks like 2026-11-19.")
        db.kv_set(c, "setting_warmup_date", text)
        return back("/", "Saved.")

    @app.get("/queue", response_class=HTMLResponse, dependencies=[Depends(authed)])
    def queue_page(request: Request, c=Depends(conn)):
        rows = c.execute(
            "SELECT * FROM jobs WHERE type != 'autopilot' ORDER BY id DESC LIMIT 60").fetchall()
        jobs = [_describe_job(c, r) for r in rows]
        counts = {s: 0 for s in ("queued", "running", "done", "failed")}
        for row in c.execute(
                "SELECT status, COUNT(*) AS n FROM jobs WHERE type != 'autopilot' GROUP BY status"):
            counts[row["status"]] = row["n"]
        row = c.execute(
            "SELECT status, updated_at FROM jobs WHERE type='autopilot' ORDER BY id DESC LIMIT 1").fetchone()
        tick = {"status": row["status"], "ago": _ago(row["updated_at"])} if row else None
        from ..publish import flow

        return page(request, "queue.html", jobs=jobs, counts=counts, tick=tick,
                    upnext=_up_next(c), next_post=flow.next_slot_text(c, settings))

    @app.post("/plan/{clip_id}/move", dependencies=[Depends(authed)])
    def plan_move(clip_id: int, how: str = Form(...), c=Depends(conn)):
        from ..publish import flow

        try:
            flow.reorder(c, settings, clip_id, how)
        except ValueError as exc:
            return back("/queue", str(exc))
        return back("/queue", f"Clip {clip_id} moved {how}. Posting follows your order now.")

    @app.post("/plan/reset", dependencies=[Depends(authed)])
    def plan_reset(c=Depends(conn)):
        from ..publish import flow

        flow.reset_order(c)
        return back("/queue", "Back to the automatic order.")

    def _up_next(c) -> list[dict]:
        from datetime import datetime

        from ..publish import flow

        out = []
        for p in flow.plan(c, settings, limit=30):
            r = review.get(c, p["clip_id"])
            m = review.meta_of(r)
            day = "today" if p["at"].date() == datetime.now().date() else p["at"].strftime("%a %d %b")
            out.append({**p, "title": m.get("title") or r["hook"] or f"Clip {r['id']}",
                        "campaign": r["campaign_name"], "duration": m.get("duration"),
                        "when": f"{day} {p['at']:%H:%M}"})
        return out

    @app.post("/job/{job_id}/retry", dependencies=[Depends(authed)])
    def retry(job_id: int, c=Depends(conn)):
        row = c.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None or row["status"] != "failed":
            return back("/queue", "Only a failed job can be retried.")
        queue.retry(c, job_id)
        return back("/queue", f"Job {job_id} queued again.")

    @app.get("/ready", response_class=HTMLResponse, dependencies=[Depends(authed)])
    def ready(request: Request, c=Depends(conn)):
        clips = [review.get(c, r["id"]) for r in review.by_status(c, "ready", "approved", "held")]
        items = []
        for r in clips:
            notes = [n.strip() for n in (r["compliance_notes"] or "").splitlines() if n.strip()]
            items.append({"row": r, "meta": review.meta_of(r),
                          "fails": [_plain_note(n) for n in notes if n.startswith("FAIL")],
                          "warns": [_plain_note(n) for n in notes if not n.startswith("FAIL")]})
        groups = [("To review", [i for i in items if i["row"]["status"] == "ready"]),
                  ("Approved, waiting for a posting slot", [i for i in items if i["row"]["status"] == "approved"]),
                  ("Held: cannot post as it is", [i for i in items if i["row"]["status"] == "held"])]
        return page(request, "ready.html", groups=[g for g in groups if g[1]], total=len(items))

    @app.get("/posted", response_class=HTMLResponse, dependencies=[Depends(authed)])
    def posted(request: Request, c=Depends(conn)):
        posts = stats_view.post_rows(c)
        trend = stats_view.series(c)
        geo = stats_view.geo(c)["posts"]
        for p in posts:
            p["geo"] = geo.get(p["id"])
            p["spark"] = stats_view.spark(trend.get(p["id"], []))
        return page(request, "posted.html", posts=posts, counts=stats_view.review_counts(posts))

    @app.post("/post/{post_id}/review", dependencies=[Depends(authed)])
    def post_review(post_id: int, status: str = Form(...), c=Depends(conn)):
        if status not in ("pending", "approved", "rejected"):
            return back("/posted", "Unknown review status.")
        done = c.execute("UPDATE posts SET campaign_review_status=? WHERE id=?", (status, post_id)).rowcount
        return back("/posted", f"Post {post_id}: campaign review set to {status}." if done
                    else f"No post {post_id}.")

    @app.get("/learn", response_class=HTMLResponse, dependencies=[Depends(authed)])
    def learn(request: Request, c=Depends(conn)):
        niches = []
        for path in sorted(p for p in settings.rubrics_dir.glob("*.json")
                           if not p.stem.endswith(".draft")):
            rub = rubric_mod.load(path)
            changes = reweight_mod.propose(
                rub, reweight_mod.own_views(c, niche=path.stem, rubrics_dir=settings.rubrics_dir))
            niches.append({"niche": path.stem, "version": rub.version, "changes": changes,
                          "posts": sum(ch.n for ch in changes),
                          "movable": any(ch.new != ch.old for ch in changes)})
        return page(request, "learn.html", niches=niches, min_posts=reweight_mod.MIN_POSTS,
                    settle=reweight_mod.SETTLE_HOURS)

    @app.post("/learn/{niche}/apply", dependencies=[Depends(authed)])
    def learn_apply(niche: str, c=Depends(conn)):
        path = settings.rubrics_dir / f"{niche}.json"
        if not path.is_file():
            raise HTTPException(status_code=404)
        changes = reweight_mod.propose(
            rubric_mod.load(path), reweight_mod.own_views(c, niche=niche, rubrics_dir=settings.rubrics_dir))
        if not any(ch.new != ch.old for ch in changes):
            return back("/learn", "Nothing to change yet.")
        out = reweight_mod.apply(path, changes)
        return back("/learn", f"{niche}: rubric re-weighted, now version {out['version']}.")

    @app.get("/settings", response_class=HTMLResponse, dependencies=[Depends(authed)])
    def settings_page(request: Request, c=Depends(conn)):
        return page(request, "settings.html", values=stats_view.settings_map(c),
                    disk_floor=settings.disk_floor_gb)

    @app.post("/settings", dependencies=[Depends(authed)])
    def settings_save(review_mode: str = Form("review"), auto_threshold: str = Form("60"),
                      daily_cap: str = Form("3"), post_times: str = Form(""),
                      lkr_per_usd: str = Form("300"), prime_times: str = Form(""),
                      live_clips: str = Form("4"),
                      c=Depends(conn)):
        try:
            threshold, cap, lkr = float(auto_threshold), int(daily_cap), float(lkr_per_usd)
            live_n = int(live_clips)
        except ValueError:
            return back("/settings", "Threshold, cap and rate must be numbers.")
        if (review_mode not in ("review", "auto") or not 0 <= threshold <= 100
                or not 1 <= cap <= 8 or lkr <= 0 or not 0 <= live_n <= 10):
            return back("/settings", "Out of range: cap is 1-8, threshold 0-100.")
        times = [t.strip() for t in post_times.split(",") if t.strip()]
        if not all(len(t) == 5 and t[2] == ":" and t[:2].isdigit() and t[3:].isdigit()
                   for t in times):
            return back("/settings", "Times look like 10:00,14:00.")
        prime = [t.strip() for t in prime_times.split(",") if t.strip()]
        if not all(t in times for t in prime):
            return back("/settings", "Each best-clip time must be one of the posting times.")
        for key, val in (("review_mode", review_mode), ("auto_threshold", str(threshold)),
                         ("daily_cap", str(cap)), ("post_times", ",".join(times)),
                         ("lkr_per_usd", str(lkr)), ("prime_times", ",".join(prime)),
                         ("live_clips", str(live_n))):
            db.kv_set(c, f"setting_{key}", val)
        return back("/settings", "Saved.")

    # ---- clip actions --------------------------------------------------
    def _act(fn, clip_id: int, c, ok: str, **kw):
        try:
            fn(c, clip_id, **kw)
        except review.ReviewError as exc:
            return back("/ready", str(exc))
        return back("/ready", ok)

    @app.post("/post/{post_id}/submitted", dependencies=[Depends(authed)])
    def submitted(post_id: int, c=Depends(conn)):
        from ..publish import flow

        try:
            flow.mark_submitted(c, post_id)
        except ValueError as exc:
            return back("/posted", str(exc))
        return back("/posted", f"Post {post_id} marked as submitted.")

    @app.post("/clip/{clip_id}/approve", dependencies=[Depends(authed)])
    def approve(clip_id: int, c=Depends(conn)):
        return _act(review.approve, clip_id, c, f"Clip {clip_id} approved.")

    @app.post("/clip/{clip_id}/reject", dependencies=[Depends(authed)])
    def reject(clip_id: int, note: str = Form(""), c=Depends(conn)):
        return _act(review.reject, clip_id, c, f"Clip {clip_id} rejected.", note=note or None)

    @app.post("/clip/{clip_id}/restore", dependencies=[Depends(authed)])
    def restore(clip_id: int, c=Depends(conn)):
        return _act(review.restore, clip_id, c, f"Clip {clip_id} is back in the queue.")

    @app.get("/clip/{clip_id}/edit", response_class=HTMLResponse, dependencies=[Depends(authed)])
    def edit_form(request: Request, clip_id: int, c=Depends(conn)):
        try:
            row = review.get(c, clip_id)
        except review.ReviewError:
            raise HTTPException(404)
        return page(request, "edit.html", row=row, meta=review.meta_of(row),
                    title_max=review.TITLE_MAX)

    @app.post("/clip/{clip_id}/edit", dependencies=[Depends(authed)])
    def edit_save(clip_id: int, title: str = Form(""), description: str = Form(""),
                  c=Depends(conn)):
        try:
            review.edit(c, clip_id, title=title, description=description)
        except review.ReviewError as exc:
            return back(f"/clip/{clip_id}/edit", str(exc))
        return back("/ready", f"Clip {clip_id} text saved.")

    # ---- files: only paths recorded in the database -----------------------
    def _file(clip_id: int, column: str, media: str, c, name: str | None = None):
        row = c.execute(f"SELECT {column} AS p FROM clips WHERE id=?", (clip_id,)).fetchone()
        path = Path(row["p"]) if row and row["p"] else None
        if path is None or not path.is_file():
            raise HTTPException(404)
        return FileResponse(path, media_type=media, filename=name)

    @app.get("/clip/{clip_id}/video", dependencies=[Depends(authed)])
    def video(clip_id: int, c=Depends(conn)):
        return _file(clip_id, "path", "video/mp4", c)

    @app.get("/clip/{clip_id}/thumb", dependencies=[Depends(authed)])
    def thumb(clip_id: int, c=Depends(conn)):
        return _file(clip_id, "thumb_path", "image/jpeg", c)

    @app.get("/clip/{clip_id}/package", dependencies=[Depends(authed)])
    def package_zip(clip_id: int, c=Depends(conn)):
        return _file(clip_id, "package_path", "application/zip", c, f"clip-{clip_id}.zip")

    return app
