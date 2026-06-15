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

## Data sources (the `ZEUS_DATA_SOURCE` switch)

ZEUS reads everything through `providers.get_sounds()`. Switch the source with
one environment variable — the rest of the API never changes.

| `ZEUS_DATA_SOURCE` | Cost | What it is |
|--------------------|------|------------|
| `simulated` (default) | free | Realistic placeholder data — for demos |
| `chart` | **free** | **Apple/Shazam official trending chart (RSS) — your $0 MVP** |
| `live` | paid | Licensed provider (Songstats/Soundcharts) — full TikTok counts |

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

### Connecting real licensed data later (`live` mode)

1. Set `ZEUS_DATA_SOURCE=live`
2. Implement `LiveProvider.fetch()` in `providers.py` (a stub is ready)
3. Map the provider's fields onto the `Sound` schema via `LiveProvider._map()`

> ZEUS does not scrape TikTok. The `chart` source is Apple's public feed; the
> `live` source is a licensed provider. The unsigned pipeline assumes the
> artist agrees to any distribution.
