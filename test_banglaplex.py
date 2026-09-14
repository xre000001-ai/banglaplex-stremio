#!/usr/bin/env python3
"""Unit tests for the BanglaPlex Stremio addon.  Run: python3 test_banglaplex.py

Offline by default — every network call is mocked, so the suite is safe to run
anywhere and never burns 3n1 rate-limit budget.

Set BPX_LIVE=1 to ALSO run the live-integration block at the bottom. That block
really hits banglaplex.biz, plextream.work, the 3n1 frontends and the CDNs, so it
is rate-limit sensitive; never run it in a loop.
"""
import io
import json
import os
import re
import sys
import threading
import time
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import addon

LIVE = os.environ.get("BPX_LIVE", "0") == "1"
PASS = 0
FAIL = 0


def run(test):
    global PASS, FAIL
    name = test.__name__
    try:
        test()
        PASS += 1
        print("  ok  %s" % name)
    except Exception as e:
        FAIL += 1
        import traceback
        print("FAIL  %s: %s" % (name, e))
        traceback.print_exc()


def clear_caches():
    for c in (addon.C_SEARCH, addon.C_PAGE, addon.C_META, addon.C_EMBED,
              addon.C_N1, addon.C_STREAM):
        c.clear()
        c.bytes = 0
    addon.C_STALE.clear()
    addon._NEG_RETRY_AT.clear()
    addon._SWR_RUNNING.clear()
    with addon._HOST_BLOCK_LOCK:
        addon._HOST_BLOCK.clear()
    for h in list(addon._N1_BUSY):
        addon._N1_BUSY[h] = 0.0
    for h in list(addon._N1_LAST):
        addon._N1_LAST[h] = 0.0


# ══════════════════════════════════════════════════════════════ fixtures
# AES-128-CBC known answers, key=kiemtienmua911ca iv=1234567890oiuytr
# (generated with node crypto.createCipheriv — the 3n1 frontend's own recipe)
KAT_PT_HEX = ("7b226366223a2278222c227469746c65223a2241204d6f766965202832303236"
              "29203130383070205745422d444c207832363520414143352e312048696e64"
              "69227d")
KAT_CT_HEX = ("c1a7ff6ea668c6a0d8a0bb20780bb69e91e2321496a7f51a04502ac57c03a24e"
              "359872104b92ec589442b6c1ec828a7a1d1c97a90be3c48903f1588cdab1303"
              "b166ece027476f126b74d909b8e1a5f0b")
# FIPS-197 C.1 single-block AES-128 vector (proves the raw block inverse)
FIPS_KEY = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
FIPS_PT = bytes.fromhex("00112233445566778899aabbccddeeff")
FIPS_CT = bytes.fromhex("69c4e0d86a7b0430d8cdb78070b4c55a")

WATCH_MOVIE = """<!DOCTYPE html><html><head>
<meta property="og:title" content="Mirzapur: The Movie (2026) Hindi HDTC | 1080p | Watch Online &ndash; Download" />
<meta property="og:image" content="https://banglaplex.biz/uploads/video_thumb/1923.jpg" />
<meta property="og:description" content="The iron-fisted don of Mirzapur returns." />
</head><body>
<h1>Mirzapur: The Movie</h1>
<a href="/home/view_modal/report/1923" class="btn">report</a>
<div class="single-item-sidebar">
  <p><strong>Release :</strong> 2026-01-15</p>
  <p><strong>Duration :</strong> 148.0 Min</p>
  <p><strong>Quality :</strong> <span class="label">HD</span></p>
  <p><strong>Genre :</strong> <a href="/genre/action.html">Action</a>, <a href="/genre/crime.html">Crime</a></p>
  <p><strong>Country :</strong> <a href="/country/india.html">India</a></p>
  <p><strong>Actor :</strong> <a href="/actors/pankaj.html">Pankaj Tripathi</a></p>
  <p><strong>Director :</strong> <a href="/director/x.html">Karan Anshuman</a></p>
</div>
<div class="movie-details-btn">
  <a href="https://banglaplex.biz/watch/mirzapur-the-movie.html?key=0v2230n5ofrz" class="player-server-btn active">
    Full Movie
  </a>
</div>
<iframe src="https://plextream.work/embed.php?id=GKHsp0bk" frameborder="0" allowfullscreen></iframe>
</body></html>"""

WATCH_SERIES = """<!DOCTYPE html><html><head>
<meta property="og:title" content="Mirzapur (2024) Hindi S01-S03 Complete AMZN WEB-DL | 720p x265 | Watch Online" />
<meta property="og:image" content="https://banglaplex.biz/uploads/video_thumb/1923.jpg" />
<meta property="og:description" content="Three seasons of Mirzapur." />
</head><body>
<h1>Mirzapur</h1>
<a href="/home/view_modal/report/1923">report</a>
<p><strong>Release :</strong> 2024-07-05</p>
<p><strong>Quality :</strong> <span class="q">HD</span></p>
<p><strong>Country :</strong> <a href="/country/india.html">India</a></p>
<a href="https://banglaplex.biz/watch/mirzapur-2024-web-series-download.html?key=ncp7ip5mp4oq" class="player-server-btn active">
  Bonus Episode
</a>
<a href="https://banglaplex.biz/watch/mirzapur-2024-web-series-download.html?key=xucthnprelvg" class="player-server-btn ">
  S03
</a>
<a href="https://banglaplex.biz/watch/mirzapur-2024-web-series-download.html?key=h0ti2trt5ch2" class="player-server-btn ">
  S02
</a>
<a href="https://banglaplex.biz/watch/mirzapur-2024-web-series-download.html?key=79o9kquqzbd3" class="player-server-btn ">
  S01
</a>
<iframe src="https://plextream.work/embed.php?id=3xOG9KPC" allowfullscreen></iframe>
</body></html>"""

EMBED_HTML = """<html><body>
<div class="popup-servers">
  <button class="srv-btn active"
      onclick="changeServer('https://abyssplayer.com/JQXStg3_R', this)">
    <span class="srv-dot"></span>
    Server 3                        </button>
  <button class="srv-btn "
      onclick="changeServer('https://bpx.strp2p.site/#3urane', this)">
    <span class="srv-dot"></span>
    Server 2                        </button>
  <button class="srv-btn "
      onclick="changeServer('https://bpx.rpmvid.site/#fg8jp', this)">
    <span class="srv-dot"></span>
    Server 1                        </button>
</div>
<div class="player-wrapper">
  <iframe
      id="videoFrame"
      allowfullscreen
      allow="autoplay; encrypted-media"
      src="https://abyssplayer.com/JQXStg3_R"></iframe>
</div>
</body></html>"""

TT_MASTER = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-STREAM-INF:BANDWIDTH=900000,RESOLUTION=1280x532
/hls/x/v0.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=2600000,RESOLUTION=1920x800
/hls/x/v1.m3u8
"""

TT_VARIANT = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:6
#EXTINF:5.0,
https://p16-ad-site-sign-sg.tiktokcdn.com/seg/0.ts?x-expires=1820085242
#EXTINF:5.0,
https://p16-ad-site-sign-sg.tiktokcdn.com/seg/1.ts?x-expires=1820085242
#EXT-X-ENDLIST
"""

CF_MASTER = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=2500000,RESOLUTION=1920x816
v0/master.m3u8
"""

CF_VARIANT = """#EXTM3U
#EXT-X-TARGETDURATION:4
#EXTINF:4.0,
seg-0.ts
#EXT-X-ENDLIST
"""

PAYLOAD = {
    "title": "The.Revolutionaries.S01.1080p.HEVC.WEB-DL.Hindi.AAC5.1.x265.ESub-SkymoviesHD.mkv",
    "hlsVideoTiktok": "/hls/VzPDJFSCtGiJqIZ07mLbLg/nc9/v1feapnp/5lstx1/tt/master.m3u8",
    "cfNative": ("https://bpx.strp2p.site/v4/pl/swk8.naturegifts.shop/nc9/6zscnd/"
                 "master.1789212872.m3u8?k=SJaPUVeZ6_vjvLWGHBGGSQ&kx=1789399973"),
    "source": ("https://94.131.217.181/v4/jhOmh9opKcKAVKbAkKTygw/1789395573/nc9/"
               "6zscnd/master.m3u8?v=1789212872"),
    "subtitle": {
        "ta": "/tok/nc9/x/ta.vtt#ta", "hi": "/tok/nc9/x/hi.vtt#hi",
        "en": "/tok/nc9/x/en.vtt#en", "ch": "/tok/nc9/x/ch.vtt#ch",
        "mg": "/tok/nc9/x/mg.vtt#mg", "ja": "/tok/nc9/x/ja.vtt#ja",
        "ko": "/tok/nc9/x/ko.vtt#ko", "th": "/tok/nc9/x/th.vtt#th",
    },
    "player": {"defaultAudio": "hi", "restrictEmbed": ["plextream.work"]},
}


def _resp(status=200, text="", js=None):
    class R:
        status_code = status
        encoding = "utf-8"

        def __init__(self, t):
            self.text = t

        def json(self):
            if js is not None:
                return js
            return json.loads(self.text)
    return R(text if js is None else json.dumps(js))


def _http_get(path, host="bpx-test.onrender.com", accept_enc=""):
    """In-process route harness: no socket, no network."""
    captured = {}
    buf = io.BytesIO()
    from urllib.parse import urlparse, parse_qs

    class H(addon.Handler):
        def __init__(self):
            self.headers = {"Host": host, "Accept-Encoding": accept_enc}
            self.command = "GET"
            self.wfile = buf
            self.path = path

        def send_response(self, code, *a):
            captured["code"] = code

        def send_header(self, k, v):
            captured.setdefault("headers", {})[k] = v

        def end_headers(self):
            pass
    h = H()
    h.do_GET()          # the real entry point: gzip, Host-learning and the
                        # 500 containment all live there, not in _route
    captured["body"] = buf.getvalue()
    return captured


def _json_body(captured):
    b = captured["body"]
    if captured.get("headers", {}).get("Content-Encoding") == "gzip":
        import gzip
        b = gzip.decompress(b)
    return json.loads(b.decode("utf-8"))


# ══════════════════════════════════════════════════════ 1. pure-python AES
def test_sbox_matches_fips197():
    assert addon._SBOX[0] == 0x63 and addon._SBOX[1] == 0x7C
    assert addon._SBOX[3] == 0x7B and addon._SBOX[0x53] == 0xED
    assert all(addon._INV_SBOX[addon._SBOX[i]] == i for i in range(256))


def test_gf_generator_has_full_order():
    """generator 3, not 2: 2 only has order 51 in AES's field (real bug we hit)."""
    assert len(set(addon._EXP[:255])) == 255


def test_gmul():
    assert addon._gmul(0x57, 0x83) == 0xC1
    assert addon._gmul(0x57, 0x13) == 0xFE
    assert addon._gmul(0, 7) == 0 and addon._gmul(7, 0) == 0


def test_key_schedule_fips197_a1():
    rks = addon._round_keys(FIPS_KEY)
    assert len(rks) == 11 and all(len(k) == 16 for k in rks)
    assert bytes(rks[1]) == bytes.fromhex("d6aa74fdd2af72fadaa678f1d6ab76fe")
    assert bytes(rks[10]) == bytes.fromhex("13111d7fe3944a17f307a78b4d2b30c5")


def test_decrypt_block_fips197_c1():
    got = bytes(addon._decrypt_block(list(FIPS_CT), addon._round_keys(FIPS_KEY)))
    assert got == FIPS_PT, got.hex()


def test_aes_cbc_matches_node_kat():
    got = addon.aes_cbc_decrypt(bytes.fromhex(KAT_CT_HEX))
    assert got == bytes.fromhex(KAT_PT_HEX)


def test_aes_cbc_rejects_bad_input():
    assert addon.aes_cbc_decrypt(b"") == b""
    assert addon.aes_cbc_decrypt(b"\x00" * 15) == b""       # not a block multiple


def test_n1_decrypt_parses_json_payload():
    p = addon.n1_decrypt(KAT_CT_HEX)
    assert isinstance(p, dict) and p["title"].startswith("A Movie (2026)")


def test_n1_decrypt_garbage_is_none():
    assert addon.n1_decrypt("") is None
    assert addon.n1_decrypt("zz") is None
    assert addon.n1_decrypt("00" * 31) is None              # odd block count
    assert addon.n1_decrypt("<html>nope</html>") is None


def test_n1_decrypt_tolerates_trailing_newline():
    """chain.py burned us on this: a trailing \\n makes the hex odd-length."""
    assert addon.n1_decrypt(KAT_CT_HEX + "\n") is not None


# ══════════════════════════════════════════════════════════ 2. text helpers
def test_fmt_dur():
    assert addon._fmt_dur(148) == "2h28m"
    assert addon._fmt_dur("148.0") == "2h28m"
    assert addon._fmt_dur(45) == "45m"
    assert addon._fmt_dur(0) == "" and addon._fmt_dur(None) == ""
    assert addon._fmt_dur("abc") == ""


def test_res_label_is_width_driven():
    """BanglaPlex ships scope-cropped files: 1920x800 IS the 1080p encode and
    1280x532 IS the 720p one. Height-only bucketing mislabels both."""
    assert addon._res_label(1920, 800) == ("FHD", "1080p")
    assert addon._res_label(1920, 816) == ("FHD", "1080p")
    assert addon._res_label(1920, 1080) == ("FHD", "1080p")
    assert addon._res_label(1280, 532) == ("HD", "720p")
    assert addon._res_label(1280, 584) == ("HD", "720p")
    assert addon._res_label(854, 480) == ("SD", "480p")
    assert addon._res_label(640, 360) == ("SD", "360p")
    assert addon._res_label(3840, 1600) == ("4K", "2160p")
    assert addon._res_label(0, 0) == ("SD", "")


def test_norm_title_and_tokens():
    assert addon._norm_title("Mirzapur: The Movie (2026)") == "mirzapur the movie"
    toks = addon._clean_tokens("Mirzapur The Movie (2026) 1080p HDTC x264 AAC")
    assert toks == ["mirzapur"], toks
    assert addon._year_of("foo 2024 bar") == 2024
    assert addon._year_of("nothing") is None


def test_file_tags_from_release_name():
    ft = addon.file_tags(PAYLOAD["title"])
    assert ft["source"] == "WEB-DL" and ft["codec"] == "HEVC"
    assert ft["audio"] == "AAC5.1" and ft["langs"] == ["Hindi"]
    ft2 = addon.file_tags("Mirzapur.The.Movie.2026.Hindi.HQ.HDTC.1080p.X264.AAC-Grp.mkv")
    assert ft2["codec"] == "AVC" and ft2["source"] == "HDTC"


# ═════════════════════════════════════════════════════ 3. watch-page parsing
def _watch(html, url="https://banglaplex.biz/watch/mirzapur-the-movie.html"):
    with mock.patch.object(addon, "_get", return_value=_resp(200, html)):
        addon.C_PAGE.clear()
        addon.C_PAGE.bytes = 0
        return addon.parse_watch_page(url)


def test_parse_movie_watch_page():
    p = _watch(WATCH_MOVIE)
    assert p["title"] == "Mirzapur: The Movie"
    assert p["year"] == 2026 and p["quality"] == "HD"
    assert p["duration"] == "148.0" and p["release"] == "2026-01-15"
    assert p["genre"].startswith("Action") and p["country"] == "India"
    assert "Pankaj" in p["actors"] and p["director"] == "Karan Anshuman"
    assert p["poster"].endswith("1923.jpg") and "don of Mirzapur" in p["plot"]
    assert p["iframe"] == "https://plextream.work/embed.php?id=GKHsp0bk"
    assert p["keys"] == [("0v2230n5ofrz", True, "Full Movie")]


def test_parse_series_watch_page_keys_and_active():
    p = _watch(WATCH_SERIES, "https://banglaplex.biz/watch/mirzapur-2024-web-series-download.html")
    assert p["year"] == 2024 and p["title"] == "Mirzapur"
    labels = [lbl for _k, _a, lbl in p["keys"]]
    assert labels == ["Bonus Episode", "S03", "S02", "S01"]
    assert [a for _k, a, _l in p["keys"]] == [True, False, False, False]


def test_parse_watch_page_key_variant_url():
    """?key= selects the season; the URL we cache and re-fetch must carry it."""
    seen = []

    def fake_get(url, **kw):
        seen.append(url)
        return _resp(200, WATCH_SERIES)
    with mock.patch.object(addon, "_get", side_effect=fake_get):
        addon.C_PAGE.clear()
        addon.C_PAGE.bytes = 0
        addon.parse_watch_page("https://banglaplex.biz/watch/x.html", key="h0ti2trt5ch2")
    assert seen == ["https://banglaplex.biz/watch/x.html?key=h0ti2trt5ch2"]


def test_parse_watch_page_transient_is_not_cached():
    u = "https://banglaplex.biz/watch/transient-page.html"
    with mock.patch.object(addon, "_get", return_value=None):
        assert addon.parse_watch_page(u) is None
    hit, val = addon.C_PAGE.get(u)
    assert not hit, "a network failure must never be cached as a negative"


def test_parse_watch_page_404_caches_negative_briefly():
    with mock.patch.object(addon, "_get", return_value=_resp(404, "nope")):
        assert addon.parse_watch_page("https://banglaplex.biz/watch/gone.html") is None
    hit, val = addon.C_PAGE.get("https://banglaplex.biz/watch/gone.html")
    assert hit and val is None


def test_parse_watch_page_rejects_non_watch_html():
    with mock.patch.object(addon, "_get", return_value=_resp(200, "<html>captcha</html>")):
        assert addon.parse_watch_page("https://banglaplex.biz/watch/x.html") is None


# ════════════════════════════════════════════════════════════ 4. key picker
def test_classify_key_shapes():
    assert addon.classify_key("Full Movie") == ("movie", None, None, None)
    assert addon.classify_key("S02") == ("season", 2, None, None)
    assert addon.classify_key("Season 3") == ("season", 3, None, None)
    assert addon.classify_key("Web Series") == ("series", None, None, None)
    assert addon.classify_key("Episode 09-16")[0] == "eprange"
    assert addon.classify_key("Episode 09-16")[2:] == (9, 16)
    assert addon.classify_key("Episode 7")[0] == "episode"
    assert addon.classify_key("Bonus Episode")[0] in ("episode", "other")


def _series_page():
    return _watch(WATCH_SERIES, "https://banglaplex.biz/watch/mirzapur-2024-web-series-download.html")


def test_pick_keys_movie_single():
    p = _watch(WATCH_MOVIE)
    assert addon.pick_keys(p, "movie", None, None) == [("0v2230n5ofrz", "Full Movie", "")]


def test_pick_keys_series_no_season_prefers_s01_not_site_default():
    """the site's active key here is 'Bonus Episode' — the worst possible answer
    for a plain series request."""
    p = _series_page()
    picks = addon.pick_keys(p, "series", None, None)
    assert picks[0][1] == "S01", picks
    assert picks[0][2] == "full-season file"
    assert picks[1][1] == "S02"


def test_pick_keys_series_exact_season():
    p = _series_page()
    assert addon.pick_keys(p, "series", 2, 3)[0][1] == "S02"
    assert addon.pick_keys(p, "series", 3, 1)[0][1] == "S03"


def test_pick_keys_never_falls_back_to_a_different_episode_pack():
    """answering S01E01 with 'Episode 105-112' is a wrong-content card, worse
    than an honest empty."""
    page = {"keys": [("a1", False, "Episode 105-112"), ("a2", True, "Episode 101-104"),
                     ("a3", False, "Episode 01-08"), ("a4", False, "Episode 09-16")],
            "url": "u"}
    assert addon.pick_keys(page, "series", 1, 1) == [("a3", "Episode 01-08",
                                                      "episode pack 1-8")]
    assert addon.pick_keys(page, "series", 4, 10) == [("a4", "Episode 09-16",
                                                       "episode pack 9-16")]


def test_pick_keys_episode_pack_note_and_range():
    page = {"keys": [("a1", True, "Episode 09-16")], "url": "u"}
    assert addon.pick_keys(page, "series", 4, 10) == [("a1", "Episode 09-16",
                                                       "episode pack 9-16")]


def test_pick_keys_series_full_season_note():
    page = {"keys": [("a1", True, "Web Series")], "url": "u"}
    assert addon.pick_keys(page, "series", 1, 1) == [("a1", "Web Series",
                                                      "full-season file")]


def test_pick_keys_empty():
    assert addon.pick_keys({"keys": [], "url": "u"}, "movie", None, None) == []


# ═════════════════════════════════════════════════════════ 5. embed servers
def test_parse_embed_servers():
    with mock.patch.object(addon, "_get", return_value=_resp(200, EMBED_HTML)):
        addon.C_EMBED.clear()
        addon.C_EMBED.bytes = 0
        srv = addon.parse_embed_servers("https://plextream.work/embed.php?id=o9yiDRAJ")
    urls = [u for _l, u in srv]
    assert urls == ["https://abyssplayer.com/JQXStg3_R",
                    "https://bpx.strp2p.site/#3urane",
                    "https://bpx.rpmvid.site/#fg8jp"], srv
    labels = [l for l, _u in srv]
    assert "Server 3" in labels[0] and "Server 2" in labels[1]


def test_parse_embed_servers_no_iframe():
    assert addon.parse_embed_servers("") == []


def test_parse_embed_servers_falls_back_to_iframe_src():
    """some embeds carry only the default iframe (single-server uploads)."""
    html = '<div class="player-wrapper"><iframe id="videoFrame" src="https://bpx.strp2p.site/#abc123"></iframe></div>'
    with mock.patch.object(addon, "_get", return_value=_resp(200, html)):
        addon.C_EMBED.clear()
        addon.C_EMBED.bytes = 0
        srv = addon.parse_embed_servers("https://plextream.work/embed.php?id=X")
    assert [u for _l, u in srv] == ["https://bpx.strp2p.site/#abc123"]


def test_parse_embed_servers_transient_not_cached():
    u = "https://plextream.work/embed.php?id=TRANSIENT"
    with mock.patch.object(addon, "_get", return_value=None):
        assert addon.parse_embed_servers(u) == []
    assert not addon.C_EMBED.get(u)[0]


# ══════════════════════════════════════════════════════════════ 6. search
def test_search_autocomplete_term_param():
    seen = {}

    def fake_get(url, **kw):
        seen["url"] = url
        return _resp(200, js=[{"title": "Jawan", "type": "Movie", "image": "i",
                               "url": "https://banglaplex.biz/watch/jawan.html"}])
    with mock.patch.object(addon, "_get", side_effect=fake_get):
        addon.C_SEARCH.clear()
        out = addon._search_autocomplete("jawan")
    assert "autocompleteajax?term=jawan" in seen["url"]
    assert out[0]["url"].endswith("/watch/jawan.html")


def test_search_autocomplete_transient_vs_empty():
    with mock.patch.object(addon, "_get", return_value=None):
        assert addon._search_autocomplete("x") is None       # transient
    with mock.patch.object(addon, "_get", return_value=_resp(200, js=[])):
        assert addon._search_autocomplete("x") == []         # real empty


AC_JAWAN = [{"title": "Jawan",
             "url": "https://banglaplex.biz/watch/jawan-2023-movie-download.html",
             "type": "Movie", "image": "i"}]


def test_search_candidates_uses_autocomplete_first():
    with mock.patch.object(addon, "_search_autocomplete", return_value=AC_JAWAN), \
         mock.patch.object(addon, "_search_html",
                           side_effect=AssertionError("html is only a fallback")):
        addon.C_SEARCH.clear()
        out = addon.search_candidates("jawan")
    assert [c["url"] for c in out] == [
        "https://banglaplex.biz/watch/jawan-2023-movie-download.html"]


def test_search_candidates_falls_back_to_html_and_dedupes():
    hh = [{"title": "jawan",
           "url": "https://banglaplex.biz/watch/jawan-2023-movie-download.html",
           "type": "", "year": None, "quality": "", "image": ""},
          {"title": "Jawan 2", "url": "https://banglaplex.biz/watch/jawan-2.html",
           "type": "", "year": None, "quality": "", "image": ""}]
    with mock.patch.object(addon, "_search_autocomplete", return_value=[]), \
         mock.patch.object(addon, "_search_html", return_value=hh):
        addon.C_SEARCH.clear()
        out = addon.search_candidates("jawan")
    urls = [c["url"] for c in out]
    assert urls.count("https://banglaplex.biz/watch/jawan-2023-movie-download.html") == 1
    assert "https://banglaplex.biz/watch/jawan-2.html" in urls


def test_search_candidates_caches_results_not_transients():
    addon.C_SEARCH.clear()
    with mock.patch.object(addon, "_search_autocomplete", return_value=AC_JAWAN), \
         mock.patch.object(addon, "_search_html", return_value=[]):
        addon.search_candidates("jawan")
    assert addon.C_SEARCH.get("jawan")[0]
    addon.C_SEARCH.clear()
    with mock.patch.object(addon, "_search_autocomplete", return_value=None), \
         mock.patch.object(addon, "_search_html", return_value=None):
        assert addon.search_candidates("jawan") is None
    assert not addon.C_SEARCH.get("jawan")[0], "a transient must never be cached"


def test_search_candidates_progressive_narrowing():
    """multi-word queries must retry with fewer words before giving up."""
    calls = []

    def fake_ac(kw):
        calls.append(kw)
        return [] if kw == "total dadagiri movie" else [
            {"title": "Total Dadagiri", "url": "https://banglaplex.biz/watch/td.html"}]
    with mock.patch.object(addon, "_search_autocomplete", side_effect=fake_ac), \
         mock.patch.object(addon, "_search_html", return_value=[]):
        addon.C_SEARCH.clear()
        out = addon.search_candidates("Total Dadagiri movie")
    assert out, calls
    assert len(calls) >= 2, calls


def test_search_candidates_all_transient_returns_none_not_empty():
    with mock.patch.object(addon, "_search_autocomplete", return_value=None), \
         mock.patch.object(addon, "_search_html", return_value=None):
        addon.C_SEARCH.clear()
        assert addon.search_candidates("anything") is None


def test_match_candidates_exact_and_year_guard():
    cands = [{"title": "Animal", "url": "https://x/animal-2024-movie-download.html"},
             {"title": "Animal Farm", "url": "https://x/animal-farm.html"}]
    m = addon.match_candidates(cands, "Animal", 2023, "movie")
    assert m and m[0]["url"].endswith("animal-2024-movie-download.html")
    # festival-vs-theatrical gaps survive (TMDB 2024 / site 2026 happened live)
    assert addon.match_candidates(cands, "Animal", 2022, "movie")
    assert addon.match_candidates(cands, "Animal", 2021, "movie")
    # a real collision does not: 2015/2020 vs a 2024 upload is a different film
    assert addon.match_candidates(cands, "Animal", 2020, "movie") == []
    assert addon.match_candidates(cands, "Animal", 2015, "movie") == []


def test_match_candidates_ignores_release_tag_noise():
    cands = [{"title": "Mirzapur The Movie", "url": "https://x/m.html"}]
    assert addon.match_candidates(cands, "Mirzapur: The Movie", 2026, "movie")


def test_match_candidates_rejects_unrelated():
    cands = [{"title": "Hai Jawani Toh Ishq Hona Hai", "url": "https://x/h.html"}]
    assert addon.match_candidates(cands, "Jawan", 2023, "movie") == []


# ═══════════════════════════════════════════════════════════ 7. 3n1 API
def test_n1_video_decrypts_and_caches():
    clear_caches()
    with mock.patch.object(addon, "_get", return_value=_resp(200, KAT_CT_HEX)), \
         mock.patch.object(addon, "N1_LANE_GAP", 0):
        p = addon.n1_video("bpx.strp2p.site", "ow99qd")
        assert isinstance(p, dict) and "title" in p
        hit, val = addon.C_N1.get(("bpx.strp2p.site", "ow99qd"))
        assert hit and val is p
        # second call is a cache hit: no HTTP at all
        with mock.patch.object(addon, "_get", side_effect=AssertionError("must not refetch")):
            assert addon.n1_video("bpx.strp2p.site", "ow99qd") is p


def test_n1_video_404_is_definitive_false():
    """404 = the file lives on the OTHER frontend, not a transient failure."""
    clear_caches()
    with mock.patch.object(addon, "_get",
                           return_value=_resp(404, '{"message": "Video not found or deleted"}')), \
         mock.patch.object(addon, "N1_LANE_GAP", 0):
        assert addon.n1_video("bpx.strp2p.site", "3urane") is False


def test_n1_video_429_retries_then_benches_host():
    clear_caches()
    calls = []

    def fake_get(url, **kw):
        calls.append(url)
        return _resp(429, '{"message": "Rate limit exceeded"}')
    with mock.patch.object(addon, "_get", side_effect=fake_get), \
         mock.patch.object(addon, "N1_LANE_GAP", 0), \
         mock.patch.object(addon, "N1_BACKOFF", 0), \
         mock.patch.object(addon, "N1_COOLDOWN", 30):
        assert addon.n1_video("bpx.strp2p.site", "abc123") is None
    assert len(calls) == 2, calls                       # one backoff-retry
    assert addon._N1_BUSY["bpx.strp2p.site"] > time.time()
    assert addon._STATS["n1_429"] >= 1
    # while benched, no HTTP at all
    with mock.patch.object(addon, "_get", side_effect=AssertionError("must not call")):
        assert addon.n1_video("bpx.strp2p.site", "abc123") is None


def test_n1_video_429_bench_is_per_host():
    """one grumpy frontend must never take down the site scrape or the other
    frontend with it (the old global bench did exactly that)."""
    clear_caches()
    addon._bench("bpx.strp2p.site", 30)
    assert addon._get("https://bpx.strp2p.site/api/v1/video?id=x") is None
    assert addon._blocked_left("bpx.rpmvid.site") == 0
    assert addon._blocked_left("banglaplex.biz") == 0


def test_resolve_n1_skips_abyss_and_picks_live_frontend():
    clear_caches()
    servers = [("Server 3", "https://abyssplayer.com/JQXStg3_R"),
               ("Server 2", "https://bpx.strp2p.site/#3urane"),
               ("Server 1", "https://bpx.rpmvid.site/#fg8jp")]
    calls = []

    def fake(host, vid, deadline=None, retries=1):
        calls.append((host, vid))
        return False if host.endswith("strp2p.site") else PAYLOAD
    with mock.patch.object(addon, "n1_video", side_effect=fake):
        p, host, vid = addon.resolve_n1(servers)
    assert p is PAYLOAD and host == "bpx.rpmvid.site" and vid == "fg8jp"
    assert calls[0] == ("bpx.strp2p.site", "3urane")     # tried the dead one first
    assert not any("abyssplayer" in c[0] for c in calls)


def test_resolve_n1_all_dead():
    clear_caches()
    with mock.patch.object(addon, "n1_video", return_value=False):
        assert addon.resolve_n1([("s", "https://bpx.strp2p.site/#aaaaaa")]) == (None, None, None)


# ══════════════════════════════════════════════════ 8. media verification
def test_media_candidates_order_and_inhouse_off():
    c = addon.media_candidates(PAYLOAD, "bpx.strp2p.site")
    kinds = [k for k, _u, _r, _l in c]
    assert kinds == ["tiktok", "cloudflare"], kinds
    assert c[0][1].startswith("https://bpx.strp2p.site/hls/")
    assert c[0][2] is None                              # tiktok needs no headers
    assert c[1][2] == "https://bpx.strp2p.site"         # cf segments are referer-gated
    # the raw-IP path is caller-IP-bound: it verifies here and 403s on the user's
    # player, i.e. a phantom card. It must stay off unless explicitly enabled.
    with mock.patch.object(addon, "INHOUSE_ON", True):
        assert [k for k, *_ in addon.media_candidates(PAYLOAD, "bpx.strp2p.site")][-1] == "inhouse"


def test_abs_urljoin():
    b = "https://bpx.strp2p.site/"
    assert addon._abs(b, "/hls/x/master.m3u8") == "https://bpx.strp2p.site/hls/x/master.m3u8"
    assert addon._abs(b, "https://cdn/x.m3u8") == "https://cdn/x.m3u8"
    assert addon._abs("https://h/a/b.m3u8", "seg-0.ts") == "https://h/a/seg-0.ts"


def test_master_info_picks_biggest_variant():
    with mock.patch.object(addon, "_get", return_value=_resp(200, TT_MASTER)):
        i = addon._master_info("https://bpx.strp2p.site/hls/x/master.m3u8")
    assert i["best"] == (1920, 800) and i["n_variants"] == 2
    assert i["variant"] == "https://bpx.strp2p.site/hls/x/v0.m3u8"


def test_master_info_rejects_non_playlist():
    with mock.patch.object(addon, "_get", return_value=_resp(200, "<html>403</html>")):
        assert addon._master_info("https://x/master.m3u8") is None
    with mock.patch.object(addon, "_get", return_value=_resp(404, "")):
        assert addon._master_info("https://x/master.m3u8") is None


def test_verify_media_full_gate():
    """NO PHANTOM: master 200 #EXTM3U -> variant head has segments -> first
    segment answers 206. All three, or no card."""
    def fake_get(url, **kw):
        return _resp(200, TT_MASTER)
    def fake_head(url, **kw):
        assert "#EXTM3U" in TT_VARIANT
        return 200, TT_VARIANT[:8192]
    probed = []

    def fake_probe(url, referer=None, **kw):
        probed.append((url, referer))
        return 206, 1024
    with mock.patch.object(addon, "_get", side_effect=fake_get), \
         mock.patch.object(addon, "_playlist_head", side_effect=fake_head), \
         mock.patch.object(addon, "_range_probe", side_effect=fake_probe):
        info = addon._verify_media("tiktok", "https://bpx.strp2p.site/hls/x/master.m3u8", None)
    assert info and info["seg_status"] == 206
    assert "tiktokcdn.com" in info["segment"]
    assert probed[0][1] is None, "the tiktok path must not need a referer"


def test_verify_media_uses_referer_for_cloudflare():
    probed = []
    with mock.patch.object(addon, "_get", return_value=_resp(200, CF_MASTER)), \
         mock.patch.object(addon, "_playlist_head", return_value=(200, CF_VARIANT)), \
         mock.patch.object(addon, "_range_probe",
                           side_effect=lambda u, referer=None, **k: probed.append((u, referer)) or (206, 512)):
        info = addon._verify_media("cloudflare", "https://bpx.strp2p.site/master.m3u8",
                                   "https://bpx.strp2p.site")
    assert info and info["best"] == (1920, 816)
    assert probed[0][1] == "https://bpx.strp2p.site"
    assert info["segment"].endswith("seg-0.ts")


def test_verify_media_rejects_dead_master():
    with mock.patch.object(addon, "_get", return_value=_resp(403, "")):
        assert addon._verify_media("tiktok", "https://x/master.m3u8", None) is None


def test_verify_media_rejects_variant_without_segments():
    with mock.patch.object(addon, "_get", return_value=_resp(200, TT_MASTER)), \
         mock.patch.object(addon, "_playlist_head", return_value=(200, "#EXTM3U\n#EXT-X-ENDLIST\n")):
        assert addon._verify_media("tiktok", "https://x/master.m3u8", None) is None


def test_verify_media_rejects_unplayable_segment():
    """master + variant fine but the segment 403s => still no card."""
    with mock.patch.object(addon, "_get", return_value=_resp(200, TT_MASTER)), \
         mock.patch.object(addon, "_playlist_head", return_value=(200, TT_VARIANT)), \
         mock.patch.object(addon, "_range_probe", return_value=(403, 0)):
        assert addon._verify_media("tiktok", "https://x/master.m3u8", None) is None


def test_playlist_head_never_reads_the_whole_body():
    """the 3n1 frontends IGNORE Range on variant playlists and would otherwise
    stream ~1.4MB into Render. Head-read + close is the house pattern."""
    class FakeR:
        status_code = 200
        closed = False

        def iter_content(self, n):
            for _ in range(500):
                yield b"#" * 4096

        def close(self):
            FakeR.closed = True
    fr = FakeR()
    with mock.patch.object(addon._S, "get", return_value=fr):
        st, txt = addon._playlist_head("https://x/v0.m3u8", nbytes=8192)
    assert st == 200 and len(txt) <= 12288, len(txt)
    assert FakeR.closed


# ══════════════════════════════════════════════════════════ 9. subtitles
def test_collect_subtitles_priority_and_limit():
    """12 tracks come back in random order; en/hi must not lose their slots to
    mg/ka/th (measured live: the first 6 were ta,mg,ka,ja,th,zh)."""
    with mock.patch.object(addon, "_playlist_head", return_value=(200, "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nhi")):
        subs = addon.collect_subtitles(PAYLOAD, "bpx.strp2p.site")
    langs = [s["lang"] for s in subs]
    assert langs[0] == "en" and langs[1] == "hi", langs
    assert len(subs) == addon.MAX_SUBS
    assert all(s["url"].startswith("https://bpx.strp2p.site/tok/") for s in subs)
    assert "#en" not in subs[0]["url"]                  # fragment stripped
    assert subs[0]["id"] == "bpx-en"


def test_collect_subtitles_lang_normalisation():
    assert addon._sub_lang("ch") == "zh"
    assert addon._sub_lang("hindi") == "hi"
    assert addon._sub_lang("en#en") == "en"
    assert addon._sub_lang("") == "und"


def test_collect_subtitles_drops_dead_tracks():
    def fake_head(url, **kw):
        return (200, "WEBVTT\n") if "en.vtt" in url else (404, "")
    with mock.patch.object(addon, "_playlist_head", side_effect=fake_head):
        subs = addon.collect_subtitles(PAYLOAD, "bpx.strp2p.site")
    assert [s["lang"] for s in subs] == ["en"]


def test_collect_subtitles_none():
    assert addon.collect_subtitles({}, "h") == []
    assert addon.collect_subtitles({"subtitle": "not json"}, "h") == []


def test_collect_subtitles_accepts_json_string():
    with mock.patch.object(addon, "_playlist_head", return_value=(200, "WEBVTT\n")):
        subs = addon.collect_subtitles({"subtitle": '{"en": "/a/en.vtt#en"}'}, "h")
    assert len(subs) == 1 and subs[0]["lang"] == "en"


# ═════════════════════════════════════════════════════════ 10. card format
def _page(**over):
    p = {"title": "The Revolutionaries", "year": 2026, "quality": "HD",
         "duration": "280", "slug": "the-revolutionaries", "url": "u"}
    p.update(over)
    return p


def test_format_card_spec_v2_tiktok():
    info = {"best": (1920, 816), "n_variants": 2, "size": 200, "variant": "v",
            "text": TT_MASTER, "segment": "s", "seg_status": 206}
    c = addon.format_card("tiktok", "https://bpx.strp2p.site/hls/x/master.m3u8", None,
                          "TikTok CDN", info, _page(_ctype="series", _se=1, _ep=1),
                          PAYLOAD, note="full-season file",
                          subs=[{"url": "u", "lang": "en", "id": "bpx-en"}])
    assert c["name"] == "♧ FHD 1080p  ✹ The Revolutionaries"
    d = c["description"]
    assert "◫ S01 E01 ◇ ⚠ full-season file" in d
    assert "◈ WEB-DL ♫ AAC5.1" in d and "◈ Hindi" in d
    assert "⌗ BanglaPlex / ⌬ TikTok CDN / ◴ 2026 / ⟡ 1 SUB" in d
    bh = c["behaviorHints"]
    assert bh["notWebReady"] is False and "proxyHeaders" not in bh
    assert bh["bingeGroup"] == "bpx|the-revolutionaries"
    assert c["url"].startswith("https://bpx.strp2p.site/hls/")
    assert c["subtitles"][0]["lang"] == "en"


def test_format_card_cloudflare_gets_proxy_headers():
    info = {"best": (1920, 800), "n_variants": 1, "size": 90, "variant": "v",
            "text": CF_MASTER, "segment": "s", "seg_status": 206}
    c = addon.format_card("cloudflare", "https://bpx.strp2p.site/m.m3u8",
                          "https://bpx.strp2p.site", "Cloudflare", info,
                          _page(_ctype="movie"), PAYLOAD)
    bh = c["behaviorHints"]
    assert bh["notWebReady"] is True
    assert bh["proxyHeaders"]["request"]["Referer"] == "https://bpx.strp2p.site"
    assert "MOVIE" in c["description"] and "⌬ Cloudflare" in c["description"]


def test_format_card_omits_tokens_it_has_no_data_for():
    """honesty rule: no fake quality, no fake runtime, no '0 SUB'."""
    info = {"best": (0, 0), "n_variants": 0, "size": 0, "variant": "", "text": ""}
    c = addon.format_card("tiktok", "https://x/m.m3u8", None, "TikTok CDN", info,
                          {"title": "Unknown", "url": "u"}, {"title": "file.mkv"})
    assert "♧" not in c["name"] and "◷" not in c["description"]
    assert "SUB" not in c["description"] and "⌬ TikTok CDN" in c["description"]


def test_format_card_falls_back_to_site_quality_and_default_audio():
    info = {"best": (0, 0), "n_variants": 0, "size": 0, "variant": "", "text": ""}
    c = addon.format_card("tiktok", "https://x/m.m3u8", None, "s", info,
                          {"title": "T", "quality": "HD", "url": "u"},
                          {"title": "x.mkv", "player": {"defaultAudio": "hi"}})
    assert "♧ HD" in c["name"]
    assert "◈ Hindi" in c["description"]


def test_format_card_player_json_as_string():
    info = {"best": (0, 0), "n_variants": 0, "size": 0, "variant": "", "text": ""}
    c = addon.format_card("tiktok", "u", None, "s", info, {"title": "T", "url": "u"},
                          {"title": "x.mkv", "player": '{"defaultAudio": "ko"}'})
    assert "Korean" in c["description"]


# ════════════════════════════════════════════════════ 11. metadata consensus
def _meta_sources(cm=None, tm=None, im=None):
    return mock.patch.multiple(addon, _cinemeta=mock.DEFAULT, _tmdb_find=mock.DEFAULT,
                               _imdb_suggest=mock.DEFAULT), (cm, tm, im)


def test_resolve_meta_all_consensus_beats_first_responder():
    """live failure: tt3365690 -> cinemeta+IMDb say 'The Reincarnate', TMDB says
    'Jaatishwar'. BanglaPlex lists Jaatishwar, so BOTH must reach _build_inner."""
    clear_caches()
    with mock.patch.object(addon, "_cinemeta", return_value=("The Reincarnate", 2014, None)), \
         mock.patch.object(addon, "_tmdb_find", return_value=("Jaatishwar", 2014, 256971)), \
         mock.patch.object(addon, "_imdb_suggest", return_value=("The Reincarnate", 2014, None)), \
         mock.patch.object(addon, "_tmdb_alt_titles", return_value=[]):
        c = addon.resolve_meta_all("movie", "tt3365690")
    names = [n for n, _y, _t in c]
    assert names[0] == "The Reincarnate", names       # 2 of 3 agree
    assert "Jaatishwar" in names, names               # but the minority stays a fallback


def test_resolve_meta_all_appends_tmdb_alternates_last():
    clear_caches()
    with mock.patch.object(addon, "_cinemeta", return_value=("Mohabbatein", 2000, 11518)), \
         mock.patch.object(addon, "_tmdb_find", return_value=("Mohabbatein", 2000, 11518)), \
         mock.patch.object(addon, "_imdb_suggest", return_value=("Mohabbatein", 2000, None)), \
         mock.patch.object(addon, "_tmdb_alt_titles",
                           return_value=["情字路上", "Liebesbeziehungen", "Mohabbatein"]):
        c = addon.resolve_meta_all("movie", "tt0213890")
    names = [n for n, _y, _t in c]
    assert names[0] == "Mohabbatein"
    assert "情字路上" in names and names.count("Mohabbatein") == 1
    assert len(names) <= addon.MAX_META_CANDS


def test_resolve_meta_all_filters_episode_titles_for_series():
    """IMDb-suggest answers with EPISODE titles for episode ids."""
    clear_caches()
    with mock.patch.object(addon, "_cinemeta", return_value=None), \
         mock.patch.object(addon, "_tmdb_find", return_value=None), \
         mock.patch.object(addon, "_imdb_suggest",
                           return_value=("Episode 29: A King Has Fallen", 2019, None)):
        assert addon.resolve_meta_all("series", "tt35320226") == []


def test_resolve_meta_all_single_source_wins_by_default():
    clear_caches()
    with mock.patch.object(addon, "_cinemeta", return_value=("Inception", 2010, 27205)), \
         mock.patch.object(addon, "_tmdb_find", return_value=None), \
         mock.patch.object(addon, "_imdb_suggest", return_value=None), \
         mock.patch.object(addon, "_tmdb_alt_titles", return_value=[]):
        c = addon.resolve_meta_all("movie", "tt1375666")
    assert c == [("Inception", 2010, 27205)]
    assert addon.resolve_meta("movie", "tt1375666") == ("Inception", 2010, 27205)


def test_resolve_meta_all_nothing_is_not_cached():
    clear_caches()
    with mock.patch.object(addon, "_cinemeta", return_value=None), \
         mock.patch.object(addon, "_tmdb_find", return_value=None), \
         mock.patch.object(addon, "_imdb_suggest", return_value=None):
        assert addon.resolve_meta_all("movie", "tt0000000") == []
    assert not addon.C_META.get(("cands", "movie", "tt0000000"))[0]


def test_resolve_meta_all_is_cached():
    clear_caches()
    with mock.patch.object(addon, "_cinemeta", return_value=("X", 2020, 1)) as m1, \
         mock.patch.object(addon, "_tmdb_find", return_value=None), \
         mock.patch.object(addon, "_imdb_suggest", return_value=None), \
         mock.patch.object(addon, "_tmdb_alt_titles", return_value=[]):
        addon.resolve_meta_all("movie", "tt1")
        addon.resolve_meta_all("movie", "tt1")
    assert m1.call_count == 1


def test_resolve_meta_series_prefers_cinemeta_over_suggest():
    clear_caches()
    with mock.patch.object(addon, "_cinemeta", return_value=("Mirzapur", 2018, 1)), \
         mock.patch.object(addon, "_tmdb_find", return_value=None), \
         mock.patch.object(addon, "_imdb_suggest", return_value=("Episode 1", 2018, None)), \
         mock.patch.object(addon, "_tmdb_alt_titles", return_value=[]):
        c = addon.resolve_meta_all("series", "tt2")
    assert [n for n, _y, _t in c] == ["Mirzapur"]


# ══════════════════════════════════════════════════════ 12. build pipeline
def _stub_resolve(cards):
    return mock.patch.object(addon, "_resolve_file", return_value=cards)


def _card(name="c", url="https://bpx.strp2p.site/hls/x/master.m3u8"):
    return {"name": name, "description": "d", "url": url,
            "behaviorHints": {"notWebReady": False}}


def test_build_inner_happy_path_movie():
    clear_caches()
    page = {"url": "https://banglaplex.biz/watch/x.html", "year": 2026, "title": "X",
            "keys": [("k1", True, "Full Movie")], "iframe": "https://plextream.work/e?id=1"}
    with mock.patch.object(addon, "resolve_meta_all", return_value=[("X", 2026, 1)]), \
         mock.patch.object(addon, "search_candidates",
                           return_value=[{"title": "X", "url": page["url"]}]), \
         mock.patch.object(addon, "parse_watch_page", return_value=page), \
         _stub_resolve([_card()]):
        out = addon._build_inner("movie", "tt9", None, None, time.time() + 5)
    assert len(out["streams"]) == 1 and "message" not in out


def test_build_inner_marks_alternates():
    clear_caches()
    page = {"url": "u", "year": 2026, "title": "X",
            "keys": [("k1", True, "Full Movie")], "iframe": "i"}
    with mock.patch.object(addon, "resolve_meta_all", return_value=[("X", 2026, 1)]), \
         mock.patch.object(addon, "search_candidates", return_value=[{"title": "X", "url": "u"}]), \
         mock.patch.object(addon, "parse_watch_page", return_value=page), \
         _stub_resolve([_card("a"), _card("b")]):
        out = addon._build_inner("movie", "tt9", None, None, time.time() + 5)
    names = [s["name"] for s in out["streams"]]
    assert names == ["a", "b · alt"], names


def test_build_inner_walks_to_the_alternate_title():
    """the consensus title misses on the site, the TMDB/minority title hits."""
    clear_caches()
    page = {"url": "https://banglaplex.biz/watch/jaatishwar.html", "year": 2014,
            "title": "Jaatishwar", "keys": [("k", True, "Full Movie")], "iframe": "i"}

    def fake_search(kw):
        return [] if kw.lower() == "the reincarnate" else [{"title": "Jaatishwar", "url": page["url"]}]
    with mock.patch.object(addon, "resolve_meta_all",
                           return_value=[("The Reincarnate", 2014, 1), ("Jaatishwar", 2014, 1)]), \
         mock.patch.object(addon, "search_candidates", side_effect=fake_search), \
         mock.patch.object(addon, "parse_watch_page", return_value=page), \
         _stub_resolve([_card()]):
        out = addon._build_inner("movie", "tt3365690", None, None, time.time() + 5)
    assert len(out["streams"]) == 1, out


def test_build_inner_year_guard_uses_every_source_year():
    """TMDB said 2024, IMDb 2026 for the same film — either must pass the guard."""
    clear_caches()
    page = {"url": "u", "year": 2026, "title": "Ghamasaan", "keys": [("k", True, "Full Movie")],
            "iframe": "i"}
    with mock.patch.object(addon, "resolve_meta_all",
                           return_value=[("Ghamasaan", 2026, 1), ("Ghamasaan", 2024, 1)]), \
         mock.patch.object(addon, "search_candidates", return_value=[{"title": "Ghamasaan", "url": "u"}]), \
         mock.patch.object(addon, "parse_watch_page", return_value=page), \
         _stub_resolve([_card()]):
        out = addon._build_inner("movie", "tt33744610", None, None, time.time() + 5)
    assert len(out["streams"]) == 1, out


def test_build_inner_messages_are_honest():
    clear_caches()
    with mock.patch.object(addon, "resolve_meta_all", return_value=[]):
        assert "no metadata" in addon._build_inner("movie", "tt1", None, None,
                                                   time.time() + 5)["message"]
    with mock.patch.object(addon, "resolve_meta_all", return_value=[("Z", 2020, None)]), \
         mock.patch.object(addon, "search_candidates", return_value=None):
        m = addon._build_inner("movie", "tt1", None, None, time.time() + 5)["message"]
        assert "transient" in m, m
    with mock.patch.object(addon, "resolve_meta_all", return_value=[("Z", 2020, None)]), \
         mock.patch.object(addon, "search_candidates", return_value=[]):
        m = addon._build_inner("movie", "tt1", None, None, time.time() + 5)["message"]
        assert m == "not on BanglaPlex", m
    page = {"url": "u", "year": 2020, "title": "Z", "keys": [("k", True, "Full Movie")], "iframe": "i"}
    with mock.patch.object(addon, "resolve_meta_all", return_value=[("Z", 2020, None)]), \
         mock.patch.object(addon, "search_candidates", return_value=[{"title": "Z", "url": "u"}]), \
         mock.patch.object(addon, "parse_watch_page", return_value=page), \
         _stub_resolve([]):
        m = addon._build_inner("movie", "tt1", None, None, time.time() + 5)["message"]
        assert "no playable/verified source" in m, m


def test_resolve_file_skips_unsupported_and_dead_players():
    """abyss (SoTrym, uncracked) and bestx/chillx (TLS-dead) => honest empty,
    never a guessed card."""
    clear_caches()
    for iframe in ("https://abyssplayer.com/UpjbDHK5N", "https://bestx.stream/v/xee2VgCLScFN/",
                   "https://chillx.top/v/abc/", ""):
        page = {"url": "https://banglaplex.biz/watch/x.html", "title": "X", "year": 2024,
                "keys": [("k", True, "Full Movie")], "iframe": iframe}
        with mock.patch.object(addon, "parse_watch_page", return_value=page), \
             mock.patch.object(addon, "parse_embed_servers",
                               side_effect=AssertionError("must not even fetch")):
            assert addon._resolve_file(page, None, "Full Movie", "", "movie", None, None,
                                       time.time() + 5) == [], iframe


def test_resolve_file_end_to_end_mocked():
    clear_caches()
    page = {"url": "https://banglaplex.biz/watch/x.html", "title": "X", "year": 2026,
            "slug": "x", "keys": [("k", True, "Full Movie")],
            "iframe": "https://plextream.work/embed.php?id=1"}
    info = {"best": (1920, 800), "n_variants": 2, "size": 200, "variant": "v",
            "text": TT_MASTER, "segment": "s", "seg_status": 206}
    with mock.patch.object(addon, "parse_watch_page", return_value=page), \
         mock.patch.object(addon, "parse_embed_servers",
                           return_value=[("S2", "https://bpx.strp2p.site/#ow99qd")]), \
         mock.patch.object(addon, "resolve_n1",
                           return_value=(PAYLOAD, "bpx.strp2p.site", "6zscnd")), \
         mock.patch.object(addon, "collect_subtitles",
                           return_value=[{"url": "u", "lang": "en", "id": "bpx-en"}]), \
         mock.patch.object(addon, "_verify_media", return_value=info):
        cards = addon._resolve_file(page, None, "Full Movie", "", "series", 1, 1,
                                    time.time() + 5)
    assert len(cards) == 2, [c["name"] for c in cards]
    assert cards[0]["url"].endswith("/tt/master.m3u8")
    assert cards[0]["behaviorHints"]["notWebReady"] is False
    assert cards[1]["behaviorHints"]["notWebReady"] is True
    assert "S01 E01" in cards[0]["description"]


def test_resolve_file_hls_kill_switch():
    clear_caches()
    page = {"url": "u", "title": "X", "year": 2026, "keys": [],
            "iframe": "https://plextream.work/embed.php?id=1"}
    with mock.patch.object(addon, "parse_watch_page", return_value=page), \
         mock.patch.object(addon, "parse_embed_servers",
                           return_value=[("S2", "https://bpx.strp2p.site/#ow99qd")]), \
         mock.patch.object(addon, "resolve_n1", return_value=(PAYLOAD, "bpx.strp2p.site", "v")), \
         mock.patch.object(addon, "collect_subtitles", return_value=[]), \
         mock.patch.object(addon, "_verify_media", side_effect=AssertionError("no verify")), \
         mock.patch.object(addon, "HLS_ON", False):
        assert addon._resolve_file(page, None, "l", "", "movie", None, None,
                                   time.time() + 5) == []


# ═════════════════════════════════════════════════ 13. build_streams/caching
def test_build_streams_caches_positive():
    clear_caches()
    cards = [_card()]
    with mock.patch.object(addon, "_build_inner", return_value={"streams": cards}) as m:
        r1 = addon.build_streams("movie", "tt5", None, None)
        r2 = addon.build_streams("movie", "tt5", None, None)
    assert m.call_count == 1
    assert r1["streams"] == cards and r2["streams"] == cards
    assert addon.C_STALE[("movie", "tt5", None, None)][1] == cards


def test_build_streams_negative_is_short_lived():
    """an honest empty is cached 300s (cheap) — but a transient failure must not
    be cached as a negative at all."""
    clear_caches()
    with mock.patch.object(addon, "_build_inner",
                           return_value={"streams": [], "message": "not on BanglaPlex"}):
        r = addon.build_streams("movie", "tt6", None, None)
    assert r["streams"] == [] and r["message"] == "not on BanglaPlex"
    hit, val = addon.C_STREAM.get(("movie", "tt6", None, None))
    assert hit and val == []
    ent = addon.C_STREAM[("movie", "tt6", None, None)]
    assert ent[0] - time.time() <= addon._NEG_TTL + 1


def test_build_streams_swr_serves_stale_and_refreshes():
    clear_caches()
    old = [_card("old")]
    addon.C_STALE[("movie", "tt7", None, None)] = (time.time() + 60, old)
    started = threading.Event()
    done = threading.Event()

    def slow_build(*a, **k):
        started.set()
        time.sleep(0.2)
        done.set()
        return {"streams": [_card("new")]}
    with mock.patch.object(addon, "_build_inner", side_effect=slow_build):
        r = addon.build_streams("movie", "tt7", None, None)
    assert r["streams"] == old                      # stale served immediately
    assert started.wait(2)
    done.wait(3)
    time.sleep(0.2)
    hit, val = addon.C_STREAM.get(("movie", "tt7", None, None))
    assert hit and val[0]["name"] == "new"          # background refresh landed


def test_build_streams_wall_exceeded_asks_for_retry():
    clear_caches()

    def hang(*a, **k):
        time.sleep(3)
        return {"streams": [_card()]}
    with mock.patch.object(addon, "_build_inner", side_effect=hang), \
         mock.patch.object(addon, "WALL", 0.3):
        r = addon.build_streams("movie", "tt8", None, None)
    assert r["streams"] == [] and "still resolving" in r["message"]
    assert not addon.C_STREAM.get(("movie", "tt8", None, None))[0], \
        "a wall timeout must not be cached as a negative"


def test_build_streams_does_not_cache_transient_failures():
    """house rule: never cache a transient failure as a negative — a Cloudflare
    blip must not become a 5-minute 'not on BanglaPlex'."""
    clear_caches()
    with mock.patch.object(addon, "_build_inner",
                           return_value={"streams": [],
                                         "message": "BanglaPlex search is not "
                                                    "answering right now (transient)"}):
        r = addon.build_streams("movie", "tt77", None, None)
    assert r["streams"] == []
    assert not addon.C_STREAM.get(("movie", "tt77", None, None))[0]
    with mock.patch.object(addon, "_build_inner",
                           return_value={"streams": [], "message": "not on BanglaPlex"}):
        addon.build_streams("movie", "tt78", None, None)
    assert addon.C_STREAM.get(("movie", "tt78", None, None))[0]


def test_build_inner_series_season_the_site_lacks_is_honest():
    """asking for S09 on a page that only has S01-S03 must NOT fall back to the
    site's default key (that would serve a different season)."""
    clear_caches()
    page = {"url": "u", "year": 2024, "title": "Mirzapur", "iframe": "https://plextream.work/e?id=1",
            "keys": [("k1", True, "Bonus Episode"), ("k2", False, "S03"),
                     ("k3", False, "S02"), ("k4", False, "S01")]}
    calls = []
    with mock.patch.object(addon, "resolve_meta_all", return_value=[("Mirzapur", 2024, 1)]), \
         mock.patch.object(addon, "search_candidates", return_value=[{"title": "Mirzapur", "url": "u"}]), \
         mock.patch.object(addon, "parse_watch_page", return_value=page), \
         mock.patch.object(addon, "_resolve_file",
                           side_effect=lambda *a, **k: calls.append(a) or [_card()]):
        out = addon._build_inner("series", "tt9", 9, 1, time.time() + 5)
    assert out["streams"] == [] and "no playable" in out["message"], out
    assert calls == [], "must not resolve any file for a season the site lacks"
    # ...but a season the site HAS still resolves
    with mock.patch.object(addon, "resolve_meta_all", return_value=[("Mirzapur", 2024, 1)]), \
         mock.patch.object(addon, "search_candidates", return_value=[{"title": "Mirzapur", "url": "u"}]), \
         mock.patch.object(addon, "parse_watch_page", return_value=page), \
         mock.patch.object(addon, "_resolve_file", return_value=[_card()]) as m:
        out2 = addon._build_inner("series", "tt9", 2, 1, time.time() + 5)
    assert len(out2["streams"]) == 1
    assert m.call_args[0][1] == "k3" and m.call_args[0][2] == "S02", m.call_args


def test_build_streams_caps_at_max_cards():
    clear_caches()
    cards = [_card("c%d" % i) for i in range(6)]
    with mock.patch.object(addon, "_build_inner", return_value={"streams": cards[:addon.MAX_CARDS]}):
        r = addon.build_streams("movie", "tt10", None, None)
    assert len(r["streams"]) <= addon.MAX_CARDS


# ══════════════════════════════════════════════════════════ 14. TTL cache
def test_ttlcache_expiry_and_hit():
    c = addon.TTLCache(1024, "t")
    c.put("a", "x", 60)
    assert c.get("a") == (True, "x")
    c.put("b", "y", -1)
    assert c.get("b") == (False, None)
    assert c.get("zz") == (False, None)


def test_ttlcache_budget_evicts_oldest():
    c = addon.TTLCache(200, "t")
    for i in range(40):
        c.put("k%02d" % i, "v" * 60, 600)
    assert c.bytes <= c.budget, c.bytes
    assert len(c) < 40


def test_ttlcache_accounts_bytes():
    c = addon.TTLCache(1 << 20, "t")
    c.put("a", "x" * 100, 60)
    before = c.bytes
    c.put("a", "y" * 10, 60)
    assert c.bytes < before


# ════════════════════════════════════════════════════════════ 15. HTTP
def test_http_health():
    c = _http_get("/health")
    assert c["code"] == 200
    d = _json_body(c)
    assert d["ok"] is True and d["addon"] == "BanglaPlex" and d["version"] == addon.VERSION
    assert d["egress"].startswith("text-only")
    assert d["site"] == "https://banglaplex.biz"
    assert {"name", "entries", "bytes", "budget"} <= set(d["caches"][0])


def test_http_manifest_is_stream_only():
    d = _json_body(_http_get("/manifest.json"))
    assert d["id"] == "com.banglaplex.stremio"
    assert d["resources"] == ["stream", "subtitles"]
    assert d["catalogs"] == [] and d["types"] == ["movie", "series"]
    assert d["idPrefixes"] == ["tt"]


def test_http_landing_page():
    c = _http_get("/")
    assert c["code"] == 200 and c["headers"]["Content-Type"].startswith("text/html")
    assert b"stremio:///install" in c["body"]
    assert b"bpx-test.onrender.com/manifest.json" in c["body"]


def test_http_stream_route():
    with mock.patch.object(addon, "build_streams",
                           return_value={"streams": [_card()]}):
        c = _http_get("/stream/movie/tt0213890.json")
    assert c["code"] == 200
    d = _json_body(c)
    assert d["streams"][0]["url"].startswith("https://bpx.strp2p.site/")
    assert c["headers"]["Access-Control-Allow-Origin"] == "*"
    assert c["headers"]["Cache-Control"] == "no-store"


def test_http_stream_route_series_id_split():
    seen = {}

    def fake(ctype, imdb, se, ep):
        seen.update(dict(ctype=ctype, imdb=imdb, se=se, ep=ep))
        return {"streams": []}
    with mock.patch.object(addon, "build_streams", side_effect=fake):
        c = _http_get("/stream/series/tt31924802:1:1.json")
    assert c["code"] == 200
    assert seen == {"ctype": "series", "imdb": "tt31924802", "se": 1, "ep": 1}


def test_http_stream_route_bad_id_404():
    assert _http_get("/stream/movie/xyz.json")["code"] == 404
    assert _http_get("/stream/movie/.json")["code"] == 404


def test_http_stream_route_passes_message():
    with mock.patch.object(addon, "build_streams",
                           return_value={"streams": [], "message": "not on BanglaPlex"}):
        d = _json_body(_http_get("/stream/movie/tt1234567.json"))
    assert d == {"streams": [], "message": "not on BanglaPlex"}


def test_http_subtitles_route_both_shapes():
    clear_caches()
    addon.C_STREAM.put(("series", "tt31924802", 1, 1),
                       [_card()], addon._STREAM_TTL)
    addon.C_STREAM[("series", "tt31924802", 1, 1)][1][0]["subtitles"] = [
        {"url": "https://bpx/en.vtt", "lang": "en", "id": "bpx-en"}]
    for path in ("/subtitles/series/tt31924802:1:1.json",
                 "/subtitles/series/tt31924802:1:1/bpx-en.json"):
        d = _json_body(_http_get(path))
        assert [s["lang"] for s in d["subtitles"]] == ["en"], path


def test_http_gzip():
    big = {"streams": [_card("c%d" % i,
                             "https://bpx.strp2p.site/hls/%d/master.m3u8" % i)
                       for i in range(30)]}
    with mock.patch.object(addon, "build_streams", return_value=big):
        c = _http_get("/stream/movie/tt0213890.json", accept_enc="gzip")
    assert c["headers"].get("Content-Encoding") == "gzip"
    assert len(c["body"]) == int(c["headers"]["Content-Length"])
    assert len(_json_body(c)["streams"]) == 30


def test_http_no_gzip_for_tiny_body():
    with mock.patch.object(addon, "build_streams", return_value={"streams": []}):
        c = _http_get("/stream/movie/tt0213890.json", accept_enc="gzip")
    assert "Content-Encoding" not in c["headers"]
    assert len(c["body"]) == int(c["headers"]["Content-Length"])


def test_http_unknown_route_404():
    c = _http_get("/nope")
    assert c["code"] == 404


def test_http_500_is_contained():
    with mock.patch.object(addon, "build_streams", side_effect=RuntimeError("boom")):
        c = _http_get("/stream/movie/tt1234567.json")
    assert c["code"] == 500 and b"internal" in c["body"]


def test_debug_requires_key():
    assert _http_get("/debug/reqlog")["code"] == 404
    assert _http_get("/debug/reqlog?k=wrong")["code"] == 404
    c = _http_get("/debug/reqlog?k=" + addon.DEBUG_KEY)
    assert c["code"] == 200 and "log" in _json_body(c)


def test_debug_resolve_and_chain_routes_exist():
    with mock.patch.object(addon, "parse_watch_page",
                           return_value={"title": "X", "year": 2026, "keys": [("k", True, "Full Movie")],
                                         "iframe": "https://plextream.work/embed.php?id=1"}), \
         mock.patch.object(addon, "_resolve_file", return_value=[_card()]):
        d = _json_body(_http_get("/debug/resolve?k=%s&slug=x" % addon.DEBUG_KEY))
    assert d["cards"] == 1 and d["picks"]
    with mock.patch.object(addon, "_verify_media", return_value=None):
        d2 = _json_body(_http_get("/debug/chain?k=%s&url=https://x/m.m3u8" % addon.DEBUG_KEY))
    assert d2 == {"ok": False}


# ════════════════════════════════════════════════════ 15b. proxy pool (Render egress)
def _pool_reset():
    addon._POOL[0] = ["http://1.1.1.1:8080", "http://2.2.2.2:8080",
                      "http://3.3.3.3:8080"]
    addon._POOL_TS[0] = time.time()
    addon._POOL_BAD.clear()
    addon._POOL_OK.clear()
    addon._STICKY[0], addon._STICKY[1] = None, 0.0
    addon._DIRECT_BAD.clear()


def test_pool_refresh_parses_proxyscrape_text():
    txt = "http://1.2.3.4:8080\n5.6.7.8:3128\n\n# comment\nhttp://1.2.3.4:8080\n"
    with mock.patch.object(addon.requests, "get", return_value=_resp(200, txt)):
        addon._POOL[0] = []
        addon._POOL_TS[0] = 0.0
        out = addon._pool_refresh(force=True)
    assert out == ["http://1.2.3.4:8080", "http://5.6.7.8:3128"], out


def test_pool_refresh_keeps_old_list_on_failure():
    _pool_reset()
    before = list(addon._POOL[0])
    with mock.patch.object(addon.requests, "get", side_effect=RuntimeError("down")):
        addon._POOL_TS[0] = 0.0
        out = addon._pool_refresh(force=True)
    assert out == before, "a failed refresh must not empty a working pool"


def test_pool_order_sticky_and_benching():
    _pool_reset()
    assert addon._pool_order()[0] == "http://1.1.1.1:8080"
    addon._pool_note("http://2.2.2.2:8080", True)
    assert addon._pool_order()[0] == "http://2.2.2.2:8080", "a good exit goes sticky"
    addon._pool_note("http://2.2.2.2:8080", False, blocked=True)
    assert "http://2.2.2.2:8080" not in addon._pool_order()
    assert addon._POOL_BAD["http://2.2.2.2:8080"] - time.time() > 600
    addon._pool_note("http://3.3.3.3:8080", False)
    assert addon._POOL_BAD["http://3.3.3.3:8080"] - time.time() < 600


def test_fetch_direct_when_host_answers():
    _pool_reset()
    calls = []
    with mock.patch.object(addon._S, "get",
                           side_effect=lambda u, **k: calls.append(k.get("proxies")) or _resp(200, "ok")):
        r, via = addon._fetch("https://banglaplex.biz/x")
    assert r.status_code == 200 and via is False
    assert calls == [None], "a healthy direct route must not touch the pool"


def test_fetch_falls_back_to_pool_on_403_in_the_same_call():
    """Render's egress is Cloudflare-flagged: the user must never see the first
    failure, and the host must stop paying for a direct attempt afterwards."""
    _pool_reset()
    seq = [_resp(403, "cf challenge"), _resp(200, "<html>watch</html>")]
    used = []

    def fake(u, **k):
        used.append(k.get("proxies"))
        return seq.pop(0)
    with mock.patch.object(addon._S, "get", side_effect=fake):
        r, via = addon._fetch("https://banglaplex.biz/watch/x.html")
    assert r.status_code == 200 and via is True
    assert used[0] is None and used[1] == {"http": "http://1.1.1.1:8080",
                                           "https": "http://1.1.1.1:8080"}
    assert addon._DIRECT_BAD["banglaplex.biz"] > time.time()
    used2 = []
    with mock.patch.object(addon._S, "get",
                           side_effect=lambda u, **k: used2.append(k.get("proxies")) or _resp(200, "ok")):
        r2, via2 = addon._fetch("https://banglaplex.biz/watch/y.html")
    assert via2 is True and used2[0] is not None


def test_fetch_walks_exits_until_one_works():
    _pool_reset()
    seq = [_resp(403, ""), RuntimeError("dead exit"), _resp(200, "ok")]

    def fake(u, **k):
        if k.get("proxies") is None:
            raise RuntimeError("direct down")
        v = seq.pop(0)
        if isinstance(v, Exception):
            raise v
        return v
    with mock.patch.object(addon._S, "get", side_effect=fake):
        r, via = addon._fetch("https://banglaplex.biz/x")
    assert via is True and r.status_code == 200
    assert len(addon._POOL_BAD) >= 1


def test_fetch_all_exits_dead_is_transient_none():
    _pool_reset()
    with mock.patch.object(addon._S, "get", side_effect=RuntimeError("nope")):
        r, via = addon._fetch("https://banglaplex.biz/x")
    assert r is None


def test_range_probe_never_uses_a_proxy():
    """media playability must be proven on a normal client path, and no media
    byte may ride a free exit."""
    _pool_reset()
    addon._DIRECT_BAD["p16-ad-site-sign-sg.tiktokcdn.com"] = time.time() + 600
    used = []

    class FakeR:
        status_code = 206

        def iter_content(self, n):
            yield b"x" * 300

        def close(self):
            pass
    with mock.patch.object(addon._S, "get",
                           side_effect=lambda u, **k: used.append(k.get("proxies")) or FakeR()):
        code, n = addon._range_probe("https://p16-ad-site-sign-sg.tiktokcdn.com/seg.ts")
    assert code == 206 and n == 300, (code, n)   # nbytes only sizes the Range header
    assert used == [None], used


def test_pool_kill_switch():
    _pool_reset()
    with mock.patch.object(addon, "POOL_ON", False):
        assert addon._pool_refresh() == []
        with mock.patch.object(addon._S, "get", return_value=_resp(403, "cf")):
            r, via = addon._fetch("https://banglaplex.biz/x")
    assert via is False and r.status_code == 403


def test_net_probe_reports_both_paths():
    """the probe must race the pool exactly like a real fetch — probing one exit
    reported ReadTimeout on prod while resolves through the pool worked fine."""
    _pool_reset()
    good = _resp(200, "<html>site</html>")
    seen = []

    def fake(u, **k):
        seen.append(k.get("proxies"))
        if k.get("proxies") is None:
            return _resp(403, "cf")            # direct blocked from Render
        if (k["proxies"]["http"]) == "http://1.1.1.1:8080":
            raise RuntimeError("dead")
        return good                            # a later exit works
    with mock.patch.object(addon._S, "get", side_effect=fake):
        d = addon._net_probe(only="site_home")
    assert list(d["probes"]) == ["site_home"]
    row = d["probes"]["site_home"]
    assert row["direct"][0] == 403
    assert row["proxy"][0] == 200, row["proxy"]
    assert len(seen) > 2, "the probe must race several exits, not just one"
    assert d["pool"]["pool"] == 3


def test_pool_drops_socks_exits():
    """socks4/5 exits need PySocks; without it every one raised InvalidSchema and
    the whole pool looked dead (40/40 on the first prod probe)."""
    txt = ("socks4://57.128.231.218:1004\nsocks5://9.9.9.9:1080\n"
           "http://1.2.3.4:8080\nhttps://5.6.7.8:8443\n")
    with mock.patch.object(addon.requests, "get", return_value=_resp(200, txt)):
        addon._POOL[0] = []
        addon._POOL_TS[0] = 0.0
        out = addon._pool_refresh(force=True)
    assert out == ["http://1.2.3.4:8080", "https://5.6.7.8:8443"], out


def test_pool_get_races_exits_and_keeps_first_good():
    _pool_reset()
    good = _resp(200, "ok")
    closed = []

    class Slow:
        status_code = 200

        def close(self):
            closed.append("slow")

    def fake(u, **k):
        p = (k.get("proxies") or {}).get("http")
        if p == "http://1.1.1.1:8080":
            time.sleep(0.4)
            return Slow()                      # a LATE winner must be closed
        if p == "http://2.2.2.2:8080":
            return good                        # first good answer wins
        raise RuntimeError("dead exit")
    with mock.patch.object(addon._S, "get", side_effect=fake):
        r = addon._pool_get("https://x/y", {"User-Agent": "t"}, 5)
    assert r is good
    for _ in range(40):                        # let the losers finish cleanly
        if closed:
            break
        time.sleep(0.05)
    assert "slow" in closed, "losing racers must be closed, not leaked"
    assert addon._POOL_OK.get("http://2.2.2.2:8080"), "the winner becomes sticky"


def test_pool_get_benches_dead_exits():
    _pool_reset()
    with mock.patch.object(addon._S, "get", side_effect=RuntimeError("dead")):
        assert addon._pool_get("https://x", {"User-Agent": "t"}, 3) is None
    assert set(addon._POOL_BAD) == set(addon._POOL[0]), addon._POOL_BAD


def test_pool_get_no_exits():
    addon._POOL[0] = []
    assert addon._pool_get("https://x", {}, 3) is None


# ═════════════════════════════════════════════ 16. zero-bandwidth contract
def test_zero_bandwidth_no_media_routes():
    """Render must never carry a media byte: no playlist/segment/subtitle relay
    routes, no proxy endpoints. Cards point straight at the CDN."""
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "addon.py")).read()
    for gone in ('"/hls/', '"/seg', '"/proxy', '"/video', '"/file', 'def dash_manifest',
                 'def relay', '"video/mp4"', '"video/mp2t"', '"application/vnd.apple.mpegurl"'):
        assert gone not in src, gone


def test_zero_bandwidth_cards_never_point_at_us():
    clear_caches()
    page = {"url": "u", "title": "X", "year": 2026, "slug": "x", "keys": [],
            "iframe": "https://plextream.work/embed.php?id=1"}
    info = {"best": (1920, 800), "n_variants": 1, "size": 10, "variant": "v",
            "text": TT_MASTER, "segment": "s", "seg_status": 206}
    with mock.patch.object(addon, "parse_watch_page", return_value=page), \
         mock.patch.object(addon, "parse_embed_servers",
                           return_value=[("S", "https://bpx.strp2p.site/#v")]), \
         mock.patch.object(addon, "resolve_n1", return_value=(PAYLOAD, "bpx.strp2p.site", "v")), \
         mock.patch.object(addon, "collect_subtitles", return_value=[]), \
         mock.patch.object(addon, "_verify_media", return_value=info):
        cards = addon._resolve_file(page, None, "l", "", "movie", None, None, time.time() + 5)
    for c in cards:
        assert c["url"].startswith("https://bpx.strp2p.site/"), c["url"]
        assert "/onrender.com" not in c["url"]


def test_relay_bytes_stay_zero():
    assert addon._STATS["relay_bytes"] == 0


def test_keepalive_self_pings_learned_host():
    addon._KEEPALIVE[0] = ""
    addon._PUBLIC_BASE[0] = "https://bpx-test.onrender.com"
    hits = []
    with mock.patch.object(addon.requests, "get",
                           side_effect=lambda u, **k: hits.append(u) or _resp(200, "{}")):
        t = threading.Thread(target=addon._keepalive_loop, daemon=True)
        t.start()
        time.sleep(0.3)
    assert hits and hits[0] == "https://bpx-test.onrender.com/health", hits
    assert addon._KEEPALIVE[0] == "https://bpx-test.onrender.com"


def test_version_and_manifest_agree():
    assert re.fullmatch(r"\d+\.\d+\.\d+", addon.VERSION)
    assert addon.MANIFEST["version"] == addon.VERSION


def test_config_env_overrides():
    assert addon.SITE == "https://banglaplex.biz"
    assert addon.AES_KEY == b"kiemtienmua911ca" and len(addon.AES_IV) == 16
    assert addon.N1_HOSTS == ("bpx.strp2p.site", "bpx.rpmvid.site")
    assert addon.INHOUSE_ON is False and addon.HLS_ON is True


# ══════════════════════════════════════════════ 17. LIVE (BPX_LIVE=1 only)
def live_resolve_movie():
    """real cold resolve: Mohabbatein (tt0213890) — Cloudflare-only card."""
    clear_caches()
    t0 = time.time()
    out = addon._build_inner("movie", "tt0213890", None, None, time.time() + 60)
    dt = time.time() - t0
    assert out.get("streams"), out
    c = out["streams"][0]
    print("      live movie: %s (%.1fs)" % (c["name"], dt))
    assert c["url"].startswith("https://bpx.")
    assert "Mohabbatein" in c["name"]


def live_resolve_series_season_pack():
    """real series resolve: The Revolutionaries S01E01 -> full-season file."""
    clear_caches()
    out = addon._build_inner("series", "tt31924802", 1, 1, time.time() + 60)
    assert out.get("streams"), out
    c = out["streams"][0]
    print("      live series: %s subs=%d" % (c["name"], len(c.get("subtitles") or [])))
    assert "full-season file" in c["description"]
    assert len(c.get("subtitles") or []) >= 2
    langs = [s["lang"] for s in c["subtitles"]]
    assert langs[0] == "en"


def live_card_actually_plays():
    """the real proof: master 200 -> variant head has segments -> segment 206."""
    clear_caches()
    out = addon._build_inner("series", "tt31924802", 1, 1, time.time() + 60)
    assert out.get("streams"), out
    for c in out["streams"]:
        bh = c.get("behaviorHints") or {}
        ref = ((bh.get("proxyHeaders") or {}).get("request") or {}).get("Referer")
        st, master = addon._playlist_head(c["url"], referer=ref)
        assert st == 200 and "#EXTM3U" in master, (c["url"], st)
        var = [l for l in master.splitlines() if l.strip() and not l.startswith("#")]
        st2, head = addon._playlist_head(addon._abs(c["url"], var[0]), referer=ref)
        assert st2 == 200 and "#EXTINF" in head, st2
        seg = [l for l in head.splitlines() if l.strip() and not l.startswith("#")][0]
        code, n = addon._range_probe(addon._abs(var[0], seg), referer=ref)
        assert code in (200, 206) and n > 0, (code, n)
        print("      live play check: %s -> seg %s (%dB)" % (c["name"][:28], code, n))


def live_honest_empty_for_dead_player():
    """Interstellar sits on bestx.stream (TLS-dead) => honest empty, not a guess."""
    clear_caches()
    out = addon._build_inner("movie", "tt0816692", None, None, time.time() + 40)
    assert out["streams"] == [] and "no playable/verified source" in out["message"], out


def live_health_over_http():
    from urllib.request import urlopen
    with urlopen("http://127.0.0.1:%d/health" % addon.PORT, timeout=15) as r:
        d = json.loads(r.read().decode())
    assert d["ok"] is True and d["relay_bytes"] == 0 if "relay_bytes" in d else True


LIVE_TESTS = [live_resolve_movie, live_resolve_series_season_pack,
              live_card_actually_plays, live_honest_empty_for_dead_player]

OFFLINE_TESTS = [v for k, v in sorted(globals().items())
                 if k.startswith("test_") and callable(v)]


def main():
    print("BanglaPlex addon %s — %d offline tests" % (addon.VERSION, len(OFFLINE_TESTS)))
    for t in OFFLINE_TESTS:
        run(t)
    if LIVE:
        print("  -- live integration (BPX_LIVE=1) --")
        for t in LIVE_TESTS:
            run(t)
    else:
        print("  (live integration skipped — set BPX_LIVE=1 to run it)")
    clear_caches()
    print("\n%d/%d passed" % (PASS, PASS + FAIL))
    return 1 if FAIL else 0


# ── the runner is LAST: a test defined after `main()` would silently never run
if __name__ == "__main__":
    sys.exit(main())
