"""Thin adb wrapper: read the UI tree, find elements, tap, type, push files.

Talks to the adb server already running on the host (network_mode: host, the
server owns the USB device). Every call is a short subprocess; nothing here
keeps state besides the serial.
"""
from __future__ import annotations

import base64
import re
import shlex
import subprocess
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass

ADB_IME = "com.android.adbkeyboard/.AdbIME"
REMOTE_DIR = "/sdcard/Movies/ClipPoster"


class PhoneError(RuntimeError):
    """A step could not be completed; the message is shown to the user."""


class ScreenBusy(PhoneError):
    """The UI tree could not be read because the screen never went idle (video playing)."""


@dataclass
class Node:
    text: str
    desc: str
    rid: str          # resource id without the package prefix
    cls: str
    clickable: bool
    checked: bool
    bounds: tuple[int, int, int, int]
    checkable: bool = False

    @property
    def center(self) -> tuple[int, int]:
        x1, y1, x2, y2 = self.bounds
        return (x1 + x2) // 2, (y1 + y2) // 2

    def label(self) -> str:
        return self.text or self.desc or self.rid


class Phone:
    def __init__(self, serial: str, log=print):
        self.serial = serial
        self.log = log

    # ---------------------------------------------------------------- adb
    def _adb(self, *args: str, timeout: float = 60, binary: bool = False):
        cmd = ["adb", "-s", self.serial, *args]
        try:
            p = subprocess.run(cmd, capture_output=True, timeout=timeout, text=not binary)
        except subprocess.TimeoutExpired as e:
            raise PhoneError(f"adb timeout: {' '.join(args)[:80]}") from e
        if p.returncode != 0:
            err = p.stderr if not binary else p.stderr.decode(errors="replace")
            raise PhoneError(f"adb {' '.join(args)[:60]} failed: {err.strip()[:200]}")
        return p.stdout

    def sh(self, *args: str, timeout: float = 60) -> str:
        return self._adb("shell", *args, timeout=timeout)

    def online(self) -> bool:
        try:
            return self._adb("get-state", timeout=10).strip() == "device"
        except PhoneError:
            return False

    # ----------------------------------------------------------- UI tree
    def nodes(self, attempts: int = 4) -> list[Node]:
        # uiautomator waits for an idle UI. While a feed video plays it may never
        # get there: the dump gives up after ~10 s with "could not get idle state"
        # (measured: 3 of 10 TikTok feed videos, every Instagram reels feed).
        # Retrying does not help then, so callers that poll pass attempts=1 and
        # react to ScreenBusy themselves.
        xml, start, busy = "", -1, False
        for attempt in range(attempts):
            out = self.sh("uiautomator dump /sdcard/cp_ui.xml 2>&1", timeout=30)
            if "dumped to" in out:
                xml = self._adb("exec-out", "cat", "/sdcard/cp_ui.xml", timeout=30)
                start = xml.find("<?xml")
                if start >= 0:
                    break
            busy = "idle state" in out
            if attempt < attempts - 1:
                time.sleep(1 + attempt)
        if start < 0:
            raise (ScreenBusy if busy else PhoneError)("Bildschirm des Handys nicht auslesbar (uiautomator)")
        self.last_xml = xml[start:]  # kept for read-only inspection runs
        return parse_nodes(self.last_xml)

    def top_activity(self) -> str:
        out = self.sh("dumpsys", "activity", "activities")
        m = re.search(r"topResumedActivity=\S+ \S+ (\S+)", out)
        return m.group(1) if m else ""

    @staticmethod
    def find(nodes: list[Node], text: str | None = None, desc: str | None = None,
             rid: str | None = None, any_: str | None = None) -> list[Node]:
        """Regex full-match on text / content-desc / resource id. any_ = text or desc."""
        def ok(pat, val):
            return pat is None or re.fullmatch(pat, val, re.S) is not None
        hits = []
        for n in nodes:
            if any_ is not None and not (re.fullmatch(any_, n.text, re.S) or re.fullmatch(any_, n.desc, re.S)):
                continue
            if ok(text, n.text) and ok(desc, n.desc) and ok(rid, n.rid):
                hits.append(n)
        return hits

    # ------------------------------------------------------------ input
    def tap(self, x: int | Node, y: int | None = None) -> None:
        if isinstance(x, Node):
            self.log(f"  tap '{x.label()[:40]}'")
            x, y = x.center
        self.sh("input", "tap", str(x), str(y))

    def key(self, code: str) -> None:
        self.sh("input", "keyevent", code)

    def swipe(self, x1: int, y1: int, x2: int, y2: int, ms: int = 300) -> None:
        self.sh("input", "swipe", str(x1), str(y1), str(x2), str(y2), str(ms))

    def type_text(self, text: str) -> None:
        """Unicode-safe typing via ADBKeyBoard; the previous keyboard is restored."""
        prev = self.sh("settings", "get", "secure", "default_input_method").strip()
        self.sh("ime", "enable", ADB_IME)
        self.sh("ime", "set", ADB_IME)
        try:
            time.sleep(1.5)
            msg = base64.b64encode(text.encode("utf-8")).decode()
            self.sh("am", "broadcast", "-a", "ADB_INPUT_B64", "--es", "msg", msg)
            time.sleep(1.5)
        finally:
            if prev and prev != "null" and prev != ADB_IME:
                self.sh("ime", "set", prev)

    def keyboard_shown(self) -> bool:
        """True while a soft keyboard is on screen (BACK would close it, not the page)."""
        try:
            return "mInputShown=true" in self.sh("dumpsys", "input_method", timeout=10)
        except PhoneError:
            return False

    def close_keyboard(self) -> None:
        """Press BACK only if a keyboard is open; otherwise BACK leaves the screen."""
        if self.keyboard_shown():
            self.key("BACK")

    def screenshot(self) -> bytes:
        return self._adb("exec-out", "screencap", "-p", binary=True, timeout=30)

    # ------------------------------------------------------------- apps
    def launch(self, pkg: str) -> None:
        self.sh("am", "force-stop", pkg)
        time.sleep(1)
        self.sh("monkey", "-p", pkg, "-c", "android.intent.category.LAUNCHER", "1")

    def stop(self, pkg: str) -> None:
        self.sh("am", "force-stop", pkg)

    def home(self) -> None:
        self.key("HOME")

    def wake(self) -> None:
        """Screen on, keyguard off (no secure lock on this device)."""
        self.key("KEYCODE_WAKEUP")
        self.sh("wm", "dismiss-keyguard")

    # ------------------------------------------------------------ media
    def push_clip(self, local_path: str, name: str) -> int:
        """Replace the phone-side clip folder with this one file, index it, return duration ms.

        Gallery pickers sort newest first, so the freshly pushed and scanned file is
        the first tile. Only our own folder is touched; app-saved copies elsewhere
        (DCIM/Camera) stay, they are older and sort behind.
        """
        # Index rows of deleted files would linger as blank tiles; drop them first.
        self.purge_index()
        self.sh("rm", "-rf", REMOTE_DIR)
        self.sh("mkdir", "-p", REMOTE_DIR)
        remote = f"{REMOTE_DIR}/{name}"
        self._adb("push", local_path, remote, timeout=300)
        self.sh("touch", remote)
        self.sh("am", "broadcast", "-a", "android.intent.action.MEDIA_SCANNER_SCAN_FILE",
                "-d", f"file://{remote}")
        indexed = remote.replace("/sdcard", "/storage/emulated/0")
        where = shlex.quote(f"_data='{indexed}'")  # adb shell re-parses: quote for the phone's sh
        for _ in range(30):
            time.sleep(1)
            row = self.sh("content", "query", "--uri", "content://media/external/video/media",
                          "--projection", "_data:duration", "--where", where)
            m = re.search(r"duration=(\d+)", row)
            if m:
                return int(m.group(1))
        raise PhoneError("Clip taucht nach 30 s nicht in der Galerie des Handys auf")

    def purge_index(self) -> None:
        """Drop media-index rows of our folder whose files are gone (keeps pickers clean)."""
        where = shlex.quote("_data LIKE '/storage/emulated/0/Movies/ClipPoster/%'")
        self.sh("content", "delete", "--uri", "content://media/external/video/media", "--where", where)


def label_seconds(label: str) -> int | None:
    """Gallery duration label ('00:23' TikTok, '0:23' Instagram) -> seconds."""
    m = re.fullmatch(r"(\d+):(\d{2})", label.strip())
    return int(m.group(1)) * 60 + int(m.group(2)) if m else None

def parse_nodes(xml: str) -> list[Node]:
    """uiautomator XML -> Node list (module level so recorded screens can be tested offline)."""
    out: list[Node] = []
    for n in ET.fromstring(xml).iter("node"):
        b = [int(x) for x in re.findall(r"\d+", n.get("bounds", ""))]
        if len(b) != 4:
            continue
        out.append(Node(
            text=n.get("text") or "",
            desc=n.get("content-desc") or "",
            rid=(n.get("resource-id") or "").split("/")[-1],
            cls=(n.get("class") or "").split(".")[-1],
            clickable=n.get("clickable") == "true",
            checked=n.get("checked") == "true",
            bounds=(b[0], b[1], b[2], b[3]),
            checkable=n.get("checkable") == "true",
        ))
    return out
