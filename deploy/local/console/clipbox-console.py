#!/usr/bin/env python3
"""ClipBox status dashboard for the attached monitor (tty1), green-on-black.

Shows that the box is on and what it is doing: clock, addresses, GPU (load,
temperatures, VRAM, power, fan, clock), CPU (load, per core, temperature,
clock), RAM, network speed, disk and disk I/O, the loaded Ollama model,
tokens per second, Whisper/vision/render progress of the running job, the
containers, and falling "matrix rain" in the rows that are left over.

Runs on the host as clipbox-console.service instead of the login prompt on
tty1 (login: Alt+F2). Python stdlib only.

    clipbox-console.py                 run on the current terminal
    clipbox-console.py --once [COLSxROWS]   print one frame and exit (ssh)

Glyphs are limited to what the console font (Uni2-TerminusBold) can draw:
box lines, the shades  ░ ▒ █  and  ● ■ ▶ ↓ ↑ ; no eighth blocks, no check mark.
Everything is best-effort: an unreadable value is left out, nothing here may
crash the screen or disturb a running job.
"""
from __future__ import annotations

import json
import os
import random
import re
import socket
import subprocess
import sys
import time
import urllib.request
from collections import deque
from pathlib import Path

LOCAL = Path(__file__).resolve().parent.parent          # deploy/local
DATA = Path(os.environ.get("CLIPBOX_DATA", LOCAL.parent.parent / "data"))
OLLAMA = os.environ.get("CLIPBOX_OLLAMA", "http://127.0.0.1:11434")
SYS, PROC = Path("/sys"), Path("/proc")

R = "\033[0m"
G = "\033[0;32m"        # normal green
HI = "\033[1;32m"       # bright green
W = "\033[1;37m"        # white (values that matter)
DIM = "\033[2;32m"      # dark green (labels, frames)
YEL, RED, CYA = "\033[1;33m", "\033[1;31m", "\033[0;36m"
DAYS = ["MO", "DI", "MI", "DO", "FR", "SA", "SO"]
STAGES = [("download", "DOWNLOAD"), ("extract", "DECODE"), ("signals", "SIGNALE"),
          ("analyze", "ANALYSE"), ("refine", "AUSWAHL"), ("render", "SCHNITT"), ("upload", "UPLOAD")]
_ANSI = re.compile(r"\033\[[0-9;]*m")


# ------------------------------------------------------------------ helpers
def _read(p: Path) -> str | None:
    try:
        return p.read_text(errors="replace").strip()
    except OSError:
        return None


def _int(p: Path) -> int | None:
    try:
        return int(_read(p) or "")
    except ValueError:
        return None


def _json(p: Path):
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None


def vlen(s: str) -> int:
    return len(_ANSI.sub("", s))


def fit(s: str, width: int) -> str:
    """Pad or cut to `width` visible characters; colour codes do not count."""
    out, seen, i = [], 0, 0
    while i < len(s) and seen < width:
        m = _ANSI.match(s, i)
        if m:
            out.append(m.group())
            i = m.end()
        else:
            out.append(s[i])
            seen += 1
            i += 1
    return "".join(out) + R + " " * (width - seen)


def level(frac: float, warn: float = 0.75, crit: float = 0.9) -> str:
    return RED if frac >= crit else YEL if frac >= warn else HI


def bar(frac: float | None, width: int, warn: float = 0.75, crit: float = 0.9) -> str:
    if frac is None:
        return DIM + "░" * width + R
    frac = max(0.0, min(1.0, frac))
    full = int(round(frac * width))
    return level(frac, warn, crit) + "█" * full + DIM + "░" * (width - full) + R


def graph(values, width: int, top: float | None = None) -> str:
    """One-row history strip, newest on the right: blank, ░, ▒, █ by height."""
    vals = list(values)[-width:]
    hi = top or max(vals, default=0) or 1
    cells = "".join(" ░▒█"[min(3, int(4 * max(0.0, v) / hi))] if v > 0 else "·" for v in vals)
    return DIM + "·" * (width - len(vals)) + G + cells + R


def span(seconds: float) -> str:
    m = int(max(0, seconds) // 60)
    if m < 60:
        return f"{m} MIN"
    if m < 48 * 60:
        return f"{m // 60} H {m % 60:02d} MIN"
    return f"{m // 1440} TAGE"


def rate(bytes_per_s: float) -> str:
    if bytes_per_s >= 1e6:
        return f"{bytes_per_s / 1e6:6.1f} MB/s"
    return f"{bytes_per_s / 1e3:6.0f} kB/s"


def kv(label: str, value: str) -> str:
    return f"{DIM}{label:<8}{R}{value}"


# ------------------------------------------------------------------ sampler
class Sampler:
    """Reads the host every couple of seconds and keeps short histories for the strips."""

    def __init__(self):
        self.t = 0.0
        self.cpu_prev: dict[str, tuple[int, int]] = {}
        self.net_prev = self.disk_prev = None
        self.h_cpu, self.h_gpu, self.h_rx, self.h_tx, self.h_tok = (deque(maxlen=200) for _ in range(5))
        self.tok_at = None
        self.d: dict = {}
        self.slow: dict = {}
        self.slow_t = 0.0

    def sample(self) -> None:
        now = time.time()
        dt = now - self.t if self.t else 0.0
        self.t = now
        d: dict = {}
        self._cpu(d)
        self._mem(d)
        self._gpu(d)
        self._net(d, dt)
        self._disk(d, dt)
        if now - self.slow_t > 6:          # docker, ollama, archive: slower sources
            self.slow_t = now
            self.slow = {"containers": containers(), "ollama": ollama_models(),
                         "archive": archive(), "addresses": addresses()}
        d.update(self.slow)
        d["job"] = job_state()
        last = ((d["job"].get("metrics") or {}).get("llm") or {}).get("last") or {}
        if last.get("at") and last.get("at") != self.tok_at and last.get("gen_tok_s"):
            self.tok_at = last["at"]
            self.h_tok.append(float(last["gen_tok_s"]))
        self.d = d

    def _cpu(self, d: dict) -> None:
        cores = []
        for line in (_read(PROC / "stat") or "").splitlines():
            if not line.startswith("cpu"):
                break
            name, *nums = line.split()
            nums = [int(x) for x in nums[:8]]
            total, idle = sum(nums), nums[3] + nums[4]
            pt, pi = self.cpu_prev.get(name, (total, idle))
            self.cpu_prev[name] = (total, idle)
            frac = 1 - (idle - pi) / (total - pt) if total > pt else 0.0
            if name == "cpu":
                d["cpu"] = frac
            else:
                cores.append(frac)
        d["cores"] = cores
        self.h_cpu.append(d.get("cpu", 0.0))
        freqs = [f for f in (_int(p) for p in SYS.glob("devices/system/cpu/cpu[0-9]*/cpufreq/scaling_cur_freq")) if f]
        if freqs:
            d["cpu_ghz"], d["cpu_ghz_max"] = sum(freqs) / len(freqs) / 1e6, max(freqs) / 1e6
        for hw in SYS.glob("class/hwmon/hwmon*"):
            if _read(hw / "name") in ("k10temp", "coretemp"):
                t = _int(hw / "temp1_input")
                if t is not None:
                    d["cpu_temp"] = t / 1000
                break
        d["load"] = (_read(PROC / "loadavg") or "").split()[:3]

    def _mem(self, d: dict) -> None:
        mem = {k: int(v) for k, v in re.findall(r"(\w+):\s+(\d+)", _read(PROC / "meminfo") or "")}
        if mem.get("MemTotal"):
            d["ram_total"] = mem["MemTotal"] / 1048576
            d["ram_used"] = (mem["MemTotal"] - mem.get("MemAvailable", 0)) / 1048576
            d["swap_total"] = mem.get("SwapTotal", 0) / 1048576
            d["swap_used"] = (mem.get("SwapTotal", 0) - mem.get("SwapFree", 0)) / 1048576

    def _gpu(self, d: dict) -> None:
        for dev in sorted(SYS.glob("class/drm/card*/device")):
            if not (dev / "gpu_busy_percent").exists():
                continue
            g: dict = {}
            # Reading the sensors would wake a sleeping card on every sample.
            if _read(dev / "power/runtime_status") == "suspended":
                g["asleep"] = True
            else:
                g["busy"] = (_int(dev / "gpu_busy_percent") or 0) / 100
                used, total = _int(dev / "mem_info_vram_used"), _int(dev / "mem_info_vram_total")
                if total:
                    g["vram_used"], g["vram_total"] = (used or 0) / 1e9, total / 1e9
                for hw in dev.glob("hwmon/hwmon*"):
                    for n in (1, 2, 3):
                        t = _int(hw / f"temp{n}_input")
                        if t is not None:
                            g[(_read(hw / f"temp{n}_label") or f"t{n}").lower()] = t / 1000
                    p = _int(hw / "power1_average") or _int(hw / "power1_input")
                    cap = _int(hw / "power1_cap")
                    g["watt"], g["watt_cap"] = (p / 1e6 if p else None), (cap / 1e6 if cap else None)
                    g["fan"] = _int(hw / "fan1_input")
                    f1 = _int(hw / "freq1_input")
                    g["mhz"] = f1 / 1e6 if f1 else None
                    break
            d["gpu"] = g
            self.h_gpu.append(g.get("busy", 0.0))
            return

    def _net(self, d: dict, dt: float) -> None:
        iface = ""
        for line in (_read(PROC / "net/route") or "").splitlines()[1:]:
            f = line.split()
            if len(f) > 1 and f[1] == "00000000":
                iface = f[0]
                break
        if not iface:
            return
        rx, tx = _int(SYS / f"class/net/{iface}/statistics/rx_bytes"), _int(SYS / f"class/net/{iface}/statistics/tx_bytes")
        d["iface"], d["link"] = iface, _int(SYS / f"class/net/{iface}/speed")
        if rx is not None and tx is not None:
            if self.net_prev and self.net_prev[0] == iface and dt > 0:
                d["rx"], d["tx"] = max(0, rx - self.net_prev[1]) / dt, max(0, tx - self.net_prev[2]) / dt
                self.h_rx.append(d["rx"])
                self.h_tx.append(d["tx"])
            self.net_prev = (iface, rx, tx)

    def _disk(self, d: dict, dt: float) -> None:
        try:
            st = os.statvfs(DATA if DATA.exists() else "/")
            d["disk_free"], d["disk_total"] = st.f_bavail * st.f_frsize / 1e9, st.f_blocks * st.f_frsize / 1e9
        except OSError:
            pass
        rd = wr = 0
        for line in (_read(PROC / "diskstats") or "").splitlines():
            f = line.split()
            if len(f) > 9 and re.fullmatch(r"sd[a-z]+|nvme\d+n\d+|vd[a-z]+", f[2]):
                rd += int(f[5]) * 512
                wr += int(f[9]) * 512
        if self.disk_prev and dt > 0:
            d["io_r"], d["io_w"] = max(0, rd - self.disk_prev[0]) / dt, max(0, wr - self.disk_prev[1]) / dt
        self.disk_prev = (rd, wr)


# ------------------------------------------------------------ slow sources
def _env() -> dict[str, str]:
    out = {}
    for line in (_read(LOCAL / ".env") or "").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"')
    return out


def _ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))   # no packet is sent; only picks the outgoing interface
        return s.getsockname()[0]
    except OSError:
        return ""
    finally:
        s.close()


def addresses() -> dict:
    env, urls = _env(), []
    m = re.match(r"(https?://[^/]+)", env.get("PUBLIC_URL", ""))
    if m and "proxy" in env.get("COMPOSE_PROFILES", ""):
        urls.append(m.group(1) + "/")
    ip = _ip()
    if ip:
        urls.append(f"http://{ip}:{env.get('WEB_PORT', '9443')}")
    return {"ip": ip, "urls": urls}


def containers() -> list[tuple[str, bool, str]] | None:
    """[(name, ok, text)] for the clipbox-* containers, None if Docker does not answer."""
    try:
        p = subprocess.run(["docker", "ps", "-a", "--format", "{{.Names}}\t{{.State}}\t{{.Status}}"],
                           capture_output=True, text=True, timeout=8)
    except (OSError, subprocess.SubprocessError):
        return None
    if p.returncode:
        return None
    rows = []
    for line in p.stdout.splitlines():
        name, state, status = (line.split("\t") + ["", ""])[:3]
        if not name.startswith("clipbox-") or name.endswith("-init"):
            continue
        ok = state == "running" and "unhealthy" not in status
        text = "ONLINE" if ok else ("KEINE ANTWORT" if state == "running" else state.upper())
        if ok and "starting" in status:
            text = "STARTET"
        rows.append((name.removeprefix("clipbox-").upper(), ok, text))
    return sorted(rows)


def ollama_models() -> list[dict] | None:
    try:
        with urllib.request.urlopen(OLLAMA + "/api/ps", timeout=1.5) as r:
            return json.load(r).get("models", [])
    except (OSError, ValueError):
        return None


def archive() -> dict:
    try:
        vods = [p for p in DATA.iterdir() if p.is_dir() and p.name.isdigit()]
        return {"vods": len(vods), "clips": sum(len(list((v / "clips").glob("*.mp4"))) for v in vods)}
    except OSError:
        return {}


def _vod_id(j: dict) -> str:
    m = re.search(r"(\d{6,})", str(j.get("vod", "")))
    return m.group(1) if m else ""


def _title(j: dict) -> str:
    vid = _vod_id(j)
    info = _json(DATA / vid / "info.json") if vid else None
    return ((info or {}).get("title") or (f"VOD {vid}" if vid else str(j.get("title", ""))))


def job_state() -> dict:
    jobs = _json(DATA / "jobs.json")
    if not isinstance(jobs, list) or not jobs:
        return {}
    active = [j for j in jobs if j.get("status") in ("running", "queued")]
    cur = next((j for j in active if j.get("status") == "running"), active[0] if active else jobs[-1])
    vid = _vod_id(cur)
    return {"running": cur.get("status") == "running", "status": cur.get("status", ""),
            "queued": sum(1 for j in active if j.get("status") == "queued"), "title": _title(cur),
            "metrics": (_json(DATA / vid / "metrics.json") if vid else None) or {},
            "run": (_json(DATA / vid / "run.json") if vid else None) or {}}


# ------------------------------------------------------------------- panels
def box(title: str, lines: list[str], width: int, height: int) -> list[str]:
    inner = width - 4
    head = f"{DIM}┌─{HI} {title} {DIM}" + "─" * max(0, width - 5 - len(title)) + f"┐{R}"
    body = [f"{DIM}│{R} {fit(line, inner)} {DIM}│{R}" for line in (lines + [""] * height)[:height]]
    return [head, *body, f"{DIM}└" + "─" * (width - 2) + f"┘{R}"]


def side_by_side(a: list[str], b: list[str]) -> list[str]:
    return [x + " " + y for x, y in zip(a, b)]


def p_gpu(s: Sampler, w: int) -> list[str]:
    g, bw = s.d.get("gpu") or {}, max(8, w - 34)
    if not g:
        return [f"{DIM}keine Grafikkarte gefunden{R}"]
    if g.get("asleep"):
        return [kv("STATUS", f"{CYA}STANDBY{R}  {DIM}Karte schläft, wacht bei Bedarf auf{R}"),
                kv("LAST", bar(None, bw)), "", "", kv("VERLAUF", graph(s.h_gpu, w - 14, 1.0))]
    busy = g.get("busy", 0.0)
    t = g.get("edge")
    temps = "   ".join(f"{DIM}{k.upper()}{R} {level((v - 40) / 60)}{v:.0f} °C{R}"
                       for k, v in g.items() if k in ("edge", "junction", "mem"))
    vram = ""
    if g.get("vram_total"):
        vram = f"{bar(g['vram_used'] / g['vram_total'], bw, 0.85, 0.95)} {W}{g['vram_used']:5.1f}{R} / {g['vram_total']:.0f} GB"
    power = "   ".join(x for x in (
        f"{W}{g['watt']:.0f} W{R}" + (f"{DIM} / {g['watt_cap']:.0f}{R}" if g.get("watt_cap") else "") if g.get("watt") else "",
        f"{DIM}LÜFTER{R} {g['fan']} U/min" if g.get("fan") is not None else "",
        f"{DIM}TAKT{R} {g['mhz']:.0f} MHz" if g.get("mhz") else "") if x)
    return [kv("LAST", f"{bar(busy, bw)} {W}{busy * 100:5.0f} %{R}"),
            kv("TEMP", temps or (f"{t:.0f} °C" if t else "")),
            kv("VRAM", vram), kv("ENERGIE", power), kv("VERLAUF", graph(s.h_gpu, w - 14, 1.0))]


def p_cpu(s: Sampler, w: int) -> list[str]:
    d, bw = s.d, max(8, w - 34)
    cpu = d.get("cpu", 0.0)
    temp = d.get("cpu_temp")
    line2 = "   ".join(x for x in (
        f"{level((temp - 45) / 45)}{temp:.0f} °C{R}" if temp is not None else "",
        f"{DIM}TAKT{R} {W}{d['cpu_ghz']:.2f} GHz{R} {DIM}max {d['cpu_ghz_max']:.2f}{R}" if d.get("cpu_ghz") else "",
        f"{DIM}LOAD{R} {' '.join(d.get('load') or [])}") if x)
    cores = "".join((DIM + "· " if c < 0.1 else level(c) + "░▒██"[min(3, int(c * 4))] + " ")
                    for c in d.get("cores", []))
    ram = ""
    if d.get("ram_total"):
        ram = f"{bar(d['ram_used'] / d['ram_total'], bw, 0.85, 0.95)} {W}{d['ram_used']:5.1f}{R} / {d['ram_total']:.0f} GB"
        if d.get("swap_used", 0) > 0.1:
            ram += f"  {YEL}SWAP {d['swap_used']:.1f}{R}"
    return [kv("LAST", f"{bar(cpu, bw)} {W}{cpu * 100:5.0f} %{R}"), kv("TEMP", line2),
            kv("KERNE", cores + R), kv("RAM", ram), kv("VERLAUF", graph(s.h_cpu, w - 14, 1.0))]


def p_llm(s: Sampler, w: int) -> list[str]:
    job = s.d.get("job") or {}
    m = job.get("metrics") or {}
    llm, wh = m.get("llm") or {}, m.get("whisper") or {}
    models = s.d.get("ollama")
    if models is None:
        model = f"{RED}OLLAMA ANTWORTET NICHT{R}"
    elif not models:
        model = f"{DIM}kein Modell im Speicher (lädt bei Bedarf){R}"
    else:
        model = "  ".join(f"{W}{x.get('name', '?')}{R} {DIM}{x.get('size_vram', 0) / 1e9:.1f} GB VRAM{R}" for x in models)
    last = llm.get("last") or {}
    live = job.get("running")
    tag = "" if live else f" {DIM}· alt{R}"
    tok = f"{DIM}noch keine Messung{R}"
    if last.get("gen_tok_s"):
        tok = (f"{W if live else G}{last['gen_tok_s']:6.1f}{R}  {DIM}Ø{R} {llm.get('gen_tok_s_avg', 0):.1f}"
               f"   {DIM}LESEN{R} {last.get('prefill_tok_s', 0):.0f} tok/s{tag}")
    calls = f"{DIM}–{R}"
    if llm.get("calls"):
        calls = (f"{llm['calls']} {DIM}Aufrufe{R}   {DIM}IN{R} {llm.get('input_tokens', 0) / 1000:.1f}k"
                 f"   {DIM}OUT{R} {llm.get('output_tokens', 0) / 1000:.1f}k Token")
    whisper = f"{DIM}–{R}"
    if wh.get("chunks_total"):
        whisper = (f"{wh.get('chunks_done', 0)}/{wh['chunks_total']} {DIM}Blöcke{R}   {W if live else G}"
                   f"{wh.get('factor', 0):.0f}×{R} {DIM}Echtzeit{R}   {DIM}{wh.get('model', '')}{R}")
    return [kv("MODELL", model), kv("TOKEN/S", tok), kv("VERLAUF", graph(s.h_tok, w - 14)),
            kv("UMSATZ", calls), kv("WHISPER", whisper)]


def p_net(s: Sampler, w: int) -> list[str]:
    d, gw = s.d, max(6, w - 34)
    link = f"{d['link']} Mbit" if (d.get("link") or 0) > 0 else ""
    ip = (d.get("addresses") or {}).get("ip", "")
    head = f"{W}{ip or 'KEIN NETZ'}{R}  {DIM}{d.get('iface', '')} {link}{R}"
    top = max(list(s.h_rx) + list(s.h_tx) + [1e5])
    disk = ""
    if d.get("disk_total"):
        used = 1 - d["disk_free"] / d["disk_total"]
        disk = f"{bar(used, max(8, w - 40), 0.85, 0.95)} {W}{d['disk_free']:.0f} GB{R} frei {DIM}/ {d['disk_total']:.0f}{R}"
    arch = d.get("archive") or {}
    io = (f"{DIM}R{R} {rate(d.get('io_r', 0)).strip()}  {DIM}W{R} {rate(d.get('io_w', 0)).strip()}"
          f"   {DIM}ARCHIV{R} {arch.get('vods', 0)} VODs · {arch.get('clips', 0)} Clips")
    return [kv("UPLINK", head),
            kv("↓ EMPF.", f"{W}{rate(d.get('rx', 0))}{R} {graph(s.h_rx, gw, top)}"),
            kv("↑ SEND.", f"{W}{rate(d.get('tx', 0))}{R} {graph(s.h_tx, gw, top)}"),
            kv("PLATTE", disk), kv("I/O", io)]


def p_job(s: Sampler, w: int) -> list[str]:
    job = s.d.get("job") or {}
    if not job:
        return [f"{DIM}■ BEREIT   noch kein Auftrag gelaufen{R}", "", ""]
    m, run = job.get("metrics") or {}, job.get("run") or {}
    title = job.get("title", "")
    if job.get("running"):
        head = f"{HI}▶ AKTIV{R}   {W}{title}{R}"
        if job.get("queued"):
            head += f"   {DIM}+{job['queued']} wartend{R}"
    elif job.get("status") == "queued":
        head = f"{YEL}■ WARTET{R}   {title}"
    else:
        word = {"done": f"{G}FERTIG", "error": f"{RED}FEHLER", "cancelled": f"{YEL}ABGEBROCHEN",
                "interrupted": f"{YEL}UNTERBROCHEN"}.get(job.get("status"), job.get("status", "?").upper())
        head = f"{DIM}■ BEREIT   zuletzt:{R} {word}{R}  {DIM}{title}{R}"
    stages, cur = m.get("stages") or {}, m.get("stage", "")
    chain = []
    for key, label in STAGES:
        if key not in stages and not (key == cur):
            if key == "upload":
                continue
            chain.append(f"{DIM}· {label}{R}")
        elif key == cur and job.get("running"):
            chain.append(f"{W}▶ {label}{R}")
        else:
            chain.append(f"{G}■ {label}{R}")
    detail = []
    wh, vi, re_ = m.get("whisper") or {}, m.get("vision") or {}, m.get("render") or {}
    if wh.get("chunks_total"):
        detail.append(f"{DIM}WHISPER{R} {wh.get('chunks_done', 0)}/{wh['chunks_total']}")
    if vi.get("frames"):
        detail.append(f"{DIM}BILDER{R} {vi.get('scored', 0)}/{vi['frames']}")
    if re_.get("total"):
        detail.append(f"{DIM}CLIPS{R} {re_.get('done', 0)}/{re_['total']}")
    if job.get("running") and m.get("started"):
        detail.append(f"{DIM}LAUFZEIT{R} {W}{span(time.time() - m['started'])}{R}")
        if run.get("eta_seconds") and run.get("started"):
            left = run["started"] + run["eta_seconds"] - time.time()
            detail.append(f"{DIM}REST{R} {W}~{span(left)}{R}" if left > 60 else f"{DIM}REST{R} gleich fertig")
    elif m.get("started") and m.get("updated"):
        detail.append(f"{DIM}DAUER{R} {span(m['updated'] - m['started'])}")
    return [head, f" {DIM}›{R} ".join(chain), "   ".join(detail)]


def p_services(s: Sampler) -> list[str]:
    cs = s.d.get("containers")
    if cs is None:
        return [f"{RED}● DOCKER ANTWORTET NICHT{R}"]
    if not cs:
        return [f"{YEL}● CONTAINER NOCH NICHT GESTARTET{R}"]
    return ["     ".join(f"{HI if ok else RED}●{R} {name} {DIM if ok else RED}{text}{R}" for name, ok, text in cs)]


def panels(s: Sampler, cols: int) -> list[str]:
    width = max(60, cols - 2)
    half, rest = (width - 1) // 2, width - 1 - (width - 1) // 2
    now = time.localtime()
    up = float((_read(PROC / "uptime") or "0").split()[0])
    urls = (s.d.get("addresses") or {}).get("urls") or []
    title = f"{HI}●{R} {W}{' '.join(socket.gethostname().upper())}{R}  {DIM}::{R}  {HI}ONLINE{R}  {DIM}seit {span(up)}{R}"
    clock = f"{DIM}{DAYS[now.tm_wday]} {time.strftime('%d.%m.%Y', now)}{R}  {W}{time.strftime('%H:%M:%S', now)}{R}"
    inner = width - 4
    head = [title + " " * max(1, inner - vlen(title) - vlen(clock)) + clock,
            f"{DIM}ZUGANG{R}  " + f"   {DIM}|{R}   ".join(f"{CYA}{u}{R}" for u in urls) if urls else f"{RED}KEIN NETZWERK{R}"]
    out = box("SYSTEM", head, width, 2)
    out += side_by_side(box("NEURAL CORE // GRAFIKKARTE", p_gpu(s, half - 4), half, 5),
                        box("PROZESSOR // SPEICHER", p_cpu(s, rest - 4), rest, 5))
    out += side_by_side(box("SPRACHMODELL // TOKEN", p_llm(s, half - 4), half, 5),
                        box("DATENSTROM // NETZ + PLATTE", p_net(s, rest - 4), rest, 5))
    out += box("AUFTRAG", p_job(s, inner), width, 3)
    out += box("SUBSYSTEME", p_services(s), width, 1)
    return [" " + line for line in out]


# --------------------------------------------------------------------- rain
class Rain:
    """Falling characters for the rows the panels leave free."""
    CHARS = "0123456789ABCDEF<>=+*:·≡×"

    def __init__(self):
        self.cols = self.rows = 0
        self.drops: list[list[float]] = []
        self.grid: list[list[str]] = []

    def resize(self, cols: int, rows: int) -> None:
        self.cols, self.rows = cols, rows
        self.drops = [[random.uniform(-rows, rows), random.uniform(0.25, 0.9), random.randint(3, 9)]
                      for _ in range(cols)]
        self.grid = [[random.choice(self.CHARS) for _ in range(cols)] for _ in range(rows)]

    def frame(self) -> list[str]:
        lines = []
        for d in self.drops:
            d[0] += d[1]
            if d[0] - d[2] > self.rows:
                d[0], d[1], d[2] = random.uniform(-6, 0), random.uniform(0.25, 0.9), random.randint(3, 9)
        for _ in range(max(1, self.cols // 12)):   # a few characters mutate each frame
            self.grid[random.randrange(self.rows)][random.randrange(self.cols)] = random.choice(self.CHARS)
        for y in range(self.rows):
            out, attr = [], ""
            for x in range(self.cols):
                dist = self.drops[x][0] - y
                if x % 2 or dist < 0 or dist > self.drops[x][2]:
                    want, ch = "", " "
                else:
                    want = W if dist < 1 else HI if dist < 3 else G if dist < 6 else DIM
                    ch = self.grid[y][x]
                if want != attr and ch != " ":
                    out.append(want)
                    attr = want
                out.append(ch)
            lines.append("".join(out) + R)
        return lines


# --------------------------------------------------------------------- main
FONTS = [(32, 16), (28, 14), (24, 12), (22, 11), (20, 10)]   # height x width, largest first


def pick_font() -> None:
    """Largest console font that still gives 118x32 characters on this monitor (tty1 only)."""
    size = _read(SYS / "class/graphics/fb0/virtual_size")
    if not size or os.environ.get("CLIPBOX_SETFONT") != "1":
        return
    try:
        width, height = (int(x) for x in size.split(","))
    except ValueError:
        return
    for h, w in FONTS:
        if width // w >= 118 and height // h >= 32 or (h, w) == FONTS[-1]:
            subprocess.run(["setfont", "-C", "/dev/tty1", f"Uni2-TerminusBold{h}x{w}"],
                           capture_output=True, timeout=10, check=False)
            return


def term_size() -> tuple[int, int]:
    try:
        ts = os.get_terminal_size(sys.stdout.fileno())
        return ts.columns, ts.lines
    except OSError:
        return 120, 33


def main() -> int:
    s = Sampler()
    if "--once" in sys.argv:
        m = re.fullmatch(r"(\d+)x(\d+)", sys.argv[-1])
        cols, rows = (int(m.group(1)), int(m.group(2))) if m else (120, 33)
        s.sample()
        time.sleep(1)
        s.sample()
        lines = panels(s, cols)
        rain = Rain()
        if rows - len(lines) > 0:
            rain.resize(cols, rows - len(lines))
            for _ in range(20):
                extra = rain.frame()
            lines += extra
        print("\n".join(lines[:rows]))
        return 0

    out = sys.stdout
    rain, size, fb = Rain(), (0, 0), None
    lines: list[str] = []
    next_sample = next_panel = 0.0
    while True:
        try:
            now = time.time()
            cur_fb = _read(SYS / "class/graphics/fb0/virtual_size")
            if cur_fb != fb:                      # monitor plugged in or changed
                fb = cur_fb
                pick_font()
            if term_size() != size:
                size = term_size()
                out.write("\033[?25l\033[2J")      # hide cursor, clear
                next_panel = 0.0
                rain.resize(0, 0)
            cols, rows = size
            if now >= next_sample:
                s.sample()
                next_sample = now + 2
            if now >= next_panel:
                lines = [fit(line, cols) for line in panels(s, cols)][:rows]
                out.write("\033[H" + "\n".join(lines))
                next_panel = now + 1
                free = rows - len(lines)
                if free > 0 and (rain.rows, rain.cols) != (free, cols):
                    rain.resize(cols, free)
            if rain.rows:
                out.write(f"\033[{len(lines) + 1};1H" + "\n".join(rain.frame()))
            out.flush()
        except Exception as e:  # noqa: BLE001 - the screen must never die
            try:
                out.write(f"\033[H\033[2JSTATUSANZEIGE: {e}\n")
                out.flush()
            except OSError:
                pass
            time.sleep(5)
        time.sleep(0.12)


if __name__ == "__main__":
    sys.exit(main())
