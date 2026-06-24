"""CLI orchestrator: runs the full VOD -> clips -> upload pipeline (or single stages)."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from rich.console import Console

from . import analyze as analyze_stage
from . import audio as audio_stage
from . import clip as clip_stage
from . import score as score_stage
from . import transcribe as transcribe_stage
from . import twitch as twitch_stage
from . import upload as upload_stage
from . import vision as vision_stage
from .config import data_dir, load_config

console = Console()


def _vod_workdir(vod_id: str) -> Path:
    d = data_dir() / vod_id
    (d / "clips").mkdir(parents=True, exist_ok=True)
    return d


def run_pipeline(vod_url: str, cfg: dict, *, no_upload: bool, console: Console = console) -> int:
    console.rule("[bold]1/8 Twitch — resolve VOD")
    info = twitch_stage.fetch_vod_info(vod_url)
    work = _vod_workdir(info.vod_id)
    console.print(f"VOD {info.vod_id} — {info.title or '(no title)'} — {info.streamer}")

    console.rule("[bold]1b — download")
    dl = cfg.get("download", {})
    media = twitch_stage.download_vod(
        info, work, fmt=dl.get("format", "best"), reuse=dl.get("reuse_existing", True)
    )
    console.print(f"media: {media}")

    console.rule("[bold]2/8 Whisper — transcribe")
    w = cfg.get("whisper", {})
    segments = transcribe_stage.transcribe(
        media, work / "transcript.json",
        model=w.get("model", "small"),
        device=w.get("device", "cpu"),
        compute_type=w.get("compute_type", "int8"),
        language=w.get("language"),
        cpu_threads=w.get("cpu_threads", 2),
        chunk_seconds=w.get("chunk_seconds", 1800),
    )
    console.print(f"{len(segments)} segments")

    console.rule("[bold]3/8 LLM — text moments")
    a = cfg.get("analyze", {})
    text_moments = analyze_stage.analyze(
        segments,
        model=a.get("model", "claude-sonnet-4-6"),
        categories=a.get("categories", ["funny", "emotional", "exciting", "skillful"]),
        max_clips=a.get("max_clips", 10),
        min_clip_seconds=a.get("min_clip_seconds", 15),
        max_clip_seconds=a.get("max_clip_seconds", 60),
    ) if segments else []
    console.print(f"{len(text_moments)} text moments")

    console.rule("[bold]4/8 Vision — score frames")
    vision_frames = []
    vcfg = cfg.get("vision", {})
    if vcfg.get("enabled", True):
        vision_frames = vision_stage.run(media, work, cfg)
        thr = vcfg.get("highlight_threshold", 0.5)
        hot = sum(1 for f in vision_frames if f.visual_score >= thr)
        console.print(f"{len(vision_frames)} frames scored, {hot} above threshold")
    else:
        console.print("disabled")

    console.rule("[bold]5/8 Audio — loudness energy")
    audio_energy = []
    if cfg.get("audio", {}).get("enabled", True):
        audio_energy = audio_stage.run(media, work, cfg.get("vision", {}).get("interval_seconds", 30))
        console.print(f"{len(audio_energy)} audio buckets")
    else:
        console.print("disabled")

    console.rule("[bold]6/8 Fuse — highlight score")
    if vision_frames or audio_energy:
        moments = score_stage.fuse(text_moments, vision_frames, audio_energy, cfg)
    else:
        moments = text_moments  # transcript-only fallback
    score_stage.save_moments(moments, work / "moments.json")
    console.print(f"{len(moments)} highlights")
    for m in moments:
        console.print(
            f"  [{m.start:.0f}s {m.category} {m.score:.2f}] {m.title}"
            f"  (a={m.audio_score:.2f} t={m.text_score:.2f} v={m.video_score:.2f})"
        )
    if not moments:
        console.print("[red]no highlights found — aborting")
        return 1

    console.rule("[bold]7/8 FFmpeg — cut clips")
    c = cfg.get("clip", {})
    clips = []
    for m in moments:
        cl = clip_stage.render_clip(
            media, m, segments, work / "clips",
            vertical=c.get("vertical", True),
            width=c.get("width", 1080),
            height=c.get("height", 1920),
            pad_start=c.get("pad_start", 1.0),
            pad_end=c.get("pad_end", 1.0),
            burn_subtitles=c.get("burn_subtitles", True),
        )
        clips.append(cl)
        console.print(f"  rendered {cl.path}")

    if no_upload:
        console.rule("[bold]8/8 upload — SKIPPED")
        console.print(f"[green]done — {len(clips)} clips in {work / 'clips'}")
        return 0

    console.rule("[bold]8/8 Upload")
    u = cfg.get("upload", {})
    platforms = u.get("platforms", ["youtube"])
    privacy = u.get("privacy", "private")
    for cl in clips:
        for res in upload_stage.upload_clip(cl, platforms, privacy):
            tag = "[green]OK" if res.ok else "[red]FAIL"
            console.print(f"  {tag} {res.platform}: {res.url or res.error}")

    console.print(f"[green]done — {len(clips)} clips")
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
