# ZEUS API

Objective cross-platform signals for TikTok sounds — an A&R radar.
ZEUS surfaces and sorts raw data (TikTok video usage, Shazam tags, Spotify
streams). The A&R team makes the evaluation. No subjective scoring.

## What it does

Returns, per sound, the raw measurable signals a label needs to spot rising
sounds early and identify **unsigned** artists worth reaching out to.

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Service meta + endpoint list |
| GET | `/health` | Health check |
| GET | `/sounds` | List sounds (filters + sorting + pagination) |
| GET | `/sounds/<isrc>` | Single sound by ISRC |
| GET | `/unsigned` | Unsigned artists only |
| GET | `/stats` | Aggregate counters |

### `/sounds` query parameters (all objective)

- `unsigned=true` — only unsigned artists
- `genre=Drill` — filter by genre
- `region=US` — filter by active region
- `q=bloom` — search title/artist
- `sort=tiktok_videos|shazam|listeners|velocity|growth` (default: `tiktok_videos`)
- `order=desc|asc` (default: `desc`)
- `limit=50` (max 200), `offset=0`

Example:
```
/sounds?unsigned=true&sort=velocity&limit=20
```

## Run locally

```bash
pip install -r requirements.txt
python app.py
# → http://localhost:5000
```

## Deploy on Render

1. Push this folder to a GitHub repo.
2. New → Web Service → connect the repo.
3. Build command: `pip install -r requirements.txt`
4. Start command: `gunicorn app:app`
5. Deploy. Render gives you a public URL.

## Publish on RapidAPI

1. Add a new API → point it at your Render URL.
2. Define the endpoints above, set your pricing tiers (Basic / Pro / Ultra).
3. RapidAPI handles auth, billing, and rate limiting — you never touch the
   client's payment directly.
4. In RapidAPI Studio → Security, copy the **Proxy Secret** and set it as
   `ZEUS_RAPIDAPI_PROXY_SECRET` on Render (see below) — this is what stops
   people from calling your Render URL directly and faking a paid plan.

## Multi-marketplace listing

ZEUS can be listed on more than one marketplace at once (RapidAPI, Zyla API
Hub, ...) — they all just point to the same Render URL. Base/free endpoints
are never blocked, regardless of which marketplace (or no marketplace) the
request came from, so listing on a new one never requires a code change.

## Tier gating (pay-to-unlock, zero cost to you)

Everyone gets the free chart data from `/sounds` and `/tiktok-trends`. Paying
tiers additionally get real Spotify artist signals merged in — followers,
popularity score, genres — fetched live via Spotify's official (free) API.
Nothing is fetched, and nothing costs anything, until a paying request comes in.

This gating currently only works for **RapidAPI** plans, because RapidAPI is
the only marketplace that gives providers a secret to verify a request
actually came through its billing proxy. A request claiming a paid plan
without a valid proxy secret is simply treated as free — never blocked,
just not enriched. (If another marketplace offers an equivalent verification
mechanism later, the same pattern can be added for it in `app.py`.)

Setup (both required for RapidAPI gating to actually work):

- `SPOTIFY_CLIENT_ID` / `SPOTIFY_CLIENT_SECRET` — free, from
  https://developer.spotify.com/dashboard (create an app, no cost, no
  volume billing). Without these, premium enrichment is silently skipped.
- `ZEUS_RAPIDAPI_PROXY_SECRET` — the Proxy Secret from RapidAPI Studio
  (Hub Listing → Gateway → Firewall Settings). Without this set, a request
  claiming `X-RapidAPI-Subscription: ultra` is never trusted, so it just
  gets the free data — safe by default either way.
- `ZEUS_PREMIUM_PLANS` — comma-separated plan names that count as "paying"
  (default: `pro,ultra`). Must match the plan names you create in RapidAPI
  Studio exactly (case-insensitive).
- `ZEUS_SPOTIFY_CACHE_TTL` — seconds to cache each artist's Spotify data
  (default: 21600 = 6h), to stay well under Spotify's rate limits.

Every response includes `"premium_enriched": true/false` so clients (and you)
can see whether the gate fired.

## Data sources (the `ZEUS_DATA_SOURCE` switch)

ZEUS reads everything through `providers.get_sounds()`. Switch the source with
one environment variable — the rest of the API never changes.

| `ZEUS_DATA_SOURCE` | Cost | What it is |
|--------------------|------|------------|
| `simulated` (default) | free | Realistic placeholder data — for demos |
| `chart` | **free** | **Apple/Shazam official trending chart (RSS) — your $0 MVP** |
| `live` | paid | Licensed provider (Songstats/Soundcharts) — full TikTok counts (optional, future upgrade) |

> Note: paying tiers do **not** require `live`/a paid provider anymore — see
> "Tier gating" below. `live` stays available for later if you want exact
> TikTok counts and are willing to pay a licensed provider for them.

### The free `chart` mode — your $0 launch

Set `ZEUS_DATA_SOURCE=chart`. ZEUS pulls Apple's official Marketing Tools RSS
feed (the data behind the Shazam / Apple Music trending charts). It's public,
updated daily, needs no API key, and is **not scraping** — Apple publishes it
for exactly this kind of use.

It gives you, per sound: title, artist, genre, chart rank, artwork, and which
countries it's charting in — i.e. **which sounds are trending right now**,
globally and per country. It does *not* give exact TikTok video counts; those
come from the paid `live` source later, once the client pays.

Optional env vars:
- `ZEUS_CHART_COUNTRIES` — comma-separated, e.g. `us,fr,jp` (default: us,gb,fr,de,br,mx,au,ca)
- `ZEUS_CHART_LIMIT` — entries per country: 10, 25, 50, or 100 (default: 50)
- `ZEUS_CACHE_TTL` — seconds to cache fetched results per data source/feed
  (default: 900 = 15 min). Country feeds are fetched in parallel and cached
  so requests stay fast and don't hammer the upstream feed or a paid
  provider on every call.

### Connecting real licensed data later (`live` mode)

1. Set `ZEUS_DATA_SOURCE=live`
2. Implement `LiveProvider.fetch()` in `providers.py` (a stub is ready)
3. Map the provider's fields onto the `Sound` schema via `LiveProvider._map()`

> ZEUS does not scrape TikTok. The `chart` source is Apple's public feed; the
> `live` source is a licensed provider. The unsigned pipeline assumes the
> artist agrees to any distribution.
