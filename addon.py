#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BanglaPlex — Stremio addon (stream-only, strict zero-bandwidth)
===============================================================
Site      : https://banglaplex.biz            (OVOO Movie/TV CMS, CodeIgniter, Cloudflare)
Player    : plextream.work/embed.php?id={eid} -> server buttons
            -> bpx.strp2p.site/#{vid} | bpx.rpmvid.site/#{vid}   ("3n1" frontend family)
            -> abyssplayer.com/{code}                            (SoTrym, NOT supported yet)
Media API : GET https://{n1host}/api/v1/video?id={vid}
            -> body = hex(AES-128-CBC(json))   key=kiemtienmua911ca iv=1234567890oiuytr
            -> {hlsVideoTiktok, cfNative, cf, source, subtitle{}, title, streamingConfig, ...}

Zero-bandwidth contract
-----------------------
The addon serves JSON only. Cards point DIRECTLY at the player frontend's own
playlist URLs:
  * TikTok path   : master+variant served by {n1host} (KB of text), segments are
                    ABSOLUTE https://p16-*.tiktokcdn.com URLs (x-expires ~1 year,
                    no Referer, Range/206 verified)  -> no headers needed at all.
  * Cloudflare    : master+variant via {n1host}/v4/pl/... , segments on the cf
                    domain need `Referer: https://{n1host}/` -> proxyHeaders card.
  * In-house /v4/ : raw-IP master, Referer needed -> last-resort card.
No media byte ever transits this server. Subtitles are direct {n1host} VTT URLs.

Design laws inherited from the sister addons (moviebox / animedekho / netmirror):
  * NO PHANTOM CARDS — a card is emitted only after master+variant-head+segment
    were verified (200/206) from this process.
  * first-card-answer (as_completed) + deadline inheritance + honest negatives
    (None = transient, never cached; [] = "not there", short TTL) + SWR.
  * byte-budgeted TTL caches with sweep, keep-alive + liveness watchdog,
    runtime log ring (no stderr writes — a stalled log pipe once froze a dyno).
"""

import base64
import gzip
import html as _html
import io
import json
import os
import re
import socket
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, unquote, urljoin, urlparse, parse_qs

import requests

# ═══════════════════════════════════════════════════════════════════ 1. CONFIG
VERSION    = "1.3.1"
BRAND      = "BanglaPlex"
ADDON_NAME = "BanglaPlex"
SITE       = os.environ.get("BPX_SITE", "https://banglaplex.biz").rstrip("/")
EMBED_HOST = "plextream.work"
N1_HOSTS   = ("bpx.strp2p.site", "bpx.rpmvid.site")   # 3n1 frontends (same software)
ABYSS_HOST = "abyssplayer.com"                        # SoTrym — unsupported (honest skip)
LEGACY_PLAYERS = ("bestx.stream", "chillx.top")       # dead TLS on old catalog entries

# 3n1 AES-128-CBC constants (reversed from the frontend bundle, verified live)
AES_KEY = b"kiemtienmua911ca"
AES_IV  = b"1234567890oiuytr"

PORT       = int(os.environ.get("PORT", "7055"))
PUBLIC_URL = os.environ.get("BPX_PUBLIC_URL", "").rstrip("/")
TMDB_KEY   = os.environ.get("TMDB_API_KEY", "adc48d20c0956934fb224de5c40bb85d")
DEBUG_KEY  = os.environ.get("BPX_DEBUG_KEY", "bpx-dbg-4c9e")
CINEMETA   = "https://v3-cinemeta.strem.io"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

WALL           = float(os.environ.get("BPX_WALL", "22"))   # player-facing answer wall
N1_LANE_GAP    = 1.15      # min seconds between /api/v1/* calls per frontend host
N1_BACKOFF     = 3.5       # one retry after a 429 before benching the host
N1_COOLDOWN    = 25.0      # host cooldown after a 429
MAX_CARDS      = int(os.environ.get("BPX_MAX_CARDS", "3"))
MAX_SUBS       = int(os.environ.get("BPX_MAX_SUBS", "6"))
HLS_ON         = os.environ.get("BPX_CARDS", "1") != "0"   # kill switch
META_WAIT      = 3.0       # metadata consensus window
MAX_META_CANDS = 4         # distinct titles _build_inner will walk
INHOUSE_ON     = os.environ.get("BPX_INHOUSE", "0") == "1" # ip-bound; debug only

# cache TTLs ------------------------------------------------------------
_SEARCH_TTL   = 3600
_PAGE_TTL     = 6 * 3600
_META_TTL     = 12 * 3600
_EMBED_TTL    = 6 * 3600
_N1_TTL       = 30 * 60        # video payload (tiktok urls live ~1y, cf k/kx ~24h)
_STREAM_TTL   = 40 * 60
_STREAM_STALE = 100 * 60       # SWR ceiling (cf token ~24h, tiktok ~1y)
_NEG_TTL      = 300
_CACHE_SWEEP_AT   = 384
_STALE_SWEEP_AT   = 192
_NEG_RETRY_CD     = 120.0      # background re-resolve throttle for cached empties

MANIFEST = {
    "id": "com.banglaplex.stremio",
    "version": VERSION,
    "name": ADDON_NAME,
    "description": ("Stream-only addon for BanglaPlex — Bangla / Hindi / Hollywood "
                    "movies & web-series, direct CDN streams (TikTok-CDN HLS, "
                    "multi-language VTT subtitles). Zero-bandwidth: only tiny JSON "
                    "is served, media flows straight from the CDN to your player."),
    "logo": SITE + "/uploads/system_logo/logo_619305d7d016f.png",
    "background": SITE + "/assets/images/default_bg.jpg",
    "types": ["movie", "series"],
    "idPrefixes": ["tt", "bpx-"],
    "resources": ["stream", "subtitles", "catalog", "meta"],
    "catalogs": [],                 # filled in section 13b once CAT_DEFS exists
    "behaviorHints": {"configurable": True, "configurationRequired": False},
}


def manifest(cfg=None):
    """Per-install manifest: the catalog list follows the `cat` config so a user
    who only wants streams does not get BanglaPlex shelves in their board."""
    cfg = cfg or CFG_DEFAULTS
    man = dict(MANIFEST)
    man["version"] = VERSION
    want = cfg.get("cat") or "all"
    if want == "off":
        man["catalogs"] = []
        man["resources"] = [r for r in MANIFEST["resources"] if r != "catalog"]
    elif want in ("movie", "series"):
        man["catalogs"] = [c for c in CAT_DEFS if c["type"] == want]
    else:
        man["catalogs"] = list(CAT_DEFS)
    return man

# ═══════════════════════════════════════════════════════════════ 2. UTILITIES
class TTLCache(dict):
    """Byte-budgeted TTL cache (multimovies v2.5.0 lesson: an entry-count cap
    lets 1-3MB HTML pages blow past Render's 512MB)."""

    def __init__(self, budget=8 * 1024 * 1024, name="?"):
        super().__init__()
        self.budget = budget
        self.name = name
        self.bytes = 0
        self.lock = threading.Lock()

    @staticmethod
    def _size(v):
        try:
            return len(v) if isinstance(v, (str, bytes)) else len(repr(v))
        except Exception:
            return 256

    def put(self, key, val, ttl):
        ent = (time.time() + ttl, val)
        add = self._size(ent)
        with self.lock:
            old = self.get(key)
            if old is not None:
                self.bytes -= self._size(old)
            self[key] = ent
            self.bytes += add
            if self.bytes > self.budget:
                for k in sorted(self, key=lambda k: self[k][0])[:8]:
                    self.bytes -= self._size(self.pop(k))

    def get(self, key):
        ent = super().get(key)
        if ent is None:
            return False, None
        exp, val = ent
        if exp < time.time():
            with self.lock:
                cur = super().get(key)
                if cur is not None:
                    self.bytes -= self._size(cur)
                    self.pop(key, None)
            return False, None
        return True, val

    def stats(self):
        return {"name": self.name, "entries": len(self), "bytes": self.bytes,
                "budget": self.budget}


C_SEARCH  = TTLCache(6 * 1024 * 1024, "search")
C_PAGE    = TTLCache(24 * 1024 * 1024, "pages")
C_META    = TTLCache(4 * 1024 * 1024, "meta")
C_EMBED   = TTLCache(2 * 1024 * 1024, "embeds")
C_N1      = TTLCache(8 * 1024 * 1024, "n1")
C_STREAM  = TTLCache(12 * 1024 * 1024, "streams")
C_LIST    = TTLCache(8 * 1024 * 1024, "listings")
C_IMDB    = TTLCache(2 * 1024 * 1024, "imdbmap")
C_METARES = TTLCache(6 * 1024 * 1024, "metares")
C_SLUG    = TTLCache(1 * 1024 * 1024, "slugmap")
C_STALE   = {}                                    # key -> (expiry, cards)
_NEG_RETRY_AT = {}
_SWR_RUNNING = set()
_SWR_LOCK = threading.Lock()

_IO_EX   = ThreadPoolExecutor(max_workers=12, thread_name_prefix="io")
_V_EX    = ThreadPoolExecutor(max_workers=6, thread_name_prefix="verify")
_P_EX    = ThreadPoolExecutor(max_workers=10, thread_name_prefix="proxy")
_PP_EX   = ThreadPoolExecutor(max_workers=16, thread_name_prefix="poolprobe")
_BUILD_EX = ThreadPoolExecutor(max_workers=2, thread_name_prefix="build")

_S = requests.Session()
_S.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})

_REQLOG = []
_REQLOG_LOCK = threading.Lock()
_STATS = {"started": time.time(), "resolves": 0, "cards": 0, "empties": 0,
          "n1_calls": 0, "n1_429": 0, "relay_bytes": 0, "slug_hits": 0}


def _log(entry):
    with _REQLOG_LOCK:
        _REQLOG.append(entry)
        if len(_REQLOG) > 120:
            del _REQLOG[:60]


def _sweep():
    if len(C_STALE) >= _STALE_SWEEP_AT:
        now = time.time()
        for k in [k for k, v in C_STALE.items() if v[0] < now]:
            C_STALE.pop(k, None)


def _fmt_dur(mins):
    try:
        m = int(float(mins))
    except Exception:
        return ""
    if m <= 0:
        return ""
    return "%dh%02dm" % (m // 60, m % 60) if m >= 60 else "%dm" % m


def _res_label(w, h):
    """(width,height) -> ('FHD','1080p') house card-spec tokens.
    WIDTH drives the tier: BanglaPlex files are scope-cropped, so a real 1080p
    encode arrives as 1920x800 and a 720p one as 1280x532. Height-only bucketing
    mislabels every one of them (measured live: 1920x800 -> '720p', 1280x584 ->
    '480p')."""
    try:
        w = int(w or 0)
        h = int(h or 0)
    except Exception:
        return "", ""
    if w >= 7000 or h >= 4000:
        return "8K", "4320p"
    if w >= 3400 or h >= 2000:
        return "4K", "2160p"
    if w >= 1800 or h >= 1000:
        return "FHD", "1080p"
    if w >= 1200 or h >= 680:
        return "HD", "720p"
    if w >= 840 and h >= 460:
        return "SD", "480p"
    if w >= 600 or h >= 330:
        return "SD", "360p"
    return "SD", ("%dp" % h) if h else ""


def _norm_title(t):
    t = _html.unescape(t or "").lower()
    t = re.sub(r"\((?:19|20)\d{2}\)", " ", t)
    t = re.sub(r"[^\w\s&]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


# site slugs/titles carry release-tag noise; strip it for matching
_JUNK_WORDS = {
    "download", "downloads", "watch", "online", "free", "full", "movie", "movies",
    "web", "series", "webseries", "hd", "hdrip", "webrip", "webdl", "web", "hindi",
    "bangla", "bengali", "english", "dubbed", "dual", "audio", "esub", "sub", "subs",
    "1080p", "720p", "480p", "2160p", "4k", "x264", "x265", "hevc", "avc", "aac",
    "mkv", "mp4", "skymovieshd", "downloadhub", "ms", "lat", "hq", "hdtc", "cam",
    "proper", "complete", "season", "episodes", "episode", "ep", "s", "the", "of",
    "in", "on", "at", "to", "and", "or", "part", "bluray", "brrip", "dvdrip",
}


def _clean_tokens(t):
    """title -> meaningful token list (junk release-tags removed)."""
    out = []
    for w in _norm_title(t).split():
        if w in _JUNK_WORDS or len(w) < 2:
            continue
        if re.fullmatch(r"(?:19|20)\d{2}", w):
            continue
        if re.fullmatch(r"s\d{1,2}|e\d{1,3}|\d{3,4}p|\d+", w):
            continue
        out.append(w)
    return out


def _year_of(text):
    m = re.search(r"(?:19|20)\d{2}", text or "")
    return int(m.group(0)) if m else None


# ══════════════════════════════════════════════════════ 3. PURE-PYTHON AES-128
# (no crypto deps on Render; the 3n1 API answers hex(AES-128-CBC(json)))
_EXP = [0] * 512
_LOG = [0] * 256


def _init_gf():
    """log/antilog tables for GF(2^8) with the AES polynomial 0x11B.
    The generator MUST be 3 (2 only has order 51 here — classic footgun)."""
    x = 1
    for i in range(255):
        _EXP[i] = x
        _LOG[x] = i
        xt = x << 1                                  # xtime(x) = x*2
        if xt & 0x100:
            xt = (xt ^ 0x11B) & 0xFF
        x = xt ^ x                                   # x*3 -> order 255
    for i in range(255, 512):
        _EXP[i] = _EXP[i - 255]


_init_gf()


def _gmul(a, b):
    if a == 0 or b == 0:
        return 0
    return _EXP[_LOG[a] + _LOG[b]]


def _build_sbox():
    sb = [0] * 256
    for i in range(256):
        b = 0 if i == 0 else _EXP[255 - _LOG[i]]
        s = b
        for _ in range(4):
            b = ((b << 1) | (b >> 7)) & 0xFF
            s ^= b
        sb[i] = s ^ 0x63
    return sb


_SBOX = _build_sbox()
_INV_SBOX = [0] * 256
for _i, _v in enumerate(_SBOX):
    _INV_SBOX[_v] = _i
_RCON = [0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36]


def _round_keys(key):
    w = [list(key[4 * i:4 * i + 4]) for i in range(4)]
    for i in range(4, 44):
        t = list(w[i - 1])
        if i % 4 == 0:
            t = t[1:] + t[:1]
            t = [_SBOX[b] for b in t]
            t[0] ^= _RCON[i // 4 - 1]
        w.append([w[i - 4][j] ^ t[j] for j in range(4)])
    # state is column-major: block[i] -> state[i%4][i//4]
    rks = []
    for r in range(11):
        cols = w[4 * r:4 * r + 4]
        rks.append([b for c in range(4) for b in cols[c]])
    return rks


def _xor(a, b):
    return [x ^ y for x, y in zip(a, b)]


def _inv_shift_rows(s):
    # s is a flat 16-byte list in column-major order (index = 4*col + row).
    # row r rotates RIGHT by r:  new[r][c] = old[r][(c - r) mod 4]
    out = [0] * 16
    for r in range(4):
        for c in range(4):
            out[4 * c + r] = s[4 * ((c - r) % 4) + r]
    return out


def _inv_sub_bytes(s):
    return [_INV_SBOX[b] for b in s]


def _inv_mix_columns(s):
    out = [0] * 16
    for c in range(4):
        a = s[4 * c:4 * c + 4]
        out[4 * c + 0] = _gmul(a[0], 14) ^ _gmul(a[1], 11) ^ _gmul(a[2], 13) ^ _gmul(a[3], 9)
        out[4 * c + 1] = _gmul(a[0], 9) ^ _gmul(a[1], 14) ^ _gmul(a[2], 11) ^ _gmul(a[3], 13)
        out[4 * c + 2] = _gmul(a[0], 13) ^ _gmul(a[1], 9) ^ _gmul(a[2], 14) ^ _gmul(a[3], 11)
        out[4 * c + 3] = _gmul(a[0], 11) ^ _gmul(a[1], 13) ^ _gmul(a[2], 9) ^ _gmul(a[3], 14)
    return out


_RK_CACHE = {}


def _rks():
    k = _RK_CACHE.get("k")
    if k is None:
        k = _round_keys(AES_KEY)
        _RK_CACHE["k"] = k
    return k


def _decrypt_block(blk, rks):
    s = _xor(list(blk), rks[10])
    for rnd in range(9, 0, -1):
        s = _inv_shift_rows(s)
        s = _inv_sub_bytes(s)
        s = _xor(s, rks[rnd])
        s = _inv_mix_columns(s)
    s = _inv_shift_rows(s)
    s = _inv_sub_bytes(s)
    return bytes(_xor(s, rks[0]))


def aes_cbc_decrypt(data, key=None, iv=None):
    """AES-128-CBC decrypt + PKCS#7 unpad. Returns bytes (b'' on bad padding)."""
    key = key or AES_KEY
    iv = iv or AES_IV
    if len(data) == 0 or len(data) % 16:
        return b""
    rks = _round_keys(key) if key != AES_KEY else _rks()
    out = bytearray()
    prev = list(iv)
    for i in range(0, len(data), 16):
        blk = list(data[i:i + 16])
        dec = _decrypt_block(blk, rks)
        out.extend(_xor(dec, prev))
        prev = blk
    pad = out[-1] if out else 0
    if not isinstance(pad, int) or pad < 1 or pad > 16 or pad > len(out):
        return bytes(out)
    if bytes(out[-pad:]) != bytes([pad]) * pad:
        return bytes(out)
    return bytes(out[:-pad])


def n1_decrypt(hexstr):
    """hex ciphertext -> parsed JSON (or None)."""
    h = (hexstr or "").strip()
    if not h or len(h) % 32:
        return None
    try:
        raw = bytes.fromhex(h)
    except ValueError:
        return None
    try:
        plain = aes_cbc_decrypt(raw)
        return json.loads(plain.decode("utf-8", "replace"))
    except Exception:
        return None


# ════════════════════════════════════════════════════════════ 4. HTTP HELPERS
# Render's Singapore egress is Cloudflare-flagged on banglaplex.biz: the JSON
# autocomplete answers 200-but-empty while /watch/ and /search/ come back 403.
# Same calls from a normal IP are fine. So every site fetch adapts: try direct,
# and the moment a host proves blocked from here, ride the free proxy pool
# (house pattern — MovieBox §19). Nothing media-bearing ever goes through a
# proxy: cards still point the player straight at the CDN.
POOL_SRCS = [s.strip() for s in os.environ.get(
    "BPX_PROXY_SOURCE",
    "https://api.proxyscrape.com/v4/free-proxy-list/get?request=display_proxies"
    "&proxy_type=http&proxy_format=protocolipport&format=text").split(",") if s.strip()]
# hand-picked exits (paid/reliable) always ride first and are never evicted by
# the free-list trainer: BPX_PROXY_LIST="http://user:pass@host:port,http://h2:p2"
POOL_MANUAL = [s.strip() for s in os.environ.get("BPX_PROXY_LIST", "").split(",")
               if s.strip()]
POOL_ON   = os.environ.get("BPX_PROXY", "auto") != "0"
POOL_TTL  = 300.0          # re-pull the source list every 5 min
POOL_MAX  = int(os.environ.get("BPX_POOL_MAX", "20"))   # trained exits kept
POOL_CAND = 90             # candidates pulled from the sources per refresh
POOL_TRY  = int(os.environ.get("BPX_PROXY_TRY", "5"))   # exits raced per fetch
POOL_TO   = 8.0            # per-exit connect/read timeout (free proxies are slow)
PROBE_TO  = 6.0            # training probe timeout
PROBE_WAVE = 45            # wave size: publish as soon as wave 1 lands
DIRECT_BLOCK_TTL = 600.0   # host benched for direct egress after a 403/503

_HOST_BLOCK = {}
_HOST_BLOCK_LOCK = threading.Lock()
_POOL = [[]]               # current members (fastest/trained first)
_POOL_TS = [0.0]           # last source pull
_POOL_LOCK = threading.Lock()
_POOL_BAD = {}             # exit -> benched-until
# trained records: exit -> {"ok": n, "fail": n, "lat": EWMA ms, "blocked": n}
_POOL_STATS = {}
_STICKY = [None, 0.0]      # ride one good exit for 90s (a chain, not a dice roll)
_STICKY_BUSY = {}          # exit -> in-flight count (a sticky exit caps at 2)
_DIRECT_BAD = {}           # host -> direct egress benched until
_TRAINING = [False]        # one background trainer at a time
_PROBE_URL = SITE + "/home/autocompleteajax?term=a"     # cheap (~360B) real-work probe


def _bench(host, secs):
    with _HOST_BLOCK_LOCK:
        _HOST_BLOCK[host] = time.time() + secs


def _blocked_left(host=None):
    now = time.time()
    with _HOST_BLOCK_LOCK:
        if host:
            return max(0.0, _HOST_BLOCK.get(host, 0.0) - now)
        vals = [v - now for v in _HOST_BLOCK.values()]
        return max([0.0] + vals)


def _pool_pull():
    """Free-list sources -> deduped http(s) candidates.

    socks4/socks5 exits need PySocks; without it requests raises InvalidSchema,
    so every such exit looks "dead" (measured on prod: 40/40 exits were socks4
    and the whole pool was unusable)."""
    urls = list(POOL_MANUAL)
    for src in POOL_SRCS:
        try:
            r = requests.get(src, timeout=12, headers={"User-Agent": UA})
            for ln in (r.text or "").splitlines():
                ln = ln.strip().strip(",")
                if not ln or ln.startswith("#") or ":" not in ln:
                    continue
                if "://" not in ln:
                    ln = "http://" + ln
                if ln.split("://")[0] not in ("http", "https"):
                    continue
                urls.append(ln)
        except Exception:
            continue                      # one dead source must not kill the pull
    return list(dict.fromkeys(urls))[:POOL_CAND]


def _site_probe(u, timeout=None):
    """Does this exit actually reach the BLOCKED host? -> (kind, ms).

    kind: "good" (200), "blocked" (403/503 = the exit itself is flagged),
    "dead" (timeout/connect error). A 200-with-empty-body still counts as good:
    the site answers that way to a 1-letter term."""
    t0 = time.time()
    try:
        r = _S.get(_PROBE_URL, timeout=timeout or PROBE_TO,
                   headers={"User-Agent": UA, "Accept": "*/*",
                            "Referer": SITE + "/"},
                   proxies={"http": u, "https": u})
    except Exception:
        return "dead", int((time.time() - t0) * 1000)
    ms = int((time.time() - t0) * 1000)
    code = r.status_code
    try:
        r.close()
    except Exception:
        pass
    if code in (403, 503):
        return "blocked", ms
    return ("good", ms) if code == 200 else ("dead", ms)


def _pool_train(cands):
    """Background trainer (MovieBox pattern): platform-probe the candidates in
    parallel waves, publish wave 1 immediately so a usable pool exists early,
    then keep the POOL_MAX fastest good exits, MERGED with the healthy members
    already trained (never wholesale-replace: that throws away latency records
    and the sticky pick)."""
    now = time.time()
    with _POOL_LOCK:
        prev = [u for u in _POOL[0] if _POOL_BAD.get(u, 0.0) <= now]

    def wave(batch):
        got = []
        futs = {_PP_EX.submit(_site_probe, u): u for u in batch}
        for f in as_completed(futs, timeout=PROBE_TO + 12):
            u = futs[f]
            try:
                kind, ms = f.result(timeout=0)
            except Exception:
                kind, ms = "dead", 9999
            if kind == "good":
                got.append((u, ms))
            else:
                _pool_note(u, False, blocked=(kind == "blocked"))
        return got

    alive = []
    try:
        alive = wave(cands[:PROBE_WAVE])
        best = sorted(alive, key=lambda x: x[1])[:POOL_MAX]
        _pool_publish(prev, best)
        if len(cands) > PROBE_WAVE:
            alive += wave(cands[PROBE_WAVE:])
    except Exception:
        pass
    alive.sort(key=lambda x: x[1])                    # fastest first
    _pool_publish(prev, alive[:POOL_MAX])
    with _POOL_LOCK:
        for u, ms in alive:                           # seed training records
            st = _POOL_STATS.setdefault(u, {"ok": 0, "fail": 0, "lat": None})
            st["ok"] += 1
            st["lat"] = ms if st.get("lat") is None else int(0.6 * st["lat"] + 0.4 * ms)
            _POOL_BAD.pop(u, None)
        # bound the learning dicts: a long-lived instance otherwise keeps records
        # for exits that left the pool long ago
        live = set(_POOL[0]) | {u for u, t in _POOL_BAD.items() if t > time.time()}
        for u in [u for u in _POOL_STATS if u not in live]:
            _POOL_STATS.pop(u, None)


def _pool_publish(prev, best):
    """MERGE previous healthy members with the newly proven fastest ones."""
    lat = {u: (_POOL_STATS.get(u) or {}).get("lat") for u in prev}

    def key(u):
        return (lat.get(u) if lat.get(u) else 9999, u)
    merged = list(dict.fromkeys([u for u, _ in best] if best and isinstance(best[0], tuple)
                                else list(best)))
    with _POOL_LOCK:
        out = list(dict.fromkeys(POOL_MANUAL + prev + merged))
        out.sort(key=lambda u: ((_POOL_STATS.get(u) or {}).get("lat") or 9999))
        _POOL[0] = out[:max(POOL_MAX, len(POOL_MANUAL))]


def _pool_refresh(force=False):
    """Cheap, synchronous: pull the source list, publish it, and (once per TTL)
    kick the background trainer. Never blocks a user request on probing."""
    if not POOL_ON:
        return []
    now = time.time()
    with _POOL_LOCK:
        fresh = now - _POOL_TS[0] < POOL_TTL
        if _POOL[0] and not force and fresh:
            return list(_POOL[0])
        if not force and fresh:
            return []
        _POOL_TS[0] = now
    cands = _pool_pull()
    if not cands:
        return list(_POOL[0])
    with _POOL_LOCK:
        # publish untrained right away: a racing fetch can use these immediately
        # while the trainer learns which are fast
        keep = [u for u in _POOL[0] if _POOL_BAD.get(u, 0.0) <= now]
        _POOL[0] = list(dict.fromkeys(POOL_MANUAL + keep + cands))[:POOL_CAND]
    if not _TRAINING[0]:
        _TRAINING[0] = True

        def run():
            try:
                _pool_train(cands)
            finally:
                _TRAINING[0] = False
        threading.Thread(target=run, daemon=True, name="pooltrain").start()
    return list(_POOL[0])


def _pool_score(u):
    """quality x speed — a trained exit that is reliable AND fast wins."""
    st = _POOL_STATS.get(u) or {}
    ok, fail = st.get("ok", 0), st.get("fail", 0)
    quality = (ok + 1.0) / (ok + fail + 2.0)
    lat = st.get("lat") or 4000
    return quality * (4000.0 / max(lat, 250.0))


def _pool_order():
    """Healthy exits, sticky first (unless it is already busy), then best score."""
    now = time.time()
    urls = _pool_refresh()
    good = [u for u in urls if _POOL_BAD.get(u, 0.0) <= now]
    sticky = _STICKY[0] if _STICKY[1] > now else None
    if sticky and (sticky in good) and _STICKY_BUSY.get(sticky, 0) < 2:
        rest = [u for u in good if u != sticky]
        rest.sort(key=_pool_score, reverse=True)
        return [sticky] + rest
    good.sort(key=_pool_score, reverse=True)
    return good


def _pool_note(u, ok, blocked=False, ms=None):
    now = time.time()
    st = _POOL_STATS.setdefault(u, {"ok": 0, "fail": 0, "lat": None})
    if ok:
        st["ok"] += 1
        if ms:
            st["lat"] = ms if st.get("lat") is None else int(0.6 * st["lat"] + 0.4 * ms)
        _POOL_BAD.pop(u, None)
        _STICKY[0], _STICKY[1] = u, now + 90
    else:
        st["fail"] += 1
        if blocked:
            st["blocked"] = st.get("blocked", 0) + 1
        _POOL_BAD[u] = now + (900 if blocked else 300)
        if _STICKY[0] == u:
            _STICKY[1] = 0.0


def _pool_stats():
    now = time.time()
    trained = [u for u in _POOL[0] if (_POOL_STATS.get(u) or {}).get("lat")]
    lats = sorted((_POOL_STATS.get(u) or {}).get("lat") or 0 for u in trained)
    return {"pool": len(_POOL[0]),
            "healthy": len([u for u in _POOL[0] if _POOL_BAD.get(u, 0.0) <= now]),
            "trained": len(trained),
            "median_ms": lats[len(lats) // 2] if lats else None,
            "sticky": _STICKY[0] if _STICKY[1] > now else None,
            "known_good": len([u for u, s in _POOL_STATS.items() if s.get("ok")]),
            "manual": len(POOL_MANUAL),
            "sources": len(POOL_SRCS),
            "training": bool(_TRAINING[0]),
            "direct_blocked_hosts": [h for h, t in _DIRECT_BAD.items() if t > now],
            "enabled": POOL_ON}


_CF_MARKERS = ("cf-browser-verification", "cf-chl-", "challenge-platform",
               "just a moment", "checking your browser", "cf-turnstile",
               "attention required", "ray id", "cloudflare")


def _looks_blocked(text, status=200):
    """True for a 403/503 AND for a 200 whose body is really a Cloudflare
    interstitial.

    Some free exits are themselves flagged, and they answer 200 with a challenge
    page. Measured on prod: a shelf came back "0 items" and got cached as an
    honest empty, because a 200 interstitial parses to zero cards. Requiring two
    markers inside a small body keeps real payloads (hex API blobs, tiny JSON)
    out of the net."""
    if status in (403, 503):
        return True
    if not text or len(text) > 20000:
        return False
    head = text[:3000].lower()
    return sum(1 for m in _CF_MARKERS if m in head) >= 2


def _fetch(url, timeout=12, referer=None, extra_headers=None, stream=False,
           allow_proxy=None):
    """(requests.Response | None, via_proxy). Direct first; on a 403/503/transport
    failure the host is benched for direct egress and the pool takes over inside
    the SAME call, so the user never sees the first failure."""
    host = urlparse(url).netloc
    hd = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}
    if referer:
        hd["Referer"] = referer
    if extra_headers:
        hd.update(extra_headers)
    use_pool = POOL_ON and (allow_proxy is not False)
    if not use_pool or time.time() >= _DIRECT_BAD.get(host, 0.0):
        if time.time() < (_HOST_BLOCK.get(host) or 0.0):
            return None, False
        try:
            r = _S.get(url, headers=hd, timeout=timeout, stream=stream)
        except Exception:
            r = None
        if r is not None and r.status_code not in (403, 503):
            if not stream and _looks_blocked(r.text, r.status_code):
                try:
                    r.close()
                except Exception:
                    pass
                r = None                       # this egress is flagged too
            else:
                if r.status_code in (429,):
                    _bench(host, 20)
                return r, False
        if stream and r is not None:
            try:
                r.close()
            except Exception:
                pass
        if not use_pool:
            return r, False
        _DIRECT_BAD[host] = time.time() + DIRECT_BLOCK_TTL
    r = _pool_get(url, hd, min(timeout, POOL_TO), stream)
    if r is None:
        return None, True
    if r.status_code == 429:
        _bench(host, 20)
    return r, True


def _pool_get(url, hd, timeout, stream=False):
    """Race POOL_TRY exits at once and keep the first good answer.

    Free proxies are mostly dead or slow: trying them one after another spent
    8s per corpse and a cold resolve never fit inside the 22s wall. Racing costs
    the same wall-clock as the fastest healthy exit. Losers are closed."""
    exits = _pool_order()[:POOL_TRY]
    if not exits:
        return None

    def one(u):
        t0 = time.time()
        _STICKY_BUSY[u] = _STICKY_BUSY.get(u, 0) + 1
        try:
            r = _S.get(url, headers=hd, timeout=timeout, stream=stream,
                       proxies={"http": u, "https": u})
        except Exception:
            _pool_note(u, False)
            _STICKY_BUSY[u] = max(0, _STICKY_BUSY.get(u, 1) - 1)
            return None
        if r.status_code in (403, 503) or (
                not stream and _looks_blocked(getattr(r, "text", ""), r.status_code)):
            try:
                r.close()
            except Exception:
                pass
            _pool_note(u, False, blocked=True)     # this exit is flagged too
            _STICKY_BUSY[u] = max(0, _STICKY_BUSY.get(u, 1) - 1)
            return None
        _pool_note(u, True, ms=int((time.time() - t0) * 1000))
        _STICKY_BUSY[u] = max(0, _STICKY_BUSY.get(u, 1) - 1)
        return r

    if len(exits) == 1:
        return one(exits[0])
    futs = {_P_EX.submit(one, u): u for u in exits}
    win = []

    def reap(f):
        if win and f is not win[0]:
            try:
                r = f.result(timeout=0)
                if r is not None:
                    r.close()
            except Exception:
                pass
    for f in futs:
        f.add_done_callback(reap)
    try:
        for f in as_completed(futs, timeout=timeout + 3):
            try:
                r = f.result(timeout=0)
            except Exception:
                r = None
            if r is not None:
                win.append(f)
                return r
    except Exception:
        pass                                       # every racer timed out
    for f in futs:
        if not f.done():
            f.cancel()
    return None


def _get(url, timeout=12, referer=None, want_json=False):
    """Site/player fetch -> requests.Response | None (transient)."""
    host = urlparse(url).netloc
    if time.time() < (_HOST_BLOCK.get(host) or 0.0):
        return None
    r, _via = _fetch(url, timeout=timeout, referer=referer)
    if r is not None and r.status_code in (403, 429, 503):
        _bench(host, 20 if r.status_code == 429 else 60)
    return r


def _playlist_head(url, referer=None, nbytes=8192, timeout=15):
    """Read ONLY the head of a playlist (the 3n1 variants ignore Range and would
    otherwise stream 1.4MB into us). stream+read+close, house pattern."""
    try:
        r, _via = _fetch(url, timeout=timeout, referer=referer, stream=True)
        if r is None:
            return None, ""
        try:
            if r.status_code not in (200, 206):
                return r.status_code, ""
            buf = b""
            for chunk in r.iter_content(4096):
                buf += chunk
                if len(buf) >= nbytes:
                    break
            return r.status_code, buf.decode("utf-8", "ignore")
        finally:
            r.close()
    except Exception:
        return None, ""


def _range_probe(url, referer=None, nbytes=1024, timeout=12):
    """Segment playability probe: expect 206 (or 200 with a body).
    Never proxied: this must prove the file plays from a normal client path,
    and media must not touch a free exit."""
    try:
        r, _via = _fetch(url, timeout=timeout, referer=referer, stream=True,
                         allow_proxy=False)
        if r is None:
            return None, 0
        try:
            code = r.status_code
            body = next(r.iter_content(256), b"") if code in (200, 206) else b""
            return code, len(body or b"")
        finally:
            r.close()
    except Exception:
        return None, 0


# ═══════════════════════════════════════════════════════════ 5. METADATA RACE
def _cinemeta(ctype, imdb):
    hit, val = C_META.get(("cm", ctype, imdb))
    if hit:
        return val
    out = None
    try:
        r = _S.get("%s/meta/%s/%s.json" % (CINEMETA, ctype, imdb), timeout=7)
        if r.status_code == 200:
            m = (r.json() or {}).get("meta") or {}
            name = m.get("name")
            if name:
                yr = _year_of(m.get("releaseInfo") or m.get("year") or "")
                out = (name, yr, m.get("moviedb_id") or None)
    except Exception:
        out = None
    if out:
        C_META.put(("cm", ctype, imdb), out, _META_TTL)
    return out


def _tmdb_find(imdb):
    hit, val = C_META.get(("tmdb", imdb))
    if hit:
        return val
    out = None
    try:
        r = _S.get("https://api.themoviedb.org/3/find/%s?external_source=imdb_id"
                   "&api_key=%s" % (imdb, TMDB_KEY), timeout=7)
        if r.status_code == 200:
            j = r.json() or {}
            for k in ("movie_results", "tv_results"):
                arr = j.get(k) or []
                if arr:
                    t = arr[0].get("title") or arr[0].get("name")
                    date = arr[0].get("release_date") or arr[0].get("first_air_date") or ""
                    if t:
                        out = (t, _year_of(date), arr[0].get("id"))
                        break
    except Exception:
        out = None
    if out:
        C_META.put(("tmdb", imdb), out, _META_TTL)
    return out


def _imdb_suggest(imdb):
    hit, val = C_META.get(("imdb", imdb))
    if hit:
        return val
    out = None
    try:
        r = _S.get("https://v2.sg.media-imdb.com/suggestion/%s/%s.json"
                   % (imdb[2] if len(imdb) > 2 else "x", imdb), timeout=7)
        if r.status_code == 200:
            for d in (r.json() or {}).get("d") or []:
                if d.get("id") == imdb:
                    out = (d.get("l"), d.get("y"), None)
                    break
    except Exception:
        out = None
    if out:
        C_META.put(("imdb", imdb), out, _META_TTL)
    return out


_META_LOCKS = {}


_EPISODE_TITLE_RE = re.compile(r"^(episode\b|ep\.?\s*\d|part\s*\d)", re.I)
# IMDb-suggest answers with EPISODE titles for series ids that are actually
# episode ids (measured: tt35320226 -> "Episode 29: A King Has Fallen"), so for
# series it ranks last; for movies it is IMDb's own data and ranks first.
_META_PREF = {"movie": ("imdb", "cinemeta", "tmdb"),
              "series": ("cinemeta", "tmdb", "imdb")}


def _tmdb_alt_titles(tmdb_id, ctype):
    """TMDB alternative titles. Biggest single hit-rate win for Bangla/Hindi
    films: IMDb's primary title is often the English one (tt3365690 is
    "The Reincarnate" on IMDb, "Jaatishwar" on BanglaPlex)."""
    if not tmdb_id:
        return []
    hit, val = C_META.get(("alt", tmdb_id, ctype))
    if hit:
        return val or []
    kind = "tv" if ctype == "series" else "movie"
    out = []
    try:
        r = _S.get("https://api.themoviedb.org/3/%s/%s/alternative_titles?api_key=%s"
                   % (kind, tmdb_id, TMDB_KEY), timeout=6)
        if r.status_code == 200:
            j = r.json() or {}
            for t in (j.get("titles") or j.get("results") or []):
                nm = (t.get("title") or "").strip()
                if nm and 1 < len(nm) < 90:
                    out.append(nm)
    except Exception:
        out = []
    out = out[:10]
    C_META.put(("alt", tmdb_id, ctype), out, _META_TTL)
    return out


def resolve_meta_all(ctype, imdb):
    """[(name, year, tmdb_id), ...] best first — every distinct title we can
    learn about this id, for _build_inner to walk.

    Consensus, not a race. All three sources answer inside META_WAIT and a title
    two of them agree on wins; the rest stay as fallbacks. First-responder-wins
    misfired live: for tt3365690 cinemeta+IMDb said "The Reincarnate" while TMDB
    said "Jaatishwar" — BanglaPlex lists it as Jaatishwar, so a coin flip decided
    hit vs. honest-empty."""
    hit, val = C_META.get(("cands", ctype, imdb))
    if hit:
        return val or []
    got, lock = {}, threading.Lock()
    left = [3]
    all_done = threading.Event()

    def run(tag, fn):
        try:
            r = fn(ctype, imdb) if fn is _cinemeta else fn(imdb)
        except Exception:
            r = None
        with lock:
            nm = (r[0] or "").strip() if r else ""
            if nm and not _EPISODE_TITLE_RE.match(nm):
                got.setdefault(tag, (nm, r[1], r[2]))
            left[0] -= 1
            if left[0] <= 0:
                all_done.set()

    futs = [_IO_EX.submit(run, t, f) for t, f in
            (("cinemeta", _cinemeta), ("tmdb", _tmdb_find), ("imdb", _imdb_suggest))]
    all_done.wait(META_WAIT)
    for f in futs:
        try:
            f.result(timeout=0.05) if f.done() else None
        except Exception:
            pass

    pref = _META_PREF.get(ctype, _META_PREF["movie"])
    groups = {}
    for tag, (nm, yr, tid) in got.items():
        g = groups.setdefault(_norm_title(nm), {"names": {}, "srcs": [], "years": [],
                                                "tmdb": None})
        g["names"][tag] = nm
        g["srcs"].append(tag)
        if yr:
            g["years"].append(int(yr))
        if tid and not g["tmdb"]:
            g["tmdb"] = tid
    if not groups:
        return []                                   # transient: never cached
    ordered = sorted(groups.items(),
                     key=lambda kv: (-len(kv[1]["srcs"]),
                                     min(pref.index(s) for s in kv[1]["srcs"])))
    tmdb_id = next((g["tmdb"] for _k, g in ordered if g["tmdb"]), None)
    any_year = next((g["years"][0] for _k, g in ordered if g["years"]), None)
    cands, seen = [], set()
    for _k, g in ordered:
        nm = g["names"].get(pref[0]) or list(g["names"].values())[0]
        key = _norm_title(nm)
        if not key or key in seen:
            continue
        seen.add(key)
        cands.append((nm, g["years"][0] if g["years"] else any_year, tmdb_id))
    for alt in _tmdb_alt_titles(tmdb_id, ctype):
        key = _norm_title(alt)
        if key and key not in seen:
            seen.add(key)
            cands.append((alt, any_year, tmdb_id))
    cands = cands[:MAX_META_CANDS]
    C_META.put(("cands", ctype, imdb), cands, _META_TTL)
    return cands


def resolve_meta(ctype, imdb):
    """Top metadata candidate: (name, year, tmdb_id) or (None, None, None)."""
    c = resolve_meta_all(ctype, imdb)
    return c[0] if c else (None, None, None)


# ═══════════════════════════════════════════════════════════ 6. SITE SEARCH
def _search_autocomplete(kw):
    """GET /home/autocompleteajax?term={kw} -> [{title,type,image,url}]
    (jQuery-UI autocomplete; POST or ?q= returns a default list — must use term=)."""
    url = SITE + "/home/autocompleteajax?term=" + quote(kw)
    r = _get(url, timeout=10, referer=SITE + "/")
    if r is None or r.status_code != 200:
        return None if r is None else []
    try:
        arr = r.json()
    except Exception:
        return []
    out = []
    for it in arr if isinstance(arr, list) else []:
        u = (it.get("url") or "").strip()
        t = (it.get("title") or "").strip()
        if not u or not t or "/watch/" not in u:
            continue
        out.append({"title": _html.unescape(t), "url": u,
                    "type": it.get("type") or "",
                    "image": it.get("image") or ""})
    return out


_SEARCH_HTML_RE = re.compile(
    r'<a[^>]+href="(https?://[^"]+/watch/[^"]+\.html)"[^>]*>(?:(?!</a>).)*?'
    r'(?:class="movie-title"[^>]*>\s*([^<]{1,120}?)\s*<)', re.S)


def _search_html(kw):
    """Fallback: /search/?q={kw} HTML cards (OVOO markup)."""
    r = _get(SITE + "/search/?q=" + quote(kw), timeout=12, referer=SITE + "/")
    if r is None:
        return None
    if r.status_code != 200:
        return []
    h = r.text or ""
    out, seen = [], set()
    # OVOO card: <div class="latest-movie-img-container" style="url(...)"> ...
    #            <a href=".../watch/slug.html"> ... <div class="movie-title">Title</div>
    for m in re.finditer(r'href="(https?://[^"]+/watch/[a-z0-9\-]+\.html)"', h):
        u = m.group(1)
        if u in seen:
            continue
        seen.add(u)
        tail = h[m.end():m.end() + 1200]
        tm = re.search(r'class="movie-title"[^>]*>\s*([^<]{1,140}?)\s*<', tail)
        ym = re.search(r'class="video_year_movie"[^>]*>\s*([^<]{1,20}?)\s*<', tail)
        qm = re.search(r'class="video_quality_movie"[^>]*>\s*([^<]{1,20}?)\s*<', tail)
        out.append({"title": _html.unescape(tm.group(1)).strip() if tm
                    else re.sub(r"[-.]", " ", urlparse(u).path.split("/watch/")[-1][:-5]),
                    "url": u, "type": "",
                    "year": _year_of(ym.group(1)) if ym else None,
                    "quality": (qm.group(1).strip() if qm else ""),
                    "image": ""})
    return out


def search_candidates(kw):
    """Progressive narrowing search (the site ANDs words, colons break it)."""
    hit, val = C_SEARCH.get(kw)
    if hit:
        return val if val is not None else []
    words = re.sub(r"[^\w\s]", " ", kw or "").split()
    queries = [kw]
    if len(words) >= 3:
        queries.append(" ".join(words[:3]))
    if len(words) >= 2:
        queries.append(" ".join(words[:2]))
    if words and len(words[0]) >= 4:
        queries.append(words[0])
    out, seen = [], set()
    transient = False
    for q in list(dict.fromkeys(queries))[:4]:
        res = _search_autocomplete(q)
        if res is None:
            transient = True
            continue
        for it in res:
            if it["url"] in seen:
                continue
            seen.add(it["url"])
            out.append(it)
        if len(out) >= 8:
            break
        if not res:                        # autocomplete found nothing -> HTML
            hres = _search_html(q)
            if hres is None:
                transient = True
            for it in (hres or []):
                if it["url"] in seen:
                    continue
                seen.add(it["url"])
                out.append(it)
            if len(out) >= 8:
                break
    if transient and not out:
        # the site/Cloudflare was sick, NOT the title: return None so the caller
        # can tell this from a real empty (house rule: never cache a transient
        # failure as a negative) and cache nothing at all.
        return None
    C_SEARCH.put(kw, out or None, _SEARCH_TTL if out else _NEG_TTL)
    return out


# ═══════════════════════════════════════════════════════ 7. WATCH-PAGE PARSE
_KEY_RE = re.compile(
    r'\?key=([a-z0-9]{8,16})"\s+class="player-server-btn([^"]*)">\s*'
    r'(?:<i[^>]*></i>)?\s*([^<]{0,60})')
_IFRAME_RE = re.compile(r'<iframe[^>]*\ssrc="([^"]+)"')


def _meta_tag(h, prop):
    m = re.search(r'<meta[^>]+(?:property|name)="%s"[^>]+content="([^"]*)"' % re.escape(prop), h)
    if not m:
        m = re.search(r'<meta[^>]+content="([^"]*)"[^>]+(?:property|name)="%s"' % re.escape(prop), h)
    return _html.unescape(m.group(1)).strip() if m else ""


def _strong_field(h, label):
    m = re.search(r'<strong>\s*%s\s*:?\s*</strong>\s*(.{0,400}?)</p>' % re.escape(label), h, re.S)
    if not m:
        return ""
    frag = m.group(1)
    txt = re.sub(r"<[^>]+>", " ", frag)
    return re.sub(r"\s+", " ", _html.unescape(txt)).strip()


def parse_watch_page(url, key=None):
    """watch page -> {slug, video_id, title, year, quality, duration, release,
    genre, country, actors, poster, plot, keys[(key,label,active)], iframe}"""
    u = url if not key else (url.split("?")[0] + "?key=" + key)
    hit, val = C_PAGE.get(u)
    if hit:
        return val
    r = _get(u, timeout=15, referer=SITE + "/")
    if r is None:
        return None                                    # transient
    if r.status_code != 200:
        C_PAGE.put(u, None, _NEG_TTL)
        return None
    h = r.text or ""
    if "/watch/" not in h and "player-server-btn" not in h:
        C_PAGE.put(u, None, _NEG_TTL)
        return None
    slug = urlparse(u).path.split("/watch/")[-1].replace(".html", "")
    og_title = _meta_tag(h, "og:title") or ""
    h1 = re.search(r"<h1>\s*(.*?)\s*</h1>", h, re.S)
    title = (h1.group(1).strip() if h1 else "") or re.sub(r"\s*[|–-].*$", "", og_title)
    title = _html.unescape(re.sub(r"<[^>]+>", "", title)).strip()
    keys = [(k, ("active" in cls), re.sub(r"\s+", " ", lbl).strip())
            for k, cls, lbl in _KEY_RE.findall(h)]
    iframe = _IFRAME_RE.search(h)
    rid = re.search(r"/home/view_modal/report/(\d+)", h)
    rel = ""
    m = re.search(r'<strong>\s*Release\s*:?\s*</strong>\s*([^<]{4,40})', h)
    if not m:
        m = re.search(r'>\s*((?:19|20)\d{2}-\d{2}-\d{2})\s*</p>', h)
    if m:
        rel = m.group(1).strip()
    year = _year_of(og_title) or _year_of(rel) or _year_of(slug)
    dur = ""
    m = re.search(r"<strong>\s*Duration\s*:?\s*</strong>\s*([\d.]+)\s*Min", h)
    if m:
        dur = m.group(1)
    qual = ""
    m = re.search(r'<strong>\s*Quality\s*:?\s*</strong>\s*<span[^>]*>\s*([^<]{1,20})', h)
    if m:
        qual = m.group(1).strip()
    val = {
        "slug": slug,
        "video_id": rid.group(1) if rid else "",
        "title": title,
        "og_title": og_title,
        "year": year,
        "release": rel,
        "duration": dur,
        "quality": qual,
        "genre": _strong_field(h, "Genre"),
        "country": _strong_field(h, "Country"),
        "actors": _strong_field(h, "Actor"),
        "director": _strong_field(h, "Director"),
        "poster": _meta_tag(h, "og:image"),
        "plot": _meta_tag(h, "og:description"),
        "keys": keys,
        "iframe": (iframe.group(1).strip() if iframe else ""),
        "url": url,
    }
    C_PAGE.put(u, val, _PAGE_TTL)
    return val


# ═══════════════════════════════════════════════════════ 8. KEY (FILE) PICKER
_SEASON_RE = re.compile(r"\bs(?:eason)?\s*0*(\d{1,2})\b", re.I)
_EPRANGE_RE = re.compile(r"ep(?:isode)?s?\s*0*(\d{1,3})\s*[-–—to]{1,3}\s*0*(\d{1,3})", re.I)
_EPONE_RE = re.compile(r"ep(?:isode)?\s*0*(\d{1,3})\b", re.I)
_MOVIE_WORDS = ("full movie", "movie", "film")


def classify_key(label):
    """label -> ('movie'|'season'|'eprange'|'episode'|'series'|'other', se, ep_lo, ep_hi)"""
    t = (label or "").strip()
    low = t.lower()
    m = _EPRANGE_RE.search(low)
    if m:
        se = _SEASON_RE.search(low)
        return ("eprange", int(se.group(1)) if se else None,
                int(m.group(1)), int(m.group(2)))
    m = _EPONE_RE.search(low)
    if m:
        se = _SEASON_RE.search(low)
        return ("episode", int(se.group(1)) if se else None,
                int(m.group(1)), int(m.group(1)))
    if "bonus" in low:
        return ("other", None, None, None)
    m = _SEASON_RE.search(low)
    if m and not any(w in low for w in _MOVIE_WORDS):
        return ("season", int(m.group(1)), None, None)
    if any(w in low for w in _MOVIE_WORDS):
        return ("movie", None, None, None)
    if "series" in low:
        return ("series", None, None, None)
    return ("other", None, None, None)


def pick_keys(page, ctype, se, ep):
    """Choose the site video-file(s) that answer this request.
    Returns [(key, label, note)] — note explains season-pack honesty."""
    keys = [(k, lbl) for k, _act, lbl in (page.get("keys") or [])]
    if not keys:
        return []
    if len(keys) == 1:
        k, lbl = keys[0]
        kind, kse, lo, hi = classify_key(lbl)
        note = ""
        if ctype == "series" and kind in ("series", "season"):
            note = "full-season file"
        elif ctype == "series" and kind == "eprange":
            note = "episode pack %d-%d" % (lo, hi)
        return [(k, lbl, note)]
    # movies: prefer an explicit movie-labelled file
    if ctype == "movie":
        pref = [(k, lbl) for k, lbl in keys if classify_key(lbl)[0] == "movie"]
        k, lbl = (pref or keys)[0]
        return [(k, lbl, "")]
    # series: season match, then episode-range containment
    scored = []
    for k, lbl in keys:
        kind, kse, lo, hi = classify_key(lbl)
        rank, note = 9, ""
        if kind == "episode" and (ep is None or kse in (None, se)) and lo == ep:
            rank, note = 0, ""
        elif kind == "eprange" and (kse in (None, se)) and lo is not None \
                and hi is not None and ep is not None and lo <= ep <= hi:
            rank, note = 1, "episode pack %d-%d" % (lo, hi)
        elif kind == "season" and se is None:
            # no season asked for: lowest season first, never the site default
            # (mirzapur's default key is "Bonus Episode" — worst possible pick)
            rank, note = 2.5 + 0.001 * (kse or 99), "full-season file"
        elif kind == "season" and kse == se:
            rank, note = 2, "full-season file"
        elif kind == "series" and kse is None:
            rank, note = (2.4 if se is None else 3), "full-season file"
        elif kind == "season" and kse != se:
            rank = 8
        elif kind == "other":
            rank = 7
        else:
            rank = 6
        scored.append((rank, k, lbl, note))
    scored.sort(key=lambda x: x[0])
    # A season/episode-level ask may only be answered by a REAL match: the
    # episode itself (0), a pack containing it (1), that season (2/2.5) or the
    # whole-series file (3). Never "Bonus Episode"/unknown leftovers (6/7) and
    # never a different season (8) — wrong content is worse than an honest empty.
    if se is not None or ep is not None:
        hits = [x for x in scored if x[0] <= 3.0]
        return [(k, lbl, note) for _r, k, lbl, note in hits[:2]]
    out = []
    for rank, k, lbl, note in scored[:2]:
        if rank >= 8:
            break
        out.append((k, lbl, note))
    return out


# ══════════════════════════════════════════════════════ 9. PLAYER RESOLUTION
_SERVER_BTN_RE = re.compile(r"changeServer\(\s*['\"]([^'\"]+)['\"]")
_EMBED_LABEL_RE = re.compile(r'srv-btn[^>]*>\s*<span[^>]*></span>\s*([^<]{0,40})')


def parse_embed_servers(iframe_url):
    """plextream.work/embed.php?id=X -> [(label, url)] server list."""
    if not iframe_url:
        return []
    host = urlparse(iframe_url).netloc
    if EMBED_HOST not in host:
        return [("", iframe_url)]
    hit, val = C_EMBED.get(iframe_url)
    if hit:
        return val or []
    r = _get(iframe_url, timeout=12, referer=SITE + "/")
    if r is None:
        return []
    h = r.text or ""
    srvs = [m.group(1) for m in _SERVER_BTN_RE.finditer(h)]
    labels = [m.group(1).strip() for m in _EMBED_LABEL_RE.finditer(h)]
    if not srvs:
        m = _IFRAME_RE.search(h)
        srvs = [m.group(1)] if m else []
    out = []
    for i, s in enumerate(srvs):
        lbl = labels[i] if i < len(labels) else ""
        out.append((lbl, s.strip()))
    C_EMBED.put(iframe_url, out or None, _EMBED_TTL)
    return out


_N1_LOCKS = {h: threading.Lock() for h in N1_HOSTS}
_N1_LAST = {h: 0.0 for h in N1_HOSTS}
_N1_BUSY = {h: 0.0 for h in N1_HOSTS}


def _n1_throttle(host):
    """Per-host lane throttle: the 3n1 API 429s after ~10 rapid calls."""
    with _N1_LOCKS[host]:
        wait = _N1_LAST.get(host, 0.0) + N1_LANE_GAP - time.time()
        if wait > 0:
            time.sleep(min(wait, 6.0))
        _N1_LAST[host] = time.time()


def n1_video(host, vid, deadline=None, retries=1):
    """GET /api/v1/video?id={vid} -> decrypted payload dict | None (transient)
    | False (definitively not on this host: the file lives on the other one).
    A 429 is transient, so it gets one backoff-retry before we give up."""
    key = (host, vid)
    hit, val = C_N1.get(key)
    if hit:
        return val
    if time.time() < _N1_BUSY.get(host, 0.0):
        return None                                   # cooling down after a 429
    url = "https://%s/api/v1/video?id=%s" % (host, vid)
    for attempt in range(retries + 1):
        _n1_throttle(host)
        r = _get(url, timeout=14, referer="https://%s/" % host)
        _STATS["n1_calls"] += 1
        if r is None:
            return None
        txt = (r.text or "").strip()
        if r.status_code == 429 or "Rate limit" in txt:
            _STATS["n1_429"] += 1
            if attempt < retries and (deadline is None or
                                      time.time() + N1_BACKOFF < deadline):
                time.sleep(N1_BACKOFF)
                continue
            _N1_BUSY[host] = time.time() + N1_COOLDOWN
            return None
        if r.status_code == 404 or "not found" in txt.lower():
            return False
        if r.status_code != 200:
            return None
        if txt[:1] in "{[":
            try:
                payload = json.loads(txt)
            except Exception:
                return None
        else:
            payload = n1_decrypt(txt)
        if not payload:
            return None
        C_N1.put(key, payload, _N1_TTL)
        return payload
    return None


_N1_ID_RE = re.compile(r"https?://([^/]+)/#([A-Za-z0-9_\-]{4,16})")


def resolve_n1(servers, deadline=None):
    """server list -> (payload, host, vid) for the first frontend that answers."""
    cands = []
    for _lbl, u in servers:
        m = _N1_ID_RE.match(u or "")
        if m and m.group(1) in N1_HOSTS:
            cands.append((m.group(1), m.group(2)))
    for host, vid in cands:
        p = n1_video(host, vid, deadline=deadline)
        if isinstance(p, dict):
            return p, host, vid
    return None, None, None


# ══════════════════════════════════════════════════════ 10. MEDIA CANDIDATES
def _abs(base, rel):
    if not rel:
        return ""
    if rel.startswith("http"):
        return rel
    return urljoin(base if base.endswith("/") else base.rsplit("/", 1)[0] + "/", rel)


def media_candidates(payload, host):
    """payload -> ordered [(kind, master_url, referer|None, server_label)]
    kind: tiktok (open, no headers) | cloudflare (referer on segments) | inhouse
    """
    out = []
    base = "https://%s/" % host
    tt = payload.get("hlsVideoTiktok")
    if tt:
        out.append(("tiktok", _abs(base, tt), None, "TikTok CDN"))
    cfn = payload.get("cfNative")
    if cfn:
        out.append(("cloudflare", cfn, base.rstrip("/"), "Cloudflare"))
    src = payload.get("source")
    if src and INHOUSE_ON:
        # raw-IP origin: its token binds to the FETCHER's ip. It verifies from
        # Render and then 403s on the user's player -> phantom card. Off.
        out.append(("inhouse", src, base.rstrip("/"), "In-House"))
    return out


_RES_RE = re.compile(r"RESOLUTION=(\d+)x(\d+)")


def _master_info(url, referer=None):
    """fetch master -> (text, best_quality(w,h), variant_url, n_variants) or None."""
    r = _get(url, timeout=14, referer=referer)
    if r is None or r.status_code != 200:
        return None
    txt = r.text or ""
    if not txt.lstrip().startswith("#EXTM3U"):
        return None
    res = [(int(a), int(b)) for a, b in _RES_RE.findall(txt)]
    best = max(res, key=lambda x: x[0] * x[1]) if res else (0, 0)
    lines = [l.strip() for l in txt.splitlines()
             if l.strip() and not l.startswith("#")]
    variant = _abs(url, lines[0]) if lines else ""
    return {"text": txt, "best": best, "variant": variant,
            "n_variants": len(lines), "size": len(txt)}


def _verify_media(kind, master_url, referer):
    """NO-PHANTOM gate: master 200 #EXTM3U -> variant head has segments ->
    first segment answers 206 (with the referer the path needs)."""
    info = _master_info(master_url, referer)
    if not info or not info["variant"]:
        return None
    seg_referer = referer if kind != "tiktok" else None
    st, head = _playlist_head(info["variant"], referer=referer)
    if st not in (200, 206) or "#EXTM3U" not in head:
        return None
    segs = [l.strip() for l in head.splitlines()
            if l.strip() and not l.startswith("#")]
    if not segs:
        return None
    seg = _abs(info["variant"], segs[0])
    code, n = _range_probe(seg, referer=seg_referer)
    if code not in (200, 206) or n <= 0:
        # cloudflare/in-house segments need the frontend referer; the tiktok
        # path never does — if it fails, the file is gone for everyone.
        if seg_referer is None:
            code, n = _range_probe(seg, referer=referer)
        if code not in (200, 206) or n <= 0:
            return None
    info["segment"] = seg
    info["seg_status"] = code
    return info


# ═══════════════════════════════════════════════════════════ 11. SUBTITLES
_SUB_LANG_FIX = {"ch": "zh", "chi": "zh", "zh-cn": "zh", "mg": "mg", "bn": "bn",
                 "bangla": "bn", "hindi": "hi", "english": "en", "japanese": "ja",
                 "korean": "ko", "tamil": "ta", "telugu": "te", "arabic": "ar",
                 "french": "fr", "thai": "th", "kannada": "kn"}


def _sub_lang(code):
    c = (code or "").strip().lower().split("#")[0]
    c = _SUB_LANG_FIX.get(c, c)
    return re.sub(r"[^a-z\-]", "", c)[:8] or "und"


_SUB_PRIORITY = {"en": 0, "hi": 1, "bn": 2, "ur": 3, "ta": 4, "te": 5, "ml": 6,
                 "kn": 7, "mr": 8, "gu": 9, "pa": 10, "id": 11, "ms": 12,
                 "ar": 13, "zh": 14, "ja": 15, "ko": 16}


def collect_subtitles(payload, host, limit=None):
    """payload['subtitle'] = {"en": "/{tok}/.../en.vtt#en", ...}
    -> [{url, lang, id}] verified with a 512B head read (dead tracks dropped).
    en/hi/bn first: MAX_SUBS=6 must not be filled up with mg/ka/th while the
    two languages the audience actually reads are left out."""
    subs = payload.get("subtitle") or {}
    if isinstance(subs, str):
        try:
            subs = json.loads(subs)
        except Exception:
            subs = {}
    if not isinstance(subs, dict) or not subs:
        return []
    limit = limit or MAX_SUBS
    base = "https://%s/" % host
    items = []
    for lang, path in list(subs.items())[:16]:
        if isinstance(path, dict):                    # {url:..., lang:...} variant
            path = path.get("url") or path.get("file") or ""
        u = _abs(base, (path or "").split("#")[0])
        if u:
            items.append((_sub_lang(lang), u))
    # de-dup by (lang,url), then rank by usefulness
    seen, uniq = set(), []
    for lg, u in items:
        if (lg, u) in seen:
            continue
        seen.add((lg, u))
        uniq.append((lg, u))
    uniq.sort(key=lambda x: (_SUB_PRIORITY.get(x[0], 50), x[0]))

    def probe(it):
        lg, u = it
        st, head = _playlist_head(u, nbytes=512, timeout=10)
        ok = st in (200, 206) and ("WEBVTT" in head or "-->" in head or len(head) > 40)
        return (lg, u, ok)

    out = []
    try:
        for res in _IO_EX.map(probe, uniq[:12]):
            lg, u, ok = res
            if ok:
                out.append({"url": u, "lang": lg, "id": "bpx-%s" % lg})
            if len(out) >= limit:
                break
    except Exception:
        pass
    return out


# ════════════════════════════════════════════════════════ 12. CARD FORMATTING
_FILE_TAG_RE = re.compile(
    r"(WEB-?DL|WEBRip|WEB|HDTC|HDCAM|CAM|BluRay|BDRip|HDRip|DVDRip|PROPER)", re.I)
_CODEC_RE = re.compile(r"(x265|x264|HEVC|H\.?265|H\.?264|AVC|XViD)", re.I)
_AUDIO_RE = re.compile(r"(AAC\s*5\.1|AAC\s*2\.0|AAC|DDP?5\.1|DD5\.1|DTS|5\.1|2\.0)", re.I)
_LANG_TAG_RE = re.compile(
    r"\b(Hindi|Bengali|Bangla|English|Tamil|Telugu|Korean|Japanese|Dual|Multi)\b", re.I)


def file_tags(payload_title):
    t = payload_title or ""
    def first(rx):
        m = rx.search(t)
        return m.group(1).upper().replace("WEBDL", "WEB-DL") if m else ""
    codec = first(_CODEC_RE)
    codec = {"X265": "HEVC", "H.265": "HEVC", "H265": "HEVC", "X264": "AVC",
             "H.264": "AVC", "H264": "AVC"}.get(codec, codec)
    audio = first(_AUDIO_RE).replace(" ", "")
    langs = []
    for m in _LANG_TAG_RE.finditer(t):
        w = m.group(1).capitalize()
        w = {"Bangla": "Bengali"}.get(w, w)
        if w not in langs and w not in ("Dual", "Multi"):
            langs.append(w)
    return {"source": first(_FILE_TAG_RE), "codec": codec, "audio": audio,
            "langs": langs}


def format_card(kind, master_url, referer, server_label, info, page,
                payload, note="", subs=None, brand=ADDON_NAME):
    """CARD SPEC v2 (user-mandated, same as every sister addon):
       ♧ quality ✹ title / ◫ kind+size+codec+runtime / ◈ source·audio /
       ◈ languages / ⌗ addon / ⌬ server ◴ year ⟡ N SUB
       Tokens/lines with no data are omitted (honesty)."""
    subs = subs or []
    w, h = info.get("best") or (0, 0)
    tier, res = _res_label(w, h)
    ft = file_tags((payload or {}).get("title") or "")
    site_q = (page or {}).get("quality") or ""
    if not res and site_q:
        res, tier = site_q, ""       # no measured resolution -> no tier claim
    name_toks = []
    if tier and res:
        name_toks.append("♧ %s %s" % (tier, res))
    elif res:
        name_toks.append("♧ %s" % res)
    title = (page or {}).get("title") or BRAND
    name_toks.append("✹ %s" % title)
    name = "  ".join(name_toks)

    lines = []
    l2 = []
    se = (page or {}).get("_se")
    ep = (page or {}).get("_ep")
    ctype = (page or {}).get("_ctype") or "movie"
    if ctype == "series" and (se or ep):
        l2.append("S%02d E%02d" % (int(se or 1), int(ep or 1)))
    else:
        l2.append("MOVIE")
    if note:
        l2.append("⚠ %s" % note)
    dur = _fmt_dur((page or {}).get("duration"))
    if dur:
        l2.append("◷ " + dur)
    if ft["codec"]:
        l2.append("▧ " + ft["codec"])
    lines.append("◫ " + " ◇ ".join(l2))
    l3 = [x for x in (ft["source"], ("♫ " + ft["audio"]) if ft["audio"] else "") if x]
    if l3:
        lines.append("◈ " + " ".join(l3))
    langs = ft["langs"]
    if not langs:
        da = ((payload or {}).get("player") or {})
        if isinstance(da, str):
            try:
                da = json.loads(da)
            except Exception:
                da = {}
        if da.get("defaultAudio"):
            langs = [{"hi": "Hindi", "en": "English", "bn": "Bengali",
                      "ja": "Japanese", "ko": "Korean"}.get(da["defaultAudio"],
                                                            da["defaultAudio"].capitalize())]
    if langs:
        lines.append("◈ " + " · ".join(langs[:4]))
    tail = ["⌗ " + brand, "⌬ " + (server_label or kind)]
    yr = (page or {}).get("year")
    if yr:
        tail.append("◴ %s" % yr)
    if subs:
        tail.append("⟡ %d SUB" % len(subs))
    lines.append(" / ".join(tail))

    hints = {"bingeGroup": "bpx|%s" % ((page or {}).get("slug") or title),
             "filename": "%s.%s.mp4" % (re.sub(r"[^\w.-]+", ".", title),
                                        res or "1080p")}
    if referer:
        # segments are referer-gated -> Stremio must attach it (notWebReady)
        hints["proxyHeaders"] = {"request": {"Referer": referer,
                                             "User-Agent": UA}}
        hints["notWebReady"] = True
    else:
        hints["notWebReady"] = False
    card = {"name": name, "description": "\n".join(lines), "url": master_url,
            "behaviorHints": hints,
            # private build markers: read by apply_cfg(), stripped before sending
            "_cdn": kind, "_res": res, "_tier": tier, "_nsubs": len(subs)}
    if subs:
        card["subtitles"] = subs
    return card


# ═══════════════════════════════════════════════════════ 13. BUILD PIPELINE
def _match_score(cand_title, want_title, want_tokens):
    ct = _norm_title(cand_title)
    wt = _norm_title(want_title)
    if not ct:
        return -1
    if ct == wt:
        return 100
    ctoks = _clean_tokens(cand_title)
    if not ctoks:
        return -1
    if ctoks == want_tokens:
        return 95
    # article-insensitive equality
    strip_art = lambda xs: [x for x in xs if x not in ("the", "a", "an")]
    if strip_art(ctoks) == strip_art(want_tokens):
        return 92
    s_c, s_w = set(ctoks), set(want_tokens)
    if s_w and s_w <= s_c and len(s_w) >= 2:
        return 80                                    # site title has extra tags
    if s_c and s_c <= s_w and len(s_c) >= 2:
        return 78
    inter = s_c & s_w
    if len(inter) >= 2 and len(inter) >= 0.6 * max(1, len(s_w)):
        return 60
    if len(inter) == 1 and len(s_w) == 1:
        return 30        # single-word overlap is too weak to ship: it let
                         # "Animal" match "Animal Farm" (and dodge the year
                         # guard, which has no year to work with)
    return -1


def match_candidates(cands, want_title, want_year=None, ctype="movie"):
    want_tokens = _clean_tokens(want_title)
    scored = []
    for c in cands or []:
        sc = _match_score(c.get("title"), want_title, want_tokens)
        if sc < 0:
            continue
        yr = c.get("year") or _year_of(c.get("url", "")) or _year_of(c.get("title", ""))
        if want_year and yr:
            d = abs(int(yr) - int(want_year))
            if d > 1:
                # franchise/collision guard, graduated: a flat -45 still let a
                # 9-year gap through on an exact title match ("Animal" 2015 vs
                # 2024). -25/yr past the first keeps 2-3yr festival-vs-theatrical
                # gaps alive and kills real collisions.
                sc -= 25 * (d - 1)
        if ctype == "series" and re.search(r"\bs\d{1,2}\b", (c.get("title") or "").lower()):
            sc += 2
        scored.append((sc, c))
    scored.sort(key=lambda x: -x[0])
    out = []
    for sc, c in scored:
        if sc < 40:
            break
        out.append(c)
    return out[:4]


def _resolve_file(page, key, label, note, ctype, se, ep, deadline):
    """(page,key) -> [cards] ; verified media only (no phantom)."""
    pg = page if not key else (parse_watch_page(page["url"], key) or page)
    iframe = pg.get("iframe") or ""
    host = urlparse(iframe).netloc
    if not iframe:
        return []
    if ABYSS_HOST in host:
        return []                                      # SoTrym — unsupported (honest)
    if any(x in host for x in LEGACY_PLAYERS):
        return []                                      # dead player hosts (old catalog)
    servers = parse_embed_servers(iframe)
    if not servers:
        return []
    payload, n1host, vid = resolve_n1(servers, deadline=deadline)
    if not payload:
        return []
    subs = collect_subtitles(payload, n1host)
    pg = dict(pg)
    pg["_ctype"], pg["_se"], pg["_ep"] = ctype, se, ep
    cands = media_candidates(payload, n1host)
    if not cands or not HLS_ON:
        return []
    # verify in parallel, keep the site's own priority order (TikTok > CF > in-house)
    futs = [(kind, murl, ref, srv,
             _V_EX.submit(_verify_media, kind, murl, ref)) for kind, murl, ref, srv in cands]
    cards = []
    for kind, murl, ref, srv, f in futs:
        if len(cards) >= MAX_CARDS:
            f.cancel()
            continue
        try:
            info = f.result(timeout=max(0.5, deadline - time.time()))
        except Exception:
            info = None
        if not info:
            continue
        cards.append(format_card(kind, murl, ref, srv, info, pg, payload,
                                 note=note, subs=subs))
    return cards


def _cards_from_matches(matched, years, ctype, se, ep, deadline):
    """matched site urls -> verified cards (first page that answers wins)."""
    cards = []
    for cand in matched[:2]:
        if time.time() >= deadline or cards:
            break
        page = parse_watch_page(cand["url"])
        if not page:
            continue
        py = page.get("year")
        if py and years and all(abs(int(py) - int(y)) > 2 for y in years):
            continue                                   # year guard on the real page
        picks = pick_keys(page, ctype, se, ep)
        if not picks:
            if ctype == "series" and (se or ep):
                # the site has other seasons but NOT this one: answering with the
                # site's default key would serve a different season. Honest empty.
                continue
            picks = [(page["keys"][0][0], page["keys"][0][2], "")] if page.get("keys") else []
        futs = {}
        for key, label, note in picks[:2]:
            futs[_IO_EX.submit(_resolve_file, page, key, label, note,
                               ctype, se, ep, deadline)] = (key, label)
        for f in as_completed(futs):
            if time.time() >= deadline:
                break
            try:
                got = f.result(timeout=max(0.3, deadline - time.time()))
            except Exception:
                got = None
            if got:
                cards.extend(got)
                break                                   # first answer wins
    return cards


def _build_inner(ctype, imdb, se, ep, deadline):
    if imdb.startswith("bpx-"):
        # a catalog card the metadata providers never heard of: go straight to
        # the watch page (no IMDb/TMDB hop exists for it anyway)
        url = SITE + "/watch/" + imdb[4:] + ".html"
        cards = _cards_from_matches([{"url": url}], [], ctype, se, ep, deadline)
        if not cards:
            return {"streams": [],
                    "message": "matched %s but no playable/verified source "
                               "(unsupported player or dead file)" % BRAND}
        for i in range(1, len(cards)):
            cards[i]["name"] += " · alt"
        _STATS["cards"] += len(cards)
        return {"streams": cards[:MAX_CARDS]}
    metas = resolve_meta_all(ctype, imdb)
    if not metas:
        return {"streams": [], "message": "no metadata for this id"}
    years = {int(y) for _n, y, _t in metas if y}
    # fast path: a catalog build already learned which site slug this id is, so
    # the autocomplete hop (a proxied fetch on a flagged egress) can be skipped.
    # The year guard inside _cards_from_matches still applies, and a miss falls
    # through to the normal search.
    hit, slug = C_SLUG.get((ctype, imdb))
    if hit and slug:
        cards = _cards_from_matches([{"url": SITE + "/watch/%s.html" % slug}],
                                    years, ctype, se, ep, deadline)
        if cards:
            for i in range(1, len(cards)):
                cards[i]["name"] += " · alt"
            _STATS["cards"] += len(cards)
            _STATS["slug_hits"] = _STATS.get("slug_hits", 0) + 1
            return {"streams": cards[:MAX_CARDS]}
    searched = matched_any = transient = False
    for name, year, _tid in metas:
        if time.time() >= deadline:
            break
        title = re.sub(r"\s*\((?:19|20)\d{2}\)\s*$", "", name).strip()
        if not title:
            continue
        cands = search_candidates(title)
        if cands is None:
            transient = True                      # site sick: not a "not on site"
            continue
        searched = True
        if not cands:
            continue
        matched = match_candidates(cands, title, year, ctype)
        if not matched:
            continue
        matched_any = True
        cards = _cards_from_matches(matched, years, ctype, se, ep, deadline)
        if cards:
            for i in range(1, len(cards)):
                cards[i]["name"] += " · alt"
            _STATS["cards"] += len(cards)
            return {"streams": cards[:MAX_CARDS]}
    if not searched:
        return {"streams": [],
                "message": ("%s search is not answering right now (transient)"
                            % BRAND) if transient else "metadata race timed out"}
    if not matched_any:
        return {"streams": [], "message": "not on %s" % BRAND}
    return {"streams": [],
            "message": "matched %s but no playable/verified source "
                       "(unsupported player or dead file)" % BRAND}


def _swr_refresh(ctype, imdb, se, ep, key):
    try:
        res = _build_inner(ctype, imdb, se, ep, time.time() + WALL + 8)
        cards = res.get("streams") or []
        if cards:
            C_STREAM.put(key, cards, _STREAM_TTL)
            C_STALE[key] = (time.time() + _STREAM_STALE, cards)
    except Exception:
        pass
    finally:
        with _SWR_LOCK:
            _SWR_RUNNING.discard(key)


def _neg_bg_retry(ckey, fn):
    now = time.time()
    if _NEG_RETRY_AT.get(ckey, 0) > now:
        return
    _NEG_RETRY_AT[ckey] = now + _NEG_RETRY_CD
    threading.Thread(target=fn, daemon=True).start()


def build_streams(ctype, imdb, se, ep):
    key = (ctype, imdb, se, ep)
    hit, val = C_STREAM.get(key)
    if hit:
        return {"streams": val}
    stale = C_STALE.get(key)
    if stale and stale[0] > time.time() and stale[1]:
        if key not in _SWR_RUNNING:
            with _SWR_LOCK:
                if key not in _SWR_RUNNING:
                    _SWR_RUNNING.add(key)
                    threading.Thread(target=_swr_refresh,
                                     args=(ctype, imdb, se, ep, key),
                                     daemon=True).start()
        return {"streams": stale[1]}
    _STATS["resolves"] += 1
    fut = _BUILD_EX.submit(_build_inner, ctype, imdb, se, ep, time.time() + WALL)
    try:
        res = fut.result(timeout=WALL)
    except Exception:
        res = None
    if res is None:
        # build still running in background: it will fill the cache; ask for a
        # retry instead of lying with an empty list.
        _log({"t": int(time.time()), "id": imdb, "se": se, "ep": ep,
              "wall": True, "ms": int(WALL * 1000)})
        return {"streams": [],
                "message": "%s is still resolving — tap streams again in a few "
                           "seconds" % BRAND}
    cards = res.get("streams") or []
    if cards:
        C_STREAM.put(key, cards, _STREAM_TTL)
        C_STALE[key] = (time.time() + _STREAM_STALE, cards)
        _sweep()
    else:
        msg = res.get("message") or ""
        _STATS["empties"] += 1
        if "transient" in msg or "timed out" in msg:
            # the network was sick, not the title: cache NOTHING, so the next
            # tap retries immediately instead of serving a 5-minute false empty.
            pass
        else:
            # honest negative (the site really answered) -> short cache + retry
            C_STREAM.put(key, [], _NEG_TTL)

        def retry():
            try:
                r2 = _build_inner(ctype, imdb, se, ep, time.time() + WALL + 8)
                c2 = r2.get("streams") or []
                if c2:
                    C_STREAM.put(key, c2, _STREAM_TTL)
                    C_STALE[key] = (time.time() + _STREAM_STALE, c2)
            except Exception:
                pass
        _neg_bg_retry(key, retry)
        res = dict(res)
        res["message"] = msg
    _log({"t": int(time.time()), "id": imdb, "se": se, "ep": ep,
          "cards": len(cards), "msg": (res.get("message") or "")[:60]})
    # binge prewarm: the next episode of the same series
    if ctype == "series" and cards and ep:
        threading.Thread(target=_prewarm_next, args=(ctype, imdb, se, ep + 1),
                         daemon=True).start()
    return res


def _prewarm_next(ctype, imdb, se, ep):
    try:
        key = (ctype, imdb, se, ep)
        hit, _v = C_STREAM.get(key)
        if hit:
            return
        res = _build_inner(ctype, imdb, se, ep, time.time() + WALL + 10)
        cards = res.get("streams") or []
        if cards:
            C_STREAM.put(key, cards, _STREAM_TTL)
            C_STALE[key] = (time.time() + _STREAM_STALE, cards)
    except Exception:
        pass


# ═════════════════════════════════════ 13b. CATALOG · META · CONFIGURATION
# Stremio's own catalogs never list Bangla regional titles and TMDB/IMDb are
# missing a chunk of what BanglaPlex carries. So the addon browses the site
# itself, and when IMDb/TMDB/Cinemeta have nothing for a card it falls back to
# the SOURCE's own metadata (og:* tags + the cast/director/country/release
# rows). No catalog item is ever an empty shell: each one either maps to a real
# IMDb id or carries a `bpx:<slug>` id that this addon can BOTH stream and
# describe.

CFG_DEFAULTS = {
    "n": MAX_CARDS,        # max cards per title
    "subs": "en,hi,bn",    # subtitle language preference, or "off"
    "q": "all",            # "all" | "1080" | "720"  (minimum resolution tier)
    "cdn": "both",         # "both" | "tiktok" | "cf"  (server preference)
    "cat": "all",          # "all" | "movie" | "series" | "off"
    "tmdb": "",            # the user's OWN TMDB v3 key — never stored server-side
}
_CFG_TLS = threading.local()
_Q_TIERS = {"all": 0, "720": 1, "1080": 2}
_Q_RANK = {"4320p": 6, "2160p": 5, "1080p": 4, "720p": 3, "480p": 2, "360p": 1}


def cfg_pack(cfg):
    """config -> short URL-safe segment (only keys that differ from default)."""
    slim = {}
    for k, v in (cfg or {}).items():
        if k in CFG_DEFAULTS and str(v) != str(CFG_DEFAULTS[k]):
            slim[k] = v
    if not slim:
        return ""
    raw = json.dumps(slim, separators=(",", ":"), sort_keys=True).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def cfg_unpack(seg):
    """URL segment -> config dict, or None when it is not a config segment."""
    cfg = dict(CFG_DEFAULTS)
    if not seg:
        return cfg
    try:
        slim = json.loads(base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4)).decode())
        if not isinstance(slim, dict):
            return None
    except Exception:
        return None
    for k, v in slim.items():
        if k not in CFG_DEFAULTS or not isinstance(v, (str, int, float)):
            continue
        v = str(v).strip()
        if k == "n":
            try:
                cfg["n"] = max(1, min(MAX_CARDS, int(float(v))))
            except Exception:
                pass
        elif k == "subs":
            langs = [x.strip().lower() for x in v.split(",") if x.strip()]
            cfg["subs"] = "off" if v.lower() == "off" else ",".join(langs[:6]) \
                or CFG_DEFAULTS["subs"]
        elif k in ("q", "cdn", "cat"):
            allowed = {"q": _Q_TIERS, "cdn": ("both", "tiktok", "cf"),
                       "cat": ("all", "movie", "series", "off")}[k]
            if v.lower() in allowed:
                cfg[k] = v.lower()
        elif k == "tmdb":
            if re.fullmatch(r"[A-Za-z0-9]{20,64}", v):
                cfg["tmdb"] = v
    return cfg


def set_cfg(cfg):
    _CFG_TLS.cfg = cfg or dict(CFG_DEFAULTS)


def get_cfg():
    return getattr(_CFG_TLS, "cfg", None) or CFG_DEFAULTS


def apply_cfg(cards, cfg=None):
    """Post-filter built cards by the requesting install's config, then strip the
    private build markers. Filtering happens here (not inside the build) so the
    shared cache stays config-independent and one build serves every config."""
    cfg = cfg or get_cfg()
    out = []
    subs_want = None if (cfg.get("subs") or "").lower() == "off" else \
        [x.strip().lower() for x in (cfg.get("subs") or "").split(",") if x.strip()]
    minq = _Q_TIERS.get(cfg.get("q") or "all", 0)
    cdn = (cfg.get("cdn") or "both").lower()
    pool = []
    for c in cards or []:
        rank = _Q_RANK.get(c.get("_res") or "", 0)
        # an UNMEASURED resolution (rank 0) is never dropped: the site sometimes
        # labels a file "HDTC" with no playlist to measure, and hiding it would
        # look exactly like a dead title
        if minq and rank and rank < (4 if minq == 2 else 3):
            continue                       # below the requested floor
        pool.append(c)
    if cdn == "tiktok" and pool:
        pool = [c for c in pool if "tiktok" in (c.get("_cdn") or "").lower()] or pool
    elif cdn == "cf" and pool:
        pool = [c for c in pool if "tiktok" not in (c.get("_cdn") or "").lower()] or pool
    for c in pool:
        c = dict(c)
        if subs_want is None:
            c.pop("subtitles", None)
        elif c.get("subtitles"):
            order = {l: i for i, l in enumerate(subs_want)}
            subs = sorted(c["subtitles"],
                          key=lambda s: order.get((s.get("lang") or "").lower(), 99))
            keep = [s for s in subs if (s.get("lang") or "").lower() in order] or subs
            if keep:
                c["subtitles"] = keep
                c["description"] = re.sub(r"⟡ \d+ SUB", "⟡ %d SUB" % len(keep),
                                          c.get("description") or "")
            else:
                c.pop("subtitles", None)
        for k in ("_cdn", "_res", "_tier", "_nsubs"):
            c.pop(k, None)
        out.append(c)
    return out[:max(1, int(cfg.get("n") or MAX_CARDS))]


# ── site listing parse ───────────────────────────────────────────────────────
_CARD_SPLIT = 'class="col-md-2 col-sm-3 col-xs-6"'
# the card container's column class VARIES by page (homepage/genre use col-xs-6,
# /year/ uses col-xs-4), so the poster div is the only anchor present in every
# layout — splitting on a column class silently returned 0 cards for a whole
# shelf (measured on prod: /year/2026.html, 24 cards in the HTML, 0 parsed)
_POSTER_RE = re.compile(
    r"""latest-movie-img-container lazy"\s*style="background-image:\s*url\('([^']*)'\)""")
_LIST_TTL = 45 * 60
_IMDB_TTL = 24 * 3600
_META_RES_TTL = 12 * 3600
_PAGE_N = 24                                  # the site's own page size


def parse_listing(h):
    """OVOO card grid -> [{slug,url,title,poster,year,quality,series,rating}].

    Splitting on the card container and running small regexes per card is far
    sturdier than one big multi-line pattern: the grid mixes trending badges,
    lazy background-images and TV labels in varying order."""
    out, seen = [], set()
    h = h or ""
    marks = list(_POSTER_RE.finditer(h))
    chunks = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else min(len(h), m.end() + 6000)
        chunks.append((m.group(1), h[m.start():end]))
    if not chunks:                       # layout without lazy posters: old split
        chunks = [("", c) for c in h.split(_CARD_SPLIT)[1:]]
    for poster, chunk in chunks:
        m = re.search(r"/watch/([a-z0-9\-_.]+?)\.html", chunk)
        if not m:
            continue
        slug = m.group(1)
        if slug in seen:
            continue
        tm = re.search(r'class="movie-title">\s*<h3>\s*<a[^>]*>([^<]{1,160})</a>', chunk)
        if not tm:
            tm = re.search(r'<a[^>]+href="[^"]*/watch/[^"]+"[^>]*>([^<]{3,160})</a>', chunk)
        title = _html.unescape(tm.group(1)).strip() if tm else slug.replace("-", " ").title()
        pm = re.search(r"background-image:\s*url\('([^']+)'\)", chunk) or \
            re.search(r'<img[^>]+src="([^"]+)"', chunk)
        ym = re.search(r'label-year">\s*((?:19|20)\d{2})', chunk)
        qm = re.search(r'label-primary">\s*([^<]{1,20}?)\s*<', chunk)
        rm = re.search(r'IMDB\s*([\d.]+)', chunk)
        seen.add(slug)
        out.append({
            "slug": slug,
            "url": SITE + "/watch/" + slug + ".html",
            "title": title,
            "poster": _html.unescape(poster or (pm.group(1) if pm else "")).strip(),
            "year": int(ym.group(1)) if ym else None,
            "quality": _html.unescape(qm.group(1)).strip() if qm else "",
            "series": "label-tvseries" in chunk,
            "rating": float(rm.group(1)) if rm and rm.group(1) not in ("0", "0.0") else 0.0,
            "trending": "video_trending_badge" in chunk,
        })
    return out


_LIST_STALE = {}          # url -> (usable_until, items)
_LIST_REFRESH = set()
_LIST_STALE_TTL = 12 * 3600
_EMPTY_PAGE_MIN = 12000       # below this a 0-card page is a broken fetch, not empty


def _list_revalidate(url, timeout):
    try:
        r = _get(url, timeout=timeout, referer=SITE + "/")
        if r is not None and r.status_code == 200:
            items = parse_listing(r.text or "")
            if items:
                C_LIST.put(url, items, _LIST_TTL)
                _LIST_STALE[url] = (time.time() + _LIST_STALE_TTL, items)
    except Exception:
        pass
    finally:
        _LIST_REFRESH.discard(url)


def list_page(url, timeout=18):
    """fetch + parse one listing page (cached parsed, never the raw HTML).

    Stale-while-revalidate: the homepage is ~375 KB and, from a Cloudflare-flagged
    egress, has to ride a free exit (measured ~9 s). Nobody should stare at a
    spinner for that when yesterday's shelf is still perfectly good — serve the
    stale page and refresh it in the background."""
    hit, val = C_LIST.get(url)
    if hit:
        return val
    st = _LIST_STALE.get(url)
    if st and st[0] > time.time() and st[1] and url not in _LIST_REFRESH:
        _LIST_REFRESH.add(url)
        threading.Thread(target=_list_revalidate, args=(url, timeout),
                         daemon=True, name="listswr").start()
        return st[1]
    r = _get(url, timeout=timeout, referer=SITE + "/")
    if r is None:
        return None                                    # transient: cache nothing
    h = r.text or ""
    items = parse_listing(h)
    if items:
        C_LIST.put(url, items, _LIST_TTL)
        _LIST_STALE[url] = (time.time() + _LIST_STALE_TTL, items)
        return items
    if r.status_code != 200 or len(h) < _EMPTY_PAGE_MIN:
        # a genuinely empty shelf (e.g. /type/web-series.html) is still a ~40 KB
        # page; anything smaller is a truncated or challenged body, and caching
        # THAT as "no such titles" would blank a whole shelf for 5 minutes
        return None
    C_LIST.put(url, [], _NEG_TTL)                      # honest empty page
    return []


_PREWARM_BUSY = [False]
PREWARM_N = int(os.environ.get("BPX_PREWARM_STREAMS", "6"))


def _prewarm_shelf(metas, ctype):
    """Warm the streams for the first few cards of a shelf the user just opened,
    so tapping one answers from cache instead of paying 3-10 s. Bounded: two at
    a time, first page only, never for something already cached."""
    if PREWARM_N <= 0 or _PREWARM_BUSY[0]:
        return
    todo = []
    for m in metas[:PREWARM_N]:
        mid = m.get("id") or ""
        if not (mid.startswith("tt") or mid.startswith("bpx-")):
            continue
        if C_STREAM.get((ctype, mid, None, None))[0]:
            continue
        todo.append(mid)
    if not todo:
        return
    _PREWARM_BUSY[0] = True

    def run():
        # Everything is optional work: a prewarm must never be able to kill its
        # thread (and never leave the busy flag stuck, which would switch shelf
        # prewarming off for the life of the process). submit() itself can raise
        # — an executor that is already shut down at exit, for instance.
        try:
            for i in range(0, len(todo), 2):
                futs = []
                for mid in todo[i:i + 2]:
                    try:
                        futs.append(_BUILD_EX.submit(build_streams, ctype, mid,
                                                     None, None))
                    except Exception:
                        pass
                for f in futs:
                    try:
                        f.result(timeout=WALL + 15)
                    except Exception:
                        pass
        except Exception:
            pass
        finally:
            _PREWARM_BUSY[0] = False
    threading.Thread(target=run, daemon=True, name="shelfwarm").start()


def catalog_prewarm():
    """Warm the three default shelves at boot so the first user does not pay for
    a 375 KB proxied fetch (and so the proxy pool gets trained early)."""
    for ctype, cat_id in (("movie", "bpx-latest"), ("movie", "bpx-year"),
                          ("series", "bpx-series")):
        try:
            catalog_items(ctype, cat_id)
        except Exception:
            pass


def _slugify(s):
    return re.sub(r"[^a-z0-9\-]+", "", (s or "").lower().strip())[:60]


MOVIE_GENRES = ["bengali-movies", "bollywood-movies", "hollywood-movies",
                "south-indian-movies", "dual-audio-movies", "chinese-movies",
                "japanese-movies", "korean-movies", "indonesia-movie", "action",
                "comedy", "crime", "drama", "family", "fantasy", "history",
                "horror", "thriller", "western", "documentary", "kids"]
SERIES_GENRES = ["bengali-web-series", "hollywood-web-series", "bollywood-series",
                 "korean-web-series", "japanese-series", "dual-audio-series"]
_THIS_YEAR = time.localtime().tm_year

CAT_DEFS = [
    {"type": "movie", "id": "bpx-latest", "name": "%s · Latest" % BRAND,
     "extra": [{"name": "search", "isRequired": False},
               {"name": "genre", "isRequired": False, "options": MOVIE_GENRES},
               {"name": "skip", "isRequired": False}]},
    {"type": "movie", "id": "bpx-year", "name": "%s · %d" % (BRAND, _THIS_YEAR),
     "extra": [{"name": "search", "isRequired": False},
               {"name": "skip", "isRequired": False}]},
    {"type": "series", "id": "bpx-series", "name": "%s · Series" % BRAND,
     "extra": [{"name": "search", "isRequired": False},
               {"name": "genre", "isRequired": False, "options": SERIES_GENRES},
               {"name": "skip", "isRequired": False}]},
]
CAT_IDS = {(c["type"], c["id"]) for c in CAT_DEFS}
MANIFEST["catalogs"] = CAT_DEFS          # MANIFEST is defined in section 1


def catalog_source(cat_id, ctype, genre, skip):
    """(url, mode) for one catalog page. Pagination on this site is PATH-based
    (`/genre/action/24.html` = offset 24) — `?page=` is silently ignored."""
    skip = max(0, int(skip or 0))
    off = (skip // _PAGE_N) * _PAGE_N
    g = _slugify(genre)
    if g:
        return (SITE + ("/genre/%s.html" % g if not off else "/genre/%s/%d.html" % (g, off)),
                "page")
    if cat_id == "bpx-year":
        y = _THIS_YEAR
        return (SITE + ("/year/%d.html" % y if not off else "/year/%d/%d.html" % (y, off)),
                "page")
    if cat_id == "bpx-series":
        g = "bengali-web-series"
        return (SITE + ("/genre/%s.html" % g if not off else "/genre/%s/%d.html" % (g, off)),
                "page")
    return SITE + "/", "home"


def imdb_suggest_title(title, year=None, ctype="movie"):
    """title -> IMDb id, or None. Strict on purpose: a wrong tt id would make
    Stremio show a DIFFERENT film's poster/synopsis, which is worse than no
    mapping at all (then the source metadata serves the card)."""
    q = re.sub(r"\s+", "_", re.sub(r"[^a-z0-9 ]", "", (title or "").lower()).strip())
    if len(q) < 2:
        return None
    key = ("sg", q, year or 0, ctype)
    hit, val = C_IMDB.get(key)
    if hit:
        return val or None
    out = None
    try:
        r = _S.get("https://v2.sg.media-imdb.com/suggestion/%s/%s.json" % (q[0], q),
                   timeout=7, headers={"User-Agent": UA})
        arr = (r.json() or {}).get("d") if r.status_code == 200 else None
    except Exception:
        arr = None
    if isinstance(arr, list):
        nt = _norm_title(title)
        want = {"movie": ("movie", "tvmovie", "video"),
                "series": ("tvseries", "tvmovie", "tvminiseries")}.get(ctype, ())
        for it in arr[:8]:
            iid = it.get("id") or ""
            if not iid.startswith("tt"):
                continue
            l = _norm_title(it.get("l"))
            if not l or not nt:
                continue
            same = (l == nt) or (len(l) >= 4 and len(nt) >= 4 and (l in nt or nt in l))
            if not same:
                continue
            qid = (it.get("qid") or "").lower()
            if qid and want and qid not in want:
                continue
            y = it.get("y")
            if year and y and abs(int(y) - int(year)) > 1:
                continue
            out = iid
            break
    C_IMDB.put(key, out or "", _IMDB_TTL)
    return out


def _map_ids(items, ctype, budget=14.0):
    """site cards -> Stremio ids, in bounded parallel waves (24 suggest calls at
    once gets 429s). Unmapped items keep a routable bpx:<slug> id."""
    ddl = time.time() + budget
    todo = [it for it in items if not it.get("id")]
    futs = {}
    for it in todo:
        if time.time() >= ddl:
            break
        futs[_IO_EX.submit(imdb_suggest_title, it["title"], it.get("year"), ctype)] = it
    for f in as_completed(futs):
        it = futs[f]
        try:
            tt = f.result(timeout=max(0.2, ddl - time.time()))
        except Exception:
            tt = None
        it["id"] = tt or ("bpx-" + it["slug"])
        if tt:
            # remember the site slug for this id: a later /stream can skip the
            # autocomplete hop entirely (one less proxied fetch ≈ 2 s on Render)
            C_SLUG.put((ctype, tt), it["slug"], _IMDB_TTL)
    for it in items:
        if not it.get("id"):
            it["id"] = "bpx-" + it["slug"]
    return items


def catalog_items(ctype, cat_id, genre=None, search=None, skip=0, cfg=None):
    """one catalog page -> Stremio meta previews (never empty shells)."""
    cfg = cfg or get_cfg()
    skip = max(0, int(skip or 0))
    ck = (ctype, cat_id, _slugify(genre), (search or "").strip().lower(), skip,
          cfg.get("tmdb") or "")
    hit, val = C_LIST.get(("cat", ck))
    if hit:
        return val
    if (search or "").strip():
        cands = _search_autocomplete(search.strip())
        if cands is None:
            return []                        # transient: empty now, retry later
        items = []
        for c in cands[:_PAGE_N * 2]:
            slug = urlparse(c["url"]).path.split("/watch/")[-1].replace(".html", "")
            is_series = "series" in (c.get("type") or "").lower() or "tv" in (c.get("type") or "").lower()
            if ctype == "series" and not is_series:
                continue
            if ctype == "movie" and is_series:
                continue
            items.append({"slug": slug, "url": c["url"], "title": c["title"],
                          "poster": c.get("image") or "", "year": _year_of(c["title"]),
                          "quality": "", "series": is_series, "rating": 0.0})
        items = items[:_PAGE_N]
    else:
        url, mode = catalog_source(cat_id, ctype, genre, skip)
        got = list_page(url)
        if got is None:
            return []
        if mode == "home":
            got = [c for c in got if c["series"] == (ctype == "series")]
            items = got[skip:skip + _PAGE_N]        # the homepage is one big page
        else:
            items = [c for c in got if (ctype == "series") == c["series"]] or \
                    (got if ctype == "series" else got)
            items = items[:_PAGE_N]
    if not items:
        C_LIST.put(("cat", ck), [], _NEG_TTL)
        return []
    items = [dict(it) for it in items]
    _map_ids(items, ctype)
    metas = []
    for it in items:
        m = {"id": it["id"], "type": ctype, "name": it["title"],
             "posterShape": "poster"}
        if it.get("poster"):
            m["poster"] = it["poster"]
        if it.get("year"):
            m["year"] = it["year"]
        if it.get("rating"):
            m["imdbRating"] = "%.1f" % it["rating"]
        bits = [b for b in (it.get("quality") or "", "TRENDING" if it.get("trending") else "")
                if b]
        if bits:
            m["description"] = " · ".join(bits)
        if it["id"].startswith("bpx-"):
            m["bpxSource"] = it["url"]
        metas.append(m)
    C_LIST.put(("cat", ck), metas, _LIST_TTL)
    return metas


# ── meta: providers first, SOURCE fallback second ────────────────────────────
def _cinemeta_full(ctype, imdb):
    hit, val = C_META.get(("cmf", ctype, imdb))
    if hit:
        return val
    out = None
    try:
        r = _S.get("%s/meta/%s/%s.json" % (CINEMETA, ctype, imdb), timeout=8)
        if r.status_code == 200:
            m = (r.json() or {}).get("meta")
            if isinstance(m, dict) and m.get("name"):
                out = m
    except Exception:
        out = None
    C_META.put(("cmf", ctype, imdb), out, _META_TTL if out else _NEG_TTL)
    return out


def _tmdb_full(ctype, imdb, key=None):
    key = key or TMDB_KEY
    if not key:
        return None
    hit, val = C_META.get(("tmf", ctype, imdb, key[-4:]))
    if hit:
        return val
    out = None
    try:
        r = _S.get("https://api.themoviedb.org/3/find/%s?external_source=imdb_id"
                   "&api_key=%s" % (imdb, key), timeout=8)
        j = r.json() if r.status_code == 200 else {}
        res = (j.get("movie_results") or j.get("tv_results") or [None])[0]
        if res:
            kind = "movie" if j.get("movie_results") else "tv"
            r2 = _S.get("https://api.themoviedb.org/3/%s/%s?api_key=%s"
                        "&append_to_response=external_ids,credits" % (kind, res["id"], key),
                        timeout=8)
            d = r2.json() if r2.status_code == 200 else {}
            if d:
                cast = [c.get("name") for c in ((d.get("credits") or {}).get("cast") or [])[:12]]
                crew = (d.get("credits") or {}).get("crew") or []
                out = {
                    "name": d.get("title") or d.get("name"),
                    "poster": ("https://image.tmdb.org/t/p/w500" + d["poster_path"])
                              if d.get("poster_path") else "",
                    "background": ("https://image.tmdb.org/t/p/w1280" + d["backdrop_path"])
                                  if d.get("backdrop_path") else "",
                    "description": d.get("overview") or "",
                    "year": _year_of(d.get("release_date") or d.get("first_air_date") or ""),
                    "genres": [g.get("name") for g in (d.get("genres") or [])],
                    "cast": [c for c in cast if c],
                    "director": next((c.get("name") for c in crew
                                      if c.get("job") == "Director"), ""),
                    "runtime": ("%d min" % d["runtime"]) if d.get("runtime") else "",
                    "imdbRating": ("%.1f" % d["vote_average"]) if d.get("vote_average") else "",
                    "releaseInfo": (d.get("release_date") or d.get("first_air_date") or "")[:4],
                }
    except Exception:
        out = None
    C_META.put(("tmf", ctype, imdb, key[-4:]), out, _META_TTL if out else _NEG_TTL)
    return out


def meta_from_page(page, ctype, imdb=None):
    """SOURCE metadata: everything the watch page knows. This is the fallback
    for titles TMDB/IMDb/Cinemeta never heard of (most Bangla regional ones)."""
    if not page:
        return None
    title = page.get("title") or ""
    m = {"id": imdb or ("bpx-" + page.get("slug", "")), "type": ctype,
         "name": title, "posterShape": "square" if ctype == "series" else "poster"}
    if page.get("poster"):
        m["poster"] = page["poster"]
        m["background"] = page["poster"]
    if page.get("plot"):
        m["description"] = page["plot"]
    if page.get("year"):
        m["year"] = int(page["year"])
        m["releaseInfo"] = str(page["year"])
    if page.get("release"):
        m["released"] = page["release"]
    if page.get("duration"):
        m["runtime"] = _fmt_dur(page["duration"])
    for src, dst in (("genre", "genres"), ("country", "country"),
                     ("actors", "cast"), ("director", "director"),
                     ("quality", "quality")):
        v = page.get(src)
        if not v:
            continue
        if dst == "genres":
            m["genres"] = [g.strip() for g in re.split(r"[,|]", v) if g.strip()][:10]
        elif dst == "cast":
            m["cast"] = [a.strip() for a in re.split(r"[,|]", v) if a.strip()][:12]
        else:
            m[dst] = v.strip()
    if page.get("slug"):
        m["bpxSlug"] = page["slug"]
    return m


def _needs_site(meta):
    """True when the providers left a visible hole the source can fill."""
    if not meta:
        return True
    return not (meta.get("poster") and meta.get("description"))


def _needs_provider(meta):
    """True when the SOURCE left a hole a provider can fill. The fallback has to
    run both ways: brand-new Bangla series often have a fuller page on
    TMDB/Cinemeta (cast, runtime, rating) than on the site itself."""
    if not meta:
        return False
    return not (meta.get("cast") and meta.get("genres") and meta.get("description"))


def _provider_meta(ctype, imdb, cfg=None):
    """Cinemeta ∥ TMDB -> one merged dict (None when neither knows the id)."""
    cfg = cfg or get_cfg()
    meta = None
    futs = [_IO_EX.submit(_cinemeta_full, ctype, imdb)]
    key = cfg.get("tmdb") or TMDB_KEY
    if key:
        futs.append(_IO_EX.submit(_tmdb_full, ctype, imdb, key))
    for f in as_completed(futs, timeout=10):
        try:
            got = f.result(timeout=0)
        except Exception:
            got = None
        if not got:
            continue
        if meta is None:
            meta = dict(got)
        else:
            meta = _merge_meta(meta, got)
        if not _needs_site(meta):
            break
    return meta


def _site_meta_for(ctype, title, year=None, slug=None):
    """source-side meta by slug, or by a site search when we only have a title."""
    if slug:
        return meta_from_page(parse_watch_page(SITE + "/watch/%s.html" % slug), ctype)
    if not title:
        return None
    cands = search_candidates(re.sub(r"\s*\((?:19|20)\d{2}\)\s*$", "", title).strip())
    if not cands:
        return None
    matched = match_candidates(cands, title, year, ctype) or cands[:1]
    return meta_from_page(parse_watch_page(matched[0]["url"]), ctype)


def build_meta(ctype, mid, cfg=None):
    """Providers race first; the SOURCE fills whatever they don't have."""
    cfg = cfg or get_cfg()
    ck = (ctype, mid, (cfg.get("tmdb") or "")[-4:])
    hit, val = C_METARES.get(ck)
    if hit:
        return val
    meta = None
    if mid.startswith("tt"):
        meta = _provider_meta(ctype, mid, cfg)
        if _needs_site(meta):
            name = (meta or {}).get("name") or ""
            year = _year_of((meta or {}).get("releaseInfo") or
                            str((meta or {}).get("year") or ""))
            if not name:
                # brand-new title: no provider knows the id at all, so there is
                # nothing to search the site with yet. Resolve the NAME from the
                # id (IMDb-suggest-by-id is part of the consensus) and then fall
                # back to the source — measured on prod: /meta/series/tt43695931
                # answered {} while the site had a full page for it.
                for nm, yr, _t in (resolve_meta_all(ctype, mid) or []):
                    if nm:
                        name, year = nm, (year or yr)
                        break
            site = _site_meta_for(ctype, name, year)
            if site:
                meta = _merge_meta(meta, site)
                meta.setdefault("name", site.get("name"))
        if meta:
            meta["id"], meta["type"] = mid, ctype
    elif mid.startswith("bpx-"):
        meta = _site_meta_for(ctype, "", slug=mid[4:])
        if _needs_provider(meta):
            # reverse direction: the source id may still be a known title under a
            # slightly different name — strict suggest, so a wrong match is
            # impossible by construction
            tt = imdb_suggest_title(meta.get("name") or "", meta.get("year"), ctype)
            if tt:
                prov = _provider_meta(ctype, tt, cfg)
                if prov:
                    meta = _merge_meta(meta, prov)
                    meta["imdb_id"] = tt
    if meta:
        meta = normalize_meta(meta)
        C_METARES.put(ck, meta, _META_RES_TTL)
    return meta


def normalize_meta(m):
    """One shape for every source: providers and the site disagree about types
    (Cinemeta sends `director` as a list and no `year`, only `releaseInfo`; the
    site sends comma strings). Stremio renders whatever it gets, so an unshaped
    field shows up as a blank line on the detail page."""
    if not m:
        return m
    out = dict(m)
    y = out.get("year") or _year_of(str(out.get("releaseInfo") or ""))
    if y:
        out["year"] = int(y)
        out.setdefault("releaseInfo", str(y))
    for k in ("director", "country", "language", "quality"):
        v = out.get(k)
        if isinstance(v, (list, tuple)):
            out[k] = ", ".join(str(x).strip() for x in v if x)
        elif isinstance(v, str):
            out[k] = re.sub(r"\s*,\s*", ", ", v).strip(" ,")
    for k in ("cast", "genres"):
        v = out.get(k)
        if isinstance(v, str):
            out[k] = [x.strip() for x in re.split(r"[,|]", v) if x.strip()]
        elif isinstance(v, (list, tuple)):
            out[k] = [str(x).strip() for x in v if x]
    if isinstance(out.get("cast"), list):
        out["cast"] = out["cast"][:20]
    if isinstance(out.get("description"), str):
        out["description"] = re.sub(r"\s+", " ", out["description"]).strip()
    return {k: v for k, v in out.items() if v not in ("", [], None, {})}


def _merge_meta(base, fill):
    """provider meta wins field-by-field; the source only fills holes."""
    out = dict(base or {})
    for k, v in (fill or {}).items():
        if not v or out.get(k):
            continue
        out[k] = v
    if (base or {}).get("name") and not out.get("name"):
        out["name"] = base["name"]
    return out


def validate_tmdb_key(key):
    if not re.fullmatch(r"[A-Za-z0-9]{20,64}", (key or "").strip()):
        return False
    try:
        r = _S.get("https://api.themoviedb.org/3/configuration?api_key=%s"
                   % key.strip(), timeout=8)
        return r.status_code == 200
    except Exception:
        return False


CONFIG_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__NAME__ — Configure</title>
<style>
:root{--bg:#0a0a0b;--card:#16161a;--line:#2a2a30;--tx:#fff;--mut:#9aa0a8;--acc:#ff277d}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:var(--bg);
color:var(--tx);line-height:1.5;padding:26px 14px 60px}
.wrap{max-width:620px;margin:0 auto}
h1{font-size:27px;letter-spacing:-.4px}
.v{color:var(--mut);font-size:13px;font-weight:400;margin-left:8px}
.tag{color:var(--mut);margin:2px 0 20px;font-size:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;
padding:18px;margin:14px 0}
.card h2{font-size:12px;text-transform:uppercase;letter-spacing:.09em;color:var(--mut);
margin-bottom:12px;font-weight:600}
.row{display:flex;align-items:center;justify-content:space-between;gap:14px;
padding:9px 0;border-top:1px solid #1f1f26}
.row:first-of-type{border-top:0}
.row label{font-size:14px}
.row .hint{display:block;color:var(--mut);font-size:12px;margin-top:2px}
select,input[type=text]{background:#0a0a0b;color:#fff;border:1px solid var(--line);
border-radius:9px;padding:9px 11px;font-size:14px;outline:none;min-width:150px}
select:focus,input[type=text]:focus{border-color:var(--acc)}
.chips{display:flex;flex-wrap:wrap;gap:7px}
.chip{border:1px solid var(--line);background:#0a0a0b;color:var(--mut);border-radius:20px;
padding:6px 12px;font-size:13px;cursor:pointer;user-select:none}
.chip.on{background:var(--acc);border-color:var(--acc);color:#fff;font-weight:600}
.btn{display:block;width:100%;background:var(--acc);color:#fff;border:0;border-radius:11px;
padding:14px;font-size:16px;font-weight:700;cursor:pointer;text-align:center;
text-decoration:none}
.btn:hover{background:#e0106a}
.btn.ghost{background:#22222a;color:#cfd3d8}
.url{width:100%;background:#0a0a0b;border:1px solid var(--line);color:#4ade80;
border-radius:9px;padding:10px;font-family:ui-monospace,Menlo,monospace;font-size:11.5px;
word-break:break-all;margin-top:10px}
.msg{margin-top:10px;padding:10px;border-radius:9px;font-size:13px;display:none}
.msg.ok{display:block;background:#0d2818;border:1px solid #1a5c2e;color:#4ade80}
.msg.err{display:block;background:#2d0a0a;border:1px solid #5c1a1a;color:#f87171}
.two{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:10px}
.small{color:var(--mut);font-size:12.5px}
.badges span{display:inline-block;background:#22222a;border:1px solid var(--line);
color:#cfd3d8;padding:3px 10px;border-radius:12px;font-size:11.5px;margin:0 4px 6px 0}
a{color:var(--acc)}
</style></head><body><div class="wrap">
<h1>🎬 __NAME__<span class="v">v__VERSION__</span></h1>
<p class="tag">Bangla · Hindi · Hollywood movies &amp; web-series from BanglaPlex —
direct CDN streams, own catalogs, zero server bandwidth.</p>
<div class="badges"><span>📺 Catalogs</span><span>⚡ Direct CDN</span><span>💬 Multi-lang subs</span>
<span>🛡️ Proxy pool</span><span>0 bytes relayed</span></div>

<div class="card"><h2>Playback</h2>
<div class="row"><div><label>Stream cards per title</label>
<span class="hint">Different CDN / server for the same file</span></div>
<select id="n"></select></div>
<div class="row"><div><label>Minimum quality</label>
<span class="hint">Cards below this are dropped</span></div>
<select id="q"><option value="all">Everything</option>
<option value="720">720p and up</option><option value="1080">1080p only</option></select></div>
<div class="row"><div><label>Preferred server</label>
<span class="hint">TikTok-CDN needs no headers; Cloudflare needs a Referer</span></div>
<select id="cdn"><option value="both">Both</option>
<option value="tiktok">TikTok CDN first only</option>
<option value="cf">Cloudflare only</option></select></div>
<div class="row"><div><label>Subtitles</label>
<span class="hint">Tap languages to reorder — leftmost wins</span></div>
<div class="chips" id="subs"></div></div>
</div>

<div class="card"><h2>Catalogs</h2>
<div class="row"><div><label>BanglaPlex shelves in your board</label>
<span class="hint">Latest · this year · Series (with genre + search filters)</span></div>
<select id="cat"><option value="all">Movies + Series</option>
<option value="movie">Movies only</option><option value="series">Series only</option>
<option value="off">No catalogs (streams only)</option></select></div>
</div>

<div class="card"><h2>TMDb key (optional)</h2>
<div class="row"><div><label>Your own API key</label>
<span class="hint">Used only for richer posters/synopses. Never stored — it lives in
your install URL. Free at <a href="https://www.themoviedb.org/settings/api"
target="_blank" rel="noopener">themoviedb.org</a>. Without it IMDb + Cinemeta +
the BanglaPlex source itself still cover every card.</span></div>
<div style="min-width:190px"><input type="text" id="tmdb" placeholder="32-char key"
autocomplete="off" spellcheck="false" style="width:100%">
<button class="btn ghost" style="margin-top:8px;padding:9px" onclick="checkKey()">Check key</button></div></div>
<div class="msg" id="kmsg"></div>
</div>

<div class="card"><h2>Install</h2>
<a class="btn" id="go" href="#">Install in Stremio</a>
<div class="two"><button class="btn ghost" onclick="copyUrl()">Copy URL</button>
<button class="btn ghost" onclick="resetAll()">Reset</button></div>
<input class="url" id="iu" readonly onclick="this.select()">
<div class="msg" id="msg"></div>
<p class="small" style="margin-top:12px">Manifest: <a id="mlink" href="#">__BASE__/manifest.json</a>
· <a href="/health">health</a> · <a href="/">about</a></p>
</div>
<p class="small" style="text-align:center;margin-top:18px">__BASE__ · v__VERSION__ ·
config travels inside the install URL, nothing is stored on the server</p>
</div>
<script>
const BASE=location.origin, DEF=__DEF__;
let CFG=Object.assign({},DEF,__CFG__), NMAX=__NMAX__;
const LANGS=[["en","English"],["hi","Hindi"],["bn","Bangla"],["ta","Tamil"],
["te","Telugu"],["ko","Korean"],["ja","Japanese"],["ar","Arabic"],["id","Indonesian"]];

function buildUI(){
  const n=document.getElementById("n"); n.innerHTML="";
  for(let i=1;i<=NMAX;i++){const o=document.createElement("option");
    o.value=i;o.textContent=i+(i===1?" card":" cards");n.appendChild(o);}
  n.value=CFG.n; n.onchange=()=>{CFG.n=parseInt(n.value);render();};
  for(const k of ["q","cdn","cat"]){
    const el=document.getElementById(k); el.value=CFG[k];
    el.onchange=()=>{CFG[k]=el.value;render();};
  }
  document.getElementById("tmdb").value=CFG.tmdb||"";
  document.getElementById("tmdb").oninput=e=>{CFG.tmdb=e.target.value.trim();render();};
  const box=document.getElementById("subs"); box.innerHTML="";
  const off=document.createElement("span");
  off.className="chip"+(CFG.subs==="off"?" on":""); off.textContent="None";
  off.onclick=()=>{CFG.subs=CFG.subs==="off"?"en,hi,bn":"off";buildUI();render();};
  box.appendChild(off);
  cur=(CFG.subs==="off"?[]:CFG.subs.split(",").filter(Boolean));
  for(const [code,name] of LANGS){
    const c=document.createElement("span");
    const on=cur.includes(code); c.className="chip"+(on?" on":""); c.textContent=name;
    c.onclick=()=>{
      if(CFG.subs==="off")CFG.subs="";
      let l=CFG.subs.split(",").filter(Boolean);
      if(l.includes(code)) l=l.filter(x=>x!==code); else l.push(code);
      CFG.subs=l.join(",")||"off"; buildUI(); render();
    };
    box.appendChild(c);
  }
}
function pack(cfg){
  const slim={};
  for(const k of Object.keys(cfg).sort()){
    if(String(cfg[k])===String(DEF[k]))continue;
    slim[k]=(k==="n")?parseInt(cfg[k]):String(cfg[k]);
  }
  if(!Object.keys(slim).length)return "";
  return btoa(unescape(encodeURIComponent(JSON.stringify(slim))))
    .replace(/\\+/g,"-").replace(/\\//g,"_").replace(/=+$/,"");
}
function render(){
  const seg=pack(CFG);
  const url=BASE+(seg?"/"+seg:"")+"/manifest.json";
  document.getElementById("iu").value=url;
  document.getElementById("mlink").href=url;
  document.getElementById("mlink").textContent=url;
  const g=document.getElementById("go");
  g.href="stremio://"+url.replace(/^https?:\\/\\//,"");
}
function flash(id,txt,ok){const e=document.getElementById(id);
  e.className="msg "+(ok?"ok":"err");e.textContent=txt;}
async function checkKey(){
  const k=document.getElementById("tmdb").value.trim();
  if(!k){flash("kmsg","No key entered — that is fine, it is optional.",true);return;}
  flash("kmsg","Checking…",true);
  try{
    const r=await fetch("/validate-key?key="+encodeURIComponent(k));
    const d=await r.json();
    flash("kmsg",d.valid?"✓ Key works — TMDb art enabled.":"✗ TMDb rejected that key.",!!d.valid);
  }catch(e){flash("kmsg","✗ Could not reach the validator.",false);}
}
function copyUrl(){
  const i=document.getElementById("iu"); i.select(); i.setSelectionRange(0,1e5);
  navigator.clipboard?.writeText(i.value);
  flash("msg","Install URL copied — paste it into Stremio ▸ Addons ▸ paste link.",true);
}
function resetAll(){CFG=Object.assign({},DEF);buildUI();render();
  flash("msg","Back to defaults.",true);}
const qp=new URLSearchParams(location.search).get("c");
if(qp){try{
  const slim=JSON.parse(decodeURIComponent(escape(atob(qp.replace(/-/g,"+").replace(/_/g,"/")))));
  CFG=Object.assign({},DEF,slim);}catch(e){}}
buildUI();render();
</script></body></html>"""


# ══════════════════════════════════════════════════════════ 14. LANDING PAGE
LANDING = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__NAME__ — Stremio addon</title>
<style>
body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#0b0f14;
color:#e6edf3;margin:0;padding:32px 18px;line-height:1.55}
.wrap{max-width:760px;margin:0 auto}
h1{font-size:26px;margin:0 0 6px}
.sub{color:#8b98a5;margin:0 0 22px}
a.btn{display:inline-block;background:#e50914;color:#fff;text-decoration:none;
padding:12px 18px;border-radius:10px;font-weight:700}
code{background:#151b23;padding:2px 6px;border-radius:6px;color:#9ecbff}
ul{padding-left:20px}li{margin:6px 0}
.card{background:#111820;border:1px solid #1d2733;border-radius:14px;padding:18px;margin:16px 0}
.small{color:#8b98a5;font-size:13px}
</style></head><body><div class="wrap">
<h1>__NAME__ <span class="small">v__VERSION__</span></h1>
<p class="sub">⚡ Stream-only Stremio addon for BanglaPlex — Bangla / Hindi /
Hollywood movies &amp; web-series. Zero-bandwidth: media flows CDN → your player.</p>
<p><a class="btn" href="/configure">⚙️ Configure &amp; install</a>
&nbsp;<a class="btn" style="background:#22222a" href="stremio:///install?addOnUrl=__BASE__/manifest.json">Quick install (defaults)</a></p>
<div class="card"><b>Install URL</b><br><code>__BASE__/manifest.json</code>
<p class="small">Works two ways: browse BanglaPlex&#39;s own shelves
(<b>Latest</b>, <b>this year</b>, <b>Series</b> — with genre + search filters), or open
any title from your existing catalogs (Cinemeta, IMDb, Trakt) and this addon answers
with direct streams. Cards per title, minimum quality, server preference, subtitle
languages and catalog visibility are all configurable — the config travels inside your
install URL and nothing is stored server-side.</p></div>
<div class="card"><b>What it does</b><ul>
<li>Searches BanglaPlex, matches title+year, picks the right video file
(season / episode-pack aware).</li>
<li>Decrypts the player API (AES-128-CBC) and verifies every stream before
showing it — <b>no phantom cards</b>.</li>
<li>Cards point straight at the CDN playlists (TikTok-CDN HLS first, Cloudflare
second) + multi-language VTT subtitles.</li>
</ul></div>
<div class="card small"><b>Health</b> <code>__BASE__/health</code> ·
<b>Debug</b> <code>/debug/reqlog?k=…</code></div>
</div></body></html>"""


# ═══════════════════════════════════════════════════════════ 15. HTTP SERVER
_STREAM_RE = re.compile(r"^/stream/(movie|series)/([^/]+)\.json$")
_CAT_RE = re.compile(r"^/catalog/(movie|series)/([A-Za-z0-9_\-.]+)(?:/([^/]*))?\.json$")
_META_RE = re.compile(r"^/meta/(movie|series)/([^/]+)\.json$")
_TOP_ROUTES = {"stream", "catalog", "meta", "subtitles", "health", "manifest.json",
               "configure", "config", "debug", "validate-key", "install",
               "index.html", "favicon.ico"}
# players call both /subtitles/{type}/{id}.json and the sdk-style
# /subtitles/{type}/{id}/{extra}.json — accept either
_SUBS_RE = re.compile(r"^/subtitles/(movie|series)/([^/]+?)(?:/[^/]*)?\.json$")
_PUBLIC_BASE = [PUBLIC_URL]


def _public_base(handler):
    if _PUBLIC_BASE[0]:
        return _PUBLIC_BASE[0]
    host = handler.headers.get("Host") or "127.0.0.1:%d" % PORT
    return "http://" + host


def _split_id(ctype, raw):
    """tt1234:2:5 -> (imdb, se, ep)"""
    parts = raw.split(":")
    imdb = parts[0]
    se = ep = None
    if ctype == "series" and len(parts) >= 3:
        try:
            se, ep = int(parts[1]), int(parts[2])
        except ValueError:
            se = ep = None
    return imdb, se, ep


def _path_extras(tail):
    """Stremio also encodes catalog extras in the path segment:
    /catalog/movie/id/genre=action&skip=24.json  (also ';' or ',' separated)"""
    out = {}
    for chunk in re.split(r"[&;,]", unquote(tail or "")):
        if "=" in chunk:
            k, v = chunk.split("=", 1)
            k, v = k.strip().lower(), v.strip()
            if k:
                out[k] = v
    return out


def _absolutize(cards, base):
    for c in cards:
        if c.get("url", "").startswith("/"):
            c["url"] = base + c["url"]
    return cards


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "BanglaPlex/%s" % VERSION

    def log_message(self, fmt, *args):
        pass                                     # NEVER stderr (dyno-freeze lesson)

    def _send(self, code, body, ctype="application/json", cache=0):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        if isinstance(body, str):
            body = body.encode("utf-8")
        ae = (self.headers.get("Accept-Encoding") or "").lower()
        if "gzip" in ae and len(body) > 512:
            buf = io.BytesIO()
            with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=6) as gz:
                gz.write(body)
            body = buf.getvalue()
            self.send_response(code)
            self.send_header("Content-Encoding", "gzip")
        else:
            self.send_response(code)
        # gzip FIRST, then Content-Length (Render edge hang lesson)
        self.send_header("Content-Type", ctype + ("; charset=utf-8"
                                                  if ctype.startswith(("application/json", "text/")) else ""))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Cache-Control",
                        "no-store" if cache == 0 else "public, max-age=%d" % cache)
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        t0 = time.time()
        parsed = urlparse(self.path)
        path = parsed.path
        if parsed.params:
            # urlparse peels ";skip=24" off the last segment as RFC-1738 params —
            # Stremio's path-style catalog extras use exactly that separator
            path = path + ";" + parsed.params
        q = parse_qs(parsed.query)
        base = _public_base(self)
        if not _PUBLIC_BASE[0] and self.headers.get("Host"):
            _PUBLIC_BASE[0] = base
        # Stremio carries per-install config as the first path segment
        segs = [s for s in path.split("/") if s]
        if segs and segs[0] not in _TOP_ROUTES:
            cfg = cfg_unpack(segs[0])
            if cfg is None:
                return self._send(404, {"error": "unknown path or bad config"})
            path = "/" + "/".join(segs[1:]) if len(segs) > 1 else "/"
            base = base + "/" + segs[0]
        else:
            cfg = dict(CFG_DEFAULTS)
        set_cfg(cfg)
        try:
            self._route(path, q, base, t0, cfg)
        except Exception as e:
            self._send(500, {"error": "internal", "detail": str(e)[:120]})

    # ─────────────────────────────────────────────────────────────── routes
    def _route(self, path, q, base, t0, cfg=None):
        cfg = cfg or get_cfg()
        if path == "/health":
            return self._send(200, {
                "ok": True, "addon": ADDON_NAME, "version": VERSION,
                "uptime_s": int(time.time() - _STATS["started"]),
                "keepalive": bool(_KEEPALIVE[0]), "keepalive_url": _KEEPALIVE[0] or None,
                "site": SITE, "n1_hosts": list(N1_HOSTS),
                "stats": dict(_STATS),
                "blocked_hosts_s": int(_blocked_left()),
                "proxy": _pool_stats(),
                "catalogs": [(c["type"], c["id"]) for c in CAT_DEFS],
                "configurable": True,
                "caches": [c.stats() for c in
                           (C_SEARCH, C_PAGE, C_META, C_EMBED, C_N1, C_STREAM,
                            C_LIST, C_IMDB, C_METARES)],
                "stale": len(C_STALE), "reqlog_len": len(_REQLOG),
                "egress": "text-only (json), zero media bytes",
            })
        if path in ("/", "/install", "/index.html"):
            html = (LANDING.replace("__NAME__", ADDON_NAME)
                    .replace("__VERSION__", VERSION).replace("__BASE__", base))
            return self._send(200, html, "text/html", cache=300)
        if path in ("/manifest.json", "/manifest"):
            return self._send(200, manifest(cfg), cache=600)
        if path in ("/configure", "/config"):
            html = (CONFIG_PAGE.replace("__NAME__", ADDON_NAME)
                    .replace("__VERSION__", VERSION).replace("__BASE__", _public_base(self))
                    .replace("__CFG__", json.dumps(cfg_unpack(q.get("c", [""])[0]) or
                                                   dict(CFG_DEFAULTS)))
                    .replace("__DEF__", json.dumps(CFG_DEFAULTS))
                    .replace("__NMAX__", str(MAX_CARDS)))
            return self._send(200, html, "text/html", cache=300)
        if path == "/validate-key":
            return self._send(200, {"valid": validate_tmdb_key((q.get("key") or [""])[0])})
        m = _CAT_RE.match(path)
        if m:
            ctype, cid = m.group(1), m.group(2)
            if (ctype, cid) not in CAT_IDS:
                return self._send(404, {"metas": [], "error": "unknown catalog"})
            extras = _path_extras(m.group(3) or "")
            genre = extras.get("genre") or (q.get("genre") or [""])[0]
            search = extras.get("search") or (q.get("search") or [""])[0]
            skip = extras.get("skip") or (q.get("skip") or ["0"])[0]
            try:
                skip = int(skip or 0)
            except Exception:
                skip = 0
            metas = catalog_items(ctype, cid, genre, search, skip, cfg)
            if metas and not search and not skip:
                _prewarm_shelf(metas, ctype)
            _log({"t": int(time.time()), "path": path, "cat": cid,
                  "genre": genre, "search": (search or "")[:30], "skip": skip,
                  "metas": len(metas), "ms": int((time.time() - t0) * 1000)})
            return self._send(200, {"metas": metas}, cache=300)
        m = _META_RE.match(path)
        if m:
            ctype, raw = m.group(1), m.group(2)
            mid, _se, _ep = _split_id(ctype, raw)
            meta = build_meta(ctype, mid, cfg)
            return self._send(200, {"meta": meta or {}},
                              cache=3600 if meta else 60)
        m = _STREAM_RE.match(path)
        if m:
            ctype, raw = m.group(1), m.group(2)
            imdb, se, ep = _split_id(ctype, raw)
            if not (imdb.startswith("tt") or imdb.startswith("bpx-")) or len(imdb) < 4:
                return self._send(404, {"streams": [], "message": "unsupported id"})
            res = build_streams(ctype, imdb, se, ep)
            cards = apply_cfg(_absolutize(res.get("streams") or [], base), cfg)
            out = {"streams": cards}
            if res.get("message"):
                out["message"] = res["message"]
            ms = int((time.time() - t0) * 1000)
            _log({"t": int(time.time()), "path": path, "ms": ms,
                  "cards": len(cards)})
            return self._send(200, out, cache=0)
        m = _SUBS_RE.match(path)
        if m:
            ctype, raw = m.group(1), m.group(2)
            imdb, se, ep = _split_id(ctype, raw)
            subs = self._subtitles_for(ctype, imdb, se, ep)
            if (cfg.get("subs") or "").lower() == "off":
                subs = []
            else:
                want = [x.strip().lower() for x in (cfg.get("subs") or "").split(",")]
                order = {l: i for i, l in enumerate(want)}
                subs = sorted(subs, key=lambda s: order.get((s.get("lang") or "").lower(), 99))
            return self._send(200, {"subtitles": subs}, cache=300)
        if path.startswith("/debug/"):
            return self._debug(path, q)
        return self._send(404, {"error": "not found"})

    def _subtitles_for(self, ctype, imdb, se, ep):
        """Nuvio-style players call /subtitles/... : reuse the cached card build."""
        key = (ctype, imdb, se, ep)
        hit, cards = C_STREAM.get(key)
        if not hit or not cards:
            res = build_streams(ctype, imdb, se, ep)
            cards = res.get("streams") or []
        out, seen = [], set()
        for c in cards:
            for s in c.get("subtitles") or []:
                k = (s.get("lang"), s.get("url"))
                if k in seen:
                    continue
                seen.add(k)
                out.append({"url": s["url"], "lang": s.get("lang") or "und",
                            "id": s.get("id") or "bpx-%d" % len(out)})
        return out

    def _debug(self, path, q):
        k = (q.get("k") or [""])[0]
        if k != DEBUG_KEY:
            return self._send(404, {"error": "not found"})
        if path == "/debug/reqlog":
            with _REQLOG_LOCK:
                return self._send(200, {"log": list(_REQLOG)[-60:],
                                        "stats": dict(_STATS)})
        if path == "/debug/search":
            kw = (q.get("q") or [""])[0]
            if not kw:
                return self._send(400, {"error": "q required"})
            t0 = time.time()
            ac = _search_autocomplete(kw)
            hh = _search_html(kw)
            return self._send(200, {
                "q": kw, "ms": int((time.time() - t0) * 1000),
                "autocomplete": (ac if ac is not None else "TRANSIENT"),
                "html_count": (len(hh) if hh is not None else "TRANSIENT"),
                "html_first": (hh or [])[:4],
                "blocked_s": int(_blocked_left())})
        if path == "/debug/page":
            u = (q.get("url") or [""])[0]
            pg = parse_watch_page(u) if u.startswith("http") else None
            return self._send(200, pg or {"error": "page parse failed"})
        if path == "/debug/embed":
            u = (q.get("url") or [""])[0]
            return self._send(200, {"servers": parse_embed_servers(u)})
        if path == "/debug/n1":
            host = (q.get("host") or [N1_HOSTS[0]])[0]
            vid = (q.get("id") or [""])[0]
            p = n1_video(host, vid)
            if p is False:
                return self._send(200, {"result": "NOT_ON_THIS_HOST"})
            if p is None:
                return self._send(200, {"result": "TRANSIENT"})
            slim = {kk: (str(vv)[:220]) for kk, vv in p.items()
                    if kk not in ("player", "ads")}
            return self._send(200, {"result": slim,
                                    "candidates": media_candidates(p, host)})
        if path == "/debug/chain":
            u = (q.get("url") or [""])[0]
            kind = (q.get("kind") or ["tiktok"])[0]
            ref = (q.get("referer") or [""])[0] or None
            info = _verify_media(kind, u, ref)
            if not info:
                return self._send(200, {"ok": False})
            return self._send(200, {"ok": True, "best": info["best"],
                                    "n_variants": info["n_variants"],
                                    "variant": info["variant"][:160],
                                    "segment": info["segment"][:160],
                                    "seg_status": info["seg_status"]})
        if path == "/debug/resolve":
            slug = (q.get("slug") or [""])[0]
            ctype = (q.get("type") or ["movie"])[0]
            se = int((q.get("s") or ["0"])[0] or 0) or None
            ep = int((q.get("e") or ["0"])[0] or 0) or None
            pg = parse_watch_page(SITE + "/watch/%s.html" % slug)
            if not pg:
                return self._send(200, {"error": "page"})
            tr = {"slug": slug, "title": pg["title"], "year": pg["year"],
                  "keys": [(k, l) for k, _a, l in pg["keys"]],
                  "iframe": pg["iframe"]}
            picks = pick_keys(pg, ctype, se, ep)
            tr["picks"] = picks
            t0 = time.time()
            cards = []
            if picks:
                cards = _resolve_file(pg, picks[0][0], picks[0][1], picks[0][2],
                                      ctype, se, ep, time.time() + 40)
            tr["cards"] = len(cards)
            tr["names"] = [c["name"][:60] for c in cards]
            tr["urls"] = [c["url"][:110] for c in cards]
            tr["subs"] = len(cards[0].get("subtitles", [])) if cards else 0
            tr["ms"] = int((time.time() - t0) * 1000)
            return self._send(200, tr)
        if path == "/debug/url":
            u = (q.get("u") or q.get("url") or [""])[0]
            if not u.startswith("http"):
                return self._send(200, {"error": "pass ?u=https://…"})
            t1 = time.time()
            r, via = _fetch(u, timeout=20, referer=SITE + "/")
            body = "" if r is None else (r.text or "")
            out = {"url": u, "via_proxy": bool(via), "ms": int((time.time() - t1) * 1000),
                   "status": None if r is None else r.status_code,
                   "bytes": len(body), "cards": len(parse_listing(body)),
                   "looks_blocked": _looks_blocked(body, 0 if r is None else r.status_code),
                   "head": body[:160].replace("\n", " ")}
            if r is not None:
                try:
                    r.close()
                except Exception:
                    pass
            return self._send(200, out)
        if path == "/debug/net":
            only = (q.get("probe") or [""])[0] or None
            return self._send(200, _net_probe(only))
        if path == "/debug/mem":
            try:
                rss = int(open("/proc/self/status").read()
                          .split("VmRSS:")[1].split()[0])
            except Exception:
                rss = -1
            return self._send(200, {"vmrss_kb": rss,
                                    "caches": [c.stats() for c in
                                               (C_SEARCH, C_PAGE, C_META, C_EMBED,
                                                C_N1, C_STREAM)],
                                    "stale": len(C_STALE)})
        return self._send(404, {"error": "not found"})


def _net_probe(only=None):
    """Diagnostic: is THIS egress (Render singapore) blocked per host, and does a
    free exit fix it? One direct + one proxied request per probe, head-only
    bodies, so it costs a little latency and no media bandwidth."""
    probes = [
        ("site_home", SITE + "/", None),
        ("site_autocomplete", SITE + "/home/autocompleteajax?term=mirzapur", SITE + "/"),
        ("site_watch", SITE + "/watch/mirzapur-the-movie.html", SITE + "/"),
        ("embed", "https://plextream.work/embed.php?id=GKHsp0bk", None),
        ("n1_api", "https://bpx.strp2p.site/api/v1/video?id=ow99qd",
         "https://bpx.strp2p.site/"),
    ]
    hd0 = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}
    exits = _pool_order()
    out = {"pool": _pool_stats(), "exit_used": (exits or [None])[0], "probes": {}}
    for name, url, ref in probes:
        if only and only != name:
            continue
        hd = dict(hd0)
        if ref:
            hd["Referer"] = ref
        row = {}
        t0 = time.time()
        try:
            r = _S.get(url, headers=hd, timeout=10)
            row["direct"] = [r.status_code, len(r.text or ""),
                             int((time.time() - t0) * 1000),
                             (r.text or "")[:70].replace("\n", " ")]
        except Exception as e:
            row["direct"] = ["EXC", type(e).__name__,
                             int((time.time() - t0) * 1000), ""]
        row["proxy"] = ["NO_EXIT", "", 0, ""]
        if exits:
            # race exactly like the real fetch path: probing a single exit
            # reported ReadTimeout while resolves through the pool worked fine
            t1 = time.time()
            r = _pool_get(url, hd, POOL_TO)
            if r is None:
                row["proxy"] = ["ALL_DEAD", "", int((time.time() - t1) * 1000), ""]
            else:
                row["proxy"] = [r.status_code, len(r.text or ""),
                                int((time.time() - t1) * 1000),
                                (r.text or "")[:70].replace("\n", " ")]
        out["probes"][name] = row
    return out


# ══════════════════════════════════════════════════════ 16. KEEPALIVE/WATCHDOG
_KEEPALIVE = [""]
_KEEPALIVE_LOCK = threading.Lock()


def _pool_maintain():
    """Re-pull + re-train the pool in the background.

    Only worth doing once this egress has proven a host blocked (otherwise the
    pool is never used and probing free exits is pure waste)."""
    try:
        if not POOL_ON or not _DIRECT_BAD:
            return
        _pool_refresh()
    except Exception:
        pass


def _keepalive_loop():
    """Render free sleeps after ~15 idle minutes. Learn the public URL from the
    first request Host header (or BPX_PUBLIC_URL) and self-ping forever."""
    while True:
        _pool_maintain()
        try:
            url = _KEEPALIVE[0] or _PUBLIC_BASE[0]
            if url:
                if not _KEEPALIVE[0]:
                    with _KEEPALIVE_LOCK:
                        _KEEPALIVE[0] = url
                requests.get(url.rstrip("/") + "/health", timeout=10,
                             headers={"User-Agent": UA})
        except Exception:
            pass
        time.sleep(240)


def _liveness_watchdog():
    fails = 0
    while True:
        time.sleep(300)
        try:
            r = requests.get("http://127.0.0.1:%d/health" % PORT, timeout=15)
            ok = r.status_code == 200
        except Exception:
            ok = False
        fails = 0 if ok else fails + 1
        if fails >= 3:
            os._exit(1)                       # let Render restart us


def main():
    threading.Thread(target=_keepalive_loop, daemon=True).start()
    threading.Thread(target=_liveness_watchdog, daemon=True).start()
    if os.environ.get("BPX_PREWARM", "1") != "0":
        # own thread: a slow proxied shelf fetch must never delay the keepalive
        # ping that keeps Render's free tier awake
        threading.Thread(target=catalog_prewarm, daemon=True, name="prewarm").start()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    srv.daemon_threads = True
    sys.stdout.write("%s %s listening on :%d (strict zero-bandwidth)\n"
                     % (ADDON_NAME, VERSION, PORT))
    sys.stdout.flush()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
