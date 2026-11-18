"""Finding the facecam in a clip's own footage, and acting on a 'no facecam' reply."""
from __future__ import annotations

import dataclasses
import json
from pathlib import Path

from clipforge.app import redo
from clipforge.core import media
from clipforge.core.config import get_settings
from clipforge.edit import camfind, compliance, layout
from clipforge.edit.camfind import CamReport, FrameCam
from clipforge.edit.layout import Box


def ov(left, top, width, height):
    return FrameCam(kind="overlay", left=left, top=top, width=width, height=height)


def test_agreeing_boxes_become_an_overlay_and_thousandths_are_understood():
    rep = CamReport(frames=[ov(0.01, 0.70, 0.23, 0.25), ov(10, 705, 232, 250), ov(0.01, 0.71, 0.23, 0.24)])
    d = camfind.decide(rep, 1920, 1080)
    assert d.kind == "overlay" and d.box.x < 40 and 740 < d.box.y < 780 and 400 < d.box.w < 470


def test_the_hand_measured_box_is_kept_when_it_matches():
    known = Box(1480, 774, 412, 232)
    rep = CamReport(frames=[ov(0.77, 0.72, 0.21, 0.22)] * 3)
    d = camfind.decide(rep, 1920, 1080, known)
    assert d.kind == "overlay" and d.box == known
    d = camfind.decide(CamReport(frames=[ov(0.01, 0.70, 0.23, 0.25)] * 3), 1920, 1080, known)
    assert d.box != known and d.box.x < 40


def test_full_screen_none_and_disagreement():
    full = FrameCam(kind="full_screen")
    none = FrameCam(kind="none")
    assert camfind.decide(CamReport(frames=[full, full, none]), 1920, 1080).kind == "full_screen"
    assert camfind.decide(CamReport(frames=[none, none, full]), 1920, 1080).kind == "none"
    assert camfind.decide(CamReport(frames=[ov(0.0, 0.0, 0.2, 0.2), ov(0.5, 0.5, 0.2, 0.2), none]),
                          1920, 1080).kind == "unknown"
    assert camfind.decide(CamReport(frames=[]), 1920, 1080).kind == "unknown"
    # a "camera" that is half the screen is a scene, not an overlay
    assert camfind._to_box(ov(0.0, 0.0, 0.9, 0.9), 1920, 1080) is None


def _video(tmp_path: Path) -> Path:
    out = tmp_path / "v.mp4"
    media.run([media._tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
               "-i", "color=c=gray:s=640x360:d=6:r=10", "-pix_fmt", "yuv420p", str(out)])
    return out


class Reply:
    status_code = 200

    def __init__(self, frames):
        self._f = frames

    def json(self):
        return {"choices": [{"message": {"content": json.dumps({"frames": self._f})}}]}


def test_locate_reads_three_frames_and_never_raises(tmp_path):
    s = dataclasses.replace(get_settings(), gemini_api_key="k")
    f = _video(tmp_path)
    box = {"kind": "overlay", "left": 0.0, "top": 0.7, "width": 0.25, "height": 0.25}
    sent = []

    def post(body):
        sent.append(sum(1 for c in body["messages"][0]["content"] if c["type"] == "image_url"))
        return Reply([box, box, box])

    d = camfind.locate(f, ss=0.0, duration=5.0, settings=s, work=tmp_path, post=post)
    assert d.kind == "overlay" and d.box.y == 252 and sent == [3]
    assert not list((tmp_path / "camframes").glob("*.jpg"))
    d = camfind.locate(f, ss=0.0, duration=5.0, settings=s, work=tmp_path, post=post,
                       points=camfind.WIDE_POINTS)
    assert sent == [3, 5]
    broke = camfind.locate(f, ss=0.0, duration=5.0,
                           settings=dataclasses.replace(s, gemini_api_key=""), work=tmp_path)
    assert broke.kind == "unknown" and "GEMINI" in broke.note


def test_a_camera_in_the_wrong_place_is_not_passed_off_as_required_cam():
    base = layout.Layout("x", 1920, 1080, None, True, None)
    kw = dict(rules=None, meta={}, duration=30, width=1080, height=1920,
              has_audio=True, overlay_burned=True, hook="", spoken="")

    def fails(lay):
        return any(f.code == "facecam" and f.level == "FAIL" for f in compliance.check(layout=lay, **kw))

    assert fails(base)
    assert not fails(dataclasses.replace(base, cam_full=True))
    f = [x for x in compliance.check(layout=dataclasses.replace(base, cam_note="no facecam is visible"), **kw)
         if x.code == "facecam"]
    assert "no facecam is visible" in str(f[0])


def test_a_note_about_the_camera_asks_for_a_new_search_without_the_model():
    assert redo.interpret(None, None, 1, "the facecam is not available").recheck_cam
    assert redo._fallback("no face cam in this").recheck_cam
    assert not redo._fallback("cut the last 3 seconds").recheck_cam
    assert "facecam" in redo.describe(redo.Edit(recheck_cam=True))


def test_clip_layout_searches_once_caches_and_applies_the_answer(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from clipforge.core import handlers

    calls = []

    def fake(footage, **kw):
        calls.append(kw["points"])
        return camfind.Detection("overlay", Box(14, 760, 450, 252), "3 of 3 frames agree")

    monkeypatch.setattr(camfind, "locate", fake)
    from clipforge.core import db

    ctx = SimpleNamespace(settings=get_settings(), conn=db.open_db(tmp_path / "t.db"))
    moment = {"rubric_category": "funny"}
    source = {"creator": "skiimasktimmy", "url": "https://www.twitch.tv/videos/1"}
    campaign = {"creator": "skiimasktimmy", "id": 1, "platform": "whop"}
    plan = {"footage": "f.mp4", "start": 10.0, "end": 40.0, "offset": 0.0}
    lay = handlers._clip_layout(ctx, tmp_path, moment, source, campaign, plan, search=True)
    assert lay.facecam == Box(14, 760, 450, 252) and calls == [camfind.POINTS]
    again = handlers._clip_layout(ctx, tmp_path, moment, source, campaign, plan, search=False)
    assert again.facecam == lay.facecam and len(calls) == 1
    other = tmp_path / "b"
    other.mkdir()
    (other / "override.json").write_text(json.dumps({"start": 10, "end": 40, "recam": True}))
    handlers._clip_layout(ctx, other, moment, source, campaign, plan, search=True)
    assert calls[-1] == camfind.WIDE_POINTS
    monkeypatch.setattr(camfind, "locate", lambda *a, **k: camfind.Detection("full_screen", None, "x"))
    third = tmp_path / "c"
    third.mkdir()
    full = handlers._clip_layout(ctx, third, moment, source, campaign, plan, search=True)
    assert full.facecam is None and full.cam_full
    monkeypatch.setattr(camfind, "locate", lambda *a, **k: camfind.Detection("unknown", None, "x"))
    fourth = tmp_path / "d"
    fourth.mkdir()
    fb = handlers._clip_layout(ctx, fourth, moment, source, campaign, plan, search=True)
    assert fb.facecam is not None and not (fourth / "camfind.json").exists()    # layout file's box, retried later


def test_a_stream_without_a_saved_layout_is_still_searched_for_a_facecam(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from clipforge.core import handlers

    calls = []
    monkeypatch.setattr(camfind, "locate", lambda footage, **kw: calls.append(1) or camfind.Detection(
        "overlay", Box(8, 150, 194, 106), "3 of 3 frames agree"))
    from clipforge.core import db

    ctx = SimpleNamespace(settings=get_settings(), conn=db.open_db(tmp_path / "t.db"))
    moment = {"rubric_category": "funny"}
    plan = {"footage": "f.mp4", "start": 10.0, "end": 40.0, "offset": 0.0}
    camp = {"creator": "replays", "id": 1, "platform": "contentrewards"}
    stream = {"creator": "replays", "url": "https://www.twitch.tv/videos/2929917089"}
    lay = handlers._clip_layout(ctx, tmp_path, moment, stream, camp, plan, search=True)
    assert lay.facecam == Box(8, 150, 194, 106) and calls == [1]
    other = tmp_path / "own"
    other.mkdir()
    own = handlers._clip_layout(ctx, other, moment, {"creator": "me", "url": None},
                                {"creator": "me", "id": 2, "platform": "own"}, plan, search=True)
    assert own.facecam is None and calls == [1]                 # own footage: no search


def test_a_box_taller_than_wide_is_trimmed_to_a_camera_shape():
    assert camfind.camera_shaped(Box(18, 342, 434, 470)) == Box(18, 342, 434, 244)   # cam plus a panel below
    assert camfind.camera_shaped(Box(12, 114, 254, 200)) == Box(12, 114, 254, 200)   # a real 4:3 cam stays
