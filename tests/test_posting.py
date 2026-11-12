from src.posting import (
    DEFAULT_TEMPLATE, PostStore, build_caption, check_caption, clip_kind, is_locked,
    merge_hashtags, parse_fixed_tags,
)


def test_merge_dedupes_case_insensitive_fixed_first():
    assert merge_hashtags(["#StarCitizen", "gaming"], ["#starcitizen", "#fail"]) == [
        "#StarCitizen", "#gaming", "#fail"]


def test_parse_fixed_tags_splits_and_normalizes():
    assert parse_fixed_tags("#starcitizen, gaming\n#Star Citizen") == [
        "#starcitizen", "#gaming", "#Star", "#Citizen"]


def test_build_caption_full():
    assert build_caption(DEFAULT_TEMPLATE, "Boom", "twitch.tv/x", ["#a", "#b"]) == \
        "Boom\n\nLive: twitch.tv/x\n\n#a #b"


def test_build_caption_drops_twitch_line_when_empty():
    assert build_caption(DEFAULT_TEMPLATE, "Boom", "", ["#a"]) == "Boom\n\n#a"


def test_build_caption_literal_backslash_n():
    assert build_caption("{title}\\n{hashtags}", "T", "x", ["#a"]) == "T\n#a"


def test_check_caption_instagram_hashtag_limit():
    r = check_caption(" ".join(f"#t{i}" for i in range(31)))
    assert r["hashtags"] == 31
    assert r["ok"] == {"instagram": False, "tiktok": True}
    assert "Instagram" in r["errors"]["instagram"]


def test_check_caption_char_limits():
    r = check_caption("x" * 2201)
    assert r["ok"] == {"instagram": False, "tiktok": True}
    assert check_caption("x" * 4001)["ok"]["tiktok"] is False


def test_check_caption_counts_links():
    assert check_caption("Live: twitch.tv/x und https://a.b/c")["links"] == 2


def test_clip_kind_unknown_name_is_edit():
    assert clip_kind("123_funny_x.mp4", {}) == "edit"
    assert clip_kind("123_funny_x.mp4", {123: {}}) == "pipeline"
    assert clip_kind("123_edit_x.mp4", {123: {}}) == "edit"
    assert clip_kind("old.mp4", {}) == "edit"


def test_store_roundtrip_and_lock(tmp_path):
    s = PostStore(tmp_path)
    assert s.get("c.mp4") == {"trim": None, "caption": None, "ai_tags": None, "posts": []}
    s.add_post("c.mp4", {"id": 7, "status": "queued", "platforms": ["tiktok"]})
    assert is_locked(s.get("c.mp4")) is True
    s.update("c.mp4", posts=[{"id": 7, "status": "done"}])
    assert is_locked(PostStore(tmp_path).get("c.mp4")) is False
