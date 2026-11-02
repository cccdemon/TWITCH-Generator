"""Host sensors for the web UI: GPU load/temperature/power/VRAM (amdgpu sysfs),
CPU temperature (k10temp/coretemp), load average and RAM.

Reads /sys and /proc, which Docker mounts read-only into the container, so no
extra privileges are needed. Every value is optional: a missing file simply
leaves its key out. On non-Linux hosts read() returns {}.
"""
from __future__ import annotations

import os
from pathlib import Path

SYS = Path(os.environ.get("TG_HOST_SYS", "/sys"))
PROC = Path(os.environ.get("TG_HOST_PROC", "/proc"))


def _read(p: Path) -> str | None:
    try:
        return p.read_text(encoding="ascii", errors="replace").strip()
    except OSError:
        return None


def _int(p: Path) -> int | None:
    v = _read(p)
    try:
        return int(v) if v is not None else None
    except ValueError:
        return None


def _hwmon_temps(hw: Path) -> dict[str, float]:
    out: dict[str, float] = {}
    for inp in sorted(hw.glob("temp*_input")):
        n = inp.name[: -len("_input")]
        label = _read(hw / f"{n}_label") or n
        v = _int(inp)
        if v is not None:
            out[label.lower()] = round(v / 1000.0, 1)
    return out


def gpu() -> dict:
    """First amdgpu card with a busy counter. Keys: name, busy_pct, temps{edge,junction,mem},
    power_w, power_cap_w, fan_rpm, sclk_mhz, mclk_mhz, vram_used_gb, vram_total_gb."""
    for dev in sorted(SYS.glob("class/drm/card*/device")):
        busy = _int(dev / "gpu_busy_percent")
        if busy is None:
            continue
        g: dict = {"busy_pct": busy}
        used, total = _int(dev / "mem_info_vram_used"), _int(dev / "mem_info_vram_total")
        if total:
            g["vram_used_gb"] = round((used or 0) / 1e9, 1)
            g["vram_total_gb"] = round(total / 1e9, 1)
            g["vram_pct"] = round(100 * (used or 0) / total)
        for hw in sorted(dev.glob("hwmon/hwmon*")):
            g["name"] = _read(hw / "name") or "gpu"
            temps = _hwmon_temps(hw)
            if temps:
                g["temps"] = temps
            p = _int(hw / "power1_average") or _int(hw / "power1_input")
            if p is not None:
                g["power_w"] = round(p / 1e6)
            cap = _int(hw / "power1_cap")
            if cap:
                g["power_cap_w"] = round(cap / 1e6)
            fan = _int(hw / "fan1_input")
            if fan is not None:
                g["fan_rpm"] = fan
            f1, f2 = _int(hw / "freq1_input"), _int(hw / "freq2_input")
            if f1:
                g["sclk_mhz"] = round(f1 / 1e6)
            if f2:
                g["mclk_mhz"] = round(f2 / 1e6)
            break
        return g
    return {}


def cpu() -> dict:
    """CPU temperature (k10temp Tctl/Tdie or coretemp package) and load averages."""
    out: dict = {}
    for hw in sorted(SYS.glob("class/hwmon/hwmon*")):
        name = (_read(hw / "name") or "").lower()
        if name in ("k10temp", "zenpower", "coretemp"):
            temps = _hwmon_temps(hw)
            if temps:
                out["temps"] = temps
                out["temp_c"] = temps.get("tctl") or temps.get("tdie") or temps.get("package id 0") \
                    or next(iter(temps.values()))
            break
    la = _read(PROC / "loadavg")
    if la:
        parts = la.split()
        try:
            out["load1"], out["load5"], out["load15"] = (float(parts[0]), float(parts[1]), float(parts[2]))
        except (ValueError, IndexError):
            pass
    out["ncpu"] = os.cpu_count()
    return out


def mem() -> dict:
    txt = _read(PROC / "meminfo")
    if not txt:
        return {}
    kv: dict[str, int] = {}
    for line in txt.splitlines():
        k, _, v = line.partition(":")
        try:
            kv[k.strip()] = int(v.split()[0])
        except (ValueError, IndexError):
            continue
    total, avail = kv.get("MemTotal"), kv.get("MemAvailable")
    if not total:
        return {}
    used = total - (avail or 0)
    return {"total_gb": round(total / 1e6, 1), "used_gb": round(used / 1e6, 1), "used_pct": round(100 * used / total)}


def read() -> dict:
    try:
        return {"gpu": gpu(), "cpu": cpu(), "mem": mem()}
    except Exception:  # noqa: BLE001 — sensors are decoration, never an error source
        return {}
