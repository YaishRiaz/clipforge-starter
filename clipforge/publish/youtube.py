"""Direct YouTube upload through the Data API (videos.insert, resumable).

Uploading needs a user OAuth token, not the read-only API key. The token is
obtained once by `youtube-auth` (loopback redirect with PKCE) and only the
long-lived refresh token is kept, in `.env`.

Nothing here logs or raises with a token, a client secret or a URL containing
one: errors carry the HTTP status and Google's own reason string only.
"""
from __future__ import annotations

import base64
import hashlib
import http.server
import json
import secrets
import threading
import urllib.parse
import webbrowser
from pathlib import Path
from typing import Callable

import httpx

from ..core.config import Settings

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"
# Upload, plus read-only analytics so the dashboard can show where viewers are.
# A token granted before the analytics scopes were added still uploads; the
# geography call then answers 403 and Yaish runs youtube-auth again.
SCOPE = " ".join(["https://www.googleapis.com/auth/youtube.upload",
                  "https://www.googleapis.com/auth/yt-analytics.readonly",
                  "https://www.googleapis.com/auth/youtube.readonly"])
REPORTS_URL = "https://youtubeanalytics.googleapis.com/v2/reports"
GAMING_CATEGORY = "20"
CHUNK = 8 * 1024 * 1024                 # must be a multiple of 256 KiB
TRANSIENT = {500, 502, 503, 504}


class YouTubeError(Exception):
    pass


def _reason(r: httpx.Response) -> str:
    try:
        err = r.json().get("error", {})
        if isinstance(err, dict):
            reasons = [e.get("reason", "") for e in err.get("errors", [])]
            return f"{r.status_code} {err.get('message', '')[:160]} {','.join(reasons)}".strip()
        return f"{r.status_code} {str(err)[:160]}"
    except ValueError:
        return str(r.status_code)


def configured(settings: Settings) -> bool:
    return bool(settings.youtube_oauth_client_id and settings.youtube_oauth_client_secret
                and settings.youtube_refresh_token)


# ---- one-time authorisation -------------------------------------------------

def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def build_auth_url(client_id: str, redirect_uri: str, challenge: str, state: str) -> str:
    q = {"client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code",
         "scope": SCOPE, "access_type": "offline", "prompt": "consent",
         "code_challenge": challenge, "code_challenge_method": "S256", "state": state}
    return f"{AUTH_URL}?{urllib.parse.urlencode(q)}"


def exchange_code(settings: Settings, code: str, verifier: str, redirect_uri: str) -> str:
    """Trade the one-time code for a refresh token."""
    try:
        r = httpx.post(TOKEN_URL, timeout=30, data={
            "client_id": settings.youtube_oauth_client_id,
            "client_secret": settings.youtube_oauth_client_secret,
            "code": code, "code_verifier": verifier, "redirect_uri": redirect_uri,
            "grant_type": "authorization_code"})
    except httpx.HTTPError as exc:
        raise YouTubeError(f"token exchange: {type(exc).__name__}") from None
    if r.status_code != 200:
        raise YouTubeError(f"token exchange: {r.json().get('error', r.status_code)}"
                           if r.headers.get("content-type", "").startswith("application/json")
                           else f"token exchange: {r.status_code}")
    token = r.json().get("refresh_token")
    if not token:
        raise YouTubeError("Google returned no refresh token; remove ClipForge at "
                           "myaccount.google.com/permissions and run youtube-auth again")
    return token


class _Catcher(http.server.BaseHTTPRequestHandler):
    result: dict = {}

    def do_GET(self) -> None:                                   # noqa: N802
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        type(self).result.update({k: v[0] for k, v in q.items()})
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"<h2>ClipForge is authorised. You can close this tab.</h2>")

    def log_message(self, *a) -> None:                          # keep the code out of any log
        pass


def authorize(settings: Settings, *, open_browser: bool = True, timeout: float = 300.0,
              say: Callable[[str], None] = print) -> str:
    """Run the browser consent flow and return the refresh token."""
    if not (settings.youtube_oauth_client_id and settings.youtube_oauth_client_secret):
        raise YouTubeError("set YOUTUBE_OAUTH_CLIENT_ID and YOUTUBE_OAUTH_CLIENT_SECRET in .env")
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(16)
    handler = type("Catcher", (_Catcher,), {"result": {}})
    server = http.server.HTTPServer(("127.0.0.1", 0), handler)
    redirect = f"http://127.0.0.1:{server.server_port}"
    url = build_auth_url(settings.youtube_oauth_client_id, redirect, challenge, state)
    server.timeout = 1.0
    say("Open this address in your browser, sign in with the channel's Google account and approve:")
    say(url)
    if open_browser:
        webbrowser.open(url)
    waited = 0.0
    done = threading.Event()
    while waited < timeout and not done.is_set():
        server.handle_request()
        waited += 1.0
        if handler.result.get("code") or handler.result.get("error"):
            done.set()
    server.server_close()
    res = handler.result
    if res.get("error"):
        raise YouTubeError(f"consent refused: {res['error']}")
    if not res.get("code"):
        raise YouTubeError("timed out waiting for the browser")
    if res.get("state") != state:
        raise YouTubeError("state mismatch; start again")
    return exchange_code(settings, res["code"], verifier, redirect)


# ---- tokens and upload ------------------------------------------------------

def access_token(settings: Settings) -> str:
    if not configured(settings):
        raise YouTubeError("YouTube upload is not set up: run youtube-auth")
    try:
        r = httpx.post(TOKEN_URL, timeout=30, data={
            "client_id": settings.youtube_oauth_client_id,
            "client_secret": settings.youtube_oauth_client_secret,
            "refresh_token": settings.youtube_refresh_token,
            "grant_type": "refresh_token"})
    except httpx.HTTPError as exc:
        raise YouTubeError(f"token refresh: {type(exc).__name__}") from None
    if r.status_code != 200:
        err = r.json().get("error") if r.headers.get("content-type", "").startswith(
            "application/json") else r.status_code
        hint = " (run youtube-auth again)" if err == "invalid_grant" else ""
        raise YouTubeError(f"token refresh: {err}{hint}")
    return r.json()["access_token"]


def snippet_body(title: str, description: str, tags: list[str], paid: bool = False) -> dict:
    body = {
        "snippet": {"title": title[:100], "description": description[:4900],
                    "tags": [t.lstrip("#") for t in tags][:15], "categoryId": GAMING_CATEGORY},
        "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False},
    }
    if paid:      # YouTube's own "includes paid promotion" label, for briefs that ask for it
        body["paidProductPlacementDetails"] = {"hasPaidProductPlacement": True}
    return body


def upload_parts(paid: bool = False) -> str:
    return "snippet,status,paidProductPlacementDetails" if paid else "snippet,status"


def _offset(r: httpx.Response) -> int:
    rng = r.headers.get("Range", "")
    return int(rng.rsplit("-", 1)[1]) + 1 if rng.startswith("bytes=") else 0


def _query(client: httpx.Client, session: str, size: int) -> tuple[int, dict | None]:
    """Where did the server get to? Returns (offset, finished-video-or-None)."""
    r = client.put(session, headers={"Content-Range": f"bytes */{size}", "Content-Length": "0"})
    if r.status_code in (200, 201):
        return size, r.json()
    if r.status_code == 308:
        return _offset(r), None
    if r.status_code in (404, 410):
        raise LookupError("upload session expired")
    raise YouTubeError(f"upload status check: {_reason(r)}")


def upload(settings: Settings, path: Path, *, title: str, description: str, tags: list[str],
           session: str | None = None, on_session: Callable[[str], None] | None = None,
           beat: Callable[[], None] | None = None, paid: bool = False) -> dict:
    """Upload one video and return YouTube's video resource (has "id").

    `session` is a resumable-upload address saved from an earlier attempt, so a
    crash halfway does not start a second upload. `on_session` receives a new
    address as soon as one exists so the caller can persist it.
    """
    size = path.stat().st_size
    token = access_token(settings)
    auth = {"Authorization": f"Bearer {token}"}
    with httpx.Client(timeout=httpx.Timeout(120.0, connect=30.0), headers=auth) as client:
        offset = 0
        if session:
            try:
                offset, finished = _query(client, session, size)
                if finished:
                    return finished
            except LookupError:
                session = None
        if not session:
            r = client.post(UPLOAD_URL, params={"uploadType": "resumable", "part": upload_parts(paid)},
                            headers={"X-Upload-Content-Length": str(size),
                                     "X-Upload-Content-Type": "video/mp4"},
                            json=snippet_body(title, description, tags, paid))
            if r.status_code != 200 or not r.headers.get("Location"):
                raise YouTubeError(f"upload start: {_reason(r)}")
            session = r.headers["Location"]
            if on_session:
                on_session(session)
            offset = 0
        failures = 0
        with path.open("rb") as fh:
            while True:
                fh.seek(offset)
                data = fh.read(CHUNK)
                end = offset + len(data) - 1
                try:
                    r = client.put(session, content=data, headers={
                        "Content-Range": f"bytes {offset}-{end}/{size}",
                        "Content-Type": "video/mp4"})
                except httpx.HTTPError:
                    r = None
                if r is not None and r.status_code in (200, 201):
                    return r.json()
                if r is not None and r.status_code == 308:
                    offset = _offset(r)
                    failures = 0
                    if beat:
                        beat()
                    continue
                if r is None or r.status_code in TRANSIENT:
                    failures += 1
                    if failures > 5:
                        raise YouTubeError("upload kept failing" if r is None else
                                           f"upload: {_reason(r)}")
                    offset, finished = _query(client, session, size)
                    if finished:
                        return finished
                    continue
                raise YouTubeError(f"upload: {_reason(r)}")


class NeedsConsent(YouTubeError):
    """The saved sign-in does not cover analytics, or the Analytics API is off."""


def views_by_country(token: str, video_id: str | None, start: str, end: str, *, get=None) -> dict[str, int]:
    """Views per country (ISO code) from the channel's own analytics: one video, or with
    `video_id` None the whole channel. YouTube leaves out countries (and whole videos)
    with too few views, so a small video often comes back empty."""
    params = {"ids": "channel==MINE", "startDate": start, "endDate": end, "metrics": "views",
              "dimensions": "country", "sort": "-views", "maxResults": 50}
    if video_id:
        params["filters"] = f"video=={video_id}"
    try:
        r = (get or httpx.get)(REPORTS_URL, timeout=30, params=params,
                               headers={"Authorization": f"Bearer {token}"})
    except httpx.HTTPError as exc:
        raise YouTubeError(f"analytics: {type(exc).__name__}") from None
    if r.status_code in (401, 403):
        raise NeedsConsent(f"analytics: {_reason(r)}")
    if r.status_code != 200:
        raise YouTubeError(f"analytics: {_reason(r)}")
    return {str(row[0]).upper(): int(row[1]) for row in r.json().get("rows") or []}


def lookup(settings: Settings, video_ids: list[str]) -> dict[str, dict]:
    """Public details for videos, read with the API key (no OAuth, no audit)."""
    out: dict[str, dict] = {}
    for i in range(0, len(video_ids), 50):
        batch = video_ids[i:i + 50]
        try:
            r = httpx.get(VIDEOS_URL, timeout=30,
                          headers={"X-Goog-Api-Key": settings.youtube_api_key},
                          params={"part": "snippet,status,statistics", "id": ",".join(batch)})
        except httpx.HTTPError as exc:
            raise YouTubeError(f"videos.list: {type(exc).__name__}") from None
        if r.status_code != 200:
            raise YouTubeError(f"videos.list: {_reason(r)}")
        for item in r.json().get("items", []):
            out[item["id"]] = item
    return out
