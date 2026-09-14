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
from urllib.parse import quote, urljoin, urlparse, parse_qs

import requests

# ═══════════════════════════════════════════════════════════════════ 1. CONFIG
VERSION    = "1.1.2"
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
    "idPrefixes": ["tt"],
    "resources": ["stream", "subtitles"],
    "catalogs": [],
    "behaviorHints": {"configurable": False},
}

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
C_STALE   = {}                                    # key -> (expiry, cards)
_NEG_RETRY_AT = {}
_SWR_RUNNING = set()
_SWR_LOCK = threading.Lock()

_IO_EX   = ThreadPoolExecutor(max_workers=12, thread_name_prefix="io")
_V_EX    = ThreadPoolExecutor(max_workers=6, thread_name_prefix="verify")
_P_EX    = ThreadPoolExecutor(max_workers=10, thread_name_prefix="proxy")
_BUILD_EX = ThreadPoolExecutor(max_workers=2, thread_name_prefix="build")

_S = requests.Session()
_S.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})

_REQLOG = []
_REQLOG_LOCK = threading.Lock()
_STATS = {"started": time.time(), "resolves": 0, "cards": 0, "empties": 0,
          "n1_calls": 0, "n1_429": 0, "relay_bytes": 0}


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
POOL_SRC = os.environ.get(
    "BPX_PROXY_SOURCE",
    "https://api.proxyscrape.com/v4/free-proxy-list/get?request=display_proxies"
    "&proxy_type=http&proxy_format=protocolipport&format=text")
POOL_ON   = os.environ.get("BPX_PROXY", "auto") != "0"
POOL_TTL  = 360.0          # refresh the list every 6 min
POOL_MAX  = 40
POOL_TRY  = int(os.environ.get("BPX_PROXY_TRY", "5"))   # exits raced per fetch
POOL_TO   = 8.0            # per-exit connect/read timeout (free proxies are slow)
DIRECT_BLOCK_TTL = 600.0   # host benched for direct egress after a 403/503

_HOST_BLOCK = {}
_HOST_BLOCK_LOCK = threading.Lock()
_POOL = [[]]               # ["http://ip:port", ...]
_POOL_TS = [0.0]
_POOL_LOCK = threading.Lock()
_POOL_BAD = {}             # exit -> benched-until
_POOL_OK = {}              # exit -> (successes, last_ok)
_STICKY = [None, 0.0]      # ride one good exit for 90s (a chain, not a dice roll)
_DIRECT_BAD = {}           # host -> direct egress benched until


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


def _pool_refresh(force=False):
    if not POOL_ON:
        return []
    now = time.time()
    with _POOL_LOCK:
        if _POOL[0] and not force and now - _POOL_TS[0] < POOL_TTL:
            return list(_POOL[0])
        _POOL_TS[0] = now
    urls = []
    try:
        r = requests.get(POOL_SRC, timeout=12, headers={"User-Agent": UA})
        for ln in (r.text or "").splitlines():
            ln = ln.strip()
            if not ln or ln.startswith("#") or ":" not in ln:
                continue
            if "://" not in ln:
                ln = "http://" + ln
            # socks4/socks5 exits need PySocks; without it requests raises
            # InvalidSchema, so every such exit looked "dead" (measured on prod:
            # 40/40 exits were socks4 and the whole pool was unusable)
            if ln.split("://")[0] not in ("http", "https"):
                continue
            urls.append(ln)
    except Exception:
        urls = []
    urls = list(dict.fromkeys(urls))[:POOL_MAX]
    with _POOL_LOCK:
        if urls:
            _POOL[0] = urls
        return list(_POOL[0])


def _pool_order():
    """Healthy exits, sticky-first, then most-successful first."""
    now = time.time()
    urls = _pool_refresh()
    good = [u for u in urls if _POOL_BAD.get(u, 0.0) <= now]
    sticky = _STICKY[0] if _STICKY[1] > now else None
    good.sort(key=lambda u: (0 if u == sticky else 1,
                             -(_POOL_OK.get(u) or (0, 0))[0]))
    return good


def _pool_note(u, ok, blocked=False):
    now = time.time()
    if ok:
        n = (_POOL_OK.get(u) or (0, 0))[0]
        _POOL_OK[u] = (n + 1, now)
        _POOL_BAD.pop(u, None)
        _STICKY[0], _STICKY[1] = u, now + 90
    else:
        _POOL_BAD[u] = now + (900 if blocked else 300)
        if _STICKY[0] == u:
            _STICKY[1] = 0.0


def _pool_stats():
    now = time.time()
    return {"pool": len(_POOL[0]),
            "healthy": len([u for u in _POOL[0] if _POOL_BAD.get(u, 0.0) <= now]),
            "sticky": _STICKY[0] if _STICKY[1] > now else None,
            "known_good": len(_POOL_OK),
            "direct_blocked_hosts": [h for h, t in _DIRECT_BAD.items() if t > now],
            "enabled": POOL_ON}


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
        try:
            r = _S.get(url, headers=hd, timeout=timeout, stream=stream,
                       proxies={"http": u, "https": u})
        except Exception:
            _pool_note(u, False)
            return None
        if r.status_code in (403, 503):
            try:
                r.close()
            except Exception:
                pass
            _pool_note(u, False, blocked=True)     # this exit is flagged too
            return None
        _pool_note(u, True)
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
            "behaviorHints": hints}
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
    metas = resolve_meta_all(ctype, imdb)
    if not metas:
        return {"streams": [], "message": "no metadata for this id"}
    years = {int(y) for _n, y, _t in metas if y}
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
<p><a class="btn" href="stremio:///install?addOnUrl=__BASE__/manifest.json">Install in Stremio</a></p>
<div class="card"><b>Install URL</b><br><code>__BASE__/manifest.json</code>
<p class="small">Open any movie/series from your own catalogs (IMDb, Trakt, Cinemeta…) —
this addon answers with direct streams. No catalogs of its own.</p></div>
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
        q = parse_qs(parsed.query)
        base = _public_base(self)
        if not _PUBLIC_BASE[0] and self.headers.get("Host"):
            _PUBLIC_BASE[0] = base
        try:
            self._route(path, q, base, t0)
        except Exception as e:
            self._send(500, {"error": "internal", "detail": str(e)[:120]})

    # ─────────────────────────────────────────────────────────────── routes
    def _route(self, path, q, base, t0):
        if path == "/health":
            return self._send(200, {
                "ok": True, "addon": ADDON_NAME, "version": VERSION,
                "uptime_s": int(time.time() - _STATS["started"]),
                "keepalive": bool(_KEEPALIVE[0]), "keepalive_url": _KEEPALIVE[0] or None,
                "site": SITE, "n1_hosts": list(N1_HOSTS),
                "stats": dict(_STATS),
                "blocked_hosts_s": int(_blocked_left()),
                "proxy": _pool_stats(),
                "caches": [c.stats() for c in
                           (C_SEARCH, C_PAGE, C_META, C_EMBED, C_N1, C_STREAM)],
                "stale": len(C_STALE), "reqlog_len": len(_REQLOG),
                "egress": "text-only (json), zero media bytes",
            })
        if path in ("/", "/install", "/index.html"):
            html = (LANDING.replace("__NAME__", ADDON_NAME)
                    .replace("__VERSION__", VERSION).replace("__BASE__", base))
            return self._send(200, html, "text/html", cache=300)
        if path == "/manifest.json":
            man = dict(MANIFEST)
            man["logo"] = MANIFEST["logo"]
            return self._send(200, man, cache=3600)
        if path == "/configure" or path == "/config":
            return self._send(200, {"configured": True})
        m = _STREAM_RE.match(path)
        if m:
            ctype, raw = m.group(1), m.group(2)
            imdb, se, ep = _split_id(ctype, raw)
            if not imdb.startswith("tt") or len(imdb) < 4:
                return self._send(404, {"streams": [], "message": "unsupported id"})
            res = build_streams(ctype, imdb, se, ep)
            cards = _absolutize(res.get("streams") or [], base)
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


def _keepalive_loop():
    """Render free sleeps after ~15 idle minutes. Learn the public URL from the
    first request Host header (or BPX_PUBLIC_URL) and self-ping forever."""
    while True:
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
