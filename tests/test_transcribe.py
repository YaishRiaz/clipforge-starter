"""Phase 2 transcription: chunking, timestamp offsets, and the failure modes.

Nothing here touches the network. The chunking test does run ffmpeg, because
the whole point of it is that a 5-hour file becomes uploads under Groq's 25 MB
limit, and that is a property of the real conversion.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from clipforge.core import media
from clipforge.core.config import get_settings
from clipforge.moments import transcribe


@pytest.fixture()
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIPFORGE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("GROQ_API_KEY", "test-key-not-real")
    monkeypatch.setenv("TRANSCRIBE_BACKEND", "groq")
    get_settings.cache_clear()
    s = get_settings()
    s.ensure_dirs()
    yield s
    get_settings.cache_clear()


def fake_audio(path: Path, seconds: int) -> Path:
    """A real audio file, so ffmpeg is exercised rather than mocked."""
    media.run([
        media._tool("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"sine=frequency=300:sample_rate=44100:duration={seconds}",
        "-c:a", "aac", str(path),
    ], timeout=300)
    return path


def stub_post(monkeypatch, replies):
    """Serve `replies` (status, payload) in order, recording each upload."""
    import httpx

    sent: list[dict] = []
    queue = list(replies)

    class FakeResponse:
        def __init__(self, status, payload):
            self.status_code = status
            self._payload = payload
            self.text = json.dumps(payload) if isinstance(payload, dict) else str(payload)
            # Real responses carry these; the code reads them to learn the quota.
            self.headers = {"content-type": "application/json",
                            "x-ratelimit-remaining-audio-seconds": "6600"}

        def json(self):
            if not isinstance(self._payload, dict):
                raise ValueError("not json")
            return self._payload

    def fake_post(url, *, headers=None, files=None, data=None, timeout=None):
        name, handle, mime = files["file"]
        sent.append({"url": url, "name": name, "mime": mime, "data": dict(data),
                     "bytes": len(handle.read()), "headers": headers})
        status, payload = queue.pop(0) if queue else (200, {"segments": []})
        return FakeResponse(status, payload)

    monkeypatch.setattr(httpx, "post", fake_post)
    return sent


def reply(*spans):
    return 200, {"segments": [{"start": s, "end": e, "text": t} for s, e, t in spans]}


# --- chunking ---------------------------------------------------------------

def test_converts_to_16k_mono_flac_and_splits(tmp_path):
    src = fake_audio(tmp_path / "in.m4a", 25)
    chunks = transcribe.prepare_chunks(src, tmp_path / "work", chunk_seconds=10)
    assert len(chunks) == 3                       # 10 + 10 + 5
    assert [round(offset) for _p, offset in chunks] == [0, 10, 20]
    # The segment muxer writes FLAC with no total-sample count, so a chunk has
    # no readable duration. Check what ffprobe CAN see instead.
    out = media.run([
        media._tool("ffprobe"), "-v", "error", "-select_streams", "a:0",
        "-show_entries", "stream=sample_rate,channels,codec_name",
        "-of", "csv=p=0", str(chunks[0][0]),
    ])
    assert out.strip() == "flac,16000,1"


def test_a_trailing_sliver_chunk_is_dropped(tmp_path):
    """ffmpeg rounds up, and Groq bills a 10 second minimum per request."""
    src = fake_audio(tmp_path / "in.m4a", 20)
    chunks = transcribe.prepare_chunks(src, tmp_path / "work", chunk_seconds=10)
    assert len(chunks) == 2
    assert list((tmp_path / "work").glob("chunk*.flac")) == [p for p, _ in chunks]


def test_audio_shorter_than_the_floor_is_refused(tmp_path):
    src = fake_audio(tmp_path / "tiny.m4a", 1)
    with pytest.raises(transcribe.TranscriptionFailed, match="too short"):
        transcribe.prepare_chunks(src, tmp_path / "work", chunk_seconds=600,
                                  duration=0.2)


def test_every_chunk_fits_groq_free_tier(tmp_path):
    """The reason chunking exists: 25 MB a file, and a VOD is 519 MB."""
    src = fake_audio(tmp_path / "in.m4a", 60)
    chunks = transcribe.prepare_chunks(src, tmp_path / "work", chunk_seconds=30)
    for path, _offset in chunks:
        assert path.stat().st_size < transcribe.MAX_UPLOAD_BYTES


def test_chunking_is_idempotent(tmp_path):
    """A re-run after a crash must not leave stale chunks behind."""
    src = fake_audio(tmp_path / "in.m4a", 25)
    work = tmp_path / "work"
    first = transcribe.prepare_chunks(src, work, chunk_seconds=10)
    second = transcribe.prepare_chunks(src, work, chunk_seconds=30)
    assert len(first) == 3 and len(second) == 1
    assert len(list(work.glob("chunk*.flac"))) == 1


def test_estimate_chunks_for_a_real_vod():
    # The 5h45 VOD Yaish chose.
    assert transcribe.estimate_chunks(20730, 600) == 35


# --- the hosted path --------------------------------------------------------

def test_offsets_timestamps_by_chunk_start(tmp_path, settings, monkeypatch):
    """Each chunk is timed from its own zero, so offsets must be re-added."""
    src = fake_audio(tmp_path / "in.m4a", 25)
    monkeypatch.setenv("TRANSCRIBE_CHUNK_SECONDS", "10")
    get_settings.cache_clear()
    s = get_settings()
    sent = stub_post(monkeypatch, [
        reply((1.0, 4.0, "first chunk")),
        reply((2.0, 5.0, "second chunk")),
        reply((0.5, 2.0, "third chunk")),
    ])
    segments = transcribe.transcribe_groq(src, s, work_dir=tmp_path / "w")
    assert [(sg.start, sg.text) for sg in segments] == [
        (1.0, "first chunk"), (12.0, "second chunk"), (20.5, "third chunk"),
    ]
    assert len(sent) == 3
    assert sent[0]["data"]["model"] == transcribe.GROQ_MODEL
    assert sent[0]["data"]["response_format"] == "verbose_json"
    assert sent[0]["data"]["timestamp_granularities[]"] == "segment"
    assert sent[0]["headers"]["Authorization"] == "Bearer test-key-not-real"
    assert sent[0]["mime"] == "audio/flac"


def test_chunks_are_deleted_as_they_are_sent(tmp_path, settings, monkeypatch):
    """A five-hour stream must not pile up converted audio on a full disk."""
    src = fake_audio(tmp_path / "in.m4a", 20)
    monkeypatch.setenv("TRANSCRIBE_CHUNK_SECONDS", "10")
    get_settings.cache_clear()
    work = tmp_path / "w"
    stub_post(monkeypatch, [reply((0.0, 1.0, "a")), reply((0.0, 1.0, "b"))])
    transcribe.transcribe_groq(src, get_settings(), work_dir=work)
    assert list(work.glob("chunk*.flac")) == []


def test_a_silent_chunk_is_not_a_failure(tmp_path, settings, monkeypatch):
    src = fake_audio(tmp_path / "in.m4a", 8)
    stub_post(monkeypatch, [(200, {"segments": [], "text": ""})])
    assert transcribe.transcribe_groq(src, settings, work_dir=tmp_path / "w") == []


def test_segments_without_usable_times_are_skipped(tmp_path, settings, monkeypatch):
    src = fake_audio(tmp_path / "in.m4a", 8)
    stub_post(monkeypatch, [(200, {"segments": [
        {"start": 0.0, "end": 1.0, "text": "kept"},
        {"start": None, "end": 2.0, "text": "no start"},
        {"end": 3.0, "text": "missing start"},
        {"start": 3.0, "end": 4.0, "text": "   "},
    ]})])
    segments = transcribe.transcribe_groq(src, settings, work_dir=tmp_path / "w")
    assert [s.text for s in segments] == ["kept"]


@pytest.mark.parametrize("status, exc, match", [
    (401, transcribe.TranscriptionUnavailable, "rejected"),
    (413, transcribe.TranscriptionFailed, "too large"),
    (429, transcribe.TranscriptionFailed, "rate limited"),
    (500, transcribe.TranscriptionFailed, "HTTP 500"),
])
def test_http_errors_are_distinguished(tmp_path, settings, monkeypatch, status, exc, match):
    """401 must be Unavailable so the job holds for a human, not retries."""
    src = fake_audio(tmp_path / "in.m4a", 8)
    stub_post(monkeypatch, [(status, {"error": "nope"})])
    with pytest.raises(exc, match=match):
        transcribe.transcribe_groq(src, settings, work_dir=tmp_path / "w")


def test_an_oversized_chunk_is_refused_before_upload(tmp_path, settings, monkeypatch):
    big = tmp_path / "big.flac"
    big.write_bytes(b"0" * (transcribe.MAX_UPLOAD_BYTES + 1))
    with pytest.raises(transcribe.TranscriptionFailed, match="TRANSCRIBE_CHUNK_SECONDS"):
        transcribe.transcribe_chunk_groq(big, settings)


def test_no_key_holds_rather_than_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIPFORGE_DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("GROQ_API_KEY", "")
    monkeypatch.setenv("TRANSCRIPTION_API_KEY", "")
    get_settings.cache_clear()
    chunk = tmp_path / "c.flac"
    chunk.write_bytes(b"x")
    with pytest.raises(transcribe.TranscriptionUnavailable, match="GROQ_API_KEY"):
        transcribe.transcribe_chunk_groq(chunk, get_settings())
    get_settings.cache_clear()


def test_a_dedicated_transcription_key_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIPFORGE_DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("GROQ_API_KEY", "groq-key")
    monkeypatch.setenv("TRANSCRIPTION_API_KEY", "dedicated-key")
    get_settings.cache_clear()
    assert transcribe._groq_key(get_settings()) == "dedicated-key"
    get_settings.cache_clear()


# --- backend selection ------------------------------------------------------

def test_backend_selection_reports_which_ran(tmp_path, settings, monkeypatch):
    src = fake_audio(tmp_path / "in.m4a", 8)
    stub_post(monkeypatch, [reply((0.0, 1.0, "hi"))])
    _segments, backend = transcribe.transcribe(src, settings, work_dir=tmp_path / "w")
    assert "groq" in backend and transcribe.GROQ_MODEL in backend


def test_an_unknown_backend_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIPFORGE_DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("TRANSCRIBE_BACKEND", "telepathy")
    get_settings.cache_clear()
    with pytest.raises(transcribe.TranscriptionUnavailable, match="telepathy"):
        transcribe.transcribe(tmp_path / "nope.m4a", get_settings())
    get_settings.cache_clear()


def test_there_is_no_silent_failover_between_backends(tmp_path, settings, monkeypatch):
    """Where licensed audio may go is a decision, not a runtime fallback."""
    src = fake_audio(tmp_path / "in.m4a", 8)
    stub_post(monkeypatch, [(500, {"error": "down"})])
    called = []
    monkeypatch.setattr(transcribe, "transcribe_local",
                        lambda *a, **k: called.append(1) or [])
    with pytest.raises(transcribe.TranscriptionFailed):
        transcribe.transcribe(src, settings, work_dir=tmp_path / "w")
    assert called == []


def test_output_matches_the_caption_segment_shape(tmp_path, settings, monkeypatch):
    """Everything downstream reads one shape, whatever produced it."""
    from clipforge.ingest import captions as caps

    src = fake_audio(tmp_path / "in.m4a", 8)
    stub_post(monkeypatch, [reply((0.0, 2.0, "spoken words"))])
    segments, _ = transcribe.transcribe(src, settings, work_dir=tmp_path / "w")
    assert all(isinstance(s, caps.Segment) for s in segments)
    saved = caps.save(segments, tmp_path / "t.json", source="groq")
    assert [s.text for s in caps.load(saved)] == ["spoken words"]


# --- rate limit reporting ---------------------------------------------------
# Groq does not document the free tier's audio allowance and a five-hour VOD is
# 35 requests, so what the provider reports as it goes is the only way to know
# whether a run will finish.

def test_rate_limit_headers_are_picked_out():
    found = transcribe.log_rate_limits({
        "x-ratelimit-limit-audio-seconds": "7200",
        "x-ratelimit-remaining-audio-seconds": "6600",
        "retry-after": "30",
        "content-type": "application/json",
    })
    assert found == {
        "x-ratelimit-limit-audio-seconds": "7200",
        "x-ratelimit-remaining-audio-seconds": "6600",
        "retry-after": "30",
    }


def test_headers_without_limits_are_fine():
    assert transcribe.log_rate_limits({"content-type": "application/json"}) == {}


def test_unreadable_headers_do_not_break_a_transcription():
    class Hostile:
        def items(self):
            raise RuntimeError("no headers for you")

    assert transcribe.log_rate_limits(Hostile()) == {}


# --- checkpointing ----------------------------------------------------------
# 35 requests for a five-hour VOD. A rate limit at chunk 30 must not throw away
# the first 29 and spend the free tier's allowance on them a second time.

def test_a_finished_chunk_is_not_sent_twice(tmp_path, settings, monkeypatch):
    src = fake_audio(tmp_path / "in.m4a", 25)
    monkeypatch.setenv("TRANSCRIBE_CHUNK_SECONDS", "10")
    get_settings.cache_clear()
    work = tmp_path / "w"

    # First run: the third chunk is rate limited, so two are banked.
    stub_post(monkeypatch, [reply((0.0, 2.0, "one")), reply((0.0, 2.0, "two")),
                            (429, {"error": "slow down"})])
    with pytest.raises(transcribe.TranscriptionFailed, match="rate limited"):
        transcribe.transcribe_groq(src, get_settings(), work_dir=work)
    assert len(list(work.glob("chunk*.json"))) == 2

    # Second run: only the chunk that never succeeded is sent.
    sent = stub_post(monkeypatch, [reply((0.0, 2.0, "three"))])
    segments = transcribe.transcribe_groq(src, get_settings(), work_dir=work)
    assert len(sent) == 1
    assert [s.text for s in segments] == ["one", "two", "three"]
    assert [s.start for s in segments] == [0.0, 10.0, 20.0]


def test_checkpoints_are_cleared_once_the_file_is_through(tmp_path, settings, monkeypatch):
    src = fake_audio(tmp_path / "in.m4a", 20)
    monkeypatch.setenv("TRANSCRIBE_CHUNK_SECONDS", "10")
    get_settings.cache_clear()
    work = tmp_path / "w"
    stub_post(monkeypatch, [reply((0.0, 1.0, "a")), reply((0.0, 1.0, "b"))])
    transcribe.transcribe_groq(src, get_settings(), work_dir=work)
    assert list(work.glob("chunk*.json")) == []
    assert list(work.glob("chunk*.flac")) == []


def test_an_unreadable_checkpoint_is_redone(tmp_path, settings, monkeypatch):
    src = fake_audio(tmp_path / "in.m4a", 12)
    monkeypatch.setenv("TRANSCRIBE_CHUNK_SECONDS", "10")
    get_settings.cache_clear()
    work = tmp_path / "w"
    work.mkdir(parents=True)
    (work / "chunk00000.json").write_text("{not json", encoding="utf-8")
    sent = stub_post(monkeypatch, [reply((0.0, 1.0, "redone")), reply((0.0, 1.0, "b"))])
    segments = transcribe.transcribe_groq(src, get_settings(), work_dir=work)
    assert len(sent) == 2
    assert segments[0].text == "redone"
