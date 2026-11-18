import dataclasses
import json
import subprocess

from clipforge.core import media
from clipforge.core.config import get_settings
from clipforge.edit import framecheck


def _video(tmp_path):
    f = tmp_path / "c.mp4"
    subprocess.run([media._tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", "testsrc=size=270x480:rate=30:duration=6", "-c:v", "libx264", str(f)], check=True)
    return f


class Reply:
    status_code = 200

    def __init__(self, data):
        self._d = data

    def json(self):
        return {"choices": [{"message": {"content": json.dumps(self._d)}}]}


def test_reports_what_the_model_saw_and_cleans_up(tmp_path):
    s = dataclasses.replace(get_settings(), gemini_api_key="k")
    post = lambda body: Reply({"caption_overlap": True, "camera_missing": True, "seconds": [2.0]})
    out = framecheck.check(_video(tmp_path), settings=s, work=tmp_path, cam_expected=True, post=post)
    assert out[0].startswith("two caption lines") and "2 s" in out[0]
    assert any("webcam" in x for x in out)
    assert not list((tmp_path / "framecheck").glob("*.jpg"))


def test_camera_missing_is_ignored_when_no_cam_is_expected(tmp_path):
    s = dataclasses.replace(get_settings(), gemini_api_key="k")
    out = framecheck.check(_video(tmp_path), settings=s, work=tmp_path, cam_expected=False,
                           post=lambda b: Reply({"camera_missing": True}))
    assert out == []


def test_no_key_or_bad_answer_is_silent(tmp_path):
    f = _video(tmp_path)
    s = dataclasses.replace(get_settings(), gemini_api_key="")
    assert framecheck.check(f, settings=s, work=tmp_path, cam_expected=True) == []
    s = dataclasses.replace(s, gemini_api_key="k")

    def bad(body):
        raise RuntimeError("boom")

    assert framecheck.check(f, settings=s, work=tmp_path, cam_expected=True, post=bad) == []
