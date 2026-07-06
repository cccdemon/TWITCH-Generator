"""Claude token pricing + per-VOD cost accounting.

analyze/vision pass each response's `usage` into a CostMeter; the pipeline
writes the total to data/<vod>/cost.json so the web UI can show what a VOD cost
in Claude API spend.
"""
from __future__ import annotations

import json
from pathlib import Path

# USD per 1M tokens (input, output). Source: platform.claude.com/docs pricing.
# Keep in sync when the analyze/vision model in config.yaml changes.
_PRICES: dict[str, tuple[float, float]] = {
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-fable-5": (10.0, 50.0),
}
_DEFAULT_RATE = (3.0, 15.0)  # unknown model → assume Sonnet-tier
_CACHE_WRITE_MULT = 1.25     # writing to cache costs ~1.25x input
_CACHE_READ_MULT = 0.1       # reading from cache costs ~0.1x input


def _rate(model: str) -> tuple[float, float]:
    return _PRICES.get(model, _DEFAULT_RATE)


class CostMeter:
    """Accumulate Claude token usage across API calls and total the USD cost."""

    def __init__(self) -> None:
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.cache_write_tokens = 0
        self.cache_read_tokens = 0
        self._usd = 0.0

    def add(self, model: str, usage) -> None:
        """Fold in one response's `usage` object (anthropic Usage)."""
        pin, pout = _rate(model)
        it = int(getattr(usage, "input_tokens", 0) or 0)
        ot = int(getattr(usage, "output_tokens", 0) or 0)
        cw = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
        cr = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
        self.calls += 1
        self.input_tokens += it
        self.output_tokens += ot
        self.cache_write_tokens += cw
        self.cache_read_tokens += cr
        self._usd += (
            it * pin
            + cw * pin * _CACHE_WRITE_MULT
            + cr * pin * _CACHE_READ_MULT
            + ot * pout
        ) / 1_000_000

    @property
    def usd(self) -> float:
        return round(self._usd, 4)

    def to_dict(self) -> dict:
        return {
            "usd": self.usd,
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "cache_read_tokens": self.cache_read_tokens,
        }

    def save(self, path: Path) -> None:
        try:
            path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        except OSError:
            pass


def load_cost(path: Path) -> dict | None:
    """Read a cost.json, or None if absent/unreadable."""
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
