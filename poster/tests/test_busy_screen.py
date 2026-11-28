"""Screens uiautomator cannot read (no phone needed).

`uiautomator dump` waits for an idle UI. An autoplaying feed never gets there
("ERROR: could not get idle state"), measured on the phone: 3 of 10 TikTok feed
videos, every Instagram reels feed. Two consequences the flows must survive:

* TikTok start page: unreadable until the video is paused (one tap).
* After the final tap both apps land on such a feed: the post is out, so an
  unreadable screen there must not be reported as a failure (a retry would
  publish the clip a second time).
"""
import pytest

from app import flows
from app.flows import Flow, TikTokFlow
from app.phone import Node, Phone, PhoneError, ScreenBusy


class Clock:
    def __init__(self):
        self.t = 1000.0

    def time(self):
        return self.t

    def sleep(self, s):
        self.t += s


def node(**kw):
    base = dict(text="", desc="", rid="", cls="View", clickable=True, checked=False, bounds=(0, 0, 10, 10))
    base.update(kw)
    return Node(**base)


class FakePhone:
    """Plays back a list of screens; an item may be a node list or an exception class."""

    def __init__(self, clock, screens, activity="app/.Share"):
        self.clock, self.screens, self.activity = clock, list(screens), activity
        self.taps, self.attempts = [], []

    def nodes(self, attempts=4):
        self.attempts.append(attempts)
        item = self.screens.pop(0) if len(self.screens) > 1 else self.screens[0]
        if isinstance(item, type) and issubclass(item, Exception):
            self.clock.sleep(11 * attempts)  # a busy dump takes ~11 s on the phone
            raise item("Bildschirm des Handys nicht auslesbar (uiautomator)")
        self.clock.sleep(2)
        return item

    def tap(self, x, y=None):
        self.taps.append((x, y))
        if self.screens and self.screens[0] is ScreenBusy:
            self.screens.pop(0)  # pausing the video makes the next dump readable

    def key(self, code):
        pass

    def top_activity(self):
        return self.activity


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(flows, "time", c)
    return c


def make(cls, phone):
    return cls(phone, "acct", False, lambda s: None)


HOME = [node(desc="Profile"), node(desc="Create")]


def test_wait_reads_once_per_round_so_the_deadline_holds(clock):
    phone = FakePhone(clock, [ScreenBusy])
    with pytest.raises(PhoneError) as e:
        make(Flow, phone).wait(lambda n: None, "Startseite", timeout=45)
    assert set(phone.attempts) == {1}
    assert clock.t - 1000 < 45 + 15
    assert "nicht auslesbar" in str(e.value)  # says what really happened, not just "timeout"


def test_tiktok_pauses_the_feed_video_when_the_start_page_is_unreadable(clock):
    phone = FakePhone(clock, [ScreenBusy, HOME])
    f = make(TikTokFlow, phone)
    hit = f.wait(lambda n: Phone.find(n, desc="Profile"), "Startseite", timeout=45, unstick=f.pause_feed)
    assert hit[0].desc == "Profile"
    assert len(phone.taps) == 1


def test_after_publish_page_seen_closed(clock):
    page = [node(rid="hb2")]
    phone = FakePhone(clock, [page, page, HOME])
    msg = make(Flow, phone).after_publish(lambda n: Phone.find(n, rid="hb2"), "Post-Seite schließt",
                                          settle=10, act_before="app/.Share")
    assert msg == "gepostet"


def test_after_publish_unreadable_feed_counts_as_posted(clock):
    phone = FakePhone(clock, [ScreenBusy])
    msg = make(Flow, phone).after_publish(lambda n: Phone.find(n, rid="hb2"), "Teilen-Seite schließt",
                                          settle=10, act_before="app/.Share")
    assert msg.startswith("gepostet") and "Screenshot" in msg


def test_after_publish_activity_change_is_enough(clock):
    phone = FakePhone(clock, [ScreenBusy], activity="app/.MainTabActivity")
    f = make(Flow, phone)
    msg = f.after_publish(lambda n: Phone.find(n, rid="hb2"), "x", settle=0, act_before="app/.Share")
    assert msg.startswith("gepostet")
    assert len(phone.attempts) == 1  # no second 11 s dump needed


def test_after_publish_page_stays_open_is_still_a_failure(clock):
    phone = FakePhone(clock, [[node(rid="hb2")]])
    with pytest.raises(PhoneError):
        make(Flow, phone).after_publish(lambda n: Phone.find(n, rid="hb2"), "Post-Seite schließt",
                                        settle=10, act_before="app/.Share")


def test_nodes_reports_busy_screen_as_screenbusy(monkeypatch):
    p = Phone("serial")
    monkeypatch.setattr(p, "sh", lambda *a, **k: "ERROR: could not get idle state.\n")
    monkeypatch.setattr("app.phone.time.sleep", lambda s: None)
    with pytest.raises(ScreenBusy):
        p.nodes(attempts=2)
