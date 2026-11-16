"""vision.mode "candidates": the picture is only checked at existing candidates."""
from src import score, vision
from src.models import Moment, VisionFrame

CFG = {"analyze": {"min_clip_seconds": 20, "max_clip_seconds": 45, "max_clips": 10},
       "vision": {"highlight_threshold": 0.5, "interval_seconds": 15, "categories": ["action"]},
       "score": {"weights": {"chat": 0.3, "audio": 0.2, "text": 0.4, "video": 0.1}}}


def _text(start, end, s=0.8):
    return Moment(start=start, end=end, category="funny", score=s, title="t", reason="r")


def test_peak_time_prefers_chat_then_audio_then_middle():
    m = _text(100, 140)
    chat, audio = [(120.0, 0.9)], [(110.0, 0.7)]
    assert score.peak_time(m, chat, audio) == 120.0
    assert score.peak_time(m, [], audio) == 110.0
    assert score.peak_time(m, [(120.0, 0.0)], []) == 120.0   # middle: an empty chat bucket is no peak
    assert score.peak_time(m, [(500.0, 1.0)], []) == 120.0   # peaks outside the window do not count


def test_targeted_frames_score_windows_but_spawn_no_candidates():
    text = [_text(100, 130)]
    frames = [VisionFrame(115.0, {"action": 0.9}, 0.9, "explosion")]
    with_dupes = score.fuse(text, frames, [], [], CFG)
    targeted = score.fuse(text, frames, [], [], CFG, spawn_video=False)
    assert len(targeted) == 1 and targeted[0].video_score == 0.9 and targeted[0].title == "t"
    # same single window either way (the grid path dedupes the video twin away)
    assert [(m.start, m.end) for m in with_dupes] == [(100, 130)]
    # a hot frame away from every candidate creates one only on the grid path
    far = [VisionFrame(900.0, {"action": 0.9}, 0.9, "crash")]
    assert len(score.fuse(text, far, [], [], CFG, spawn_video=False)) == 1
    assert len(score.fuse(text, far, [], [], CFG)) == 2


def test_run_candidates_scores_peaks_and_caches(tmp_path, monkeypatch):
    calls = []

    def fake_confirm(source, work, peaks, **kw):
        calls.append(list(peaks))
        res = {0: (0.7, {"action": 0.7}, "fight"), 2: (0.1, {"action": 0.1}, "")}   # peak 1 unanswered
        return {i: r for i, r in res.items() if i < len(peaks)}

    monkeypatch.setattr(vision, "confirm_windows", fake_confirm)
    cfg = {"vision": {"categories": ["action"], "model": "m"}}
    out = vision.run_candidates(tmp_path / "v.mp4", tmp_path, [10.04, 50.0, 90.0], cfg, log=lambda *_: None)
    assert [(f.t, f.visual_score, f.label) for f in out] == [(10.0, 0.7, "fight"), (90.0, 0.1, "")]
    again = vision.run_candidates(tmp_path / "v.mp4", tmp_path, [10.04, 50.0, 90.0], cfg, log=lambda *_: None)
    assert len(calls) == 1 and [f.t for f in again] == [10.0, 90.0]          # second call from cache
    vision.run_candidates(tmp_path / "v.mp4", tmp_path, [10.0, 60.0], cfg, log=lambda *_: None)
    assert len(calls) == 2                                                   # other peaks: rebuilt


def test_numeric_labels_are_dropped():
    assert [vision._label(x) for x in ("0.1", " 12 % ", "3000000000000000", None)] == ["", "", "", ""]
    assert vision._label("Streamer reacting to chat") == "Streamer reacting to chat"
