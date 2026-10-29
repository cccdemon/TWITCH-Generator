"""Chat stage: Twitch chat replay -> per-bucket hype score.

Chat velocity is the strongest free highlight signal on Twitch: the audience has
already marked the moment. We count weighted messages per bucket (emote spam,
laughter and "clip it" count extra), shift the timeline back by the reaction
lag, and normalise against a rolling baseline so a sleepy 20-viewer chat and a
5k-viewer chat both yield 0..1.

Raw messages are cached in data/<vod>/chat_messages.json so re-runs and the
web UI can reuse them without hitting Twitch again.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .signals import Signal, bucketize, relative

_CACHE_VERSION = 1

# Lower-cased tokens that mark a reaction rather than normal talk.
_HYPE = {
    "kekw", "lul", "lulw", "omegalul", "icant", "kek", "lmao", "lmfao", "rofl",
    "pog", "poggers", "pogchamp", "pogu", "pogo", "letsgo", "hype", "ez", "gg",
    "wtf", "omg", "holy", "nein", "neeein", "noo", "nooo", "rip", "f",
    "monkas", "monkaw", "pepelaugh", "pepega", "sadge", "pepehands", "catjam",
    "ayaya", "widepeepohappy", "peepoclap", "clap", "w", "insane", "krank",
    "geil", "wahnsinn", "alter", "digga", "bruh", "lol", "xd", "haha",
}
_LAUGH_RE = re.compile(r"(?:ha){2,}|(?:he){3,}|x+d+|l+o+l+|k+e+k+|w+t+f+|o+m+g+", re.I)
_CLIP_RE = re.compile(r"\bclip\b|\bclipp?e?n?\b|\bclip it\b|\bclip that\b", re.I)


def message_weight(text: str) -> float:
    """1 for a plain message, more for reactions; 'clip it' is the loudest vote."""
    t = text.strip()
    if not t:
        return 0.0
    w = 1.0
    low = t.lower()
    words = re.findall(r"[a-zäöüß0-9]+", low)
    if any(wd in _HYPE for wd in words):
        w += 1.5
    elif _LAUGH_RE.search(low):
        w += 1.0
    if _CLIP_RE.search(low):
        w += 3.0
    if len(t) <= 3 and not words:        # "?", "!!!", pure punctuation spam
        w += 0.5
    return w


def _download(vod_id: str) -> list[tuple[float, str]]:
    """Pull the chat replay via chat-downloader (public GQL, no auth needed)."""
    from chat_downloader import ChatDownloader  # imported lazily: optional dep

    url = f"https://www.twitch.tv/videos/{vod_id}"
    out: list[tuple[float, str]] = []
    chat = ChatDownloader().get_chat(url, message_groups=["messages"])
    for m in chat:
        t = m.get("time_in_seconds")
        text = m.get("message") or ""
        if t is None:
            continue
        out.append((round(float(t), 2), str(text)[:200]))
    return out


def load_messages(work: Path, vod_id: str, *, log=print) -> list[tuple[float, str]]:
    """Cached chat messages [(t, text)], downloading on first use. Best-effort."""
    cache = work / "chat_messages.json"
    if cache.exists():
        try:
            return [(float(t), str(x)) for t, x in json.loads(cache.read_text(encoding="utf-8"))]
        except (json.JSONDecodeError, OSError, ValueError, TypeError):
            pass
    try:
        msgs = _download(vod_id)
    except Exception as e:  # noqa: BLE001 — chat is optional; never kill the pipeline
        log(f"[yellow]chat replay unavailable ({type(e).__name__}: {e}) — skipping chat signal")
        return []
    if msgs:  # an empty replay is not cached, so a later retry can still fetch it
        try:
            cache.write_text(json.dumps(msgs, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
    return msgs


def score_messages(
    msgs: list[tuple[float, str]], *, interval: float, lag: float,
    baseline_seconds: float, scale: float = 2.0, floor: float = 2.0,
) -> Signal:
    """Weighted message rate per bucket, lag-shifted, relative to rolling median.

    score 1.0 == (1 + scale)x the local baseline rate.
    """
    if not msgs:
        return []
    samples = [(max(0.0, t - lag), message_weight(text)) for t, text in msgs]
    grid = bucketize(samples, interval, reduce=lambda a, b: a + b, empty=0.0)
    return relative(grid, interval=interval, baseline_seconds=baseline_seconds,
                    scale=scale, floor=floor, mode="ratio")


def run(vod_id: str, work: Path, cfg: dict, *, log=print) -> Signal:
    """Chat hype signal [(bucket_t, 0..1)], cached in chat.json."""
    c = cfg.get("chat", {})
    s = cfg.get("signals", {})
    interval = float(s.get("interval_seconds", 10))
    params = {
        "v": _CACHE_VERSION, "interval": interval,
        "lag": float(c.get("lag_seconds", 8)),
        "baseline": float(s.get("baseline_window_seconds", 300)),
    }
    cache = work / "chat.json"
    if cache.exists():
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("params") == params:
                return [tuple(x) for x in data["data"]]
        except (json.JSONDecodeError, OSError, KeyError):
            pass

    msgs = load_messages(work, vod_id, log=log)
    sig = score_messages(msgs, interval=interval, lag=params["lag"],
                         baseline_seconds=params["baseline"])
    try:
        cache.write_text(json.dumps({"params": params, "messages": len(msgs), "data": sig}),
                         encoding="utf-8")
    except OSError:
        pass
    return sig
