"""CLI orchestrator: runs the full VOD -> clips -> upload pipeline (or single stages)."""
from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from rich.console import Console

from . import analyze as analyze_stage
from . import audio as audio_stage
from . import chat as chat_stage
from . import clip as clip_stage
from . import extract as extract_stage
from . import pricing
from . import refine as refine_stage
from . import score as score_stage
from . import timing
from . import transcribe as transcribe_stage
from . import twitch as twitch_stage
from . import upload as upload_stage
from . import vision as vision_stage
from .config import data_dir, load_config


def _fmt_dur(seconds: float) -> str:
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"

console = Console()


def _vod_workdir(vod_id: str) -> Path:
    d = data_dir() / vod_id
    (d / "clips").mkdir(parents=True, exist_ok=True)
    return d


def _finish(work: Path, t0: float, vod_seconds: float, meter: pricing.CostMeter) -> float:
    elapsed = time.time() - t0
    timing.record(vod_seconds, elapsed)
    pricing.record_cost(vod_seconds, meter.usd)
    meter.save(work / "cost.json")
    return elapsed


def run_pipeline(vod_url: str, cfg: dict, *, no_upload: bool, console: Console = console) -> int:
    t0 = time.time()
    meter = pricing.CostMeter()
    log = console.print

    console.rule("[bold]1/7 Twitch — resolve + download")
    info = twitch_stage.fetch_vod_info(vod_url)
    work = _vod_workdir(info.vod_id)
    # Persist VOD metadata early so the web UI can label jobs/clips by stream.
    try:
        (work / "info.json").write_text(
            json.dumps({"vod_id": info.vod_id, "title": info.title,
                        "streamer": info.streamer, "url": info.url}),
            encoding="utf-8",
        )
    except OSError:
        pass
    log(f"VOD {info.vod_id} — {info.title or '(no title)'} — {info.streamer}")

    dl = cfg.get("download", {})
    media = twitch_stage.download_vod(
        info, work, fmt=dl.get("format", "best"), reuse=dl.get("reuse_existing", True)
    )
    log(f"media: {media}")

    # Predict total processing time + Claude cost from VOD length (learned).
    vod_seconds = info.duration or timing.probe_duration(media)
    eta = timing.estimate_seconds(vod_seconds)
    est_cost = pricing.estimate_cost(vod_seconds)
    if eta:
        timing.write_run(work, started=t0, vod_seconds=vod_seconds,
                         eta_seconds=eta, est_cost=est_cost)
        log(
            f"[cyan]estimated ~{_fmt_dur(eta)} · ~${est_cost:.2f} Claude[/] "
            f"(VOD {_fmt_dur(vod_seconds)}, factor {timing.factor():.2f})"
        )

    console.rule("[bold]2/7 Extract — one decode pass (wav chunks, frames, loudness)")
    extract_stage.prepare(media, work, cfg, log=log)

    console.rule("[bold]3/7 Signals — Whisper ∥ chat ∥ audio ∥ vision")
    vcfg = cfg.get("vision", {})
    w = cfg.get("whisper", {})

    def _chat():
        if not cfg.get("chat", {}).get("enabled", True):
            return []
        try:
            sig = chat_stage.run(info.vod_id, work, cfg, log=log)
        except Exception as e:  # noqa: BLE001
            log(f"[yellow]chat signal failed ({type(e).__name__}: {e}) — continuing without it")
            return []
        log(f"  chat: {len(sig)} buckets, {sum(1 for _, s in sig if s >= 0.5)} spikes")
        return sig

    def _audio():
        if not cfg.get("audio", {}).get("enabled", True):
            return []
        try:
            sig = audio_stage.run(media, work, cfg)
        except Exception as e:  # noqa: BLE001
            log(f"[yellow]audio signal failed ({type(e).__name__}: {e}) — continuing without it")
            return []
        log(f"  audio: {len(sig)} buckets, {sum(1 for _, s in sig if s >= 0.5)} bursts")
        return sig

    def _vision():
        if not vcfg.get("enabled", True):
            return []
        try:
            frames = vision_stage.run(media, work, cfg, meter=meter, log=log)
        except Exception as e:  # noqa: BLE001
            log(f"[yellow]vision signal failed ({type(e).__name__}: {e}) — continuing without it")
            return []
        thr = vcfg.get("highlight_threshold", 0.5)
        log(f"  vision: {len(frames)} frames, {sum(1 for f in frames if f.visual_score >= thr)} above threshold")
        return frames

    def _whisper_progress(done: int, total: int) -> None:
        if done == 0:
            log(f"  whisper: {total} chunk(s)")
        else:
            log(f"  whisper: chunk {done}/{total}")

    # API/network-bound signals run alongside the CPU-bound transcription.
    with ThreadPoolExecutor(max_workers=3) as pool:
        f_chat = pool.submit(_chat)
        f_audio = pool.submit(_audio)
        f_vision = pool.submit(_vision)
        segments = transcribe_stage.transcribe(
            media, work / "transcript.json",
            model=w.get("model", "small"),
            device=w.get("device", "cpu"),
            compute_type=w.get("compute_type", "int8"),
            language=w.get("language"),
            cpu_threads=w.get("cpu_threads", 2),
            chunk_seconds=w.get("chunk_seconds", 1800),
            beam_size=w.get("beam_size", 1),
            on_progress=_whisper_progress,
        )
        log(f"  whisper: {len(segments)} segments")
        chat_sig = f_chat.result()
        audio_sig = f_audio.result()
        vision_frames = f_vision.result()

    console.rule("[bold]4/7 LLM — transcript candidates")
    a = cfg.get("analyze", {})
    max_clips = int(a.get("max_clips", 10))
    factor = int(cfg.get("score", {}).get("candidate_factor", 3))
    text_moments = analyze_stage.analyze(
        segments,
        model=a.get("model", "claude-sonnet-4-6"),
        categories=a.get("categories", ["funny", "emotional", "exciting", "skillful"]),
        max_clips=max_clips * factor,
        min_clip_seconds=a.get("min_clip_seconds", 15),
        max_clip_seconds=a.get("max_clip_seconds", 60),
        meter=meter,
    ) if segments else []
    log(f"{len(text_moments)} text candidates")

    console.rule("[bold]5/7 Fuse + refine")
    candidates = score_stage.fuse(text_moments, vision_frames, audio_sig, chat_sig, cfg)
    score_stage.save_moments(candidates, work / "candidates.json")
    log(f"{len(candidates)} fused candidates")
    moments = refine_stage.refine(candidates, segments, cfg, meter=meter, log=log)
    score_stage.save_moments(moments, work / "moments.json")
    meter.save(work / "cost.json")
    log(
        f"[cyan]Claude cost ~${meter.usd:.4f}[/] "
        f"({meter.calls} calls, {meter.input_tokens + meter.cache_read_tokens + meter.cache_write_tokens} in / {meter.output_tokens} out tokens)"
    )
    log(f"{len(moments)} highlights")
    for m in moments:
        log(
            f"  [{m.start:.0f}s {m.category} {m.score:.2f}] {m.title}"
            f"  (c={m.chat_score:.2f} a={m.audio_score:.2f} t={m.text_score:.2f} v={m.video_score:.2f})"
        )
    if not moments:
        log("[red]no highlights found — aborting")
        _finish(work, t0, vod_seconds, meter)
        return 1

    console.rule("[bold]6/7 FFmpeg — cut clips")
    c = cfg.get("clip", {})
    if not c.get("auto_render", True):
        log(
            f"[yellow]auto_render off — {len(moments)} moments saved, "
            f"render clips manually in the editor (/edit/{info.vod_id})"
        )
        _finish(work, t0, vod_seconds, meter)
        return 0
    facecam = c.get("facecam") or {}
    if c.get("vertical", True) and c.get("layout", "split") == "split" and c.get("facecam_auto", True):
        det = vision_stage.detect_facecam(
            work, model=vcfg.get("model", "claude-sonnet-4-6"), meter=meter
        )
        if det:
            facecam = det
            log(f"[cyan]facecam auto-detected[/] {det}")
        else:
            log("facecam not auto-detected — using config box")
    clips = []
    for m in moments:
        cl = clip_stage.render_clip(
            media, m, segments, work / "clips",
            vertical=c.get("vertical", True),
            width=c.get("width", 1080),
            height=c.get("height", 1920),
            layout=c.get("layout", "split"),
            facecam=facecam,
            pad_start=c.get("pad_start", 1.0),
            pad_end=c.get("pad_end", 1.0),
            burn_subtitles=c.get("burn_subtitles", True),
        )
        clips.append(cl)
        log(f"  rendered {cl.path}")

    if no_upload:
        console.rule("[bold]7/7 upload — SKIPPED")
        elapsed = _finish(work, t0, vod_seconds, meter)
        log(
            f"[green]done — {len(clips)} clips in {work / 'clips'} "
            f"(took {_fmt_dur(elapsed)})"
        )
        return 0

    console.rule("[bold]7/7 Upload")
    u = cfg.get("upload", {})
    platforms = u.get("platforms", ["youtube"])
    privacy = u.get("privacy", "private")
    for cl in clips:
        for res in upload_stage.upload_clip(cl, platforms, privacy, tiktok=u.get("tiktok") or {}):
            tag = "[green]OK" if res.ok else "[red]FAIL"
            log(f"  {tag} {res.platform}: {res.url or res.error}")

    elapsed = _finish(work, t0, vod_seconds, meter)
    log(f"[green]done — {len(clips)} clips (took {_fmt_dur(elapsed)})")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="twitch-generator")
    sub = p.add_subparsers(dest="cmd", required=True)

    runp = sub.add_parser("run", help="full pipeline for a VOD")
    runp.add_argument("--vod", required=True, help="Twitch VOD url or id")
    runp.add_argument("--config", default=None)
    runp.add_argument("--no-upload", action="store_true", help="render clips, skip uploading")

    webp = sub.add_parser("web", help="launch the web interface")
    webp.add_argument("--host", default="0.0.0.0")
    webp.add_argument("--port", type=int, default=int(__import__("os").environ.get("WEB_PORT", 9443)))

    args = p.parse_args(argv)
    if args.cmd == "web":
        import uvicorn
        uvicorn.run("src.web:app", host=args.host, port=args.port, log_level="info")
        return 0
    if args.cmd == "run":
        if not args.vod:
            console.print("[red]--vod required (or set TG_VOD_URL)")
            return 2
        from .settings_store import apply_settings
        apply_settings()
        cfg = load_config(args.config)
        return run_pipeline(args.vod, cfg, no_upload=args.no_upload)
    return 2


if __name__ == "__main__":
    sys.exit(main())
