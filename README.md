# BanglaPlex — Stremio addon

**banglaplex.biz** → Stremio. Browse BanglaPlex's own shelves *and* answer any
title from your existing catalogs (Cinemeta, Trakt, IMDb lists…) with verified
direct-CDN streams + subtitles.

Bangla / Hindi / Hollywood movies and web-series, up to 1080p.

```
Install URL:  https://<your-service>.onrender.com/manifest.json
Configure:    https://<your-service>.onrender.com/configure
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

  …and when the watch page embeds abyssplayer.com instead (or the 3n1 id is dead):
  └─(4b) embed page             abyssplayer.com/{id}  →  const datas = "<base64 JSON>"
                                {slug, md5_id, user_id, media:"0|"<AES-256-CTR>, config, danmu}
  └─(5b) source list            decrypt `media` with key = utf8(md5hex(user_id:slug:md5_id))
                                (pure-python AES-256-CTR, counter = key[:16]) → per-quality
                                {label, codec, size, res_id, sub, domains[]}
  └─(6b) sora token             per source: k = md5hex(digits-of-size as bytes) → utf8,
                                iv = k[:16]; path = /mp4/{md5_id}/{res_id}/{size}/{FRAG}/0;
                                token = b64(b64(AES-256-CTR(path)))
  └─(7b) card                   url = https://{sub}.{domain}/sora/{size}/{token}
                                (+ `Referer: https://abyssplayer.com/`) → progressive MP4
```

**Stream paths, in the site's own priority order:**

| # | path | card needs | why |
|---|------|-----------|-----|
| 1 | **TikTok CDN** (`hlsVideoTiktok`) | nothing — no headers | master+variant are proxied by the 3n1 frontend, segments are absolute `*.tiktokcdn.com` URLs signed until 2027 |
| 2 | **Cloudflare** (`cfNative`) | `proxyHeaders.Referer` + `notWebReady` | segments are referer-gated on the CF edge |
| — | `source` (raw-IP origin) | — | **disabled**: its token binds to the fetcher's IP, so it verifies from Render and 403s on the user's player. A phantom card is worse than no card. (`BPX_INHOUSE=1` to debug) |
| 3 | **Abyss** (`abyssplayer.com`) | `proxyHeaders.Referer` + `notWebReady` | SoTrym player. The `datas` blob decrypts to a per-quality source list, and the origin's own `/sora/{size}/{token}` route serves the **whole file as a progressive MP4** once it is handed a token it minted the key for. `ftyp` verified at byte 0 before a card is emitted. Files over 500 MiB play straight through — no seeking (see Known limits) |
| — | `bestx.stream` / `chillx.top` | — | DNS resolves, TLS handshake fails (dead) → honest skip. Only affects 2023-24 catalog entries |

### The browser card: seeking on a file that cannot seek

Abyss files above the origin's 500 MiB `Range` ceiling play straight through in a
native player — no scrubbing. The site's own web player does not have that
problem: its service worker synthesises a `#EXT-X-BYTERANGE` playlist, so seeking
works at any size.

So when **none** of the verified native cards can seek, one extra card is emitted:

```
♧ 1080p  ✹ Queens  ·browser
◫ S01 E01 ◇ ⚠ opens in a browser, seeking works ◇ ▤ 2.65 GB ◇ ▧ H264
externalUrl → https://<host>/player/<abyss-id>
```

`/player/<id>` is a ~600-byte HTML shell that iframes `abyssplayer.com/<id>`. The
iframe is the entire trick: that page carries anti-hotlink JS

```js
if (top.location == self.location && !/^(.+?)\.abyss\.to$/.test(hostname))
    window.location = "https://abyss.to";     // a top-level window is thrown away
```

so linking the player URL directly (what an `externalUrl` card used to do) lands
the user on `abyss.to`. Inside an iframe `top.location != self.location` and the
real player stays put. `abyssplayer.com` sends no `X-Frame-Options` and no CSP,
which is why this works at all. The id is charset-restricted
(`[A-Za-z0-9]{4,64}`) — anything else is a 404, never a template injection.

Rules around it, all tested:

- **Never replaces a native card** — it is appended after them, and a build that
  verified nothing emits no card of any kind (no phantom browser card).
- **Only when nothing can seek.** If any quality is under the ceiling, the native
  cards already do the job and no browser card is offered.
- **Outside the `n` cap.** It is a different playback mode, not another quality,
  so it never costs the user a 720p card. `bc=0` (config page) or
  `BPX_BROWSER_CARD=0` turns it off entirely.
- **Still zero media bytes.** Render serves the HTML shell; the browser pulls
  video from the abyss origin.

### Series model

Each `?key=` entry on a watch page is **one video file**, not a folder of
episodes (`/api/v1/folder` always returns `[]`). Key labels look like
`Full Movie`, `Web Series`, `S01`, `Episode 01-08`, `Bonus Episode`.

So: a movie request → one card; a series request → the file that actually
contains the asked-for season/episode, labelled honestly
(`◫ S01 E01 ◇ ⚠ full-season file`). An episode-level request never falls back to
a *different* episode pack — wrong content is worse than an honest empty.

### Egress: direct first, trained proxy pool only when the host blocks us

Render's Singapore egress is **Cloudflare-flagged on banglaplex.biz**: `/watch/`
and `/search/` come back 403 (a 5.5 KB challenge page) while the JSON autocomplete
answers `200 []` — the same calls work from a normal IP. So every site fetch is
adaptive: try direct, and the moment a host proves blocked *from here*, bench
direct egress for that host (10 min) and ride the pool **inside the same call**,
so a user never sees the first failure. From an unblocked IP the pool is never
even refreshed (measured: pool size stays 0).

The pool is trained, not scraped-and-hoped (MovieBox pattern):

* **Sources** — proxyscrape by default (`proxy_type=http`), comma-separable for
  more, plus `BPX_PROXY_LIST` for hand-picked exits that always ride first.
* **Scheme filter** — socks4/5 exits are dropped: without PySocks every one of
  them raises `InvalidSchema`, so a 40/40 socks list reads as "all proxies down".
* **Probing** — a background trainer platform-probes candidates against the
  *actually blocked* endpoint (cheap ~360 B autocomplete hit) in parallel waves,
  publishes wave 1 immediately, then keeps the 20 fastest and **merges** them
  with already-trained members instead of replacing them.
* **Scoring** — every exit keeps `ok/fail/EWMA-latency`; picks order by
  `quality × speed`, and a good exit stays **sticky for 90 s** (capped at 2
  in-flight) so one resolve chain rides one exit instead of re-rolling dice.
* **Racing** — a fetch races `BPX_PROXY_TRY=5` exits concurrently and keeps the
  first good answer, closing the losers. Free exits are mostly dead or slow;
  walking them cost 8 s per corpse and a cold resolve never fit the 22 s wall.
* **Benching** — dead exit 5 min, platform-blocked (403/503) 15 min.
* **Never in front of a user** — the source list is pulled synchronously (fast),
  training happens on its own thread, and `_pool_maintain()` re-trains from the
  keepalive loop only while some host is actually benched.

Segment probes (`_range_probe`) are **never** proxied: playability must be proven
on a normal client path, and no media byte may ride a free exit
(`relay_bytes` stays 0).

`/debug/net?k=…` probes every host both ways (direct vs the racing pool) and
reports status, bytes, ms and a body snippet — the fastest way to see what an
egress can reach. The probe uses the *same* racing path as a real fetch; an
earlier version tested one exit and reported `ReadTimeout` for everything while
resolves were succeeding.

### Catalogs

Three shelves scraped from the site itself, each with `search`, `genre` and
`skip` extras:

| catalog | source | pagination |
|---|---|---|
| `movie/bpx-latest` | homepage card grid (~87 unique cards, movies and series split by their TV badge) | slices the cached listing |
| `movie/bpx-year` | `/year/<this year>` | path offsets |
| `series/bpx-series` | `/genre/bengali-web-series` | path offsets |

`genre` options are the site's own slugs (21 movie + 6 series). Note that
**pagination on this site is path-based**: `/genre/action/24.html` is page 2 —
`?page=2` is silently ignored and returns page 1 (measured).

Every card gets an id: an **IMDb `tt…`** when a suggestion matches title *and*
year *and* type strictly, otherwise **`bpx-<slug>`**, a source id this addon can
both stream and describe. A wrong `tt` would show another film's poster, which is
worse than no mapping — measured example: *Jaatishwar* is "The Reincarnate" on
IMDb, so it ships as `bpx-jaatishwar`. Roughly ⅛ of a shelf ends up source-only
and still plays.

### Metadata: providers first, **source fallback** second

`/meta/{type}/{id}.json` races Cinemeta + TMDB (the install's own key if given,
else the built-in one) and scrapes the watch page (`og:*`, plus the Director /
Writer / Actor / Country / Release / Duration / Quality / Genre rows). **The
fallback runs both ways**, whichever side has the hole:

* providers answered but left a hole → the source fills it;
* providers answered *nothing* (a brand-new regional title) → the id is resolved
  to a name via IMDb's suggest-by-id endpoint, and that name is used to find the
  watch page — without a name there is nothing to search the site with, which is
  how `tt…` ids used to ship an empty detail page;
* `bpx-<slug>` ids scrape the source first, then ask the providers to fill
  cast / genres / runtime / rating, and keep `imdb_id` when one matches.

The source always wins a conflict: it is the site the stream comes from, so its
title, year and poster are the ones the user recognises. Every shape is
normalised before it ships: Cinemeta sends `director` as a list and no `year`
(only `releaseInfo`), the site sends comma strings.

Listing pages are cached **parsed** (never the raw 375 KB HTML) and served
stale-while-revalidate, so a shelf refresh never makes a user wait for a proxied
fetch. Boot prewarms all three shelves on their own thread.

### Latency: the shelf teaches the stream endpoint

Two things made the first tap on a series slow, and both are fixed by remembering
what a catalog build already learned:

* **`tt…` → slug index.** Building a shelf asks IMDb for every card's id anyway,
  so the slug it came from is stored. `/stream` then goes straight to
  `/watch/<slug>.html` instead of paying for an autocomplete round trip — one
  fewer proxied fetch (~2 s on a flagged egress) per title.
* **Shelf prewarm.** Serving the *first* page of a catalog warms streams for the
  first `BPX_PREWARM_STREAMS` cards in the background, so the titles a user is
  most likely to tap are already resolved and cached by the time they tap one.
  Search results and deep pages are never prewarmed.

### Configuration

`behaviorHints.configurable: true` + a self-contained `/configure` page (no
third-party assets, so it renders inside Stremio's webview). The config travels
**inside the install URL** as one base64url segment — nothing is stored
server-side:

```
https://host/eyJjYXQiOiJzZXJpZXMifQ/manifest.json
```

| option | values | effect |
|---|---|---|
| `n` | 1…`BPX_MAX_CARDS` | cards per title |
| `q` | `all` `720` `1080` | minimum resolution floor (unmeasured cards are never dropped) |
| `cdn` | `both` `tiktok` `cf` | server preference — a *preference*: if the title only has the other server, that card still ships instead of an empty list |
| `subs` | `en,hi,bn,…` or `off` | subtitle languages, leftmost wins (reorders + filters the track list and the `⟡ N SUB` label) |
| `bc` | `1` `0` | offer the browser card when no native card can seek |
| `cat` | `all` `movie` `series` `off` | which shelves appear in the board |
| `tmdb` | 32-char key | the user's own TMDB key for richer art (validated live via `/validate-key`) |

Filtering happens on the way out (`apply_cfg`), not inside the build, so the
shared cache stays config-independent: one verified build serves every install.
The page's JS encoder produces byte-identical segments to the server's decoder
(asserted in tests).

### Zero bandwidth

Render carries **no media bytes**. This addon only ever emits small JSON: card
URLs point straight at `bpx.strp2p.site` / `tiktokcdn.com` / the CF edge / the
abyss `*.sssrr.org` origin, and subtitles are direct `.vtt` URLs. Abyss cards are
the strongest form of this: the token is computed locally and the player pulls the
MP4 from the origin itself — the addon never touches the video, not even its head
beyond a 64-byte magic check. The only HTML served for playback is the `/player/`
iframe shell (~600 B); the video inside it streams from the abyss origin to the
user's browser, not through this process. There are no `/hls`, `/seg`, `/proxy` or media
MIME routes in the source at all (`test_zero_bandwidth_no_media_routes` guards
this). Playlist probing uses `stream=True` + an 8 KB head-read + `close()`,
because the 3n1 frontends **ignore `Range` on variant playlists** and would
otherwise stream ~1.4 MB per probe into the dyno. Abyss probes read 64 bytes, and
drop the `Range` header entirely above the origin's 500 MiB ceiling (one of the two
backends behind the load balancer answers a ranged request with a 400).

---

## Honesty rules (house policy, all enforced by tests)

- **No phantom cards.** A card is emitted only after the master playlist, a
  variant playlist and a real segment have all answered.
- **No cached transient failures.** A Cloudflare blip or a 429 returns
  *"search is not answering right now (transient)"* and caches **nothing** — the
  next tap retries immediately. Only a genuine "the site answered: not here"
  gets the short 300 s negative cache.
- **No invented tokens.** Card lines omit whatever we don't know (no fake
  `1080p`, no `0 SUB`, no runtime we didn't parse). An abyss card says
  `no seeking (plays straight through)` when the file is above the origin's range
  ceiling, instead of letting the player discover it mid-film.
- **Cards carry a poster.** The stream picker shows the site's own thumb next to
  every card; the client fetches the image, never us.
- **No borrowed sizes.** The `▤` token only ever shows a real media byte count.
  A master playlist's own length (323 B) once printed as the size of a 1080p
  feature; the playlist probe now reports `pl_bytes` and the card ignores it.
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
| `/configure` | configuration page (install URL builder) |
| `/validate-key?key=…` | live TMDB key check |
| `/manifest.json` | `resources: [stream, subtitles, catalog, meta]`, 3 catalogs |
| `/catalog/{movie\|series}/{id}.json` | a shelf — `?genre=` `?search=` `?skip=` (also path-style `genre=x;skip=24`) |
| `/meta/{type}/{id}.json` | detail page — providers + source fallback |
| `/stream/{movie\|series}/{tt…\|bpx-…[:S:E]}.json` | the cards |
| `/subtitles/{type}/{id}[/{extra}].json` | subtitle tracks for players that ask separately |
| `/player/{abyss-id}` | ~600 B HTML shell that iframes the site's own player (the seek fallback) |
| `/health` | version, uptime, stats, per-cache bytes, keepalive state |
| `/debug/…?k=BPX_DEBUG_KEY` | `search` `page` `embed` `n1` `chain` `resolve` `reqlog` `mem` `net` |
| `/{config}/…` | any route with a per-install config segment |

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
| `BPX_ABYSS` | `1` | `0` = kill switch for the abyssplayer path |
| `BPX_BROWSER_CARD` | `1` | `0` = never offer the `/player/` iframe card |
| `BPX_PROXY` | `auto` | `0` = never use the free proxy pool |
| `BPX_PROXY_SOURCE` | proxyscrape | free HTTP proxy list URL(s), comma-separated |
| `BPX_PROXY_LIST` | *(none)* | hand-picked exits (`http://user:pass@host:port,…`) — always ride first |
| `BPX_PROXY_TRY` | `5` | exits raced concurrently per fetch |
| `BPX_POOL_MAX` | `20` | trained exits kept |
| `BPX_PREWARM` | `1` | `0` = do not warm the catalog shelves at boot |
| `BPX_PREWARM_STREAMS` | `6` | cards warmed per served shelf page, `0` = off |
| `BPX_DEBUG_KEY` | `bpx-dbg-4c9e` | `/debug/*` key — **change this in prod** |
| `TMDB_API_KEY` | built-in | metadata + alternative titles |

---

## Tests

```bash
python3 test_banglaplex.py           # 258 offline tests, every network call mocked
BPX_LIVE=1 python3 test_banglaplex.py # + 4 live integration tests (real site/CDN)
```

The offline suite covers the pure-python AES (FIPS-197 C.1 + a node
`createCipheriv` known-answer for both the 3n1 AES-128-CBC payload and the abyss
AES-256-CTR media/sora tokens), page/embed parsing, the key picker, search and
match scoring, the 429/404/transient branches of the player API, the
no-phantom media gate, subtitle ranking, card formatting, both cache paths
(positive, negative, SWR, wall timeout), every HTTP route and the
zero-bandwidth contract. The abyss block pins a real `datas` fixture from a live
title, the sora token vectors, source dedupe/ordering, the range ceiling, the
kill switch and the 3n1-dead → abyss fallback.

The live block really resolves `tt0213890` (movie) and `tt31924802:1:1`
(series season pack), then plays each emitted card the way a player would:
master → variant → first segment, asserting 206 with bytes. It burns 3n1
rate-limit budget, so don't loop it.

> **The runner is the last thing in the file.** A test defined after
> `if __name__ == "__main__"` silently never runs — that bug shipped once
> already in this fleet. `OFFLINE_TESTS` is a snapshot of `globals()`, so a test
> defined after *that* line was silently skipped too (it reported 245/245 while
> 246 existed); `main()` now diffs the two and exits 2 instead of printing green.
>
> **Pool tests must not depend on wall-clock races.** `as_completed` yields
> already-finished futures in *set* order, so "the slow exit loses" is not a
> deterministic premise — hold the loser on an `threading.Event` and release it
> after the winner is declared. Background threads (SWR refresh, negative retry,
> shelf prewarm, pool trainer) are plain `threading.Thread`s that outlive the test
> which spawned them and land in whatever mock is installed next; the pool
> diagnostics pin `_pool_order` and answer per-exit rather than per-call-order
> because of it. A racer benches its exit from *its own* thread while `_pool_get`
> returns the instant another exit answers, so `_POOL_BAD` is polled, never read
> immediately.
>
> **Cumulative counters are snapshotted, never zeroed.** `_STATS` backs `/health`
> (`late_adopts`, `walls`, `abyss_ok`, `relay_bytes`…) and lives for the process,
> so a test asserts on the *delta*. `late_adopts` itself was lying: `set_result()`
> notifies the waiter and then runs done-callbacks, so the adopt callback usually
> beat `fut.result()` returning on a perfectly normal resolve and counted it —
> prod showed `late_adopts == resolves` (11/11) with zero actual walls. It now
> counts only keys that really gave up at the wall.

---

## Known limits

- **Abyss files over 500 MiB cannot seek.** `Range` works up to
  `FRAG=524288000`; above it the origin answers 200 with the whole body (and one of
  its two backends returns 400 to a ranged request outright). The card is still
  emitted — `moov` sits at the front, so playback starts instantly and runs
  linearly — and says `no seeking (plays straight through)`. Most 1080p features
  are above the ceiling; 480p/720p renditions of the same title usually are not,
  which is why up to three qualities are offered, plus one `·browser` card that
  opens the site's own player where seeking does work.
- **Abyss titles carry no subtitle tracks.** The `datas` blob holds `media`,
  `config` and `danmu` only — no caption list exists to scrape, so those cards
  honestly show no `⟡ N SUB`.
- **~89% of *new* BanglaPlex series are abyss-only** (the 3n1 id is 404 and
  `/api/v1/folder` returns `[]`). These now resolve through path 3; before v1.4.0
  they answered empty.
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
