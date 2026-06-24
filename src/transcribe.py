"""Whisper stage: transcribe VOD audio to timestamped segments via faster-whisper."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Callable

from faster_whisper import WhisperModel

from .models import TranscriptSegment


def _split_audio_chunks(media_path: Path, out_dir: Path, chunk_seconds: int) -> list[Path]:
    """One streaming ffmpeg pass: video -> 16 kHz mono WAV chunks of N seconds.

    faster-whisper loads the WHOLE decoded audio into RAM at once (no streaming
    decode), so memory grows with VOD length and OOMs on a small box. Splitting up
    front bounds peak memory to one chunk (~16000*2*N bytes raw) regardless of how
    long the VOD is. One pass straight from the video also avoids demuxing the
    multi-GB file in memory.
    """
    chunk_dir = out_dir / "audio_chunks"
    chunk_dir.mkdir(parents=True, exist_ok=True)
    existing = sorted(chunk_dir.glob("chunk_*.wav"))
    if existing:
        return existing
    subprocess.run(
        ["ffmpeg", "-y", "-nostdin", "-i", str(media_path),
         "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
         "-f", "segment", "-segment_time", str(chunk_seconds),
         str(chunk_dir / "chunk_%04d.wav")],
        check=True,
    )
    return sorted(chunk_dir.glob("chunk_*.wav"))


def transcribe(
    media_path: Path,
    cache_path: Path,
    model: str = "small",
    device: str = "cpu",
    compute_type: str = "int8",
    language: str | None = None,
    cpu_threads: int = 2,
    chunk_seconds: int = 1800,
    on_progress: Callable[[int, int], None] | None = None,
) -> list[TranscriptSegment]:
    """Transcribe to segments, chunk by chunk. Caches JSON to allow re-runs.

    on_progress(done, total) is called with done=0 once the chunks are known, then
    after each chunk — used to surface per-chunk progress in the job log.
    """
    if cache_path.exists():
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        return [TranscriptSegment(**s) for s in data]

    chunks = _split_audio_chunks(media_path, cache_path.parent, chunk_seconds)
    total = len(chunks)
    if on_progress:
        on_progress(0, total)

    wm = WhisperModel(model, device=device, compute_type=compute_type,
                      cpu_threads=cpu_threads, num_workers=1)

    out: list[TranscriptSegment] = []
    for i, chunk in enumerate(chunks):
        offset = i * chunk_seconds  # raw-PCM segments split exactly at segment_time
        segments, _info = wm.transcribe(
            str(chunk),
            language=language,
            vad_filter=True,
            beam_size=5,
        )
        for seg in segments:
            out.append(TranscriptSegment(start=round(seg.start + offset, 2),
                                         end=round(seg.end + offset, 2),
                                         text=seg.text.strip()))
        if on_progress:
            on_progress(i + 1, total)

    cache_path.write_text(
        json.dumps([s.__dict__ for s in out], ensure_ascii=False),
        encoding="utf-8",
    )
    return out
