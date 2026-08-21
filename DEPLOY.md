# ZEUS — deploy & latency checklist

Three settings decide whether ZEUS answers in ~1 ms or in ~5 s. Get these right
and everything else is detail.

---

## 1. Your cron must call `/internal/refresh`, not `/health`

`/health` is a liveness probe. It deliberately does **not** touch the data
snapshot — so a pinger aimed at `/health` wakes the instance without ever
filling the cache, and the first real user still pays the full upstream fetch.

Point the cron at the refresh endpoint instead:

```
GET https://<your-app>.onrender.com/internal/refresh?token=<ZEUS_REFRESH_TOKEN>
```

or, preferred, with the token in a header:

```
curl -fsS -H "X-Zeus-Refresh-Token: $ZEUS_REFRESH_TOKEN" \
     https://<your-app>.onrender.com/internal/refresh
```

Suggested interval: **every 10 minutes**. Render Cron Job, GitHub Actions
schedule, cron-job.org — any of them works.

`ZEUS_REFRESH_TOKEN` is required. Without it the endpoint returns 503 and stays
closed: a refresh is the one expensive thing this service does, and it must not
be triggerable by anyone who knows the URL.

Check it is working:

```
curl -s https://<your-app>.onrender.com/internal/cache
# -> {"ttl_seconds":1800,"snapshots":{"chart:most-played":{"age_seconds":123.4,...}}}
```

If `age_seconds` keeps climbing past `ttl_seconds`, your cron is not landing.

---

## 2. `ZEUS_CACHE_TTL` must be larger than the cron interval

The TTL is no longer "how long a request may block" — nothing blocks any more.
It is only "when should a background refresh kick in if the cron went missing".

Set it comfortably above the cron interval so the cron is always what refreshes:

```
ZEUS_CACHE_TTL=1800     # 30 min, with a 10 min cron
```

Too low, and requests keep triggering background refreshes the cron was already
handling. Too high, and a dead cron goes unnoticed for a long time.

---

## 3. The Procfile: 1 sync worker serialises everything

The old start command was:

```
web: gunicorn app:app
```

That is gunicorn's default: **one sync worker, one request at a time**. During a
5 s upstream fetch, every other request sat in the accept queue behind it. Even
with the fetch now off the request path, one worker still means one concurrent
request.

Current:

```
web: gunicorn app:app --worker-class gthread --workers 2 --threads 8 \
     --timeout 30 --graceful-timeout 20 --keep-alive 5 \
     --max-requests 1000 --max-requests-jitter 100 --access-logfile -
```

- `gthread` + 8 threads: requests that are only reading an in-memory snapshot
  overlap freely.
- `2` workers: survives one worker being recycled; tune with `WEB_CONCURRENCY`
  and `WEB_THREADS` env vars without editing the Procfile.
- `--max-requests` recycles workers periodically — cheap protection against any
  slow leak.
- **No `--preload`**: each worker warms its own snapshot at boot. With
  `--preload` the prewarm thread would run pre-fork and die with the master.

If Render's start command is set in the dashboard rather than read from the
Procfile, paste the same line there.

---

## 4. Remove `/debug-headers` — it leaks your proxy secret

`/debug-headers` echoed **every incoming header**, including
`X-RapidAPI-Proxy-Secret`. Anyone who knows your direct Render URL could read
it, then forge `X-RapidAPI-Subscription: ultra` requests and get premium data
for free — exactly what the secret exists to prevent.

The endpoint has been deleted in this change. If you still need to inspect what
a marketplace forwards, re-add it temporarily behind `ZEUS_REFRESH_TOKEN` and
remove it again immediately.

**Rotate the secret** in RapidAPI Studio (Hub Listing → Gateway → Firewall
Settings) and update `ZEUS_RAPIDAPI_PROXY_SECRET` on Render — assume the old one
was exposed for as long as the endpoint was live.

---

## Rollout: prove nothing broke for your subscribers

`smoke_test.py` compares the API *contract* — status codes, top-level keys,
per-item field names and types, sort order, pagination invariants — rather than
the values, so it doesn't cry wolf when Apple's chart simply changed between the
two runs.

```bash
# 1. BEFORE deploying, against the currently live API
python3 smoke_test.py --base https://<your-app>.onrender.com --save before.json

# 2. deploy

# 3. AFTER, same URL
python3 smoke_test.py --base https://<your-app>.onrender.com --check before.json
```

Exit code 0 means every endpoint still answers with the same shape. It also
checks that `premium_enriched` is never `true` for an unauthenticated caller,
that `count`/`total`/`limit` stay consistent, that the default `/sounds` order is
still ascending `chart_rank`, and that a repeat request with `If-None-Match`
never returns a *different* body.

Measured locally on the full endpoint matrix: contract identical on 20/20,
total bytes on the wire 404 179 -> 29 180.

### What a client could still notice

| Change | Who it affects | What to do |
|---|---|---|
| `/debug-headers` returns 404 | anyone calling it (it was never a documented endpoint) | nothing — and rotate the proxy secret |
| Free responses carry `Cache-Control: public, max-age=120` | a caching layer may serve data up to 2 min old | set `ZEUS_CLIENT_CACHE=0` if you want none |
| Responses are gzipped | only clients that sent `Accept-Encoding: gzip` | nothing; every HTTP client handles it |
| `ETag` / `304` | only clients that send `If-None-Match` | nothing |
| `version` in `/` is now `1.1.0` | anyone pinning it (unlikely) | — |

Premium responses are `private, no-store` and carry
`Vary: X-RapidAPI-Subscription`, so no cache can hand enriched data to a free
caller.

### If Render's start command lives in the dashboard

The Procfile change does nothing in that case. Paste the same command into
Settings -> Start Command, or you keep the single sync worker.

On the smallest Render instance (shared CPU), `WEB_CONCURRENCY=1` with
`WEB_THREADS=8` is often better than 2 workers — same concurrency for reads,
half the memory.

### Rolling back

Nothing in this change touches stored state — the snapshot is in-memory only.
Reverting the commit and redeploying is a complete rollback, no cleanup needed.

---

## Environment variables

| Variable | Default | What it does |
|---|---|---|
| `ZEUS_DATA_SOURCE` | `simulated` | `simulated` \| `chart` \| `live` |
| `ZEUS_REFRESH_TOKEN` | *(unset)* | **Required** to enable `/internal/refresh` |
| `ZEUS_CACHE_TTL` | `1800` | Snapshot age before a background refresh kicks in |
| `ZEUS_PREWARM` | `1` | Build the snapshot at worker boot (`0` disables) |
| `ZEUS_FETCH_CONCURRENCY` | `4` | Parallel country fetches per refresh |
| `ZEUS_GZIP_LEVEL` | `3` | gzip level (3 = ~17x on this JSON for ~0.15 ms) |
| `ZEUS_COMPRESS_MIN` | `700` | Don't compress responses smaller than this |
| `ZEUS_CONNECT_TIMEOUT` / `ZEUS_READ_TIMEOUT` | `5` / `10` | Upstream timeouts (seconds) |
| `ZEUS_COLD_WAIT` | `5` | Max a first request waits on an in-flight first refresh |
| `ZEUS_FAIL_COOLDOWN` | `30` | After a failed refresh on a cold worker, answer instantly for this long instead of retrying per request |
| `ZEUS_CLIENT_CACHE` | `120` | `max-age` on free-tier responses |
| `ZEUS_CHART_COUNTRIES` | `us,gb,fr,de,br,mx,au,ca` | Markets to pull |
| `ZEUS_CHART_LIMIT` | `50` | Entries per country (10/25/50/100) |
| `ZEUS_CHART_FEED_URL` | Apple RSS | Override the feed URL (tests) |
| `ZEUS_RAPIDAPI_PROXY_SECRET` | *(unset)* | Verifies a paid-plan claim came via RapidAPI |
| `ZEUS_PREMIUM_PLANS` | `pro,ultra` | Plan names that count as paying |
| `SPOTIFY_CLIENT_ID` / `SPOTIFY_CLIENT_SECRET` | *(unset)* | Enables premium enrichment |
| `ZEUS_SPOTIFY_CACHE_TTL` | `21600` | Per-artist Spotify cache (6 h) |
| `ZEUS_SPOTIFY_CONCURRENCY` | `8` | Parallel Spotify lookups |
| `WEB_CONCURRENCY` / `WEB_THREADS` | `2` / `8` | gunicorn workers / threads |

---

## Tuning note: `ZEUS_FETCH_CONCURRENCY`

Default is `4`. With the 8 default countries that means two waves, so a refresh
takes ~2x one country's round-trip (measured: 2.4 s against a 1.2 s/country
feed, vs 1.2 s at concurrency 8).

This only affects how long a *refresh* takes, never a user request — refreshes
run on the cron and on the boot prewarm thread. The one moment it is visible is
the very first request to a brand-new instance, before the prewarm finishes.
If Render spins your instance down often and you care about that first hit,
`ZEUS_FETCH_CONCURRENCY=8` halves it, at the cost of 8 concurrent sockets
instead of 4.

---

## Measured (local, mock feed at 1.2 s per country, 8 countries)

| | Avant | Apres |
|---|---|---|
| Snapshot past TTL, first request | **1 218 ms** | **6.7 ms** |
| Upstream returning 503 | **1 217 ms** | **3.3 ms** (last known-good) |
| Upstream 503 *and* no snapshot yet (worst case) | blocks every request | **2-6 ms**, empty page, self-heals |
| Bytes on the wire, average | 25 626 B | **1 862 B** |
| Repeat request with `If-None-Match` | full body, 200 | **304, 0 B** |
| Server CPU for 6 representative requests | 4.28 ms | 4.18 ms (3.24 ms without gzip) |
| `/tiktok-trends` CPU | 1.31 ms | **0.69 ms** |
| `/stats` CPU | 0.41 ms | **0.22 ms** |
| `/sounds/<id>` CPU | 0.40 ms | **0.23 ms** |

The CPU total barely moves because gzip costs roughly what the removed
per-request work saved — but it buys a 14x cut in bytes, which is what a client
on a real network actually waits for.

---

## Why requests are fast now

| | Before | After |
|---|---|---|
| Upstream fetch | on the request path | off it — cron + boot prewarm |
| TLS handshakes per refresh | one per country | one, pooled session |
| Concurrent cold requests | N fetches | 1 (single-flight) |
| Upstream down | error or timeout | last known-good snapshot |
| Per-request work | rebuild every sound dict, re-aggregate `/stats`, re-parse dates | read a precomputed snapshot |
| Response bytes | full JSON every time | gzip + `ETag` → `304 Not Modified` |
| Concurrency | 1 request at a time | 2 workers x 8 threads |
