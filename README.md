# BanglaPlex — Stremio addon

**banglaplex.biz** → Stremio. Stream-only addon (no catalogs): open any movie or
series from your own catalogs (Cinemeta, Trakt, IMDb lists…) and this addon
answers with verified direct-CDN streams + subtitles.

Bangla / Hindi / Hollywood movies and web-series, up to 1080p.

```
Install URL:  https://<your-service>.onrender.com/manifest.json
```

---

## How it works

The site runs OVOO CMS (CodeIgniter) and embeds a third-party player chain.
Nothing here is guessed — every hop below was reversed and verified live:

```
IMDb id
  └─(1) metadata consensus      cinemeta ∥ TMDB find ∥ IMDb suggest (+ TMDB alt titles)
  └─(2) site search             GET /home/autocompleteajax?term=…   (HTML /search/?q= fallback)
  └─(3) watch page              og:title/og:image/og:description, Release, Duration,
                                Quality, Genre, Country, Actors, ?key= file list, <iframe>
  └─(4) embed page              plextream.work/embed.php?id=…  → changeServer() server buttons
  └─(5) player API              GET https://bpx.{strp2p,rpmvid}.site/api/v1/video?id=…
                                → hex( AES-128-CBC( JSON ) )        [decrypted in pure python]
  └─(6) media verification      master 200 #EXTM3U → variant head-read → first segment 206
  └─(7) card                    url = the CDN master playlist itself (+ VTT subtitles)
```

**Stream paths, in the site's own priority order:**

| # | path | card needs | why |
|---|------|-----------|-----|
| 1 | **TikTok CDN** (`hlsVideoTiktok`) | nothing — no headers | master+variant are proxied by the 3n1 frontend, segments are absolute `*.tiktokcdn.com` URLs signed until 2027 |
| 2 | **Cloudflare** (`cfNative`) | `proxyHeaders.Referer` + `notWebReady` | segments are referer-gated on the CF edge |
| — | `source` (raw-IP origin) | — | **disabled**: its token binds to the fetcher's IP, so it verifies from Render and 403s on the user's player. A phantom card is worse than no card. (`BPX_INHOUSE=1` to debug) |
| — | `abyssplayer.com` | — | SoTrym player, AES-CTR key not extracted → honest skip |
| — | `bestx.stream` / `chillx.top` | — | DNS resolves, TLS handshake fails (dead) → honest skip. Only affects 2023-24 catalog entries |

### Series model

Each `?key=` entry on a watch page is **one video file**, not a folder of
episodes (`/api/v1/folder` always returns `[]`). Key labels look like
`Full Movie`, `Web Series`, `S01`, `Episode 01-08`, `Bonus Episode`.

So: a movie request → one card; a series request → the file that actually
contains the asked-for season/episode, labelled honestly
(`◫ S01 E01 ◇ ⚠ full-season file`). An episode-level request never falls back to
a *different* episode pack — wrong content is worse than an honest empty.

### Zero bandwidth

Render carries **no media bytes**. This addon only ever emits small JSON: card
URLs point straight at `bpx.strp2p.site` / `tiktokcdn.com` / the CF edge, and
subtitles are direct `.vtt` URLs. There are no `/hls`, `/seg`, `/proxy` or media
MIME routes in the source at all (`test_zero_bandwidth_no_media_routes` guards
this). Playlist probing uses `stream=True` + an 8 KB head-read + `close()`,
because the 3n1 frontends **ignore `Range` on variant playlists** and would
otherwise stream ~1.4 MB per probe into the dyno.

---

## Honesty rules (house policy, all enforced by tests)

- **No phantom cards.** A card is emitted only after the master playlist, a
  variant playlist and a real segment have all answered.
- **No cached transient failures.** A Cloudflare blip or a 429 returns
  *"search is not answering right now (transient)"* and caches **nothing** — the
  next tap retries immediately. Only a genuine "the site answered: not here"
  gets the short 300 s negative cache.
- **No invented tokens.** Card lines omit whatever we don't know (no fake
  `1080p`, no `0 SUB`, no runtime we didn't parse).
- **Resolution comes from width, not height.** These are scope-cropped files:
  `1920x800` *is* the 1080p encode and `1280x532` *is* the 720p one.
  Height-only bucketing mislabelled every single card during testing.
- **Rate limits are respected.** The 3n1 API 429s after ~10 rapid calls per
  host, so calls are lane-throttled per frontend (1.15 s), a 429 gets one 3.5 s
  backoff-retry, and a still-429 host is benched 25 s — *per host only*, so one
  grumpy frontend can never freeze the site scrape or the metadata race.

---

## Endpoints

| path | what |
|------|------|
| `/` | landing page + install button |
| `/manifest.json` | stream-only manifest (`resources: [stream, subtitles]`, `catalogs: []`) |
| `/stream/{movie\|series}/{tt…[:S:E]}.json` | the cards |
| `/subtitles/{type}/{id}[/{extra}].json` | subtitle tracks for players that ask separately |
| `/health` | version, uptime, stats, per-cache bytes, keepalive state |
| `/debug/…?k=BPX_DEBUG_KEY` | `search` `page` `embed` `n1` `chain` `resolve` `reqlog` `mem` |

`/debug/resolve?k=…&slug=the-revolutionaries&type=series&s=1&e=1` runs the whole
chain for one site slug and reports every intermediate step — the fastest way to
see *why* a title didn't answer.

---

## Deploy

**Render (blueprint)** — push this folder to a GitHub repo, then
*New + → Blueprint → pick the repo*. `render.yaml` sets everything:
free plan, Singapore region, `pip install -r requirements.txt`,
`python3 addon.py`, health check `/health`, auto-deploy.

**Manual Render web service** — Runtime *Python 3*, build
`pip install -r requirements.txt`, start `python3 addon.py`, health check path
`/health`.

**Docker anywhere** — `docker build -t banglaplex . && docker run -p 7055:7055 banglaplex`

**Locally** — `pip install -r requirements.txt && python3 addon.py`
→ <http://127.0.0.1:7055/>

After the first deploy, set `BPX_PUBLIC_URL=https://<service>.onrender.com` so the
keepalive self-ping has an address immediately (it also learns the URL from the
first request's `Host` header, so this is optional). Render's free tier sleeps
after ~15 idle minutes; the keepalive thread pings `/health` every 240 s to stop
that, and a liveness watchdog restarts the process if `/health` fails 3×.

### Environment

| var | default | meaning |
|-----|---------|---------|
| `PORT` | `7055` | bind port (Render injects its own) |
| `BPX_SITE` | `https://banglaplex.biz` | the site to scrape |
| `BPX_PUBLIC_URL` | *(learned)* | keepalive target |
| `BPX_WALL` | `22` | player-facing answer wall, seconds |
| `BPX_MAX_CARDS` | `3` | cards per request |
| `BPX_MAX_SUBS` | `6` | subtitle tracks per card (en/hi/bn first) |
| `BPX_CARDS` | `1` | `0` = kill switch, answers empty |
| `BPX_INHOUSE` | `0` | `1` = also emit the IP-bound raw-IP path (debug only) |
| `BPX_DEBUG_KEY` | `bpx-dbg-4c9e` | `/debug/*` key — **change this in prod** |
| `TMDB_API_KEY` | built-in | metadata + alternative titles |

---

## Tests

```bash
python3 test_banglaplex.py           # 113 offline tests, every network call mocked
BPX_LIVE=1 python3 test_banglaplex.py # + 4 live integration tests (real site/CDN)
```

The offline suite covers the pure-python AES (FIPS-197 C.1 + a node
`createCipheriv` known-answer), page/embed parsing, the key picker, search and
match scoring, the 429/404/transient branches of the player API, the
no-phantom media gate, subtitle ranking, card formatting, both cache paths
(positive, negative, SWR, wall timeout), every HTTP route and the
zero-bandwidth contract.

The live block really resolves `tt0213890` (movie) and `tt31924802:1:1`
(series season pack), then plays each emitted card the way a player would:
master → variant → first segment, asserting 206 with bytes. It burns 3n1
rate-limit budget, so don't loop it.

> **The runner is the last thing in the file.** A test defined after
> `if __name__ == "__main__"` silently never runs — that bug shipped once
> already in this fleet.

---

## Known limits

- **~1 in 6 current titles is abyss-only** (`abyssplayer.com`, SoTrym player).
  Its `datas` blob is base64 JSON but the `media` field is AES-CTR under a key
  buried in an obfuscated 216 KB `core.bundle.js`; not extracted → those titles
  answer empty instead of guessing.
- **2023-24 catalog entries** often sit on `bestx.stream` / `chillx.top`, whose
  TLS handshakes now fail. Dead upstream, nothing to scrape.
- **Older uploads can have deleted 3n1 ids** (`404 Video not found or deleted`)
  even when the site still lists three servers — e.g. Animal (2023) and Jawan
  (2023) currently resolve to nothing.
- **Season packs are single files.** Stremio will start a pack from its
  beginning; the card says so (`⚠ full-season file`).
- **Titles IMDb knows by another name** are handled by walking every title the
  metadata sources give us: `tt3365690` is *The Reincarnate* on IMDb and
  *Jaatishwar* on BanglaPlex — consensus picks the IMDb name, the site miss then
  falls through to the TMDB name and hits.
