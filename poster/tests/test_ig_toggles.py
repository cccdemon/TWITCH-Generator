"""Instagram share-to switches against recorded screens (no phone needed).

ig_share_1.xml: share page right after the caption, list not scrolled; the
"Save draft / Share" bar overlaps the "Your story" switch.
ig_share_2.xml: same page after one swipe up.
"""
from pathlib import Path

from app.flows import InstagramFlow
from app.phone import parse_nodes

FIX = Path(__file__).parent / "fixtures"


def nodes(name):
    return parse_nodes((FIX / name).read_text(encoding="utf-8"))


def test_unscrolled_picks_facebook_switch_and_it_is_on():
    sw, checked, safe = InstagramFlow.toggle_row(nodes("ig_share_1.xml"), r"Facebook · .*")
    assert sw.bounds == (901, 1982, 1038, 2108) and checked is True
    assert safe is False  # bottom edge 2108 is under the Share bar (top 2105)


def test_unscrolled_threads_is_safe():
    sw, checked, safe = InstagramFlow.toggle_row(nodes("ig_share_1.xml"), r"Threads · .*")
    assert sw.bounds == (901, 1802, 1038, 1928) and checked is True and safe is True


def test_scrolled_facebook_is_safe_and_not_your_story():
    sw, checked, safe = InstagramFlow.toggle_row(nodes("ig_share_2.xml"), r"Facebook · .*")
    assert sw.bounds == (901, 1597, 1038, 1723) and checked is True and safe is True


def test_never_returns_share_or_trial():
    for f in ("ig_share_1.xml", "ig_share_2.xml"):
        for rx in (r"Facebook · .*", r"Threads · .*"):
            sw, _, _ = InstagramFlow.toggle_row(nodes(f), rx)
            assert sw.rid != "share_button" and sw.desc != "Trial" and sw.bounds[0] > 540
