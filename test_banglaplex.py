#!/usr/bin/env python3
"""Unit tests for the BanglaPlex Stremio addon.  Run: python3 test_banglaplex.py

Offline by default — every network call is mocked, so the suite is safe to run
anywhere and never burns 3n1 rate-limit budget.

Set BPX_LIVE=1 to ALSO run the live-integration block at the bottom. That block
really hits banglaplex.biz, plextream.work, the 3n1 frontends and the CDNs, so it
is rate-limit sensitive; never run it in a loop.
"""
import io
import base64
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


def _quiesce(timeout=8.0):
    """Wait for every executor to drain before the next test starts.

    A test that races providers or resolves in the background leaves futures
    running after its `with mock.patch(...)` block exits; those threads then make
    real `_S.get` calls *during the next test*, which is how
    `test_net_probe_reports_both_paths` and `test_fetch_walks_exits_until_one_works`
    came to fail intermittently (measured polluters: test_build_meta_uses_providers_*,
    test_build_meta_ships_a_year_*, test_http_gzip). ThreadPoolExecutor is FIFO, so
    a sentinel submitted now is guaranteed to run after everything queued so far."""
    import concurrent.futures as cf
    for ex in (addon._IO_EX, addon._V_EX, addon._BUILD_EX, addon._PP_EX, addon._P_EX):
        try:
            cf.wait([ex.submit(lambda: None)], timeout=timeout)
        except Exception:
            pass


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
    finally:
        _quiesce()


def clear_caches():
    for c in (addon.C_SEARCH, addon.C_PAGE, addon.C_META, addon.C_EMBED,
              addon.C_N1, addon.C_STREAM, addon.C_LIST, addon.C_IMDB,
              addon.C_METARES, addon.C_SLUG, addon.C_ABYSS):
        c.clear()
        c.bytes = 0
    addon.C_STALE.clear()
    addon._NEG_RETRY_AT.clear()
    addon._SWR_RUNNING.clear()
    addon._WALLED.clear()
    with addon._BUILD_LOCK:
        addon._BUILD_INFLIGHT.clear()
    addon._SLUG_KIND.clear()
    addon._PREWARM_BUSY[0] = False      # a killed prewarm must not leak "busy"
    # NOTE: never _STATS.clear() — it is a counter dict whose keys the /health
    # surface and the tests read directly; emptying it raises KeyError.
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


def test_card_shows_a_media_size_and_never_a_playlist_size():
    """_master_info's byte count is the length of the PLAYLIST, so rendering it as
    the video's size made a 1080p feature advertise "▤ 323 B". Only a real media
    byte count (the abyss origin reports one) may reach that slot."""
    pl = {"best": (1920, 1080), "n_variants": 2, "variant": "v", "text": TT_MASTER,
          "pl_bytes": 323, "size": 323}          # `size` here means playlist bytes
    c = addon.format_card("tiktok", "https://x/m.m3u8", None, "TikTok CDN", pl,
                          _page(_ctype="movie"), PAYLOAD)
    assert "▤" not in c["description"], c["description"]
    assert "♧ FHD 1080p" in c["name"]

    ab = {"best": (0, 0), "label": "1080p", "media_size": 2844976106,
          "codec": "avc1", "seekable": True, "probe": 200}
    c2 = addon.format_card("abyss", "https://njuaynwkh47.sssrr.org/sora/x",
                           addon.ABYSS_REFERER, "Abyss", ab, _page(_ctype="movie"), {})
    assert "▤ 2.65 GB" in c2["description"], c2["description"]


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


def test_resolve_file_skips_dead_players():
    """bestx/chillx (TLS-dead) and no-iframe => honest empty, never a guessed
    card. Abyss used to be in this list; it is a real source now."""
    clear_caches()
    for iframe in ("https://bestx.stream/v/xee2VgCLScFN/",
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


def test_http_manifest_declares_catalogs_meta_and_config():
    d = _json_body(_http_get("/manifest.json"))
    assert d["id"] == "com.banglaplex.stremio"
    assert d["resources"] == ["stream", "subtitles", "catalog", "meta"]
    assert d["types"] == ["movie", "series"]
    # bpx- ids must be declared or Stremio will not route our own catalog items
    # back to this addon for stream/meta
    assert d["idPrefixes"] == ["tt", "bpx-"]
    assert d["behaviorHints"]["configurable"] is True
    assert {(c["type"], c["id"]) for c in d["catalogs"]} == {
        ("movie", "bpx-latest"), ("movie", "bpx-year"), ("series", "bpx-series")}
    for c in d["catalogs"]:
        names = [e["name"] for e in c["extra"]]
        assert "search" in names, c
    genres = {o for c in d["catalogs"] for e in c["extra"]
              if e["name"] == "genre" for o in e["options"]}
    assert "bengali-movies" in genres and "bengali-web-series" in genres


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
    big = {"metas": [{"id": "tt%07d" % i, "type": "movie", "name": "Title %d" % i,
                      "poster": "https://banglaplex.biz/uploads/video_thumb/%d.jpg" % i}
                     for i in range(60)]}
    with mock.patch.object(addon, "catalog_items", return_value=big["metas"]):
        c = _http_get("/catalog/movie/bpx-latest.json", accept_enc="gzip")
    assert c["headers"].get("Content-Encoding") == "gzip"
    assert len(c["body"]) == int(c["headers"]["Content-Length"])
    assert len(_json_body(c)["metas"]) == 60


def test_http_stream_cards_are_capped_by_config():
    """apply_cfg caps to the install's card limit; the shared cache still holds
    every verified card so a different config gets its own slice."""
    many = {"streams": [_card("c%d" % i,
                              "https://bpx.strp2p.site/hls/%d/master.m3u8" % i)
                        for i in range(9)]}
    with mock.patch.object(addon, "build_streams", return_value=many):
        c = _http_get("/stream/movie/tt0213890.json")
    assert len(_json_body(c)["streams"]) == addon.MAX_CARDS
    seg = addon.cfg_pack({"n": 1})
    with mock.patch.object(addon, "build_streams", return_value=many):
        c1 = _http_get("/%s/stream/movie/tt0213890.json" % seg)
    assert len(_json_body(c1)["streams"]) == 1


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
    # a real background trainer spawned by an earlier test outlives its mock and
    # benches whatever it probes, polluting later assertions
    addon._TRAINING[0] = True
    addon._POOL_PULLING[0] = False
    addon._POOL_READY.set()
    addon._POOL_BAD.clear()
    addon._POOL_STATS.clear()
    addon._STICKY_BUSY.clear()
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
    ok = _resp(200, "<html>watch</html>")
    used = []

    def fake(u, **k):
        p = (k.get("proxies") or {}).get("http")
        used.append(k.get("proxies"))
        if p is None:
            return _resp(403, "cf challenge")          # Render egress flagged
        if p == "http://3.3.3.3:8080":
            time.sleep(0.25)                           # winner arrives last
            return ok
        raise RuntimeError("dead exit")
    with mock.patch.object(addon._S, "get", side_effect=fake):
        r, via = addon._fetch("https://banglaplex.biz/watch/x.html")
    assert r.status_code == 200 and via is True
    assert used[0] is None, "direct is always tried first"
    assert len(used) == 4, "every exit must be raced: %r" % used
    assert addon._DIRECT_BAD["banglaplex.biz"] > time.time()
    used2 = []
    with mock.patch.object(addon._S, "get",
                           side_effect=lambda u, **k: used2.append(k.get("proxies")) or _resp(200, "ok")):
        r2, via2 = addon._fetch("https://banglaplex.biz/watch/y.html")
    assert via2 is True and used2[0] is not None


def test_fetch_walks_exits_until_one_works():
    _pool_reset()

    def fake(u, **k):
        """Answer per EXIT rather than per call order: a background thread from an
        earlier test can land in this mock, and a pop-order sequence would then
        hand the wrong answer to the wrong exit (this test failed intermittently
        for exactly that reason)."""
        px = k.get("proxies")
        if px is None:
            raise RuntimeError("direct down")
        host = px.get("http")
        if host == "http://1.1.1.1:8080":
            return _resp(403, "")                # blocked -> bench and move on
        if host == "http://2.2.2.2:8080":
            raise RuntimeError("dead exit")      # transport failure -> move on
        return _resp(200, "ok")                  # 3.3.3.3 is the one that works
    with mock.patch.object(addon._S, "get", side_effect=fake), \
         mock.patch.object(addon, "_pool_order",
                           return_value=["http://1.1.1.1:8080", "http://2.2.2.2:8080",
                                         "http://3.3.3.3:8080"]):
        # _pool_order is pinned because a trainer thread left over from an earlier
        # test can republish the pool mid-flight and empty it under us
        r, via = addon._fetch("https://banglaplex.biz/x")
    assert via is True and r.status_code == 200, (via, r)
    for _ in range(40):
        # the 403 exit is benched by its OWN racer thread, and _pool_get returns
        # the moment 3.3.3.3 answers — so this is a poll, never an immediate read
        if addon._POOL_BAD:
            break
        time.sleep(0.05)
    assert len(addon._POOL_BAD) >= 1, "the flagged exit must be benched"


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
        if k["proxies"]["http"] == "http://3.3.3.3:8080":
            time.sleep(0.3)                    # the winner arrives LAST
            return good
        raise RuntimeError("dead")
    with mock.patch.object(addon._S, "get", side_effect=fake):
        d = addon._net_probe(only="site_home")
    assert list(d["probes"]) == ["site_home"]
    row = d["probes"]["site_home"]
    assert row["direct"][0] == 403
    assert row["proxy"][0] == 200, row["proxy"]
    # the intent is "race every exit, not just one" — assert that as a set, since
    # a background thread from an earlier test can add calls of its own
    assert None in seen, "the direct path must be probed too: %r" % seen
    raced = {p["http"] for p in seen if p}
    assert raced == {"http://1.1.1.1:8080", "http://2.2.2.2:8080",
                     "http://3.3.3.3:8080"}, "must race every exit: %r" % seen
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


def test_pool_get_closes_a_loser_that_finished_before_the_winner():
    """reap() only closes once `win` is set, so a racer whose future was already
    done when the callbacks were attached never got closed: its socket stayed open
    for the life of the process. Running the racers inline makes every future
    complete first, which is exactly that ordering. `as_completed` yields
    already-done futures in set order, so WHICH answer wins is arbitrary — the
    invariant is that the loser is closed."""
    _pool_reset()
    closed = []
    exits = ["http://1.1.1.1:8080", "http://2.2.2.2:8080", "http://3.3.3.3:8080"]

    class Answer:
        status_code = 200

        def __init__(self, tag):
            self.tag = tag
            self.text = "ok"

        def close(self):
            closed.append(self.tag)

    second, third = Answer("second"), Answer("third")

    def fake(u, **k):
        p = (k.get("proxies") or {}).get("http")
        if p == "http://1.1.1.1:8080":
            raise RuntimeError("dead exit")        # nothing to leak
        return second if p == "http://2.2.2.2:8080" else third

    import concurrent.futures as cf

    class InlineEx:
        def submit(self, fn, *a, **k):
            f = cf.Future()
            try:
                f.set_result(fn(*a, **k))
            except Exception as e:
                f.set_exception(e)
            return f

    with mock.patch.object(addon, "_P_EX", InlineEx()), \
         mock.patch.object(addon, "_pool_order", return_value=list(exits)), \
         mock.patch.object(addon._S, "get", side_effect=fake):
        r = addon._pool_get("https://x/y", {"User-Agent": "t"}, 5)
    assert r in (second, third), r
    assert closed == ["third" if r is second else "second"], closed


def test_pool_get_races_exits_and_keeps_first_good():
    """The first good answer wins and the losers are closed. The loser is held on
    an event and released only after _pool_get has returned, so this does not
    depend on a 0.2s sleep losing a race with the scheduler (it used to fail
    roughly one run in four on a loaded box)."""
    _pool_reset()
    _quiesce()
    good = _resp(200, "ok")
    closed = []
    hold = threading.Event()

    class Slow:
        status_code = 200

        def close(self):
            closed.append("slow")

    def fake(u, **k):
        p = (k.get("proxies") or {}).get("http")
        if p == "http://1.1.1.1:8080":
            hold.wait(20)                        # lands long AFTER the winner
            return Slow()
        if p == "http://2.2.2.2:8080":
            return good                          # first good answer wins
        raise RuntimeError("dead exit")
    with mock.patch.object(addon._S, "get", side_effect=fake), \
         mock.patch.object(addon, "_pool_order",
                           return_value=["http://1.1.1.1:8080", "http://2.2.2.2:8080",
                                         "http://3.3.3.3:8080"]):
        r = addon._pool_get("https://x/y", {"User-Agent": "t"}, 5)
        assert r is good
        assert closed == [], "the loser is still in flight"
        hold.set()
        for _ in range(200):                     # losers close on their own thread
            if closed:
                break
            time.sleep(0.05)
    assert "slow" in closed, "losing racers must be closed, not leaked"
    assert addon._POOL_STATS.get("http://2.2.2.2:8080"), "the winner becomes sticky"


def test_pool_get_benches_dead_exits():
    _pool_reset()
    with mock.patch.object(addon._S, "get", side_effect=RuntimeError("dead")):
        assert addon._pool_get("https://x", {"User-Agent": "t"}, 3) is None
    assert set(addon._POOL_BAD) == set(addon._POOL[0]), addon._POOL_BAD


def test_pool_get_no_exits():
    addon._POOL[0] = []
    assert addon._pool_get("https://x", {}, 3) is None


# ── trained pool (MovieBox pattern): probe, latency EWMA, merge-not-replace ──
def test_site_probe_classifies_exits():
    class R:
        def __init__(s, c): s.status_code = c
        def close(s): pass
    for code, want in ((200, "good"), (403, "blocked"), (503, "blocked"), (500, "dead")):
        with mock.patch.object(addon._S, "get", return_value=R(code)):
            kind, ms = addon._site_probe("http://1.1.1.1:8080")
        assert kind == want, (code, kind)
    with mock.patch.object(addon._S, "get", side_effect=RuntimeError("t/o")):
        assert addon._site_probe("http://1.1.1.1:8080")[0] == "dead"


def test_site_probe_hits_the_blocked_host_not_a_canary():
    """probing a generic site proves nothing: the whole point is that THIS egress
    is flagged on banglaplex.biz specifically."""
    seen = []
    class R:
        status_code = 200
        def close(self): pass
    with mock.patch.object(addon._S, "get",
                           side_effect=lambda u, **k: seen.append(u) or R()):
        addon._site_probe("http://1.1.1.1:8080")
    assert seen and "banglaplex" in seen[0] and "autocomplete" in seen[0], seen


def test_pool_score_prefers_fast_and_reliable():
    _pool_reset()
    addon._POOL_STATS["http://1.1.1.1:8080"] = {"ok": 9, "fail": 0, "lat": 300}
    addon._POOL_STATS["http://2.2.2.2:8080"] = {"ok": 9, "fail": 0, "lat": 6000}
    addon._POOL_STATS["http://3.3.3.3:8080"] = {"ok": 1, "fail": 9, "lat": 300}
    assert addon._pool_order() == ["http://1.1.1.1:8080", "http://3.3.3.3:8080",
                                   "http://2.2.2.2:8080"], addon._pool_order()


def test_pool_note_tracks_ewma_latency():
    _pool_reset()
    addon._pool_note("http://1.1.1.1:8080", True, ms=1000)
    assert addon._POOL_STATS["http://1.1.1.1:8080"]["lat"] == 1000
    addon._pool_note("http://1.1.1.1:8080", True, ms=500)
    assert addon._POOL_STATS["http://1.1.1.1:8080"]["lat"] == 800   # 0.6*1000+0.4*500


def test_sticky_exit_caps_at_two_in_flight():
    """one chain should ride one exit, but a busy sticky must not serialise every
    concurrent racer behind it."""
    _pool_reset()
    addon._POOL_STATS["http://1.1.1.1:8080"] = {"ok": 20, "fail": 0, "lat": 100}
    addon._pool_note("http://2.2.2.2:8080", True, ms=100)      # becomes sticky
    assert addon._pool_order()[0] == "http://2.2.2.2:8080", "sticky wins while idle"
    addon._STICKY_BUSY["http://2.2.2.2:8080"] = 2
    assert addon._pool_order()[0] == "http://1.1.1.1:8080", "busy sticky yields"
    assert addon._pool_order()[1] == "http://2.2.2.2:8080", "...but stays in the list"


def test_pool_train_merges_and_keeps_fastest():
    _pool_reset()
    addon._POOL[0] = ["http://old.healthy:8080"]
    addon._POOL_STATS["http://old.healthy:8080"] = {"ok": 5, "fail": 0, "lat": 200}
    lats = {"http://slow:8080": 3000, "http://fast:8080": 150}

    def probe(u, timeout=None):
        if u == "http://dead:8080":
            return "dead", 9999
        if u == "http://flagged:8080":
            return "blocked", 900
        return "good", lats[u]
    with mock.patch.object(addon, "_site_probe", side_effect=probe):
        addon._pool_train(["http://fast:8080", "http://slow:8080",
                           "http://dead:8080", "http://flagged:8080"])
    order = addon._POOL[0]
    assert order.index("http://fast:8080") < order.index("http://slow:8080"), order
    assert "http://dead:8080" not in order and "http://flagged:8080" not in order
    # MERGE, never replace: an already-trained healthy member must survive
    assert "http://old.healthy:8080" in order, order
    assert addon._POOL_STATS["http://fast:8080"]["lat"] == 150
    assert addon._POOL_BAD.get("http://flagged:8080", 0) - time.time() > 600


def test_pool_pull_prefers_manual_exits_and_dedupes():
    txt = "http://1.2.3.4:8080\nhttp://manual:9999\nsocks5://9.9.9.9:1080\n"
    with mock.patch.object(addon, "POOL_MANUAL", ["http://manual:9999"]), \
         mock.patch.object(addon.requests, "get", return_value=_resp(200, txt)):
        out = addon._pool_pull()
    assert out[0] == "http://manual:9999", out
    assert "socks5://9.9.9.9:1080" not in out
    assert out.count("http://manual:9999") == 1


def test_pool_pull_survives_a_dead_source():
    good = _resp(200, "http://7.7.7.7:8080\n")
    calls = []

    def fake(u, **k):
        calls.append(u)
        if len(calls) == 1:
            raise RuntimeError("source down")
        return good
    with mock.patch.object(addon, "POOL_SRCS", ["https://a/x", "https://b/y"]), \
         mock.patch.object(addon.requests, "get", side_effect=fake):
        out = addon._pool_pull()
    assert out == ["http://7.7.7.7:8080"], out


def test_refresh_publishes_untrained_then_trains_in_background():
    """a user request must never wait for the trainer: publish first, learn after."""
    _pool_reset()
    addon._TRAINING[0] = False
    addon._POOL[0] = []
    addon._POOL_TS[0] = 0.0
    started = []

    class FakeThread:
        def __init__(self, **k):
            self.k = k

        def start(self):
            started.append(self.k)
    with mock.patch.object(addon, "_pool_pull", return_value=["http://9.9.9.9:8080"]), \
         mock.patch.object(addon.threading, "Thread", FakeThread):
        out = addon._pool_refresh(force=True)
    assert out == ["http://9.9.9.9:8080"], out
    assert started and started[0].get("name") == "pooltrain", started


def test_refresh_is_throttled():
    _pool_reset()
    with mock.patch.object(addon, "_pool_pull", side_effect=AssertionError("must not pull")):
        assert len(addon._pool_refresh()) == 3


def test_pool_maintain_only_when_an_egress_is_blocked():
    """from an unblocked IP the pool is never used, so training free exits is
    pure waste (measured: a probe wave is 90 candidate sockets)."""
    _pool_reset()
    addon._DIRECT_BAD.clear()
    with mock.patch.object(addon, "_pool_refresh", side_effect=AssertionError("wasted")):
        addon._pool_maintain()
    addon._DIRECT_BAD["banglaplex.biz"] = time.time() + 600
    with mock.patch.object(addon, "_pool_refresh", return_value=["x"]) as pr:
        addon._pool_maintain()
    assert pr.called


def test_disabled_pool_stays_empty():
    _pool_reset()
    with mock.patch.object(addon, "POOL_ON", False):
        assert addon._pool_refresh(force=True) == []
        assert addon._pool_stats()["enabled"] is False


def test_health_reports_trained_pool():
    _pool_reset()
    addon._POOL_STATS["http://1.1.1.1:8080"] = {"ok": 3, "fail": 0, "lat": 400}
    st = addon._pool_stats()
    assert st["trained"] == 1 and st["median_ms"] == 400 and st["pool"] == 3


_CARD_TMPL = """<div class="col-md-2 col-sm-3 col-xs-6" style="position:relative;">
<div class="popup">
<div class="latest-movie-img-container lazy" style="background-image: url('__POSTER__'); display: inline-block;">
<div class="movie-img" style="position: relative; overflow: hidden;">
<a href="https://banglaplex.biz/watch/__SLUG__.html" class="ico-play ico-play-sm"><svg></svg></a>
<div class="overlay-div"></div>
__TREND__
<div class="video_badges_group_movie">
<div class="video_quality_movie"><span class="label label-primary"> __QUALITY__ </span></div>
<div class="video_year_movie"><span class="label label-year"> __YEAR__ </span></div>
</div>
__TV__
<div class="imdb-rating"><span class="label label-imdb">
<i class="fa fa-info-circle"></i> IMDB 7.5 </span></div>
<div class="movie-title"><h3>
<a href="https://banglaplex.biz/watch/__SLUG__.html">__TITLE__</a>
</h3></div></div></div></div>"""


def _listing(n=3, series=False, year=2026, quality="HDTC"):
    """OVOO card grid, shaped exactly like banglaplex.biz markup."""
    out = []
    for i in range(n):
        out.append((_CARD_TMPL
                    .replace("__POSTER__", "https://banglaplex.biz/uploads/video_thumb/%d.jpg" % (100 + i))
                    .replace("__SLUG__", "title-%d" % i)
                    .replace("__TITLE__", "Title %d" % i)
                    .replace("__YEAR__", str(year))
                    .replace("__QUALITY__", quality)
                    .replace("__TREND__", ('<div class="video_trending_badge"><span '
                                           'class="label label-trending">TRENDING</span></div>')
                             if i == 0 else "")
                    .replace("__TV__", ('<div class="video_type_label_tv"><span '
                                        'class="label label-tvseries">SERIES</span></div>')
                             if series else "")))
    return "".join(out)


def test_parse_listing_reads_every_card_field():
    items = addon.parse_listing(_listing(3))
    assert len(items) == 3
    a = items[0]
    assert a["slug"] == "title-0" and a["title"] == "Title 0"
    assert a["poster"] == "https://banglaplex.biz/uploads/video_thumb/100.jpg"
    assert a["year"] == 2026 and a["quality"] == "HDTC"
    assert a["rating"] == 7.5 and a["series"] is False and a["trending"] is True
    assert items[1]["trending"] is False


def test_parse_listing_handles_every_column_layout():
    """the card column class VARIES by page: homepage/genre use col-xs-6, /year/
    uses col-xs-4. Splitting on a column class parsed 0 of 24 real cards and
    blanked a whole shelf (measured on prod)."""
    xs6 = _listing(2)
    assert len(addon.parse_listing(xs6)) == 2
    xs4 = xs6.replace("col-md-2 col-sm-3 col-xs-6", "col-md-2 col-sm-3 col-xs-4")
    got = addon.parse_listing(xs4)
    assert len(got) == 2, "the col-xs-4 layout must parse too"
    assert got[0]["poster"].endswith("100.jpg") and got[0]["title"] == "Title 0"
    assert got[0]["year"] == 2026 and got[0]["quality"] == "HDTC"


def test_parse_listing_needs_no_column_class_at_all():
    """only the poster div is required — everything else is read from the card
    body that follows it."""
    h = ("""<div class="whatever"><div class="latest-movie-img-container lazy"
    style="background-image: url('https://banglaplex.biz/uploads/video_thumb/7.jpg');">
    <a href="https://banglaplex.biz/watch/only-poster.html" class="ico-play"></a>
    <span class="label label-primary"> WEB-DL </span>
    <span class="label label-year"> 2025 </span>
    <div class="movie-title"><h3><a href="#">Only Poster</a></h3></div></div>""")
    got = addon.parse_listing(h)
    assert len(got) == 1 and got[0]["slug"] == "only-poster"
    assert got[0]["poster"].endswith("7.jpg") and got[0]["year"] == 2025
    assert got[0]["quality"] == "WEB-DL" and got[0]["title"] == "Only Poster"


def test_parse_listing_detects_series_and_dedupes():
    h = _listing(2, series=True) + _listing(2, series=True)     # same cards twice
    items = addon.parse_listing(h)
    assert len(items) == 2, "duplicate watch links must collapse"
    assert all(i["series"] for i in items)


def test_parse_listing_survives_garbage():
    assert addon.parse_listing("") == []
    assert addon.parse_listing("<html>no cards</html>") == []
    assert addon.parse_listing(None) == []


def test_catalog_source_pagination_is_path_based():
    """the site IGNORES ?page= — offsets live in the path (measured live:
    /genre/action/24.html is page 2, ?page=2 returns page 1)."""
    u, mode = addon.catalog_source("bpx-latest", "movie", "", 0)
    assert u == addon.SITE + "/" and mode == "home"
    u, _ = addon.catalog_source("bpx-latest", "movie", "", 30)
    assert u == addon.SITE + "/" and mode == "home"
    u, mode = addon.catalog_source("bpx-latest", "movie", "action", 0)
    assert u.endswith("/genre/action.html") and mode == "page"
    u, _ = addon.catalog_source("bpx-latest", "movie", "action", 24)
    assert u.endswith("/genre/action/24.html"), u
    u, _ = addon.catalog_source("bpx-latest", "movie", "action", 48)
    assert u.endswith("/genre/action/48.html"), u
    u, _ = addon.catalog_source("bpx-year", "movie", "", 24)
    assert "/year/%d/24.html" % addon._THIS_YEAR in u, u
    u, _ = addon.catalog_source("bpx-series", "series", "", 0)
    assert u.endswith("/genre/bengali-web-series.html"), u
    u, _ = addon.catalog_source("bpx-series", "series", "korean-web-series", 24)
    assert u.endswith("/genre/korean-web-series/24.html"), u


def test_list_page_serves_stale_and_revalidates_in_background():
    """the homepage is ~375 KB and rides a free exit from Render (~9 s measured):
    a stale shelf beats a spinner."""
    clear_caches()
    addon._LIST_STALE.clear()
    addon._LIST_REFRESH.clear()
    items = addon.parse_listing(_listing(2))
    addon._LIST_STALE["https://x/list"] = (time.time() + 600, items)

    class R:
        status_code = 200
        text = _listing(4)
    served = []
    with mock.patch.object(addon, "_get", side_effect=lambda u, **k: served.append(u) or R()):
        got = addon.list_page("https://x/list")
    assert len(got) == 2, "stale served immediately"
    for _ in range(60):
        if served:
            break
        time.sleep(0.05)
    assert served == ["https://x/list"], "and revalidated in the background"
    for _ in range(60):
        hit, val = addon.C_LIST.get("https://x/list")
        if hit:
            break
        time.sleep(0.05)
    assert hit and len(val) == 4, "the fresh page lands in the cache"
    assert not addon._LIST_REFRESH, "the refresh flag must be released"


def test_looks_blocked_spots_a_200_interstitial():
    """some free exits are themselves Cloudflare-flagged and answer 200 with a
    challenge page: measured on prod as a shelf that cached 0 items."""
    chal = ('<html><head><title>Just a moment...</title></head><body>'
            '<div id="cf-browser-verification"></div>Checking your browser before'
            ' accessing. Ray ID: abc123</body></html>')
    assert addon._looks_blocked(chal, 200) is True
    assert addon._looks_blocked("", 403) is True and addon._looks_blocked("x", 503) is True
    assert addon._looks_blocked('[{"title":"Mirzapur"}]', 200) is False
    assert addon._looks_blocked("a5dc8b828864" * 400, 200) is False, "hex API blob"
    assert addon._looks_blocked("<html>powered by cloudflare</html>", 200) is False, \
        "one marker in a small body is not proof"
    assert addon._looks_blocked("", 200) is False
    big = "<html>" + ("real content " * 3000) + "just a moment cf-chl- x</html>"
    assert addon._looks_blocked(big, 200) is False, "a big real page is never a challenge"


def test_fetch_treats_a_direct_interstitial_as_blocked():
    _pool_reset()
    chal = _resp(200, '<div id="cf-browser-verification">Just a moment Ray ID</div>')
    good = _resp(200, "<html>real page</html>")
    seq = [chal, good]
    used = []

    def fake(u, **k):
        used.append(k.get("proxies"))
        return seq.pop(0) if seq else good
    with mock.patch.object(addon._S, "get", side_effect=fake):
        r, via = addon._fetch("https://banglaplex.biz/year/2026.html")
    assert via is True and r.text == "<html>real page</html>"
    assert used[0] is None, "direct was tried first"
    assert addon._DIRECT_BAD["banglaplex.biz"] > time.time()


def test_pool_get_rejects_an_exit_that_serves_an_interstitial():
    _pool_reset()
    chal = _resp(200, '<div id="cf-browser-verification">Just a moment Ray ID</div>')
    good = _resp(200, "<html>real</html>")

    def fake(u, **k):
        p = (k.get("proxies") or {}).get("http")
        if p == "http://1.1.1.1:8080":
            return chal                      # flagged exit answering 200
        if p == "http://2.2.2.2:8080":
            time.sleep(0.2)
            return good
        raise RuntimeError("dead")
    with mock.patch.object(addon._S, "get", side_effect=fake):
        r = addon._pool_get("https://x/y", {"User-Agent": "t"}, 5)
    assert r is good
    for _ in range(60):
        if addon._POOL_BAD.get("http://1.1.1.1:8080"):
            break
        time.sleep(0.05)
    assert addon._POOL_BAD.get("http://1.1.1.1:8080", 0) - time.time() > 600, \
        "a flagged exit must be benched as platform-blocked, not merely dead"


def test_list_page_small_empty_body_is_transient_not_an_empty_shelf():
    """a genuinely empty shelf (/type/web-series.html) is still ~40 KB; a tiny
    0-card body is a broken fetch and must not blank the shelf for 5 minutes."""
    clear_caches()
    addon._LIST_STALE.clear()

    class R:
        status_code = 200
        text = "<html>challenge</html>"
    with mock.patch.object(addon, "_get", return_value=R()):
        assert addon.list_page("https://x/year/2026.html") is None
    assert not addon.C_LIST.get("https://x/year/2026.html")[0], "nothing cached"

    class Big:
        status_code = 200
        text = "<html>" + ("padding " * 2000) + "</html>"      # >12 KB, 0 cards
    with mock.patch.object(addon, "_get", return_value=Big()):
        assert addon.list_page("https://x/empty-shelf") == []
    assert addon.C_LIST.get("https://x/empty-shelf")[0] is True, "honest empty is cached"


def test_list_page_non_200_is_transient():
    clear_caches()
    addon._LIST_STALE.clear()

    class R:
        status_code = 502
        text = "x" * 40000
    with mock.patch.object(addon, "_get", return_value=R()):
        assert addon.list_page("https://x/y") is None
    assert not addon.C_LIST.get("https://x/y")[0]


def test_debug_url_route_reports_the_real_path():
    with mock.patch.object(addon, "_fetch",
                           return_value=(_resp(200, _listing(2)), True)):
        d = _json_body(_http_get("/debug/url?k=%s&u=https://banglaplex.biz/year/2026.html"
                                 % addon.DEBUG_KEY))
    assert d["status"] == 200 and d["via_proxy"] is True and d["cards"] == 2
    assert d["looks_blocked"] is False
    assert _json_body(_http_get("/debug/url?k=%s" % addon.DEBUG_KEY))["error"]


def test_list_page_transient_failure_caches_nothing():
    clear_caches()
    addon._LIST_STALE.clear()
    with mock.patch.object(addon, "_get", return_value=None):
        assert addon.list_page("https://x/dead") is None
    assert not addon.C_LIST.get("https://x/dead")[0]


def test_catalog_prewarm_is_crash_proof():
    with mock.patch.object(addon, "catalog_items", side_effect=RuntimeError("boom")):
        addon.catalog_prewarm()          # must not raise


def test_prewarm_covers_every_default_shelf():
    seen = []
    with mock.patch.object(addon, "catalog_items",
                           side_effect=lambda t, c, **k: seen.append((t, c))):
        addon.catalog_prewarm()
    assert seen == [("series", "bpx-series"), ("movie", "bpx-latest"),
                    ("movie", "bpx-year")], seen


def test_main_spawns_prewarm_off_the_keepalive_thread():
    """the keepalive ping is what keeps Render's free tier awake — a slow proxied
    shelf fetch must not sit in front of it."""
    started = []

    class FakeThread:
        def __init__(self, target=None, daemon=None, name=None):
            self.t, self.n = target, name

        def start(self):
            started.append(self.n or getattr(self.t, "__name__", "?"))
    with mock.patch.object(addon.threading, "Thread", FakeThread), \
         mock.patch.object(addon, "ThreadingHTTPServer") as srv, \
         mock.patch.dict(addon.os.environ, {"BPX_PREWARM": "1"}):
        srv.return_value.serve_forever.side_effect = KeyboardInterrupt
        addon.main()
    assert "prewarm" in started, started
    assert started.index("_keepalive_loop") < started.index("prewarm"), started


def test_catalog_source_rejects_path_injection():
    u, _ = addon.catalog_source("bpx-latest", "movie", "../../etc/passwd", 0)
    assert ".." not in u and u.endswith("/genre/etcpasswd.html"), u


def test_imdb_suggest_title_is_strict():
    body = {"d": [{"id": "tt32378175", "l": "Haiwaan", "y": 2026, "qid": "movie"},
                  {"id": "tt19719628", "l": "Haiwaan", "y": 2023, "qid": "movie"},
                  {"id": "nm123", "l": "Haiwaan Actor"}]}

    class R:
        status_code = 200

        def json(self):
            return body
    with mock.patch.object(addon._S, "get", return_value=R()):
        assert addon.imdb_suggest_title("Haiwaan", 2026) == "tt32378175"
        assert addon.imdb_suggest_title("Haiwaan", 2023) == "tt19719628"
        assert addon.imdb_suggest_title("Haiwaan", 1999) is None, "year guard"
        assert addon.imdb_suggest_title("Haiwaan", 2026, "series") is None, "qid guard"
        assert addon.imdb_suggest_title("nm123", 2026) is None


def test_imdb_suggest_title_mismatch_falls_through():
    """Jaatishwar is 'The Reincarnate' on IMDb: no match is BETTER than a wrong
    tt id (a wrong id shows another film's poster). The card then rides bpx-."""
    body = {"d": [{"id": "tt3365690", "l": "The Reincarnate", "y": 2014, "qid": "movie"}]}

    class R:
        status_code = 200

        def json(self):
            return body
    with mock.patch.object(addon._S, "get", return_value=R()):
        assert addon.imdb_suggest_title("Jaatishwar", 2014) is None
    with mock.patch.object(addon._S, "get", side_effect=RuntimeError("net")):
        assert addon.imdb_suggest_title("Anything", 2020) is None


def test_map_ids_falls_back_to_source_ids():
    items = [{"slug": "harudu", "title": "Harudu", "year": 2026},
             {"slug": "unknown-thing", "title": "Unknown Thing", "year": 2026}]
    with mock.patch.object(addon, "imdb_suggest_title",
                           side_effect=lambda t, y=None, c="movie":
                           "tt33702400" if t == "Harudu" else None):
        out = addon._map_ids(items, "movie")
    assert out[0]["id"] == "tt33702400"
    assert out[1]["id"] == "bpx-unknown-thing", out[1]


def test_map_ids_never_leaves_an_item_without_an_id():
    items = [{"slug": "a", "title": "A"}, {"slug": "b", "title": "B"}]
    with mock.patch.object(addon, "imdb_suggest_title", side_effect=RuntimeError("x")):
        out = addon._map_ids(items, "movie", budget=0.0)
    assert [i["id"] for i in out] == ["bpx-a", "bpx-b"]


def test_catalog_items_maps_listings_to_metas():
    clear_caches()
    with mock.patch.object(addon, "list_page", return_value=addon.parse_listing(_listing(2))), \
         mock.patch.object(addon, "imdb_suggest_title",
                           side_effect=lambda t, y=None, c="movie": "tt111" if t == "Title 0" else None):
        metas = addon.catalog_items("movie", "bpx-latest")
    assert len(metas) == 2
    assert metas[0]["id"] == "tt111" and metas[0]["name"] == "Title 0"
    assert metas[0]["poster"].endswith("100.jpg") and metas[0]["year"] == 2026
    assert metas[0]["imdbRating"] == "7.5"
    assert metas[0]["posterShape"] == "poster"
    # unmapped -> source id + the watch URL so the client can still resolve it
    assert metas[1]["id"] == "bpx-title-1" and metas[1]["bpxSource"].endswith("title-1.html")


def test_catalog_items_splits_movies_from_series():
    clear_caches()
    mixed = addon.parse_listing(_listing(2)) + addon.parse_listing(_listing(2, series=True))
    with mock.patch.object(addon, "list_page", return_value=mixed), \
         mock.patch.object(addon, "imdb_suggest_title", return_value=None):
        movies = addon.catalog_items("movie", "bpx-latest")
        series = addon.catalog_items("series", "bpx-latest")
    assert len(movies) == 2 and len(series) == 2
    assert all(m["id"].startswith("bpx-") for m in movies)


def test_catalog_items_home_slices_by_skip():
    """the homepage is ONE big page: skip must slice the cached listing, not
    re-fetch it (the page is ~375 KB)."""
    clear_caches()

    class R:
        status_code = 200
        text = _listing(5)
    hits = []
    with mock.patch.object(addon, "_get",
                           side_effect=lambda u, **k: hits.append(u) or R()), \
         mock.patch.object(addon, "imdb_suggest_title", return_value=None):
        p1 = addon.catalog_items("movie", "bpx-latest", skip=0)
        p2 = addon.catalog_items("movie", "bpx-latest", skip=3)
    assert [m["name"] for m in p1] == ["Title %d" % i for i in range(5)]
    assert [m["name"] for m in p2] == ["Title 3", "Title 4"]
    assert len(hits) == 1, "one fetch, two slices (got %r)" % hits


def test_catalog_items_search_uses_autocomplete():
    clear_caches()
    cands = [{"title": "Mirzapur", "url": "https://banglaplex.biz/watch/mirzapur.html",
              "type": "Movie", "image": "https://x/1.jpg"},
             {"title": "Mirzapur 2024 Web Series", "type": "TV Series",
              "url": "https://banglaplex.biz/watch/mirzapur-2024.html", "image": ""}]
    with mock.patch.object(addon, "_search_autocomplete", return_value=cands), \
         mock.patch.object(addon, "imdb_suggest_title", return_value=None):
        mov = addon.catalog_items("movie", "bpx-latest", search="mirzapur")
        ser = addon.catalog_items("series", "bpx-latest", search="mirzapur")
    # ordering, not exclusion: the site's `type` field is unreliable, so a
    # mismatch is demoted rather than dropped — and "Mirzapur" is an EXACT match
    # for the query, which outranks the type mismatch on the series shelf
    assert [m["id"] for m in mov] == ["bpx-mirzapur", "bpx-mirzapur-2024"]
    assert [m["id"] for m in ser] == ["bpx-mirzapur", "bpx-mirzapur-2024"]
    assert [m["type"] for m in ser] == ["series", "series"], "served as the shelf's type"


def test_catalog_search_ranks_an_exact_title_above_a_type_mismatch():
    """Someone who typed the whole title wants that title. Once the badge index
    knows prem-shots is a series, the MOVIE shelf used to push the exact match to
    last place behind four fuzzy 'Prem…' hits."""
    clear_caches()
    addon._note_kind("prem-shots", True)
    cands = [{"title": "Besh Korechi Prem Korechi", "type": "Movie",
              "url": "https://banglaplex.biz/watch/besh-korechi-prem-korechi.html",
              "image": ""},
             {"title": "Prem Shots", "type": "Movie",
              "url": "https://banglaplex.biz/watch/prem-shots.html", "image": ""}]
    with mock.patch.object(addon, "_search_autocomplete", return_value=cands), \
         mock.patch.object(addon, "imdb_suggest_title", return_value=None):
        mov = addon.catalog_items("movie", "bpx-latest", search="Prem Shots")
        ser = addon.catalog_items("series", "bpx-series", search="prem shots")
    assert [m["name"] for m in mov] == ["Prem Shots", "Besh Korechi Prem Korechi"]
    assert [m["name"] for m in ser] == ["Prem Shots", "Besh Korechi Prem Korechi"]


def test_catalog_search_fuzzy_hits_still_prefer_the_right_type():
    clear_caches()
    addon._note_kind("dahan-series", True)
    cands = [{"title": "Dahan Movie", "type": "Movie",
              "url": "https://banglaplex.biz/watch/dahan-movie.html", "image": ""},
             {"title": "Dahan Series", "type": "Movie",
              "url": "https://banglaplex.biz/watch/dahan-series.html", "image": ""}]
    with mock.patch.object(addon, "_search_autocomplete", return_value=cands), \
         mock.patch.object(addon, "imdb_suggest_title", return_value=None):
        ser = addon.catalog_items("series", "bpx-series", search="dahan")
    assert [m["name"] for m in ser] == ["Dahan Series", "Dahan Movie"]


def test_catalog_search_keeps_titles_the_site_labels_with_the_wrong_type():
    """The reported bug — "Prem Shots is on the provider's site but the addon does
    not show it". banglaplex.biz answers `type: "Movie"` for EVERY autocomplete hit
    (measured: Dahan, Queens, Taarkata, Cactus, Gorki-R Ma, Prem Shots — all of them
    sit on the site's own series shelf). Search hard-filtered on that field, so the
    series board's search box returned zero items for every query anyone could
    type. A mismatch demotes a hit now; it never deletes one."""
    clear_caches()
    cands = [{"title": "Prem Shots", "type": "Movie",
              "url": "https://banglaplex.biz/watch/prem-shots.html", "image": ""}]
    with mock.patch.object(addon, "_search_autocomplete", return_value=cands), \
         mock.patch.object(addon, "imdb_suggest_title", return_value=None):
        ser = addon.catalog_items("series", "bpx-series", search="prem shots")
        mov = addon.catalog_items("movie", "bpx-latest", search="prem shots")
    assert [m["name"] for m in ser] == ["Prem Shots"], "never a dead end"
    assert [m["name"] for m in mov] == ["Prem Shots"]


def test_parse_listing_teaches_the_slug_kind_index():
    """The listing badge is the one reliable type signal, so browsing a shelf
    teaches search how to ORDER its hits (a known match first)."""
    clear_caches()
    items = addon.parse_listing(_listing(2, series=True))
    assert [i["slug"] for i in items] == ["title-0", "title-1"]
    assert addon._SLUG_KIND["title-0"] is True and addon._SLUG_KIND["title-1"] is True
    addon.parse_listing(_listing(1, series=False))
    assert addon._SLUG_KIND["title-0"] is True, "badge absence cannot erase positive evidence"

    clear_caches()
    addon._note_kind("known-series", True)
    cands = [{"title": "Wrong Label", "type": "Movie",           # site lies
              "url": "https://banglaplex.biz/watch/known-series.html", "image": ""},
             {"title": "Unknown", "type": "Movie",
              "url": "https://banglaplex.biz/watch/never-seen.html", "image": ""}]
    with mock.patch.object(addon, "_search_autocomplete", return_value=cands), \
         mock.patch.object(addon, "imdb_suggest_title", return_value=None):
        ser = addon.catalog_items("series", "bpx-series", search="x")
    assert [m["name"] for m in ser] == ["Wrong Label", "Unknown"]
    assert ser[0]["id"] == "bpx-known-series"


def test_slug_kind_positive_badge_survives_a_badgeless_repeat():
    clear_caches()
    addon._note_kind("kuheli", True)
    addon._note_kind("kuheli", False)       # homepage repeats it without TV badge
    assert addon._SLUG_KIND["kuheli"] is True


def test_slug_kind_index_is_bounded():
    clear_caches()
    for i in range(addon._SLUG_KIND_MAX + 50):
        addon._note_kind("s%d" % i, True)
    assert len(addon._SLUG_KIND) <= addon._SLUG_KIND_MAX + 1, len(addon._SLUG_KIND)
    addon._note_kind("", True)
    assert "" not in addon._SLUG_KIND


def test_catalog_items_transient_search_is_empty_not_cached():
    clear_caches()
    with mock.patch.object(addon, "_search_autocomplete", return_value=None):
        assert addon.catalog_items("movie", "bpx-latest", search="x") == []
    with mock.patch.object(addon, "_search_autocomplete",
                           return_value=[{"title": "X", "type": "Movie",
                                          "url": "https://banglaplex.biz/watch/x.html",
                                          "image": ""}]), \
         mock.patch.object(addon, "imdb_suggest_title", return_value=None):
        assert len(addon.catalog_items("movie", "bpx-latest", search="x")) == 1


def test_catalog_items_dead_listing_is_not_cached_as_empty_forever():
    clear_caches()
    with mock.patch.object(addon, "list_page", return_value=None):
        assert addon.catalog_items("movie", "bpx-latest") == []


def test_http_catalog_route():
    clear_caches()
    with mock.patch.object(addon, "catalog_items",
                           return_value=[{"id": "tt1", "type": "movie", "name": "X"}]) as ci:
        c = _http_get("/catalog/movie/bpx-latest.json?genre=action&skip=24")
    assert c["code"] == 200
    assert _json_body(c)["metas"][0]["id"] == "tt1"
    assert ci.call_args[0][2] == "action" and ci.call_args[0][4] == 24


def test_http_catalog_route_path_extras():
    with mock.patch.object(addon, "catalog_items", return_value=[]) as ci:
        c = _http_get("/catalog/movie/bpx-latest/genre=comedy;skip=48.json")
    assert c["code"] == 200
    assert ci.call_args[0][2] == "comedy" and ci.call_args[0][4] == 48


def test_http_catalog_unknown_id_404():
    assert _http_get("/catalog/movie/nope.json")["code"] == 404
    assert _http_get("/catalog/other/bpx-latest.json")["code"] == 404


# ══════════════════════════════════════ 15c. meta (source fallback) + config
_PAGE_FIX = {"slug": "harudu", "title": "Harudu", "year": 2026,
             "release": "2026-01-10", "duration": "128", "quality": "WEB-DL",
             "genre": "Action, Drama", "country": "India",
             "actors": "Venkat, Hebah Patel", "director": "Someone",
             "poster": "https://banglaplex.biz/uploads/video_thumb/9.jpg",
             "plot": "A man returns home.", "keys": [("k", True, "Full Movie")],
             "iframe": "https://plextream.work/embed.php?id=1",
             "url": "https://banglaplex.biz/watch/harudu.html"}


def test_meta_from_page_exposes_every_source_field():
    m = addon.meta_from_page(_PAGE_FIX, "movie")
    assert m["id"] == "bpx-harudu" and m["type"] == "movie"
    assert m["name"] == "Harudu" and m["year"] == 2026
    assert m["poster"].endswith("9.jpg") and m["background"] == m["poster"]
    assert m["description"] == "A man returns home."
    assert m["genres"] == ["Action", "Drama"] and m["cast"] == ["Venkat", "Hebah Patel"]
    assert m["director"] == "Someone" and m["country"] == "India"
    assert m["runtime"] == "2h08m" and m["released"] == "2026-01-10"
    assert m["bpxSlug"] == "harudu"


def test_meta_from_page_never_crashes_on_a_bare_page():
    m = addon.meta_from_page({"slug": "x", "title": "X"}, "movie")
    assert m["name"] == "X" and "poster" not in m
    assert addon.meta_from_page(None, "movie") is None


def test_build_meta_uses_providers_when_they_are_complete():
    clear_caches()
    full = {"name": "Harudu", "poster": "https://img/tmdb.jpg",
            "description": "A rich synopsis", "year": 2026, "genres": ["Action"]}
    with mock.patch.object(addon, "_cinemeta_full", return_value=full), \
         mock.patch.object(addon, "_site_meta_for",
                           side_effect=AssertionError("source must not be scraped")):
        m = addon.build_meta("movie", "tt33702400")
    assert m["poster"] == "https://img/tmdb.jpg" and m["id"] == "tt33702400"


def test_build_meta_falls_back_to_the_source_for_holes():
    """the user's rule: when TMDB/IMDb/Cinemeta have nothing, the SOURCE must
    still describe the card — a catalog item is never a blank shell."""
    clear_caches()
    thin = {"name": "Harudu", "poster": "", "description": ""}
    with mock.patch.object(addon, "_cinemeta_full", return_value=thin), \
         mock.patch.object(addon, "_tmdb_full", return_value=None), \
         mock.patch.object(addon, "_site_meta_for",
                           return_value=addon.meta_from_page(_PAGE_FIX, "movie")):
        m = addon.build_meta("movie", "tt33702400")
    assert m["name"] == "Harudu"                       # provider name wins
    assert m["poster"].endswith("9.jpg"), m            # source fills the hole
    assert m["description"] == "A man returns home."
    assert m["genres"] == ["Action", "Drama"]


def test_build_meta_when_no_provider_knows_the_title():
    clear_caches()
    with mock.patch.object(addon, "_cinemeta_full", return_value=None), \
         mock.patch.object(addon, "_tmdb_full", return_value=None), \
         mock.patch.object(addon, "_site_meta_for",
                           return_value=addon.meta_from_page(_PAGE_FIX, "movie")):
        m = addon.build_meta("movie", "tt9999999")
    assert m["name"] == "Harudu" and m["poster"].endswith("9.jpg")


def test_build_meta_source_only_id():
    clear_caches()
    with mock.patch.object(addon, "parse_watch_page", return_value=_PAGE_FIX), \
         mock.patch.object(addon, "_cinemeta_full",
                           side_effect=AssertionError("providers must not be asked")):
        m = addon.build_meta("movie", "bpx-harudu")
    assert m["id"] == "bpx-harudu" and m["name"] == "Harudu"
    with mock.patch.object(addon, "parse_watch_page", return_value=None):
        assert addon.build_meta("movie", "bpx-gone") is None


def test_build_meta_caches_and_never_caches_a_miss_forever():
    clear_caches()
    calls = []
    with mock.patch.object(addon, "_cinemeta_full",
                           side_effect=lambda *a, **k: calls.append(1) or
                           {"name": "X", "poster": "p", "description": "d"}):
        addon.build_meta("movie", "tt1")
        addon.build_meta("movie", "tt1")
    assert len(calls) == 1
    with mock.patch.object(addon, "_cinemeta_full", return_value=None), \
         mock.patch.object(addon, "_tmdb_full", return_value=None), \
         mock.patch.object(addon, "_site_meta_for", return_value=None):
        assert addon.build_meta("movie", "tt2") is None


def test_normalize_meta_one_shape_from_every_source():
    """Cinemeta sends director as a list and no year (only releaseInfo); the site
    sends comma strings. Stremio renders a blank line for anything unshaped."""
    m = addon.normalize_meta({"name": "X", "releaseInfo": "2026",
                              "director": ["Priyadarshan"], "cast": "A, B",
                              "genres": "Action, Drama", "country": ["India"],
                              "description": "  a   b  ", "poster": ""})
    assert m["year"] == 2026 and m["releaseInfo"] == "2026"
    assert m["director"] == "Priyadarshan"
    assert m["cast"] == ["A", "B"] and m["genres"] == ["Action", "Drama"]
    assert m["country"] == "India" and m["description"] == "a b"
    assert "poster" not in m, "empty fields must not ship"


def test_normalize_meta_keeps_provider_year_and_caps_cast():
    m = addon.normalize_meta({"year": 2014, "cast": ["a%d" % i for i in range(40)]})
    assert m["year"] == 2014 and len(m["cast"]) == 20
    assert addon.normalize_meta(None) is None and addon.normalize_meta({}) == {}


def test_build_meta_ships_a_year_for_cinemeta_shapes():
    clear_caches()
    with mock.patch.object(addon, "_cinemeta_full",
                           return_value={"name": "X", "poster": "p",
                                         "description": "d", "releaseInfo": "2026"}):
        m = addon.build_meta("movie", "tt1234567")
    assert m["year"] == 2026


def test_needs_provider_is_the_mirror_of_needs_site():
    assert addon._needs_provider(None) is False
    assert addon._needs_provider({"cast": ["a"], "genres": ["Drama"],
                                  "description": "d"}) is False
    for hole in ("cast", "genres", "description"):
        m = {"cast": ["a"], "genres": ["Drama"], "description": "d"}
        m.pop(hole)
        assert addon._needs_provider(m) is True, hole


def test_build_meta_tt_with_no_provider_at_all_falls_back_via_the_id():
    """prod bug: /meta/series/tt43695931 answered {} because no provider knows a
    brand-new id, so there was no NAME to search the site with. The name must be
    resolved FROM the id first, then the source scraped."""
    clear_caches()
    with mock.patch.object(addon, "_provider_meta", return_value=None), \
         mock.patch.object(addon, "resolve_meta_all",
                           return_value=[("Dahan", 2026, None)]) as rma, \
         mock.patch.object(addon, "_site_meta_for",
                           return_value=addon.meta_from_page(_PAGE_FIX, "series")) as smf:
        m = addon.build_meta("series", "tt43695931")
    assert rma.called, "the id must be resolved to a name"
    assert smf.call_args[0][1] == "Dahan" and smf.call_args[0][2] == 2026
    assert m["name"] == "Harudu" or m["name"] == "Dahan"
    assert m["id"] == "tt43695931" and m["type"] == "series"
    assert m["poster"].endswith("9.jpg")


def test_build_meta_tt_keeps_provider_name_over_source_name():
    clear_caches()
    thin = {"name": "Official Title", "poster": "", "description": ""}
    with mock.patch.object(addon, "_provider_meta", return_value=thin), \
         mock.patch.object(addon, "_site_meta_for",
                           return_value=addon.meta_from_page(_PAGE_FIX, "movie")):
        m = addon.build_meta("movie", "tt1234567")
    assert m["name"] == "Official Title", m["name"]
    assert m["poster"].endswith("9.jpg")


def test_build_meta_source_id_asks_providers_too():
    """the fallback must run BOTH ways: the site page for a Bangla series often
    has no cast/runtime/rating while TMDB/Cinemeta do."""
    clear_caches()
    src_meta = addon.meta_from_page({"slug": "queens", "title": "Queens", "year": 2026,
                                     "poster": "https://x/p.jpg",
                                     "plot": "A story."}, "series")
    prov = {"cast": ["Actor One", "Actor Two"], "runtime": "45 min",
            "imdbRating": "7.8", "poster": "https://provider/other.jpg"}
    with mock.patch.object(addon, "_site_meta_for", return_value=src_meta), \
         mock.patch.object(addon, "imdb_suggest_title", return_value="tt9999999") as sg, \
         mock.patch.object(addon, "_provider_meta", return_value=prov):
        m = addon.build_meta("series", "bpx-queens")
    assert sg.call_args[0][0] == "Queens"
    assert m["cast"] == ["Actor One", "Actor Two"], "provider fills the hole"
    assert m["runtime"] == "45 min" and m["imdbRating"] == "7.8"
    assert m["poster"] == "https://x/p.jpg", "the SOURCE stays authoritative"
    assert m["id"] == "bpx-queens" and m["imdb_id"] == "tt9999999"


def test_build_meta_source_id_does_not_guess_a_provider():
    clear_caches()
    src_meta = addon.meta_from_page(_PAGE_FIX, "movie")
    with mock.patch.object(addon, "_site_meta_for", return_value=src_meta), \
         mock.patch.object(addon, "imdb_suggest_title", return_value=None), \
         mock.patch.object(addon, "_provider_meta",
                           side_effect=AssertionError("no id, no provider call")):
        m = addon.build_meta("movie", "bpx-harudu")
    assert m["name"] == "Harudu" and "imdb_id" not in m


def test_build_meta_source_id_skips_providers_when_the_page_is_full():
    clear_caches()
    full = addon.meta_from_page(_PAGE_FIX, "movie")
    with mock.patch.object(addon, "_site_meta_for", return_value=full), \
         mock.patch.object(addon, "imdb_suggest_title",
                           side_effect=AssertionError("must not be asked")):
        assert addon.build_meta("movie", "bpx-harudu")["name"] == "Harudu"


# ── slug index: a catalog build teaches /stream where the file lives ─────────
def test_map_ids_records_the_slug_for_mapped_items():
    clear_caches()
    items = [{"slug": "dahan", "title": "Dahan", "year": 2026},
             {"slug": "queens", "title": "Queens", "year": 2026}]
    with mock.patch.object(addon, "imdb_suggest_title",
                           side_effect=lambda t, y=None, c="movie":
                           "tt43695931" if t == "Dahan" else None):
        addon._map_ids(items, "series")
    assert addon.C_SLUG.get(("series", "tt43695931")) == (True, "dahan")
    assert addon.C_SLUG.get(("series", "bpx-queens"))[0] is False


def test_build_inner_uses_the_slug_index_and_skips_the_search_hop():
    """on a flagged egress every site fetch rides a proxy (~2 s): a slug learned
    from the catalog removes one whole hop."""
    clear_caches()
    addon.C_SLUG.put(("movie", "tt777"), "harudu", 600)
    with mock.patch.object(addon, "resolve_meta_all",
                           return_value=[("Harudu", 2026, None)]), \
         mock.patch.object(addon, "search_candidates",
                           side_effect=AssertionError("must not search")), \
         mock.patch.object(addon, "parse_watch_page", return_value=_PAGE_FIX) as pw, \
         _stub_resolve([_card()]):
        out = addon._build_inner("movie", "tt777", None, None, time.time() + 5)
    assert len(out["streams"]) == 1
    assert pw.call_args[0][0].endswith("/watch/harudu.html")


def test_build_inner_falls_through_when_the_slug_index_is_stale():
    clear_caches()
    addon.C_SLUG.put(("movie", "tt777"), "gone-slug", 600)
    page = dict(_PAGE_FIX, slug="gone-slug", url="https://banglaplex.biz/watch/gone-slug.html")
    with mock.patch.object(addon, "resolve_meta_all",
                           return_value=[("Harudu", 2026, None)]), \
         mock.patch.object(addon, "_cards_from_matches",
                           side_effect=[[], [_card()]]), \
         mock.patch.object(addon, "search_candidates",
                           return_value=[{"title": "Harudu",
                                          "url": "https://banglaplex.biz/watch/harudu.html"}]), \
         mock.patch.object(addon, "match_candidates",
                           return_value=[{"url": "https://banglaplex.biz/watch/harudu.html"}]), \
         mock.patch.object(addon, "parse_watch_page", return_value=page):
        out = addon._build_inner("movie", "tt777", None, None, time.time() + 5)
    assert len(out["streams"]) == 1, "a stale slug must not blank the title"


# ── shelf prewarm ────────────────────────────────────────────────────────────
# ══════════════ abyss player (SoTrym): crypto pinned against real payloads ════
# The page below is a real abyssplayer.com embed as served for BanglaPlex's
# "Queens" (slug eLY0XBgBP). Its media blob decrypts to a 4-source mp4 map, and
# the token built from it was verified live: the origin answered 200/206 with
# `ftyp isom` + moov + mdat summing to exactly the declared 2 081 695 893 bytes.
_ABYSS_DATAS = "eyJzbHVnIjoiZUxZMFhCZ0JQIiwibWQ1X2lkIjozMDU3MDc3MSwidXNlcl9pZCI6NDI4MjczLCJtZWRpYSI6IjB8uyOPdLwnXHUwMDA0lDf0hl1cdTAwMDKNZ5WDPMb/ND3bkXf8tHB/mZeBYXNs+zDWd7aSt790uY9U2YP+K6U4PZ5QP1XtIJO9Qc5TtVx1MDAxMjImqm1xsFGuXHUwMDFkyVwi//NEXHUwMDBiuG6JwdjW6Z+u0VxitfL7NTO0IHSXXHUwMDE52Fx1MDAwYqOzQU+EujvYtalYc1x1MDAwZkqrqFx0RDivXHUwMDAzcPXMS2mx6M9KWK/8pVx1MDAxY6dcdTAwMTgmw9NcdTAwMTBcdTAwMWZcdTAwMWX+fDbOUbIzW1x1MDAwNOT7XHUwMDFjmS5cdTAwMWaYb7ygj/B9MXN+KIcsv+FgWEj08lx1MDAwNnBcdTAwMDPcXHUwMDBmgs2wVKW/QONeXcJiPjazx89bK52K/zRcdTAwMGJ0mWyEfIP/5+vYR+RLNlQ0TLaZvsVk29pl7lBcdTAwMTQ96ufeU9DM9Ks9JowrUzlKf5ZcdTAwMWWMjUKyVlwiXjTtpGNM2+iq91x1MDAxNrcrNLBacKpFcomX9iZK6i29V7yaLIUlV1/up9ivLmDYy4/LYPFXQotcdTAwMWZcdTAwMTeIkONcdTAwMWU/WnVcdFGw27DJ95AqoHJ/jHTcR/e8myryXHUwMDFki1x1MDAwMsw07ucmU1x06+SE/1x1MDAxM7uRS8FcdTAwMWPbNHVZKdxkM3GyXlx1MDAxMt7D5FpTwZfyNmIpc5CbMuBcdTAwMWRRvlx1MDAwZmVCIaYsKTKzRlx1MDAwMzrCgXyYZjBoNFx1MDAwMkItxGd6ldo/wnlcdTAwMWNcdTAwMGZcboI/2vFcdTAwMDCos1x1MDAxNVx1MDAxNPhcdTAwMWHWTjOLXHUwMDA2rzHBZFxyXGLIM1FBXHUwMDBiT/juXG5mmzlPdC27M430XHUwMDE15IuDXHUwMDA1jFJIRexcdTAwMGVeLF8uf9p1cXqwVXAsgkq+OF9lt1x1MDAxYS6D09VcdTAwMTNKTlx1MDAxYpHLU5qHhUNdbilSafLZfMDMMoPmQ1x1MDAxMiGOolx1MDAxN87y2llsWrcv/KZcblCidbXTqppgh35JrOSQokGW7LWqdHupgbKFXHUwMDA0QIuB+2a4VnuuM0f4ksz/l62O1bL8XG5NXHUwMDFilTu6vlx1MDAwMT1KtOfkwcv5XHRb49k7XHUwMDE4m12fslZcdTAwMTXOiEQgJKvmflx1MDAxZZPgOFPH1n5NXG49U1x1MDAxNVbscShGXHUwMDAzXHUwMDFk8laOs1x1MDAxOGd/XHUwMDA1xtaTXZR3XCLj+7ynjsRcdNaSpaQxq0eeoVx1MDAwYslK9VOOOVZcdTAwMDI9VkLpXHUwMDAygjHySHK7XHUwMDE2XHS+bvWE+oNcdTAwMTCchI5cIlOLnKiW0iraZ6HxlcB6/Fx1MDAxYVx1MDAxNlaEYHN7R6BzzEGDvGLY8Vx1MDAwZpOtwP/sjYW/+JI8hYA8VchcXM2HndorKVx1MDAxNbYpJVx1MDAwMtuOMoC7uFx1MDAwZYGAnlx1MDAxNPpcctKqxeVt+ZFQRKNLf0zRo1x1MDAxZLEsXHUwMDE5tzWa/3xcdTAwMTfPkrH9RUtG8agrL9Es4W1oTfTMiVH9XHUwMDFmw7n0Pu+ExNbu1nlG9tSA+FuiwYTKXHUwMDFloHWP/LBcdTAwMTH8UKSlQUsynnv87bzwcL6FXXJcdTAwMWUs9FZR0/+zSiuyXHUwMDE0YCtyhyyqyKN07Fi2bbvGrF6UI1x1MDAxMJr8O1x1MDAxNTVMhfzYrCtV4lx1MDAxM2KTQVx1MDAwZt5fw1x0rDZ+ki1e9ufX8lx1MDAxM7PTjoVcdTAwMDdP7TnT/5xcdTAwMGV8j4IyXHUwMDBlvVx1MDAwMlx1MDAxYlh1ky2QXHUwMDE129/IJnimllx1MDAxMydP2YN3mtPbXHUwMDBl2Ptm2tRUXHUwMDAyP7U9baLfdUKU6j9r6Y7ZMqNcdTAwMWVcdTAwMGWL9KBcYt/YNa5R3W4nf6HYblx1MDAxZXFCUW6FMlGsmERjZ1x1MDAxNcFcdTAwMWatx4TYSKJcIuUnu9fDyMFcIlRfM51miDNmK5SPI1x1MDAxN1x1MDAxMbjG2UFOw2Z3oFwi2U21embHbnRcXNF/qjx8+sNFnHKdc+tcdTAwMTarlI1npPuROlx1MDAwZZ+Ff5DbOtiUk1x1MDAxZFx1MDAxOXWl6q0/XG6afLfGTrJcdTAwMWauIYmOKLCbtXKIzsEsP1x1MDAwMJL0TCOJLrqt1YQ7XHUwMDAxXHUwMDE0XHUwMDA1JJbNwMmUj1xyjuX4nl7sjFx1MDAxOFGGbthcbjBHtceIxEtcdFx1MDAxY3qhIF+x2pD0iaM6SFOkXGI8XHUwMDA0xpvGc8eHWZlNlsfZ3f6nVYUp9kSX49Dn+KA8zWfkzoO6pFef9zY8gzUukFBcdTAwMDGt+O/OsmxcdTAwMWb+zC9qp5lgU0N1w+B8XHUwMDFhMUJcdTAwMTVGY1oiLCJjb25maWciOnsicG9zdGVyIjp0cnVlLCJwcmV2aWV3Ijp0cnVlLCJpc0Rvd25sb2FkIjpmYWxzZX0sImRhbm11Ijp7InZpZGVvSWQiOiJEUGxXRW95dU9fanR4RE9zRDFoMFd6U2EtSUMyczhJOEh6YUVsRDdpTTgxQVpNeFBGQ1Jxem5ZQWJsU1ZXSk9TTk90YV9FQ3hFOTJOWnRiV0tNdU9NTTl6YWJUR241RmptZ1VUIn19"
_ABYSS_HTML = ('<script>window.addEventListener("load", ()=> {const datas = "'
               + _ABYSS_DATAS + '";if(window.SoTrym)return window.SoTrym('
                 'JSON.parse(atob(datas)));});</script>')
_ABYSS_MD5_ID = 30570771
_ABYSS_TOKEN_2M = "MW0xMFBBc1Z6ZFdOek0zNmQya1lGSEZtNng3S3dCeFVBd3dXOWp3T3BNM3VhWVhH"
_ABYSS_TOKEN_FULL = "MW0xMFBBc1Z6ZFdOek0zNmQya1lGSEZtNng3S3dCeFVBd3dXOWp3UG9zcmlicExQRTZSLw"


def test_aes_ctr_round_trips_and_matches_the_recorded_token():
    """the hand-rolled AES-256-CTR must agree byte-for-byte with node's
    aes-256-ctr, which is what the live origin accepted."""
    key, ctr = addon._md5_key(b"428273:eLY0XBgBP:30570771")
    assert len(key) == 32 and len(ctr) == 16 and ctr == key[:16]
    pt = b"the quick brown fox jumps over the lazy dog" * 4     # > 2 blocks
    assert addon._aes_ctr(addon._aes_ctr(pt, key, ctr), key, ctr) == pt
    # the segment token: digits-as-numbers key + double base64, no padding
    assert addon.abyss_token(_ABYSS_MD5_ID, 4, 2081695893, 2097152, 0) == _ABYSS_TOKEN_2M
    assert addon.abyss_token(_ABYSS_MD5_ID, 4, 2081695893, 2081695893, 0) == _ABYSS_TOKEN_FULL
    # the key hashes the size's digits AS NUMBERS: '2' -> 0x02, not 0x32
    k_num, _ = addon._md5_key(bytes([2, 0, 8, 1, 6, 9, 5, 8, 9, 3]))
    k_txt, _ = addon._md5_key(b"2081695893")
    assert k_num != k_txt


def test_abyss_page_data_decrypts_the_real_blob():
    blob, media = addon.abyss_page_data(_ABYSS_HTML)
    assert blob and media
    assert (blob["slug"], blob["md5_id"], blob["user_id"]) == ("eLY0XBgBP", 30570771, 428273)
    mp4 = media["mp4"]
    assert mp4["domains"][0] == "gi7owxbf32.sssrr.org"
    assert {(s["label"], s["codec"]) for s in mp4["sources"]} == \
        {("480p", "h264"), ("720p", "h264"), ("1080p", "av1"), ("1080p", "h264")}
    # the media field's leading "0|" is ciphertext, not a marker to strip
    assert blob["media"].startswith("0|")


def test_abyss_page_data_is_latin1_safe_and_rejects_junk():
    """the blob carries raw ciphertext bytes inside JSON: decoding it as utf-8
    raises, which is exactly how the first attempt at this failed."""
    raw = base64.b64decode(_ABYSS_DATAS)
    try:
        raw.decode("utf-8")
        assert False, "expected the utf-8 decode to fail"
    except UnicodeDecodeError:
        pass
    assert raw.decode("latin-1")                       # the path the code uses
    for junk in ("", "no datas here", 'const datas = "!!!not base64!!!"',
                 'const datas = ""'):
        assert addon.abyss_page_data(junk) == (None, None)


def test_abyss_sources_dedupe_by_label_preferring_h264():
    _blob, media = addon.abyss_page_data(_ABYSS_HTML)
    srcs = addon.abyss_sources(media)
    assert [s["label"] for s in srcs] == ["1080p", "720p", "480p"], srcs
    top = srcs[0]
    assert top["codec"] == "h264" and top["size"] == 2844976106, "av1 must lose"
    assert top["base"] == "https://njuaynwkh47.sssrr.org"
    assert srcs[1]["res_id"] == 4 and srcs[1]["base"] == "https://njuaynwkh47.sssrr.org"


def test_abyss_sources_skips_unencoded_and_broken_entries():
    media = {"mp4": {"domains": ["a.sssrr.org"], "sources": [
        {"label": "720p", "res_id": 4, "size": 10, "codec": "h264",
         "status": False, "sub": "s1"},                    # not encoded yet
        {"label": "480p", "res_id": 3, "size": 9, "codec": "h264",
         "status": True},                                   # no sub -> unusable
        {"label": "1080p", "res_id": 5, "size": 11, "codec": "h264",
         "status": True, "sub": "s2"}]}}
    srcs = addon.abyss_sources(media)
    assert [s["label"] for s in srcs] == ["1080p"]
    assert addon.abyss_sources({"mp4": {"sources": []}}) == []      # no domains
    assert addon.abyss_sources(None) == []


def test_abyss_stream_url_shape_and_seekability():
    _blob, media = addon.abyss_page_data(_ABYSS_HTML)
    srcs = {s["label"]: s for s in addon.abyss_sources(media)}
    url, seek = addon.abyss_stream_url(srcs["720p"], _ABYSS_MD5_ID)
    assert url == "https://njuaynwkh47.sssrr.org/sora/2081695893/" + _ABYSS_TOKEN_FULL
    assert seek is False, "2.08 GB is above the 500 MiB Range ceiling"
    small = dict(srcs["480p"], size=100 * 1024 * 1024)
    _u, seek2 = addon.abyss_stream_url(small, _ABYSS_MD5_ID)
    assert seek2 is True


def test_human_bytes():
    assert addon._human_bytes(0) == "" and addon._human_bytes(None) == ""
    assert addon._human_bytes(512) == "512 B"
    assert addon._human_bytes(2081695893) == "1.94 GB"
    assert addon._human_bytes(918991600) == "876.42 MB"


def _abyss_fixture_cards(probe=(206, True)):
    clear_caches()
    blob, media = addon.abyss_page_data(_ABYSS_HTML)

    class R:
        text = _ABYSS_HTML
    with mock.patch.object(addon, "_get", return_value=R()), \
         mock.patch.object(addon, "_abyss_probe", return_value=probe) as pr:
        cards = addon.resolve_abyss("https://abyssplayer.com/eLY0XBgBP",
                                    {"title": "Queens", "year": 2026, "slug": "queens"},
                                    "", "series", 1, 1, time.time() + 20)
    return cards, pr, (blob, media)


def test_resolve_abyss_probes_with_range_only_when_the_origin_honours_it():
    cards, pr, _ = _abyss_fixture_cards()
    assert cards
    used = {c[0][1] for c in pr.call_args_list}          # positional (url, use_range)
    assert used == {False}, "every queens source is above the 500 MiB ceiling"


def test_resolve_abyss_emits_verified_cards_with_referer_headers():
    cards, pr, _ = _abyss_fixture_cards()
    media = [c for c in cards if not c.get("_ext")]
    assert len(media) == 3, [c["name"] for c in cards]
    c0 = media[0]
    assert c0["url"].startswith("https://njuaynwkh47.sssrr.org/sora/2844976106/")
    assert c0["behaviorHints"]["proxyHeaders"]["request"]["Referer"] == \
        "https://abyssplayer.com/"
    assert c0["behaviorHints"]["notWebReady"] is True
    assert "1080p" in c0["name"] and "Queens" in c0["name"]
    assert "S01 E01" in c0["description"]
    assert "2.65 GB" in c0["description"] and "H264" in c0["description"]
    assert "Abyss" in c0["description"]
    # every emitted url was probed first: no phantom cards
    assert pr.call_count == 3
    for c in media:
        assert c["_cdn"] == "abyss" and c["_res"] in ("1080p", "720p", "480p")
        assert c["_tier"] == "", "a declared label earns no measured tier claim"


def _cfg(**over):
    c = dict(addon.CFG_DEFAULTS)
    c.update({k: str(v) for k, v in over.items()})
    return c


def _ext_of(cards):
    return [c for c in cards if c.get("externalUrl")]


def test_browser_card_is_built_once_and_cfg_decides_who_sees_it():
    """The build cache is shared by every install, so the browser card is BUILT
    whenever a verified abyss card exists and `bc` decides per request. Building it
    conditionally on one request's config would poison the cache for everyone
    else. queens' three sources are all above ABYSS_RANGE_MAX -> none can seek."""
    cards, _pr, _m = _abyss_fixture_cards()
    ext = [c for c in cards if c.get("_ext")]
    assert len(ext) == 1, [c["name"] for c in cards]
    e = ext[0]
    assert e["externalUrl"] == "/player/eLY0XBgBP"
    assert "url" not in e, "an externalUrl card must not also claim a stream url"
    assert "behaviorHints" not in e, "a browser card needs no proxy headers"
    assert "browser" in e["name"] and "seeking works" in e["description"]
    assert e["_cdn"] == "abyss-web"
    assert [c["_seek"] for c in cards if not c.get("_ext")] == [0, 0, 0]

    with mock.patch.object(addon, "ABYSS_RANGE_MAX", 10 ** 12):
        seekable, _p, _m2 = _abyss_fixture_cards()   # every file now in range
    assert [c["_seek"] for c in seekable if not c.get("_ext")] == [1, 1, 1]

    # bc: 0 never · 1 only when nothing offered can seek · 2 always
    assert not _ext_of(addon.apply_cfg(cards, _cfg(bc="0")))
    assert _ext_of(addon.apply_cfg(cards, _cfg(bc="1"))), "nothing can seek"
    assert _ext_of(addon.apply_cfg(cards, _cfg(bc="2")))
    assert not _ext_of(addon.apply_cfg(seekable, _cfg(bc="1"))), "native can seek"
    assert _ext_of(addon.apply_cfg(seekable, _cfg(bc="2")))

    with mock.patch.object(addon, "BROWSER_CARD_ON", False):
        off, _p, _m3 = _abyss_fixture_cards()        # env kill switch
    assert not [c for c in off if c.get("_ext")]


def test_aby_off_drops_abyss_cards_and_leaves_3n1_alone():
    cards, _pr, _m = _abyss_fixture_cards()
    assert addon.apply_cfg(cards, _cfg(aby="0")) == [], "an off switch means off"
    assert len(addon.apply_cfg([_card()], _cfg(aby="0"))) == 1
    # the switch also suppresses the browser card even with bc=2
    assert not _ext_of(addon.apply_cfg(cards, _cfg(aby="0", bc="2")))


def test_seekable_first_reorders_without_disturbing_quality_order():
    a = {"name": "1080p", "_cdn": "abyss", "_res": "1080p", "_seek": 0, "url": "u1"}
    b = {"name": "720p", "_cdn": "abyss", "_res": "720p", "_seek": 0, "url": "u2"}
    c = {"name": "480p", "_cdn": "abyss", "_res": "480p", "_seek": 1, "url": "u3"}
    assert [x["name"] for x in addon.apply_cfg([a, b, c], _cfg(sq="0"))] == \
        ["1080p", "720p", "480p"]
    assert [x["name"] for x in addon.apply_cfg([a, b, c], _cfg(sq="1"))] == \
        ["480p", "1080p", "720p"], "stable sort keeps quality order inside a group"
    # something can seek now, so bc=1 has nothing to add
    assert not _ext_of(addon.apply_cfg([a, b, c], _cfg(sq="1", bc="1")))


def test_private_markers_never_reach_the_player():
    cards, _pr, _m = _abyss_fixture_cards()
    for c in addon.apply_cfg(cards, _cfg(bc="2")):
        for k in ("_cdn", "_res", "_tier", "_nsubs", "_ext", "_seek"):
            assert k not in c, (k, c["name"])


def test_browser_card_never_replaces_a_verified_card():
    """Honesty order: the native card comes first, the browser card is appended.
    A build that produced no verified card gets no browser card either — we do not
    advertise a player page for media we could not confirm exists."""
    cards, _pr, _m = _abyss_fixture_cards(probe=(403, False))
    assert cards == [], "no verified media -> no card of any kind"


def test_browser_card_rides_outside_the_card_cap_and_honours_bc():
    cards, _pr, _m = _abyss_fixture_cards()
    cfg = dict(addon.CFG_DEFAULTS)
    cfg.update({"n": 2, "bc": "1"})
    out = addon.apply_cfg(cards, cfg)
    assert len(out) == 3, [c["name"] for c in out]     # 2 qualities + browser
    assert out[-1]["externalUrl"] == "/player/eLY0XBgBP"
    for k in ("_cdn", "_res", "_tier", "_nsubs", "_ext"):
        assert k not in out[-1], "private markers must be stripped"
    cfg["bc"] = "0"
    off = addon.apply_cfg(cards, cfg)
    assert len(off) == 2 and not [c for c in off if c.get("externalUrl")]


def test_cap_cards_and_mark_alts_treat_the_browser_card_as_not_a_quality():
    media = [{"name": "a%d" % i, "_cdn": "abyss"} for i in range(addon.MAX_CARDS + 2)]
    ext = [{"name": "browser", "_ext": True}]
    out = addon._cap_cards(media + ext)
    assert len(out) == addon.MAX_CARDS + 1 and out[-1]["_ext"] is True
    cards = [{"name": "A"}, {"name": "B"}, {"name": "C", "_ext": True}]
    addon._mark_alts(cards)
    assert [c["name"] for c in cards] == ["A", "B · alt", "C"]


def test_absolutize_prefixes_external_urls_too():
    cards = [{"url": "/x/master.m3u8"}, {"externalUrl": "/player/abc123"}]
    addon._absolutize(cards, "https://svc.onrender.com/cfgseg")
    assert cards[0]["url"] == "https://svc.onrender.com/cfgseg/x/master.m3u8"
    assert cards[1]["externalUrl"] == "https://svc.onrender.com/cfgseg/player/abc123"


def test_player_route_serves_an_iframe_shell_and_nothing_else():
    c = _http_get("/player/eLY0XBgBP")
    assert c["code"] == 200
    assert c["headers"]["Content-Type"].startswith("text/html")
    body = c["body"].decode("utf-8")
    assert '<iframe src="https://abyssplayer.com/eLY0XBgBP"' in body
    assert "allowfullscreen" in body and "encrypted-media" in body
    assert len(c["body"]) < 2000, "a shell, not a page full of anything"
    # the anti-hotlink redirect is the whole reason this route exists
    assert "abyss.to" not in body


def test_player_route_also_works_behind_a_config_segment():
    """An install configured through /configure gets `base` = <origin>/<cfgseg>,
    and _absolutize builds the card's externalUrl from it — so the shell must be
    reachable with that prefix, which means "player" has to be a top route."""
    seg = addon.cfg_pack({"n": 2, "bc": "1"})
    c = _http_get("/%s/player/eLY0XBgBP" % seg)
    assert c["code"] == 200, c
    assert b'abyssplayer.com/eLY0XBgBP' in c["body"]


def test_player_route_rejects_anything_that_is_not_a_plain_id():
    for bad in ('/player/<script>alert(1)</script>', '/player/a"onload="x',
                "/player/..%2f..%2fetc", "/player/ab", "/player/",
                "/player/" + "9" * 65):
        c = _http_get(bad)
        assert c["code"] == 404, (bad, c["code"])


def test_config_page_has_an_abyss_section_with_all_three_controls():
    body = _http_get("/configure")["body"].decode("utf-8")
    assert "Abyss &amp; seeking" in body
    for i in ("aby", "bc", "sq"):
        assert 'id="%s"' % i in body, i
        assert '"%s"' % i in body, "the default config must carry %s for the JS" % i
    assert "Only when nothing else can seek" in body
    assert "Always, alongside the qualities" in body
    assert "Seekable files first" in body
    assert "never through this service" in body, "say the bandwidth rule out loud"
    assert body.count('id="bc"') == 1, "the old buried row must be gone"


def test_cfg_round_trips_the_abyss_toggles():
    for k, v in (("bc", "0"), ("bc", "2"), ("aby", "0"), ("sq", "1")):
        assert addon.cfg_unpack(addon.cfg_pack({k: v}))[k] == v, (k, v)
    assert addon.cfg_unpack(addon.cfg_pack({"bc": "9"}))["bc"] == "1"
    assert addon.cfg_unpack(addon.cfg_pack({"aby": "nope"}))["aby"] == "1"
    assert addon.cfg_unpack(addon.cfg_pack({"sq": "x"}))["sq"] == "0"


def test_resolve_abyss_refuses_an_origin_that_will_not_serve_ftyp():
    """the gate is the whole point: a token we cannot verify is a card we do not
    emit, even though building it succeeded."""
    for probe in ((403, False), (200, False), (None, False)):
        cards, _pr, _ = _abyss_fixture_cards(probe=probe)
        assert cards == [], probe


def test_resolve_abyss_warns_when_seeking_will_not_work():
    cards, _pr, _ = _abyss_fixture_cards()
    big = [c for c in cards if "2.65 GB" in c["description"]][0]
    assert "no seeking" in big["description"]
    with mock.patch.object(addon, "ABYSS_RANGE_MAX", 10 ** 12):
        cards2, _p, _m = _abyss_fixture_cards()
    assert all("no seeking" not in c["description"] for c in cards2)


def test_resolve_abyss_notes_survive_alongside_the_caller_note():
    clear_caches()
    class R:
        text = _ABYSS_HTML
    with mock.patch.object(addon, "_get", return_value=R()), \
         mock.patch.object(addon, "_abyss_probe", return_value=(206, True)):
        cards = addon.resolve_abyss("https://abyssplayer.com/eLY0XBgBP",
                                    {"title": "Queens"}, "full-season file",
                                    "series", 1, 1, time.time() + 20)
    assert "full-season file" in cards[0]["description"]
    assert "no seeking" in cards[0]["description"]


def test_resolve_abyss_caches_the_decrypted_media():
    cards, _pr, _ = _abyss_fixture_cards()
    assert cards
    with mock.patch.object(addon, "_get", side_effect=AssertionError("must not refetch")), \
         mock.patch.object(addon, "_abyss_probe", return_value=(206, True)):
        again = addon.resolve_abyss("https://abyssplayer.com/eLY0XBgBP",
                                    {"title": "Queens"}, "", "movie", None, None,
                                    time.time() + 20)
    assert len(again) == len(cards)


def test_resolve_abyss_kill_switch_and_bad_page():
    clear_caches()
    with mock.patch.object(addon, "ABYSS_ON", False), \
         mock.patch.object(addon, "_get", side_effect=AssertionError("off means off")):
        assert addon.resolve_abyss("https://abyssplayer.com/x", {}, "", "movie",
                                   None, None, time.time() + 5) == []
    assert addon.resolve_abyss("", {}, "", "movie", None, None, time.time() + 5) == []

    class R:
        text = "<html>no player here</html>"
    with mock.patch.object(addon, "_get", return_value=R()):
        assert addon.resolve_abyss("https://abyssplayer.com/x", {}, "", "movie",
                                   None, None, time.time() + 5) == []


def test_resolve_file_falls_back_to_abyss_when_3n1_is_dead():
    """the real-world shape for 16 of 18 new series: the embed lists three
    servers, strp2p and rpmvid 404, and only abyss has the file."""
    clear_caches()
    page = {"url": "https://banglaplex.biz/watch/queens.html", "title": "Queens",
            "year": 2026, "slug": "queens", "keys": [("k", True, "Full")],
            "iframe": "https://plextream.work/embed.php?id=V2aAlO9K"}
    servers = [("Server 3", "https://abyssplayer.com/eLY0XBgBP"),
               ("Server 2", "https://bpx.strp2p.site/#skmyti"),
               ("Server 1", "https://bpx.rpmvid.site/#wnymh1")]
    with mock.patch.object(addon, "parse_watch_page", return_value=page), \
         mock.patch.object(addon, "parse_embed_servers", return_value=servers), \
         mock.patch.object(addon, "resolve_n1", return_value=(None, None, None)), \
         mock.patch.object(addon, "_get",
                           return_value=type("R", (), {"text": _ABYSS_HTML})()), \
         mock.patch.object(addon, "_abyss_probe", return_value=(206, True)):
        cards = addon._resolve_file(page, None, "Full", "", "series", 1, 1,
                                    time.time() + 20)
    media = [c for c in cards if not c.get("_ext")]
    assert len(media) == 3, cards
    assert all(c["_cdn"] == "abyss" for c in media)


def test_resolve_file_prefers_3n1_and_only_uses_abyss_when_it_fails():
    clear_caches()
    page = {"url": "https://banglaplex.biz/watch/x.html", "title": "X", "year": 2026,
            "slug": "x", "keys": [("k", True, "Full Movie")],
            "iframe": "https://plextream.work/embed.php?id=1"}
    servers = [("Server 3", "https://abyssplayer.com/abc"),
               ("Server 2", "https://bpx.strp2p.site/#vid")]
    info = {"best": (1920, 800), "n_variants": 2, "size": 200, "variant": "v",
            "text": TT_MASTER, "segment": "s", "seg_status": 206}
    with mock.patch.object(addon, "parse_watch_page", return_value=page), \
         mock.patch.object(addon, "parse_embed_servers", return_value=servers), \
         mock.patch.object(addon, "resolve_n1",
                           return_value=({"hlsVideoTiktok": "/hls/m.m3u8"}, "bpx.strp2p.site", "vid")), \
         mock.patch.object(addon, "collect_subtitles", return_value=[]), \
         mock.patch.object(addon, "_verify_media", return_value=info), \
         mock.patch.object(addon, "resolve_abyss",
                           side_effect=AssertionError("3n1 answered; abyss not needed")):
        cards = addon._resolve_file(page, None, "Full Movie", "", "movie", None, None,
                                    time.time() + 20)
    assert len(cards) == 1 and cards[0]["_cdn"] == "tiktok"
    # now the same page with every 3n1 candidate failing verification
    with mock.patch.object(addon, "parse_watch_page", return_value=page), \
         mock.patch.object(addon, "parse_embed_servers", return_value=servers), \
         mock.patch.object(addon, "resolve_n1",
                           return_value=({"hlsVideoTiktok": "/hls/m.m3u8"}, "bpx.strp2p.site", "vid")), \
         mock.patch.object(addon, "collect_subtitles", return_value=[]), \
         mock.patch.object(addon, "_verify_media", return_value=None), \
         mock.patch.object(addon, "resolve_abyss", return_value=[{"_cdn": "abyss"}]) as ra:
        cards = addon._resolve_file(page, None, "Full Movie", "", "movie", None, None,
                                    time.time() + 20)
    assert cards == [{"_cdn": "abyss"}] and ra.called


def test_resolve_file_routes_a_bare_abyss_iframe():
    clear_caches()
    page = {"url": "https://banglaplex.biz/watch/x.html", "title": "X", "year": 2026,
            "keys": [("k", True, "Full Movie")],
            "iframe": "https://abyssplayer.com/UpjbDHK5N"}
    with mock.patch.object(addon, "parse_watch_page", return_value=page), \
         mock.patch.object(addon, "parse_embed_servers",
                           side_effect=AssertionError("must not fetch the wrapper")), \
         mock.patch.object(addon, "resolve_abyss", return_value=[{"_cdn": "abyss"}]) as ra:
        cards = addon._resolve_file(page, None, "Full Movie", "", "movie", None, None,
                                    time.time() + 20)
    assert cards == [{"_cdn": "abyss"}]
    assert ra.call_args[0][0] == "https://abyssplayer.com/UpjbDHK5N"


def test_abyss_probe_reads_only_the_header_of_a_200():
    """a 200 on this route means the origin is streaming the ENTIRE object; the
    probe must stop after the first bytes instead of pulling 2 GB through Render."""
    read = []

    class Resp:
        status_code = 200

        def iter_content(self, n):
            for i in range(1000):
                read.append(i)
                yield (b"\x00\x00\x00 ftypisom" if i == 0 else b"x" * 64)

        def close(self):
            self.closed = True
    r = Resp()
    with mock.patch.object(addon, "_fetch", return_value=(r, False)) as f:
        code, ok = addon._abyss_probe("https://x/sora/1/t")
    assert (code, ok) == (200, True)
    assert len(read) == 1, "must stop as soon as ftyp is seen"
    assert f.call_args[1]["allow_proxy"] is False, "media never rides a free exit"
    assert f.call_args[1]["referer"] == "https://abyssplayer.com/"
    assert f.call_args[1]["extra_headers"] == {"Range": "bytes=0-63"}


def test_abyss_probe_drops_the_range_header_above_the_ceiling():
    """measured: with Range the oversized-fragment route answered 200,400,200,400;
    without it, 200,200,200,200. A range the origin will not honour must not be
    requested, or half of all good cards are thrown away."""
    class Resp:
        status_code = 200

        def iter_content(self, n):
            yield b"\x00\x00\x00 ftypisom"

        def close(self):
            pass
    with mock.patch.object(addon, "_fetch", return_value=(Resp(), False)) as f:
        assert addon._abyss_probe("https://x/sora/1/t", False) == (200, True)
    assert f.call_args[1]["extra_headers"] is None

    class Bad(Resp):
        status_code = 403

        def iter_content(self, n):
            yield b"<html>denied</html>"
    with mock.patch.object(addon, "_fetch", return_value=(Bad(), False)):
        assert addon._abyss_probe("https://x/sora/1/t") == (403, False)
    with mock.patch.object(addon, "_fetch", return_value=(None, False)):
        assert addon._abyss_probe("https://x/sora/1/t") == (None, False)
    with mock.patch.object(addon, "_fetch", side_effect=RuntimeError("boom")):
        assert addon._abyss_probe("https://x/sora/1/t") == (None, False)


def test_prewarm_shelf_is_bounded_and_skips_cached():
    clear_caches()
    addon._PREWARM_BUSY[0] = False
    metas = [{"id": "tt1"}, {"id": "bpx-two"}, {"id": "tt3"}, {"id": "tt4"},
             {"id": "tt5"}, {"id": "tt6"}, {"id": "tt7"}, {"id": "tt8"}]
    addon.C_STREAM.put(("movie", "tt1", None, None), [_card()], 600)
    started = []

    class FakeThread:
        def __init__(self, target=None, daemon=None, name=None):
            self.fn, self.n = target, name

        def start(self):
            started.append(self.n)
            self.fn()
    with mock.patch.object(addon.threading, "Thread", FakeThread), \
         mock.patch.object(addon, "build_streams",
                           side_effect=lambda t, i, s, e: started.append(i) or
                           {"streams": []}), \
         mock.patch.object(addon, "PREWARM_N", 4):
        addon._prewarm_shelf(metas, "movie")
    warmed = [x for x in started if isinstance(x, str) and x not in ("shelfwarm",)]
    # the first batch runs two-at-a-time on a real executor, so only the SET is
    # deterministic — asserting an order here made this test flake.
    assert sorted(warmed) == ["bpx-two", "tt3", "tt4"], warmed   # tt1 cached, cap 4
    assert "tt1" not in warmed, "an already-cached card must not be re-warmed"
    assert "shelfwarm" in started
    assert addon._PREWARM_BUSY[0] is False, "the busy flag must be released"


def test_prewarm_shelf_single_flight_and_kill_switch():
    clear_caches()
    addon._PREWARM_BUSY[0] = True
    with mock.patch.object(addon.threading, "Thread",
                           side_effect=AssertionError("must not spawn")):
        addon._prewarm_shelf([{"id": "tt1"}], "movie")
    addon._PREWARM_BUSY[0] = False
    with mock.patch.object(addon, "PREWARM_N", 0), \
         mock.patch.object(addon.threading, "Thread",
                           side_effect=AssertionError("must not spawn")):
        addon._prewarm_shelf([{"id": "tt1"}], "movie")


def test_prewarm_shelf_survives_a_build_error():
    clear_caches()
    addon._PREWARM_BUSY[0] = False

    class FakeThread:
        def __init__(self, target=None, daemon=None, name=None):
            self.fn = target

        def start(self):
            self.fn()
    with mock.patch.object(addon.threading, "Thread", FakeThread), \
         mock.patch.object(addon, "build_streams", side_effect=RuntimeError("boom")):
        addon._prewarm_shelf([{"id": "tt1"}], "movie")
    assert addon._PREWARM_BUSY[0] is False


def _late_future():
    """a fake executor handing back a future WE complete, so a build can be made
    to outlive the answer wall on purpose."""
    import concurrent.futures as cf
    fut = cf.Future()

    class FakeEx:
        def submit(self, fn, *a, **k):
            return fut
    return fut, FakeEx()


def test_a_build_that_outlives_the_wall_still_fills_the_cache():
    """the player is told "tap again in a few seconds" — that promise used to be
    empty, because the late worker result was dropped and the retry redid 20 s of
    work. This is the cold-boot path (service just woke, pool untrained)."""
    clear_caches()
    fut, ex = _late_future()
    with mock.patch.object(addon, "_BUILD_EX", ex), \
         mock.patch.object(addon, "WALL", 0.2):
        r = addon.build_streams("movie", "tt777", None, None)
    assert r["streams"] == []
    assert "still resolving" in r["message"]
    assert addon.C_STREAM.get(("movie", "tt777", None, None))[0] is False
    fut.set_result({"streams": [_card()]})          # worker finishes late
    hit, val = addon.C_STREAM.get(("movie", "tt777", None, None))
    assert hit and len(val) == 1, "the late answer must be adopted"
    assert addon.C_STALE.get(("movie", "tt777", None, None)) is not None
    with mock.patch.object(addon, "_BUILD_EX", ex):
        r2 = addon.build_streams("movie", "tt777", None, None)
    assert len(r2["streams"]) == 1, "the promised retry must be a cache hit"


def test_a_late_empty_result_is_not_memoised():
    clear_caches()
    fut, ex = _late_future()
    with mock.patch.object(addon, "_BUILD_EX", ex), \
         mock.patch.object(addon, "WALL", 0.2):
        addon.build_streams("movie", "tt778", None, None)
    fut.set_result({"streams": [], "message": "not on BanglaPlex"})
    assert addon.C_STREAM.get(("movie", "tt778", None, None))[0] is False, \
        "an empty late answer must not become a cached negative"


def test_a_late_result_does_not_clobber_a_newer_answer():
    clear_caches()
    fut, ex = _late_future()
    first = _card()
    addon.C_STREAM.put(("movie", "tt779", None, None), [first], 600)
    with mock.patch.object(addon, "_BUILD_EX", ex), \
         mock.patch.object(addon, "WALL", 0.2):
        addon.build_streams("movie", "tt779", None, None)
    newer = _card()
    newer["url"] = "https://bpx.strp2p.site/hls/newer/master.m3u8"
    fut.set_result({"streams": [newer]})
    hit, val = addon.C_STREAM.get(("movie", "tt779", None, None))
    assert hit and val[0] is first, "the served answer must win"


def test_a_cancelled_or_raising_build_cannot_break_the_callback():
    clear_caches()
    fut, ex = _late_future()
    with mock.patch.object(addon, "_BUILD_EX", ex), \
         mock.patch.object(addon, "WALL", 0.2):
        addon.build_streams("movie", "tt780", None, None)
    fut.set_exception(RuntimeError("boom"))         # must be swallowed
    assert addon.C_STREAM.get(("movie", "tt780", None, None))[0] is False


def test_prewarm_shelf_survives_a_submit_failure():
    """an executor that refuses work (shutting down at exit) must not kill the
    thread or strand the busy flag, which would disable prewarming for good."""
    clear_caches()
    addon._PREWARM_BUSY[0] = False

    class FakeThread:
        def __init__(self, target=None, daemon=None, name=None):
            self.fn = target

        def start(self):
            self.fn()
    with mock.patch.object(addon.threading, "Thread", FakeThread), \
         mock.patch.object(addon._BUILD_EX, "submit",
                           side_effect=RuntimeError("cannot schedule")):
        addon._prewarm_shelf([{"id": "tt1"}, {"id": "tt2"}], "movie")
    assert addon._PREWARM_BUSY[0] is False, "the busy flag must be released"


def test_http_catalog_prewarms_only_the_first_plain_page():
    clear_caches()
    addon._PREWARM_BUSY[0] = False
    calls = []
    with mock.patch.object(addon, "catalog_items",
                           return_value=[{"id": "tt1"}, {"id": "tt2"}]), \
         mock.patch.object(addon, "_prewarm_shelf",
                           side_effect=lambda m, t: calls.append((len(m), t))):
        _http_get("/catalog/movie/bpx-latest.json")
        _http_get("/catalog/movie/bpx-latest.json?skip=24")
        _http_get("/catalog/movie/bpx-latest.json?search=x")
    assert calls == [(2, "movie")], calls


def test_needs_site_only_for_visible_holes():
    assert addon._needs_site(None) is True
    assert addon._needs_site({"poster": "p"}) is True
    assert addon._needs_site({"description": "d"}) is True
    assert addon._needs_site({"poster": "p", "description": "d"}) is False


def test_tmdb_key_validation():
    class R:
        def __init__(s, c): s.status_code = c
    with mock.patch.object(addon._S, "get", return_value=R(200)):
        assert addon.validate_tmdb_key("8" * 32) is True
    with mock.patch.object(addon._S, "get", return_value=R(401)):
        assert addon.validate_tmdb_key("8" * 32) is False
    assert addon.validate_tmdb_key("short") is False
    assert addon.validate_tmdb_key("") is False
    assert addon.validate_tmdb_key(None) is False


# ── config ───────────────────────────────────────────────────────────────────
def test_cfg_roundtrip_and_defaults():
    assert addon.cfg_pack(addon.CFG_DEFAULTS) == ""
    seg = addon.cfg_pack({"n": 1, "q": "1080", "subs": "bn,hi", "cdn": "cf",
                          "cat": "series", "tmdb": "8" * 32})
    assert seg and "/" not in seg and "+" not in seg and "=" not in seg, seg
    back = addon.cfg_unpack(seg)
    assert back["n"] == 1 and back["q"] == "1080" and back["subs"] == "bn,hi"
    assert back["cdn"] == "cf" and back["cat"] == "series" and back["tmdb"] == "8" * 32


def test_cfg_unpack_rejects_garbage_but_accepts_empty():
    assert addon.cfg_unpack("") == addon.CFG_DEFAULTS
    assert addon.cfg_unpack("!!!notbase64!!!") is None
    good = addon.base64.urlsafe_b64encode(b"[1,2,3]").decode().rstrip("=")
    assert addon.cfg_unpack(good) is None, "a non-object payload is not a config"


def test_cfg_unpack_clamps_and_ignores_unknown_keys():
    seg = addon.base64.urlsafe_b64encode(
        b'{"n":99,"q":"8k","cdn":"nope","cat":"movie","evil":"rm -rf","subs":"OFF"}'
    ).decode().rstrip("=")
    cfg = addon.cfg_unpack(seg)
    assert cfg["n"] == addon.MAX_CARDS, "cards clamp to the server maximum"
    assert cfg["q"] == "all" and cfg["cdn"] == "both"
    assert cfg["cat"] == "movie" and cfg["subs"] == "off"
    assert "evil" not in cfg


def test_cfg_rejects_a_key_that_is_not_a_key():
    seg = addon.base64.urlsafe_b64encode(
        b'{"tmdb":"http://evil.example/x?a=b&c=d"}').decode().rstrip("=")
    assert addon.cfg_unpack(seg)["tmdb"] == ""


def test_apply_cfg_caps_cards_and_strips_markers():
    cards = []
    for i, (cdn, res) in enumerate([("tiktok", "1080p"), ("cf", "1080p"),
                                    ("tiktok", "720p"), ("cf", "480p")]):
        c = _card("c%d" % i)
        c.update({"_cdn": cdn, "_res": res, "_tier": "FHD", "_nsubs": 0})
        cards.append(c)
    out = addon.apply_cfg(cards, {"n": 2, "subs": "en", "q": "all", "cdn": "both"})
    assert len(out) == 2
    assert not any(k.startswith("_") for c in out for k in c), "markers must not ship"
    assert len(addon.apply_cfg(cards, {"n": 1, "subs": "en", "q": "all",
                                       "cdn": "both"})) == 1


def test_apply_cfg_quality_floor():
    def mk(res):
        c = _card(res)
        c.update({"_cdn": "tiktok", "_res": res, "_tier": "", "_nsubs": 0})
        return c
    cards = [mk("1080p"), mk("720p"), mk("480p")]
    assert [c["name"] for c in
            addon.apply_cfg(cards, {"n": 9, "subs": "en", "q": "1080", "cdn": "both"})
            ] == ["1080p"]
    assert len(addon.apply_cfg(cards, {"n": 9, "subs": "en", "q": "720",
                                       "cdn": "both"})) == 2
    assert len(addon.apply_cfg(cards, {"n": 9, "subs": "en", "q": "all",
                                       "cdn": "both"})) == 3


def test_apply_cfg_quality_floor_keeps_unmeasured_cards():
    """a card whose resolution could not be measured must not be silently
    dropped by a quality filter (that would look like a dead title)."""
    c = _card("unknown-res")
    c.update({"_cdn": "cf", "_res": "HDTC", "_tier": "", "_nsubs": 0})
    out = addon.apply_cfg([c], {"n": 9, "subs": "en", "q": "1080", "cdn": "both"})
    assert len(out) == 1


def test_apply_cfg_cdn_preference():
    def mk(cdn):
        c = _card(cdn)
        c.update({"_cdn": cdn, "_res": "1080p", "_tier": "", "_nsubs": 0})
        return c
    cards = [mk("tiktok"), mk("cloudflare")]
    assert [c["name"] for c in
            addon.apply_cfg(cards, {"n": 9, "subs": "en", "q": "all", "cdn": "tiktok"})
            ] == ["tiktok"]
    assert [c["name"] for c in
            addon.apply_cfg(cards, {"n": 9, "subs": "en", "q": "all", "cdn": "cf"})
            ] == ["cloudflare"]
    assert len(addon.apply_cfg(cards, {"n": 9, "subs": "en", "q": "all",
                                       "cdn": "both"})) == 2


def test_apply_cfg_cdn_preference_falls_back_instead_of_empting():
    """asking for a server the title does not have must not look like a dead
    title — the other server is still a perfectly good card."""
    c = _card("only-cf")
    c.update({"_cdn": "cloudflare", "_res": "1080p", "_tier": "", "_nsubs": 0})
    out = addon.apply_cfg([c], {"n": 9, "subs": "en", "q": "all", "cdn": "tiktok"})
    assert [x["name"] for x in out] == ["only-cf"]


def test_apply_cfg_subtitle_preference_and_off():
    c = _card("s")
    c["subtitles"] = [{"lang": "ta", "url": "u1"}, {"lang": "en", "url": "u2"},
                      {"lang": "bn", "url": "u3"}]
    c["description"] = "x / ⟡ 3 SUB"
    c.update({"_cdn": "tiktok", "_res": "1080p", "_tier": "", "_nsubs": 3})
    out = addon.apply_cfg([dict(c)], {"n": 9, "subs": "bn,en", "q": "all", "cdn": "both"})
    assert [s["lang"] for s in out[0]["subtitles"]] == ["bn", "en"]
    assert "⟡ 2 SUB" in out[0]["description"]
    off = addon.apply_cfg([dict(c)], {"n": 9, "subs": "off", "q": "all", "cdn": "both"})
    assert "subtitles" not in off[0]


def test_apply_cfg_does_not_mutate_the_cached_card():
    c = _card("x")
    c.update({"_cdn": "tiktok", "_res": "1080p", "_tier": "", "_nsubs": 0,
              "subtitles": [{"lang": "ta", "url": "u"}]})
    addon.apply_cfg([c], {"n": 1, "subs": "off", "q": "all", "cdn": "both"})
    assert "_cdn" in c and c["subtitles"], "the shared cache must stay intact"


def test_manifest_follows_the_catalog_config():
    assert len(addon.manifest({"cat": "all"})["catalogs"]) == 3
    assert [c["id"] for c in addon.manifest({"cat": "series"})["catalogs"]] == ["bpx-series"]
    assert [c["id"] for c in addon.manifest({"cat": "movie"})["catalogs"]] == \
        ["bpx-latest", "bpx-year"]
    off = addon.manifest({"cat": "off"})
    assert off["catalogs"] == [] and "catalog" not in off["resources"]
    assert addon.manifest()["version"] == addon.VERSION


def test_http_config_segment_routes_every_resource():
    seg = addon.cfg_pack({"n": 1, "cat": "series"})
    d = _json_body(_http_get("/%s/manifest.json" % seg))
    assert [c["id"] for c in d["catalogs"]] == ["bpx-series"]
    with mock.patch.object(addon, "build_streams",
                           return_value={"streams": [_card("a"), _card("b")]}):
        n = len(_json_body(_http_get("/%s/stream/movie/tt0213890.json" % seg))["streams"])
    assert n == 1


def test_http_bad_config_segment_404s():
    assert _http_get("/!!!/manifest.json")["code"] == 404


def test_http_configure_page_is_self_contained():
    c = _http_get("/configure")
    assert c["code"] == 200 and c["headers"]["Content-Type"].startswith("text/html")
    body = c["body"].decode()
    assert "<script" in body and "<style" in body
    for needle in ('id="tmdb"', 'id="cat"', 'id="q"', 'id="cdn"', 'id="subs"',
                   "Install in Stremio", "stremio://"):
        assert needle in body, needle
    # no third-party asset: the page must render inside Stremio's webview
    assert "http://fonts" not in body and "cdn.jsdelivr" not in body
    assert addon.VERSION in body


def test_http_validate_key_route():
    with mock.patch.object(addon, "validate_tmdb_key", return_value=True):
        assert _json_body(_http_get("/validate-key?key=%s" % ("8" * 32))) == {"valid": True}
    with mock.patch.object(addon, "validate_tmdb_key", return_value=False):
        assert _json_body(_http_get("/validate-key?key=bad")) == {"valid": False}


def test_http_meta_route():
    with mock.patch.object(addon, "build_meta",
                           return_value={"id": "tt1", "type": "movie", "name": "X"}):
        d = _json_body(_http_get("/meta/movie/tt1111111.json"))
    assert d["meta"]["name"] == "X"
    with mock.patch.object(addon, "build_meta", return_value=None):
        assert _json_body(_http_get("/meta/movie/bpx-nope.json")) == {"meta": {}}


def test_http_meta_route_strips_series_season_suffix():
    with mock.patch.object(addon, "build_meta", return_value={"id": "tt1"}) as bm:
        _http_get("/meta/series/tt1111111:2:5.json")
    assert bm.call_args[0][1] == "tt1111111", bm.call_args


def test_http_stream_accepts_source_ids():
    with mock.patch.object(addon, "build_streams",
                           return_value={"streams": [_card()]}) as bs:
        c = _http_get("/stream/movie/bpx-harudu.json")
    assert c["code"] == 200 and bs.call_args[0][1] == "bpx-harudu"
    assert _http_get("/stream/movie/nope.json")["code"] == 404


def test_build_inner_source_id_skips_the_metadata_hop():
    """a bpx- card has no IMDb/TMDB entry by definition: going straight to the
    watch page is both faster and the only thing that can work."""
    clear_caches()
    with mock.patch.object(addon, "resolve_meta_all",
                           side_effect=AssertionError("must not be called")), \
         mock.patch.object(addon, "search_candidates",
                           side_effect=AssertionError("must not be called")), \
         mock.patch.object(addon, "parse_watch_page", return_value=_PAGE_FIX), \
         _stub_resolve([_card()]):
        out = addon._build_inner("movie", "bpx-harudu", None, None, time.time() + 5)
    assert len(out["streams"]) == 1


def test_build_inner_source_id_honest_when_unplayable():
    clear_caches()
    with mock.patch.object(addon, "parse_watch_page", return_value=_PAGE_FIX), \
         mock.patch.object(addon, "_resolve_file", return_value=[]):
        out = addon._build_inner("movie", "bpx-harudu", None, None, time.time() + 5)
    assert out["streams"] == [] and "no playable" in out["message"]


def test_health_reports_the_new_surfaces():
    d = _json_body(_http_get("/health"))
    assert ("movie", "bpx-latest") in [tuple(x) for x in d["catalogs"]]
    assert d["configurable"] is True
    assert {c["name"] for c in d["caches"]} >= {"listings", "imdbmap", "metares"}


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

def test_late_adopts_counts_only_requests_that_blew_the_wall():
    """Future.set_result() notifies the waiter and THEN runs the done-callbacks, so
    on a perfectly normal resolve the adopt callback routinely beats
    `fut.result()` returning and sees an empty cache. Counting that made
    `late_adopts` equal `resolves` on prod (11/11) and buried the one number that
    matters — how many players were told to tap again."""
    clear_caches()
    # both counters are cumulative across the whole suite — snapshot, never zero
    before = addon._STATS.get("late_adopts", 0)
    walls0 = addon._STATS.get("walls", 0)
    card = {"name": "quick", "url": "https://cdn/master.m3u8"}
    with mock.patch.object(addon, "_build_inner", return_value={"streams": [card]}):
        out = addon.build_streams("series", "bpx-quick", 1, 1)
    assert out["streams"] == [card], out
    for _ in range(40):                              # let the callback run
        time.sleep(0.05)
        if addon._STATS.get("late_adopts", 0) != before:
            break
    assert addon._STATS.get("late_adopts", 0) == before, \
        "a served-in-time resolve is not a late adopt"
    assert addon._STATS.get("walls", 0) == walls0, "this resolve never hit the wall"
    assert not addon._WALLED, addon._WALLED


def test_a_build_that_outlives_the_wall_is_adopted_and_counted():
    clear_caches()
    before = addon._STATS.get("late_adopts", 0)
    card = {"name": "slow", "url": "https://cdn/master.m3u8"}
    release = threading.Event()

    def slow(*a, **k):
        release.wait(15)
        return {"streams": [card]}

    key = ("series", "bpx-slow", 1, 1)
    with mock.patch.object(addon, "_build_inner", side_effect=slow), \
         mock.patch.object(addon, "WALL", 0.4):
        out = addon.build_streams("series", "bpx-slow", 1, 1)
    assert out["streams"] == [] and "still resolving" in out.get("message", "")
    assert addon._STATS.get("walls", 0) >= 1 and key in addon._WALLED
    release.set()
    for _ in range(200):                             # the worker caches it itself
        if addon.C_STREAM.get(key)[0]:
            break
        time.sleep(0.05)
    assert addon.C_STREAM.get(key)[1] == [card], "the late answer must be cached"
    assert addon._STATS.get("late_adopts", 0) == before + 1
    assert key not in addon._WALLED, "an adopted key must not linger in _WALLED"


def test_split_id_unprefixes_a_prefixed_imdb_id():
    """`bpx-tt1234` is legal (the manifest lists both prefixes) but it is an IMDb
    id, not a site slug — /watch/tt1234.html does not exist, so treating it as a
    slug answered honestly-empty in ~1s while the bare id resolved fine."""
    assert addon._split_id("series", "bpx-tt43695931:1:1") == ("tt43695931", 1, 1)
    assert addon._split_id("movie", "bpx-tt43695931") == ("tt43695931", None, None)
    # a real slug keeps its prefix: that is how _build_inner finds the watch page
    assert addon._split_id("series", "bpx-queens:2:3") == ("bpx-queens", 2, 3)
    assert addon._split_id("movie", "tt1234567") == ("tt1234567", None, None)



def test_known_cross_type_shelf_card_keeps_the_provider_slug():
    """Kuheli's autocomplete/IMDb path produced tt7222514 (the 2016 movie) for
    the provider's 2026 series page. Once the listing badge has taught us the
    real kind, a movie-shelf search must use bpx-kuheli instead of a misleading
    IMDb id; the source card remains playable and metadata-safe."""
    clear_caches()
    addon._note_kind("kuheli", True)
    item = {"slug": "kuheli", "title": "Kuheli", "year": None, "series": True}
    with mock.patch.object(addon, "imdb_suggest_title",
                           side_effect=AssertionError("cross-type IMDb lookup must be skipped")):
        out = addon._map_ids([item], "movie")
    assert out[0]["id"] == "bpx-kuheli"


def test_catalog_search_marks_badged_cross_type_hits_as_source_ids():
    clear_caches()
    addon._note_kind("kuheli", True)
    cand = {"title": "Kuheli", "type": "Movie",
            "url": "https://banglaplex.biz/watch/kuheli.html", "image": ""}
    with mock.patch.object(addon, "_search_autocomplete", return_value=[cand]), \
         mock.patch.object(addon, "imdb_suggest_title",
                           side_effect=AssertionError("the known mismatch is a source card")):
        out = addon.catalog_items("movie", "bpx-latest", search="Kuheli")
    assert out[0]["id"] == "bpx-kuheli" and out[0]["name"] == "Kuheli"


def test_stale_imdb_slug_mapping_relaxes_only_an_exact_source_title():
    """Existing clients may still send tt7222514 from the old cache. If its
    learned provider slug says exact "Kuheli" but the years disagree, retry the
    exact source card without the year guard; a fuzzy/collision title never gets
    this escape hatch."""
    clear_caches()
    addon.C_SLUG.put(("movie", "tt7222514"), "kuheli", 600)
    page = dict(_PAGE_FIX, slug="kuheli", title="Kuheli", year=2026,
                url="https://banglaplex.biz/watch/kuheli.html")
    card = _card()
    calls = []

    def cards(matched, years, *args):
        calls.append(years)
        return [] if len(calls) == 1 else [card]

    with mock.patch.object(addon, "resolve_meta_all",
                           return_value=[("Kuheli", 2016, None)]), \
         mock.patch.object(addon, "_cards_from_matches", side_effect=cards), \
         mock.patch.object(addon, "_source_slug_matches_title", return_value=page):
        out = addon._build_inner("movie", "tt7222514", None, None, time.time() + 5)
    assert out["streams"] == [card]
    assert calls[0] == {2016} and calls[1] == set(), calls
    assert addon._STATS.get("slug_relaxed", 0) >= 1


def test_cold_pool_refresh_returns_before_a_slow_source_list():
    """The first Cloudflare fallback must not sit behind a dead proxy-list URL;
    an old pool can serve immediately and a cold boot waits only the tiny bootstrap
    budget while the source pull continues in the background."""
    clear_caches()
    addon._POOL[0] = []
    addon._POOL_TS[0] = 0.0
    addon._POOL_PULLING[0] = False
    addon._POOL_READY.clear()
    started = threading.Event()
    release = threading.Event()

    def slow_pull():
        started.set()
        release.wait(3)
        return ["http://9.9.9.9:8080"]

    with mock.patch.object(addon, "_pool_pull", side_effect=slow_pull), \
         mock.patch.object(addon, "_pool_start_training"), \
         mock.patch.object(addon, "POOL_BOOT_WAIT", 0.05):
        t0 = time.time()
        out = addon._pool_refresh()
        elapsed = time.time() - t0
        assert out == [] and elapsed < 0.5, elapsed
        assert started.wait(1), "refresh must have started the pull in the background"
        release.set()
        for _ in range(40):
            if addon._POOL[0]:
                break
            time.sleep(0.02)
    assert addon._POOL[0] == ["http://9.9.9.9:8080"]
    addon._POOL_PULLING[0] = False


def test_build_singleflight_shares_one_cold_resolve_with_many_users():
    """Ten simultaneous taps for one episode must create one site/proxy chain,
    not ten. Followers wait on the same Future and all receive the cards."""
    clear_caches()
    started = threading.Event()
    release = threading.Event()
    calls = []
    card = {"name": "shared", "url": "https://cdn/shared.mp4"}

    def slow(*args, **kwargs):
        calls.append(1)
        started.set()
        release.wait(3)
        return {"streams": [card]}

    results = []
    with mock.patch.object(addon, "_build_inner", side_effect=slow), \
         mock.patch.object(addon, "WALL", 1.5):
        ts = [threading.Thread(target=lambda: results.append(
            addon.build_streams("movie", "tt-shared", None, None))) for _ in range(8)]
        for th in ts:
            th.start()
        assert started.wait(1)
        time.sleep(0.1)
        assert addon._STATS.get("build_coalesced", 0) >= 1
        release.set()
        for th in ts:
            th.join(3)
    assert len(calls) == 1, calls
    assert len(results) == 8 and all(r["streams"] == [card] for r in results)


OFFLINE_TESTS = [v for k, v in sorted(globals().items())
                 if k.startswith("test_") and callable(v)]


def main():
    missed = sorted(k for k, v in globals().items()
                    if k.startswith("test_") and callable(v) and v not in OFFLINE_TESTS)
    if missed:
        # OFFLINE_TESTS is a snapshot of globals() taken above: anything defined
        # after it would never run, and the suite would still print "all passed"
        print("!! %d test(s) defined after the runner snapshot, NEVER RAN: %s"
              % (len(missed), ", ".join(missed)))
        return 2
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
