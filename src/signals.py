"""Shared helpers for time-series highlight signals.

A signal is a list of (bucket_start_seconds, score 0..1), produced by one stage
(audio, chat, vision) and consumed by the fusion stage. Scores must be RELATIVE
to the stream's own baseline, never absolute: a Twitch stream is loudness-
normalised and a busy chat is busy all night, so only departures from the local
norm carry information.
"""
from __future__ import annotations

from typing import Callable, Iterable

Signal = list[tuple[float, float]]


def bucketize(
    samples: Iterable[tuple[float, float]], interval: float,
    reduce: Callable[[float, float], float] = max, empty: float = 0.0,
) -> list[tuple[float, float]]:
    """Fold (t, value) samples into a dense grid of `interval` seconds.

    Missing buckets between the first and last sample get `empty`.
    """
    buckets: dict[int, float] = {}
    for t, v in samples:
        b = int(t // interval)
        buckets[b] = reduce(buckets[b], v) if b in buckets else v
    if not buckets:
        return []
    lo, hi = min(buckets), max(buckets)
    return [(round(b * interval, 2), buckets.get(b, empty)) for b in range(lo, hi + 1)]


def _median(vals: list[float]) -> float:
    s = sorted(vals)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2


def rolling_median(values: list[float], half_window: int) -> list[float]:
    """Median of values[i-half_window .. i+half_window] for every i."""
    n = len(values)
    out: list[float] = []
    for i in range(n):
        lo = max(0, i - half_window)
        hi = min(n, i + half_window + 1)
        out.append(_median(values[lo:hi]))
    return out


def relative(
    grid: list[tuple[float, float]], *, interval: float,
    baseline_seconds: float, scale: float, floor: float = 0.0,
    mode: str = "diff",
) -> Signal:
    """Turn raw bucket values into 0..1 scores relative to a rolling baseline.

    mode="diff":  score = (v - baseline) / scale            (dB-like units)
    mode="ratio": score = ((v / max(baseline, floor)) - 1) / scale   (counts)
    """
    if not grid:
        return []
    vals = [v for _, v in grid]
    half = max(1, int(baseline_seconds / interval / 2))
    base = rolling_median(vals, half)
    out: Signal = []
    for (t, v), b in zip(grid, base):
        if mode == "ratio":
            excess = (v / max(b, floor)) - 1.0 if max(b, floor) > 0 else 0.0
        else:
            excess = v - b
        score = max(0.0, min(1.0, excess / scale)) if scale > 0 else 0.0
        out.append((t, round(score, 4)))
    return out


def max_in(signal: Signal, start: float, end: float) -> float:
    """Peak score of all buckets whose start lies in [start, end]."""
    return max((s for t, s in signal if start <= t <= end), default=0.0)


def peak_in(signal: Signal, start: float, end: float) -> tuple[float, float] | None:
    """(t, score) of the strongest bucket in [start, end], or None."""
    best = None
    for t, s in signal:
        if start <= t <= end and (best is None or s > best[1]):
            best = (t, s)
    return best


def clusters(
    signal: Signal, interval: float, threshold: float,
) -> list[tuple[float, float, float, float]]:
    """Group consecutive above-threshold buckets.

    Returns (start, end, peak_t, peak_score). `end` is the last hot bucket's end.
    One missing bucket (e.g. a frame the vision model skipped) does not split a
    cluster; a gap of two or more buckets does.
    """
    out: list[tuple[float, float, float, float]] = []
    cur: list[tuple[float, float]] = []

    def flush() -> None:
        if not cur:
            return
        peak_t, peak_s = max(cur, key=lambda x: x[1])
        out.append((cur[0][0], cur[-1][0] + interval, peak_t, peak_s))

    prev_t: float | None = None
    for t, s in signal:
        if s < threshold:
            flush(); cur = []; prev_t = None
            continue
        if prev_t is not None and t - prev_t > interval * 2.5:
            flush(); cur = []
        cur.append((t, s))
        prev_t = t
    flush()
    return out
