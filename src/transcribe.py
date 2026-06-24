"""Whisper stage: transcribe VOD audio to timestamped segments via faster-whisper."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from faster_whisper import WhisperModel

from .models import TranscriptSegment


def _extract_audio(media_path: Path, out_dir: Path) -> Path:
    """Stream the video to a small 16 kHz mono WAV with ffmpeg.

    Feeding the multi-GB 1080p VOD straight to faster-whisper makes it demux the
    whole video in memory and OOMs on a small box. ffmpeg extracts the audio in a
    streaming pass (low memory), and Whisper then decodes a tiny mono WAV.
    """
    audio = out_dir / "audio16k.wav"
    if audio.exists() and audio.stat().st_size > 0:
        return audio
    subprocess.run(
        ["ffmpeg", "-y", "-nostdin", "-i", str(media_path),
         "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(audio)],
        check=True,
    )
    return audio


def transcribe(
    media_path: Path,
    cache_path: Path,
    model: str = "small",
    device: str = "cpu",
    compute_type: str = "int8",
    language: str | None = None,
    cpu_threads: int = 2,
) -> list[TranscriptSegment]:
    """Transcribe to segments. Caches JSON next to the VOD to allow re-runs."""
    if cache_path.exists():
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        return [TranscriptSegment(**s) for s in data]

    audio_path = _extract_audio(media_path, cache_path.parent)

    wm = WhisperModel(model, device=device, compute_type=compute_type,
                      cpu_threads=cpu_threads, num_workers=1)
    segments, _info = wm.transcribe(
        str(audio_path),
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
