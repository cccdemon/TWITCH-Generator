"""TikTok and Instagram posting flows, driven through the real app UI.

Mapped by hand on a Motorola Edge 30 Neo (1080x2400, Android 14, English UI),
TikTok 47.7.x and Instagram 455.x. Elements are found by their visible label or
resource id, coordinates only where the app exposes nothing better (TikTok's
gallery selection circle). Any screen we don't recognise ends the run with a
screenshot instead of guessing.

dry_run walks the whole flow, fills in the caption and stops before the final
tap, then force-stops the app (nothing is published).
"""
from __future__ import annotations

import re
import time
from pathlib import Path
import unicodedata

from .phone import Node, Phone, PhoneError, ScreenBusy, label_seconds

TIKTOK = "com.zhiliaoapp.musically"
INSTAGRAM = "com.instagram.android"


def _norm(s: str) -> str:
    return unicodedata.normalize("NFC", s).replace("⁠", "").strip()


class Flow:
    pkg = ""
    name = ""

    def __init__(self, phone: Phone, account: str, dry_run: bool, log, shot_path=None):
        self.p = phone
        self.account = account
        self.dry_run = dry_run
        self.log = log
        self.shot_path = shot_path  # screen at the end of the run (success or failure)

    # ----------------------------------------------------------- helpers
    def popups(self, nodes: list[Node]) -> bool:
        """Dismiss a known interruption. True if something was handled."""
        f = Phone.find
        msg = f(nodes, rid="permission_message")
        if msg:
            text = msg[0].text.lower()
            wanted = any(k in text for k in ("photos", "videos", "pictures", "record audio", "camera"))
            if wanted:
                btn = (f(nodes, rid="permission_allow_all_button") or f(nodes, rid="permission_allow_foreground_only_button")
                       or f(nodes, rid="permission_allow_button"))
            else:
                btn = f(nodes, rid="permission_deny.*")
            if btn:
                self.log(f"  Systemdialog: {msg[0].text[:60]} -> {btn[0].text}")
                self.p.tap(btn[0])
                time.sleep(1.5)
                return True
        return self.app_popups(nodes)

    def app_popups(self, nodes: list[Node]) -> bool:
        return False

    def wait(self, check, what: str, timeout: float = 30, back_after: int = 0, unstick=None):
        """Poll the UI until check(nodes) is truthy; handle popups meanwhile.

        back_after>0: press BACK every that many misses (to climb out of a
        screen the app restored from last time).
        unstick: called when the screen is unreadable because a video keeps it
        busy (ScreenBusy); it should stop the video.
        """
        end = time.time() + timeout
        misses = reads = unreadable = 0
        while time.time() < end:
            try:
                nodes = self.p.nodes(attempts=1)  # this loop is the retry; keeps the deadline honest
            except PhoneError as e:
                unreadable += 1
                if unstick and isinstance(e, ScreenBusy):
                    unstick()
                else:
                    time.sleep(1)
                continue
            reads += 1
            if self.popups(nodes):
                continue
            hit = check(nodes)
            if hit:
                return hit
            misses += 1
            if back_after and misses % back_after == 0:
                self.p.key("BACK")
            time.sleep(1)
        if unreadable and not reads:
            raise PhoneError(f"{self.name}: Bildschirm bei „{what}“ nicht auslesbar (läuft ein Video?)")
        raise PhoneError(f"{self.name}: Zeitüberschreitung bei „{what}“")

    def _activity(self) -> str:
        try:
            return self.p.top_activity()
        except PhoneError:
            return ""

    def after_publish(self, page_open, what: str, settle: float, act_before: str = "",
                      uploading=None, timeout: float = 60) -> str:
        """After the final tap: wait for the publish page to close, then let the upload run.

        Both apps land on an autoplaying feed afterwards, which uiautomator cannot
        read. The post is out at that point, so an unreadable screen here means
        "page closed", not "failed" (reporting a failure made a retry publish the
        clip twice). Only a publish page that is still readable and still open
        after `timeout` is an error.
        uploading(nodes): truthy while the app still shows upload progress; lets
        the settle phase end early once it is gone.
        """
        end = time.time() + timeout
        busy, seen = 0, False
        while time.time() < end:
            try:
                nodes = self.p.nodes(attempts=1)
            except ScreenBusy:
                busy += 1
                act = self._activity()
                if busy >= 2 or (act and act_before and act != act_before):
                    self.log(f"  Seite nicht mehr lesbar, Video-Feed ({act or '?'}): werte als geschlossen")
                    break
                continue
            except PhoneError:
                time.sleep(1)
                continue
            busy = 0
            if self.popups(nodes):
                continue
            if not page_open(nodes):
                seen = True
                break
            time.sleep(1)
        else:
            raise PhoneError(f"{self.name}: Zeitüberschreitung bei „{what}“")

        # The upload continues in the foreground app; don't kill it early.
        start = time.time()
        while time.time() < start + settle:
            try:
                nodes = self.p.nodes(attempts=1)
                if self.popups(nodes):
                    continue
                if uploading and not uploading(nodes) and time.time() > start + 15:
                    break
            except PhoneError:
                pass
            time.sleep(3)
        return "gepostet" if seen else "gepostet (Abschluss nicht ablesbar – Screenshot prüfen)"

    def first(self, **kw):
        return lambda nodes: (Phone.find(nodes, **kw) or [None])[0]

    def type_caption(self, caption: str, field: Node) -> None:
        self.p.tap(field)
        time.sleep(1.5)
        # Trailing space closes TikTok's hashtag suggestion list.
        self.p.type_text(caption.rstrip() + " ")

    def check_caption(self, nodes: list[Node], rid: str, caption: str) -> None:
        field = Phone.find(nodes, rid=rid)
        got = _norm(field[0].text) if field else ""
        first_line = _norm(caption.strip().splitlines()[0]) if caption.strip() else ""
        if first_line and not got.startswith(first_line[:40]):
            raise PhoneError(f"{self.name}: Text kam nicht an (Feld: {got[:60]!r})")

    def finish(self) -> None:
        if self.shot_path is not None:
            try:
                self.shot_path.parent.mkdir(parents=True, exist_ok=True)
                self.shot_path.write_bytes(self.p.screenshot())
            except (PhoneError, OSError):
                pass
        try:
            self.p.stop(self.pkg)
            self.p.home()
        except PhoneError:
            pass


# --------------------------------------------------------------- TikTok
class TikTokFlow(Flow):
    pkg = TIKTOK
    name = "TikTok"

    def app_popups(self, nodes):
        f = Phone.find
        for label in ("No thanks", "Not now", "Maybe later"):
            hit = f(nodes, any_=label)
            if hit:
                self.log(f"  TikTok-Popup: {label}")
                self.p.tap(hit[0])
                time.sleep(2)
                return True
        if f(nodes, text="Your avatar, your style") or f(nodes, text="Create promotion") \
                or f(nodes, text="Ads Creation Drop-off Survey"):
            self.log("  TikTok-Werbung geschlossen")
            self.p.key("BACK")
            time.sleep(1.5)
            return True
        return False

    def pause_feed(self) -> None:
        """One tap on the feed video pauses it; a playing one can keep the screen unreadable."""
        self.log("  Feed-Video pausiert (Bildschirm war nicht auslesbar)")
        self.p.tap(540, 1200)
        time.sleep(1.5)

    def run(self, clip_path: str, clip_name: str, caption: str, opts: dict) -> str:
        f = Phone.find
        p = self.p
        p.wake()
        dur_ms = p.push_clip(clip_path, clip_name)
        self.log(f"  Clip auf dem Handy ({dur_ms / 1000:.1f} s)")
        p.launch(self.pkg)
        try:
            home = self.wait(lambda n: f(n, desc="Create") and f(n, desc="Profile"), "Startseite",
                             timeout=45, back_after=4, unstick=self.pause_feed)
            p.tap(home[0])
            handle = self.wait(lambda n: f(n, text=r"@[\w.]+"), "Profil", timeout=20)
            seen = [h.text for h in handle]
            if f"@{self.account}" not in seen:
                raise PhoneError(f"TikTok: falscher Account angemeldet ({', '.join(seen)}), erwartet @{self.account}")
            self.log(f"  Account @{self.account} ok")

            p.tap(self.wait(self.first(desc="Create"), "Create-Knopf"))
            p.tap(self.wait(self.first(rid="upload_hot_area"), "Kamera / Galerie-Knopf"))

            labels = self.wait(lambda n: f(n, text="Recents") and f(n, rid="gtc", text=r"\d+:\d{2}"), "Galerie")
            tiles = sorted(f(labels, rid="gtc", text=r"\d+:\d{2}"), key=lambda n: (n.bounds[1], n.bounds[0]))
            first = tiles[0]
            secs = label_seconds(first.text)
            if secs is None or abs(secs - dur_ms / 1000) > 1.5:
                raise PhoneError(f"TikTok: erstes Galerie-Video ist {first.text}, Clip hat {dur_ms / 1000:.0f} s")
            # Selection circle sits in the tile's top-right corner; TikTok exposes no node for it.
            x2, y2 = first.bounds[2], first.bounds[3]
            p.tap(x2 - 49, y2 - 310)
            time.sleep(1.5)
            nxt = self.wait(lambda n: f(n, text=r"Next \(1\)") or (not f(n, text="Recents") and f(n, text="Next")),
                            "Auswahl")
            p.tap(nxt[0])

            # Editor: "Next" next to "Story" / "Your Story" (TikTok renamed it; never tap Story).
            ed = self.wait(lambda n: f(n, text=r"(Your )?Story") and f(n, text="Next"), "Editor", timeout=40)
            p.tap(f(ed, text="Next")[0])

            # Return the whole screen (`A and B` would return only B's nodes).
            post = self.wait(lambda n: n if (f(n, rid="hb2") and f(n, text="Post")) else None,
                             "Post-Seite", timeout=40)
            self.type_caption(caption, f(post, rid="hb2")[0])
            p.close_keyboard()  # BACK only if a keyboard is open; else it would leave the page
            time.sleep(1.5)
            post = self.wait(lambda n: n if (f(n, text="Post") and f(n, rid="hb2")) else None,
                             "Post-Seite nach Text")
            self.check_caption(post, "hb2", caption)
            if not f(post, text="Everyone can view this post"):
                raise PhoneError("TikTok: Sichtbarkeit ist nicht „Everyone“")
            if self.dry_run:
                self.log("  Probelauf: stoppe vor „Post“")
                return "Probelauf ok – bis vor „Post“"

            act = self._activity()
            p.tap(f(post, text="Post")[0])
            self.log(f"  Post getippt, warte auf Upload ({act or '?'})")
            return self.after_publish(lambda n: f(n, rid="hb2"), "Post-Seite schließt",
                                      settle=45, act_before=act)
        finally:
            self.finish()


# ------------------------------------------------------------ Instagram
class InstagramFlow(Flow):
    pkg = INSTAGRAM
    name = "Instagram"

    def app_popups(self, nodes):
        f = Phone.find
        if f(nodes, text="To use Location Services.*") and f(nodes, text="Continue"):
            self.log("  Instagram: Standort-Hinweis -> Continue")
            self.p.tap(f(nodes, text="Continue")[0])
            time.sleep(2)
            return True
        # Leftover draft from an earlier run: start fresh (Instagram keeps the draft).
        if f(nodes, text="Keep editing your draft.*") and f(nodes, any_="Start new video"):
            self.log("  Instagram: alter Entwurf -> Start new video")
            self.p.tap(f(nodes, any_="Start new video")[0])
            time.sleep(2)
            return True
        if f(nodes, text="Open settings") and f(nodes, text="Cancel"):
            self.p.tap(f(nodes, text="Cancel")[0])
            time.sleep(1.5)
            return True
        if f(nodes, text="New ways to reuse") or f(nodes, rid="bb_primary_action.*", desc="OK"):
            ok = f(nodes, desc="OK", rid="bb_primary_action.*") or f(nodes, text="OK")
            if ok:
                self.log("  Instagram-Hinweis -> OK")
                self.p.tap(ok[0])
                time.sleep(1.5)
                return True
        for label in ("Not now", "Not Now"):
            hit = f(nodes, any_=label)
            if hit:
                self.log(f"  Instagram-Popup: {label}")
                self.p.tap(hit[0])
                time.sleep(1.5)
                return True
        return False

    @staticmethod
    def toggle_row(nodes: list[Node], subtitle_rx: str) -> tuple[Node | None, bool | None, bool]:
        """(switch, checked, safe_to_tap) for the share-to row whose subtitle matches.

        Current app (Compose): the switch is a narrow checkable View at the right edge of
        the row. The "Save draft / Share" bar is pinned over the bottom of the list, so a
        switch partly under it must NOT be tapped (a tap there hits Share: that published
        a reel during a dry run once). safe_to_tap is False until the whole switch sits
        above that bar; the caller scrolls first.
        """
        sub = Phone.find(nodes, text=subtitle_rx)
        if not sub:
            return None, None, False
        y = (sub[0].bounds[1] + sub[0].bounds[3]) // 2
        row = [n for n in nodes
               if (n.checkable or n.cls in ("Switch", "CompoundButton", "ToggleButton"))
               and n.bounds[0] > 540 and n.bounds[2] - n.bounds[0] < 300
               and n.bounds[1] - 60 <= y <= n.bounds[3] + 60]
        if not row:
            return None, None, False
        sw = min(row, key=lambda n: abs(n.center[1] - y))
        bar = Phone.find(nodes, rid="share_button") + Phone.find(nodes, text="Save draft")
        bar_top = min((n.bounds[1] for n in bar), default=None)
        if bar_top is None:  # cannot see the Share bar: do not guess where it is
            return sw, sw.checked, False
        safe = sw.bounds[3] < bar_top - 20 and sw.bounds[1] > 250
        return sw, sw.checked, safe

    def _set_toggle(self, subtitle_rx: str, want: bool, label: str) -> None:
        f = Phone.find
        for _ in range(4):
            nodes = self.p.nodes()
            sw, state, safe = self.toggle_row(nodes, subtitle_rx)
            if sw is not None and state == want:
                self.log(f"  {label}: {'an' if want else 'aus'}")
                return
            if sw is None or not safe:
                self.p.swipe(540, 1700, 540, 1100)
                time.sleep(1)
                continue
            if state == want:
                self.log(f"  {label}: {'an' if want else 'aus'}")
                return
            self.p.tap(sw)
            time.sleep(1.5)
            nodes = self.p.nodes()
            dont = f(nodes, text="Don.t share this reel")
            if dont:
                self.p.tap(dont[0])
                time.sleep(1.5)
        raise PhoneError(f"Instagram: Schalter „{label}“ nicht gefunden oder nicht umstellbar")

    def run(self, clip_path: str, clip_name: str, caption: str, opts: dict) -> str:
        f = Phone.find
        p = self.p
        p.wake()
        dur_ms = p.push_clip(clip_path, clip_name)
        self.log(f"  Clip auf dem Handy ({dur_ms / 1000:.1f} s)")
        p.launch(self.pkg)
        try:
            tab = self.wait(self.first(rid="profile_tab"), "Startseite", timeout=45, back_after=4)
            p.tap(tab)
            title = self.wait(self.first(rid="action_bar_title"), "Profil", timeout=20)
            if title.text.strip() != self.account:
                raise PhoneError(f"Instagram: falscher Account angemeldet ({title.text}), erwartet {self.account}")
            self.log(f"  Account {self.account} ok")

            p.tap(self.wait(self.first(desc="Create New"), "Create-Knopf"))
            p.tap(self.wait(self.first(desc="Create new reel"), "Reel-Auswahl"))

            def first_tile(n):
                tiles = sorted(f(n, desc="Unselected Video thumbnail.*"), key=lambda t: (t.bounds[1], t.bounds[0]))
                if not (f(n, text="New reel") and tiles):
                    return None
                x1, y1, x2, y2 = tiles[0].bounds
                lab = [t for t in f(n, text=r"\d+:\d{2}")
                       if x1 <= t.center[0] <= x2 and y1 <= t.center[1] <= y2]
                return (tiles[0], lab[0]) if lab else None

            first, lab = self.wait(first_tile, "Galerie", timeout=30)
            secs = label_seconds(lab.text)
            if secs is None or abs(secs - dur_ms / 1000) > 1.5:
                raise PhoneError(f"Instagram: erstes Galerie-Video ist {lab.text}, Clip hat {dur_ms / 1000:.0f} s")
            p.tap(first)

            ed = self.wait(lambda n: f(n, desc="Next", rid="clips_right_action.*"), "Editor", timeout=40)
            p.tap(ed[0])

            share = self.wait(self.first(rid="caption_input_text.*"), "Teilen-Seite", timeout=40)
            self.type_caption(caption, share)
            # Older app: text "OK" in action_bar_button; newer: blue check, desc "Done", no rid.
            ok = self.wait(lambda n: f(n, text="OK", rid="action_bar_button.*") or f(n, desc="Done"),
                           "Text bestätigen", timeout=10)
            p.tap(ok[0])
            time.sleep(1.5)
            nodes = self.wait(lambda n: f(n, rid="share_button") and n, "Teilen-Seite nach Text")
            self.check_caption(nodes, "caption_input_text.*", caption)
            aud = f(nodes, rid="inline_subtitle")
            if aud and aud[0].text.strip() != "Everyone":
                raise PhoneError(f"Instagram: Audience ist „{aud[0].text}“, nicht „Everyone“")

            if opts.get("inspect"):  # read-only: save the share screen, never tap a toggle or Share
                out = Path(opts["inspect"])
                out.mkdir(parents=True, exist_ok=True)
                (out / "share_1.xml").write_text(p.last_xml, encoding="utf-8")
                p.swipe(540, 1700, 540, 1100)
                time.sleep(1.5)
                p.nodes()
                (out / "share_2.xml").write_text(p.last_xml, encoding="utf-8")
                (out / "share_2.png").write_bytes(p.screenshot())
                return "inspect ok"

            self._set_toggle(r"Facebook · .*", False, "Facebook")
            self._set_toggle(r"Threads · .*", bool(opts.get("threads", True)), "Threads")

            if self.dry_run:
                self.log("  Probelauf: stoppe vor dem Teilen")
                return "Probelauf ok – bis vor dem Teilen"

            btn = self.wait(self.first(rid="share_button"), "Teilen-Knopf")
            act = self._activity()
            p.tap(btn)
            self.log(f"  Teilen getippt, warte auf Upload ({act or '?'})")
            # "Sharing to Reels…" runs in the foreground app; the reels feed it lands on is unreadable.
            return self.after_publish(lambda n: f(n, rid="caption_input_text.*"), "Teilen-Seite schließt",
                                      settle=90, act_before=act,
                                      uploading=lambda n: f(n, text=r"Sharing to Reels.*"))
        finally:
            self.finish()


FLOWS = {"tiktok": TikTokFlow, "instagram": InstagramFlow}
