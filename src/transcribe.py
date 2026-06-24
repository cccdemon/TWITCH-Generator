"""Whisper stage: transcribe VOD audio to timestamped segments via faster-whisper."""
from __future__ import annotations

import json
from pathlib import Path

from faster_whisper import WhisperModel

from .models import TranscriptSegment


def transcribe(
    media_path: Path,
    cache_path: Path,
    model: str = "large-v3",
    device: str = "cpu",
    compute_type: str = "int8",
    language: str | None = None,
) -> list[TranscriptSegment]:
    """Transcribe to segments. Caches JSON next to the VOD to allow re-runs."""
    if cache_path.exists():
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        return [TranscriptSegment(**s) for s in data]

    wm = WhisperModel(model, device=device, compute_type=compute_type)
    segments, _info = wm.transcribe(
        str(media_path),
        language=language,
        vad_filter=True,
        beam_size=5,
    )

    out: list[TranscriptSegment] = []
    for seg in segments:
        out.append(TranscriptSegment(start=round(seg.start, 2),
                                     end=round(seg.end, 2),
                                     text=seg.text.strip()))

    cache_path.write_text(
        json.dumps([s.__dict__ for s in out], ensure_ascii=False),
        encoding="utf-8",
    )
    return out
