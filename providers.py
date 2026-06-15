"""
ZEUS — Data provider layer.

This is the ONLY file you touch when you plug in real licensed data sources
(Soundcharts, Spotontrack, etc.). The rest of the API reads from get_sounds()
and never needs to know whether the data is simulated or live.

To go live:
  1. Set ZEUS_DATA_SOURCE=live in your environment
  2. Fill in LiveProvider.fetch() with your licensed API calls
  3. Map the provider's fields onto the Sound schema below — done.
"""

import os
import random
from datetime import datetime, timedelta

# ---------------------------------------------------------------------------
# Sound schema — the single shape every endpoint returns.
# Keep this stable; map any provider's fields onto these keys.
# ---------------------------------------------------------------------------
SOUND_FIELDS = [
    "id",            # stable unique id (use ISRC when available)
    "isrc",          # International Standard Recording Code — the cross-platform key
    "title",
    "artist",
    "genre",
    "signed",        # bool — is the artist already signed to a label
    "tiktok_videos", # raw count of TikTok videos using the sound
    "shazam_tags",   # Shazam tags (per day)
    "spotify_listeners",  # monthly listeners
    "stream_growth_7d",   # % growth over 7 days
    "velocity",      # raw % change, signed string e.g. "+340%"
    "regions",       # list of active region codes
    "first_detected",# ISO date the sound was first seen by ZEUS
]


# ---------------------------------------------------------------------------
# SIMULATED provider — realistic placeholder data (no scraping, no cost).
# Used until real sources are connected.
# ---------------------------------------------------------------------------
class SimulatedProvider:
    name = "simulated"

    _SEED = [
        ("Midnight Bloom", "Lena Vex", "Hyperpop", False, 12400, 842, 48210, 118, "+340%", ["US", "UK", "DE"]),
        ("Concrete Heart", "KODA", "Drill", False, 9820, 710, 61540, 96, "+288%", ["UK", "FR"]),
        ("Paper Skies", "Mira Solis", "Indie Pop", False, 7110, 680, 33900, 88, "+265%", ["US", "BR"]),
        ("Velvet Static", "No Signal", "Electro", True, 15600, 590, 128400, 102, "+210%", ["DE", "NL"]),
        ("Goldrush", "Saint Aria", "R&B", False, 5440, 540, 22180, 74, "+198%", ["US"]),
        ("Lowtide", "Echo Park Twins", "Surf Pop", False, 4210, 498, 18760, 69, "+176%", ["US", "AU"]),
        ("Ember", "Talia Rue", "Pop", True, 19200, 455, 204300, 81, "+155%", ["GLOBAL"]),
        ("Neon Saints", "VICE CITY", "Synthwave", False, 3890, 430, 15420, 58, "+142%", ["US", "MX"]),
        ("Holy Water", "Junip", "Folk", True, 22400, 388, 310500, 66, "+118%", ["GLOBAL"]),
        ("Bad Reception", "Cleo Dane", "Bedroom Pop", False, 2980, 360, 11200, 49, "+104%", ["US", "CA"]),
        ("Riptide City", "Mako Lane", "Alt Rock", False, 2440, 332, 9840, 44, "+92%", ["AU"]),
        ("Afterglow", "Sena", "Pop", True, 31200, 298, 442800, 71, "+68%", ["GLOBAL"]),
        ("Slow Burn", "The Hollow Set", "Indie", False, 1920, 270, 7610, 38, "+54%", ["UK"]),
        ("Glasshouse", "Yuna Bright", "Dream Pop", False, 1510, 240, 6330, 33, "+41%", ["US", "JP"]),
        ("Run It Back", "DBL TAP", "Dance", True, 40100, 198, 521000, 52, "+12%", ["GLOBAL"]),
        ("Cold Cathedral", "Vesper", "Ambient", False, 980, 172, 4910, 24, "-8%", ["DE"]),
    ]

    def fetch(self):
        sounds = []
        for i, row in enumerate(self._SEED):
            title, artist, genre, signed, vids, shz, listeners, growth, vel, regions = row
            # fake ISRC, deterministic per row
            isrc = "ZZ{:s}{:07d}".format("ZEUS"[:2].upper(), 1000000 + i)
            detected_days = random.choice([3, 5, 6]) if i % 3 == 0 else random.choice([22, 30, 41])
            sounds.append({
                "id": isrc,
                "isrc": isrc,
                "title": title,
                "artist": artist,
                "genre": genre,
                "signed": signed,
                "tiktok_videos": vids,
                "shazam_tags": shz,
                "spotify_listeners": listeners,
                "stream_growth_7d": growth,
                "velocity": vel,
                "regions": regions,
                "trending_in": regions,
                "countries_count": (8 if regions == ["GLOBAL"] else len(regions)),
                "first_detected": (datetime.utcnow() - timedelta(days=detected_days)).date().isoformat(),
            })
        return sounds


# ---------------------------------------------------------------------------
# CHART provider — FREE, LEGAL, REAL.
# Reads Apple's official Marketing Tools RSS feed (the same data behind the
# Shazam / Apple Music "trending" charts). No auth, no scraping, no cost.
# Apple publishes this feed for marketing use and updates it daily.
#
# This is your $0 MVP source: it tells you which sounds are trending right now,
# globally or per country. It does NOT give exact TikTok video counts — that's
# the paid enrichment layer (LiveProvider) you add once the client pays.
# ---------------------------------------------------------------------------
class ChartProvider:
    name = "chart"

    # Apple Marketing Tools RSS — most-played songs. No API key required, daily.
    # This is the only free music feed Apple exposes here. The "viral" signal is
    # DERIVED in the API layer (cross-country spread), not a separate feed.
    BASE = "https://rss.marketingtools.apple.com/api/v2/{country}/music/most-played/{limit}/songs.json"

    # default markets to pull when building a "global-ish" view
    DEFAULT_COUNTRIES = ["us", "gb", "fr", "de", "br", "mx", "au", "ca"]

    def __init__(self, feed="most-played"):
        self.feed = feed  # kept for API compatibility; only most-played is fetched
        env = os.environ.get("ZEUS_CHART_COUNTRIES", "")
        self.countries = [c.strip().lower() for c in env.split(",") if c.strip()] or self.DEFAULT_COUNTRIES
        self.limit = int(os.environ.get("ZEUS_CHART_LIMIT", "50"))

    def fetch(self):
        import requests

        seen = {}   # id -> sound  (dedupe across countries, accumulate spread)
        for country in self.countries:
            url = self.BASE.format(country=country, limit=self.limit)
            try:
                resp = requests.get(url, timeout=10)
                resp.raise_for_status()
                results = resp.json().get("feed", {}).get("results", [])
            except Exception:
                continue

            for rank, item in enumerate(results, start=1):
                key = item.get("id") or (item.get("name", "") + item.get("artistName", ""))
                if key in seen:
                    # seen in another country: add country, keep best (lowest) rank
                    if country.upper() not in seen[key]["trending_in"]:
                        seen[key]["trending_in"].append(country.upper())
                    if rank < seen[key]["_rank"]:
                        seen[key]["_rank"] = rank
                        seen[key]["chart_rank"] = rank
                    continue
                seen[key] = self._map(item, country, rank)

        # compute spread (how many countries) — the real virality proxy
        for s in seen.values():
            s["countries_count"] = len(s["trending_in"])

        return sorted(seen.values(), key=lambda s: s["_rank"])

    @staticmethod
    def _map(item, country, rank):
        """Map an Apple RSS chart entry onto the ZEUS Sound schema."""
        return {
            "id": item.get("id", ""),
            "title": item.get("name", ""),
            "artist": item.get("artistName", ""),
            "genre": (item.get("genres") or [{}])[0].get("name", "Unknown"),
            "chart_rank": rank,
            "trending_in": [country.upper()],
            "countries_count": 1,
            "artwork": item.get("artworkUrl100", ""),
            "release_date": item.get("releaseDate", ""),
            "detected": datetime.utcnow().date().isoformat(),
            "source": "Apple / Shazam trending chart",
            "premium_signals": {
                "available": False,
                "note": "TikTok video count, Shazam velocity, stream growth & unsigned status available on Pro plan",
                "fields": ["tiktok_videos", "shazam_tags", "spotify_listeners", "stream_growth_7d", "unsigned_status"],
            },
            "_rank": rank,
            "_signed": None,
        }


# ---------------------------------------------------------------------------
# LIVE provider — STUB. Fill this in when you have a licensed source.
# ---------------------------------------------------------------------------
class LiveProvider:
    name = "live"

    def __init__(self):
        self.api_key = os.environ.get("ZEUS_PROVIDER_API_KEY", "")
        self.base_url = os.environ.get("ZEUS_PROVIDER_URL", "")

    def fetch(self):
        # TODO: when a licensed provider is contracted, implement here.
        #
        # Example shape (pseudo-code):
        #   import requests
        #   resp = requests.get(f"{self.base_url}/sounds",
        #                       headers={"Authorization": f"Bearer {self.api_key}"})
        #   raw = resp.json()
        #   return [self._map(item) for item in raw["data"]]
        #
        # Map each provider record onto SOUND_FIELDS via _map() below.
        raise NotImplementedError(
            "Live data source not connected yet. "
            "Set ZEUS_DATA_SOURCE=simulated, or implement LiveProvider.fetch()."
        )

    @staticmethod
    def _map(item):
        """Translate a provider's record into the ZEUS Sound schema."""
        return {
            "id": item.get("isrc") or item.get("id"),
            "isrc": item.get("isrc"),
            "title": item.get("track_name"),
            "artist": item.get("artist_name"),
            "genre": item.get("genre", "Unknown"),
            "signed": item.get("label") not in (None, "", "Independent"),
            "tiktok_videos": item.get("tiktok_video_count", 0),
            "shazam_tags": item.get("shazam_count", 0),
            "spotify_listeners": item.get("spotify_monthly_listeners", 0),
            "stream_growth_7d": item.get("stream_growth_7d", 0),
            "velocity": item.get("velocity", "0%"),
            "regions": item.get("regions", []),
            "first_detected": item.get("first_seen", datetime.utcnow().date().isoformat()),
        }


# ---------------------------------------------------------------------------
# Provider selector — driven by env var. Defaults to simulated (safe).
# ---------------------------------------------------------------------------
def get_provider(feed="most-played"):
    source = os.environ.get("ZEUS_DATA_SOURCE", "simulated").lower()
    if source == "live":
        return LiveProvider()
    if source == "chart":
        return ChartProvider(feed=feed)
    return SimulatedProvider()


def get_sounds(feed="most-played"):
    """Single entry point the API uses. Never changes when sources change.

    feed: 'most-played' (top streamed) or 'viral' (Shazam viral / TikTok trends).
    Only affects the chart source; ignored by simulated/live.
    """
    sounds = get_provider(feed=feed).fetch()
    # strip internal sort keys (prefixed with _) before returning
    for s in sounds:
        for k in [k for k in list(s.keys()) if k.startswith("_")]:
            s.pop(k, None)
    return sounds
