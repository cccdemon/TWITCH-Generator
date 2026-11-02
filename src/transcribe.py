"""Whisper stage: transcribe VOD audio to timestamped segments.

Two backends, selected by `whisper.backend` in config.yaml:

  faster-whisper      (default, prod) — in-process CTranslate2 on CPU/CUDA.
  whispercpp-server   — HTTP to a whisper.cpp `whisper-server` (GPU via Vulkan in
                        the local deployment). One `POST {server_url}/inference`
                        per 30-min chunk, `response_format=verbose_json`
                        (`segments[].start/end` in float seconds); `srt` is parsed
                        as a fallback if the server answers with text.

Chunking is shared: audio is split up front into N-second 16 kHz mono WAVs
(bounded RAM for faster-whisper, bounded request size for the server) and each
chunk's offset is added back. transcript.json has the same shape either way.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable

from . import metrics
from .models import TranscriptSegment

BACKEND_FASTER = "faster-whisper"
BACKEND_SERVER = "whispercpp-server"


def chunks_ready(chunk_dir: Path, chunk_seconds: int) -> bool:
    """True if a complete chunk set for this chunk length is on disk."""
    meta = chunk_dir / "meta.json"
    if not meta.exists() or not any(chunk_dir.glob("chunk_*.wav")):
        return False
    try:
        return int(json.loads(meta.read_text(encoding="utf-8")).get("chunk_seconds")) == int(chunk_seconds)
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        return False


def reset_chunks(chunk_dir: Path) -> None:
    if chunk_dir.exists():
        shutil.rmtree(chunk_dir, ignore_errors=True)
    chunk_dir.mkdir(parents=True, exist_ok=True)


def write_chunks_meta(chunk_dir: Path, chunk_seconds: int) -> None:
    (chunk_dir / "meta.json").write_text(json.dumps({"chunk_seconds": int(chunk_seconds)}),
                                         encoding="utf-8")


def _split_audio_chunks(media_path: Path, out_dir: Path, chunk_seconds: int) -> list[Path]:
    """One streaming ffmpeg pass: video -> 16 kHz mono WAV chunks of N seconds.

    faster-whisper loads the WHOLE decoded audio into RAM at once (no streaming
    decode), so memory grows with VOD length and OOMs on a small box. Splitting up
    front bounds peak memory to one chunk (~16000*2*N bytes raw) regardless of how
    long the VOD is. One pass straight from the video also avoids demuxing the
    multi-GB file in memory.
    """
    chunk_dir = out_dir / "audio_chunks"
    if chunks_ready(chunk_dir, chunk_seconds):
        return sorted(chunk_dir.glob("chunk_*.wav"))
    reset_chunks(chunk_dir)  # stale length (timestamps would misalign) or partial set
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-nostdin", "-i", str(media_path),
             "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
             "-f", "segment", "-segment_time", str(chunk_seconds),
             str(chunk_dir / "chunk_%04d.wav")],
            check=True,
        )
    except subprocess.CalledProcessError:
        reset_chunks(chunk_dir)
        raise
    write_chunks_meta(chunk_dir, chunk_seconds)
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
    beam_size: int = 1,
    on_progress: Callable[[int, int], None] | None = None,
    backend: str = BACKEND_FASTER,
    server_url: str | None = None,
    request_timeout: float = 3600.0,
    log=print,
) -> list[TranscriptSegment]:
    """Transcribe to segments, chunk by chunk. Caches JSON to allow re-runs.

    on_progress(done, total) is called with done=0 once the chunks are known, then
    after each chunk — used to surface per-chunk progress in the job log.
    """
    if cache_path.exists():
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        return [TranscriptSegment(**s) for s in data]

    if backend not in (BACKEND_FASTER, BACKEND_SERVER):
        raise ValueError(f"whisper.backend must be '{BACKEND_FASTER}' or '{BACKEND_SERVER}', got {backend!r}")

    chunks = _split_audio_chunks(media_path, cache_path.parent, chunk_seconds)
    total = len(chunks)
    if on_progress:
        on_progress(0, total)

    if backend == BACKEND_SERVER:
        if not server_url:
            raise ValueError("whisper.server_url is required for backend whispercpp-server")
        log(f"  whisper: backend={backend} model={model} server={server_url}")
        transcribe_chunk = _server_transcriber(server_url, language, request_timeout)
    else:
        log(f"  whisper: backend={backend} model={model} device={device}/{compute_type}")
        from faster_whisper import WhisperModel  # lazy: heavy import, keeps module light for extract.py

        wm = WhisperModel(model, device=device, compute_type=compute_type,
                          cpu_threads=cpu_threads, num_workers=1)

        def transcribe_chunk(chunk: Path) -> list[tuple[float, float, str]]:
            segments, _info = wm.transcribe(
                str(chunk),
                language=language,
                vad_filter=True,
                beam_size=max(1, int(beam_size)),  # 1 = greedy, ~2-3x faster than 5
            )
            return [(seg.start, seg.end, seg.text) for seg in segments]

    m = metrics.current()
    if m is not None:
        m.whisper_start(backend=backend, model=model, chunks_total=total)

    out: list[TranscriptSegment] = []
    for i, chunk in enumerate(chunks):
        offset = i * chunk_seconds  # raw-PCM segments split exactly at segment_time
        t0 = time.time()
        last_end = 0.0
        for start, end, text in transcribe_chunk(chunk):
            last_end = max(last_end, end)
            text = text.strip()
            if not text:
                continue
            out.append(TranscriptSegment(start=round(start + offset, 2),
                                         end=round(end + offset, 2),
                                         text=text))
        wall = time.time() - t0
        # the last chunk is shorter than chunk_seconds; use the audio actually covered
        audio_s = float(chunk_seconds) if i < total - 1 else max(last_end, 1.0)
        log(f"  whisper: chunk {i + 1}/{total} {audio_s:.0f}s audio in {wall:.0f}s ({audio_s / wall if wall else 0:.1f}x realtime)")
        if m is not None:
            m.whisper_chunk(audio_s=audio_s, wall_s=wall, done=i + 1)
        if on_progress:
            on_progress(i + 1, total)

    cache_path.write_text(
        json.dumps([s.__dict__ for s in out], ensure_ascii=False),
        encoding="utf-8",
    )
    return out


# ------------------------------------------------------------ whisper.cpp ----
def _server_transcriber(server_url: str, language: str | None, timeout: float):
    import httpx

    url = server_url.rstrip("/") + "/inference"

    def run(chunk: Path) -> list[tuple[float, float, str]]:
        fields = {
            "temperature": "0.0",
            "response_format": "verbose_json",
        }
        if language:
            fields["language"] = str(language)
        with chunk.open("rb") as fh:
            with httpx.Client(timeout=httpx.Timeout(timeout, connect=30.0)) as client:
                resp = client.post(url, data=fields,
                                   files={"file": (chunk.name, fh, "audio/wav")})
        if resp.status_code != 200:
            raise RuntimeError(f"whisper-server {resp.status_code}: {resp.text[:300]}")
        return parse_server_response(resp.text)

    return run


def parse_server_response(body: str) -> list[tuple[float, float, str]]:
    """verbose_json → (start, end, text) per segment; falls back to SRT text."""
    stripped = body.lstrip()
    if stripped.startswith("{"):
        data = json.loads(stripped)
        segs = data.get("segments")
        if isinstance(segs, list):
            out = []
            for s in segs:
                if not isinstance(s, dict):
                    continue
                try:
                    out.append((float(s["start"]), float(s["end"]), str(s.get("text", ""))))
                except (KeyError, TypeError, ValueError):
                    continue
            return out
        # plain {"text": ...} (response_format=json): no timestamps → one segment
        text = str(data.get("text", "")).strip()
        return [(0.0, 0.0, text)] if text else []
    return parse_srt(body)


_SRT_TIME = re.compile(r"(\d+):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d+):(\d{2}):(\d{2})[,.](\d{3})")


def _srt_seconds(h: str, m: str, s: str, ms: str) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0


def parse_srt(body: str) -> list[tuple[float, float, str]]:
    out: list[tuple[float, float, str]] = []
    cur: tuple[float, float] | None = None
    lines: list[str] = []
    for raw in body.splitlines() + [""]:
        line = raw.strip()
        m = _SRT_TIME.match(line)
        if m:
            cur = (_srt_seconds(*m.groups()[:4]), _srt_seconds(*m.groups()[4:]))
            lines = []
            continue
        if not line:
            if cur is not None and lines:
                out.append((cur[0], cur[1], " ".join(lines)))
            cur, lines = None, []
            continue
        if cur is None:
            continue  # the block index line
        lines.append(line)
    return out
