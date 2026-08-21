"""
ZEUS — Data provider layer.

This is the ONLY file you touch when you plug in real licensed data sources
(Soundcharts, Spotontrack, etc.). The rest of the API reads from get_sounds()
and never needs to know whether the data is simulated or live.

To go live:
  1. Set ZEUS_DATA_SOURCE=live in your environment
  2. Fill in LiveProvider.fetch() with your licensed API calls
  3. Map the provider's fields onto the Sound schema below — done.

PERFORMANCE MODEL (important)
-----------------------------
Fetching upstream NEVER happens on the request path. A request only ever
reads an already-built, already-sanitised snapshot out of memory:

    request  ->  get_dataset()  ->  in-memory snapshot   (microseconds)
    cron     ->  /internal/refresh -> refresh_now()      (seconds, off-path)

The snapshot is rebuilt by (a) a prewarm thread at worker boot, (b) your cron
hitting /internal/refresh, and (c) a background refresh kicked off when a
request notices the snapshot went stale (belt-and-braces if the cron dies).
If upstream is down or slow, the last known-good snapshot keeps being served
instead of the request hanging or erroring.
"""

import concurrent.futures
import os
import random
import threading
import time
from datetime import datetime, timedelta

import requests
from requests.adapters import HTTPAdapter

try:  # urllib3 v1 and v2 expose Retry in different places
    from urllib3.util.retry import Retry
except ImportError:  # pragma: no cover
    Retry = None

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
# Shared HTTP session — one TLS handshake per host, reused across every
# country fetch and every refresh, instead of a fresh connection each time.
# ---------------------------------------------------------------------------
HTTP_TIMEOUT = (float(os.environ.get("ZEUS_CONNECT_TIMEOUT", "5")),
                float(os.environ.get("ZEUS_READ_TIMEOUT", "10")))
FETCH_CONCURRENCY = max(1, int(os.environ.get("ZEUS_FETCH_CONCURRENCY", "4")))


def _build_session():
    s = requests.Session()
    retry = None
    if Retry is not None:
        # one retry, not three: a refresh that is failing should fail fast and
        # let the next cron tick try again, not sit on the socket for 10 s
        kwargs = dict(total=1, backoff_factor=0.3,
                      status_forcelist=(429, 500, 502, 503, 504))
        try:
            retry = Retry(allowed_methods=frozenset(["GET"]), **kwargs)
        except TypeError:  # urllib3 < 1.26
            retry = Retry(method_whitelist=frozenset(["GET"]), **kwargs)
    adapter = HTTPAdapter(pool_connections=8, pool_maxsize=16, max_retries=retry)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    s.headers.update({"User-Agent": "zeus-api/1.1 (+https://github.com/ZiedTR/zeus-api)"})
    return s


SESSION = _build_session()


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
    BASE = os.environ.get(
        "ZEUS_CHART_FEED_URL",
        "https://rss.marketingtools.apple.com/api/v2/{country}/music/most-played/{limit}/songs.json",
    )

    # default markets to pull when building a "global-ish" view
    DEFAULT_COUNTRIES = ["us", "gb", "fr", "de", "br", "mx", "au", "ca"]

    def __init__(self, feed="most-played"):
        self.feed = feed  # kept for API compatibility; only most-played is fetched
        env = os.environ.get("ZEUS_CHART_COUNTRIES", "")
        self.countries = [c.strip().lower() for c in env.split(",") if c.strip()] or self.DEFAULT_COUNTRIES
        self.limit = int(os.environ.get("ZEUS_CHART_LIMIT", "50"))

    def fetch(self):
        def norm(s):
            return "".join(ch for ch in (s or "").lower() if ch.isalnum())

        def fetch_country(country):
            url = self.BASE.format(country=country, limit=self.limit)
            try:
                resp = SESSION.get(url, timeout=HTTP_TIMEOUT)
                resp.raise_for_status()
                return country, resp.json().get("feed", {}).get("results", [])
            except Exception:
                return country, []

        # Bounded parallelism over a pooled session: 4 in flight is enough to
        # hide latency without opening 8+ sockets on a small Render instance.
        seen = {}   # normalized title+artist -> sound  (dedupe across countries)
        workers = max(1, min(FETCH_CONCURRENCY, len(self.countries)))
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            for country, results in pool.map(fetch_country, self.countries):
                for rank, item in enumerate(results, start=1):
                    # key on title+artist so the same song across countries merges,
                    # even when Apple assigns it different ids per market
                    key = norm(item.get("name", "")) + "|" + norm(item.get("artistName", ""))
                    if not key.strip("|"):
                        key = item.get("id", str(rank))
                    if key in seen:
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
                "note": "Real Spotify artist signals (followers, popularity, genres) available on Pro/Ultra. "
                        "Exact TikTok video counts & unsigned status require a licensed provider, not yet connected.",
                "fields": ["spotify_followers", "spotify_popularity", "spotify_genres"],
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
        #   resp = SESSION.get(f"{self.base_url}/sounds",
        #                      headers={"Authorization": f"Bearer {self.api_key}"},
        #                      timeout=HTTP_TIMEOUT)
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


# ---------------------------------------------------------------------------
# Snapshot: everything a request needs, precomputed once per refresh.
# Sounds are already stripped of internal "_" keys, indexed by id/ISRC,
# and the per-sound freshness values used by /tiktok-trends are precomputed.
# TREAT THE CONTENTS AS READ-ONLY — every request shares this object.
# ---------------------------------------------------------------------------
class Dataset:
    __slots__ = ("sounds", "index", "derived", "stats",
                 "source", "built_at", "version")

    def __init__(self, sounds, source):
        self.sounds = sounds
        self.source = source
        self.built_at = time.time()
        self.version = "%s-%d-%d" % (source, len(sounds), int(self.built_at))

        # id / ISRC lookup — turns /sounds/<id> from a linear scan into O(1)
        index = {}
        for s in sounds:
            for key in (s.get("isrc"), s.get("id")):
                if key:
                    index.setdefault(str(key).lower(), s)
        self.index = index

        # freshness / emerging score, aligned index-for-index with self.sounds
        today = datetime.utcnow().date()
        derived = []
        for s in sounds:
            age = _days_old(s.get("release_date", ""), today)
            spread = s.get("countries_count", 1)
            derived.append({
                "days_since_release": age,
                "emerging_score": round(spread * _freshness(age), 1),
            })
        self.derived = derived

        # /stats aggregates — computed once per refresh instead of per request
        countries = set()
        genres = {}
        for s in sounds:
            for c in (s.get("trending_in") or s.get("regions") or []):
                countries.add(c)
            g = s.get("genre", "Unknown")
            genres[g] = genres.get(g, 0) + 1
        top_genres = sorted(genres.items(), key=lambda x: x[1], reverse=True)[:5]
        self.stats = {
            "sounds_tracked": len(sounds),
            "countries_covered": sorted(countries),
            "top_genres": [{"genre": g, "count": n} for g, n in top_genres],
            "updated": sounds[0].get("detected") if sounds else None,
        }

    @property
    def age(self):
        return time.time() - self.built_at


def _days_old(release_date, today):
    try:
        d = datetime.strptime((release_date or "")[:10], "%Y-%m-%d").date()
        return max(0, (today - d).days)
    except (ValueError, TypeError):
        return 9999  # unknown date -> treat as old


def _freshness(age):
    # <=7d strongest, decays to ~1 after ~90 days
    if age <= 7:
        return 3.0
    if age <= 30:
        return 2.0
    if age <= 90:
        return 1.3
    return 1.0


# ---------------------------------------------------------------------------
# Snapshot cache. Requests read it; they never build it (except on a truly
# cold worker, and even then only one thread does the work — see single-flight).
# ---------------------------------------------------------------------------
_CACHE = {}
_STATE_LOCK = threading.Lock()      # guards _CACHE / _LOCKS / _REFRESHING / _FAILED_AT
_LOCKS = {}
_REFRESHING = set()
_FAILED_AT = {}
_EMPTY = {}

# TTL is now only "when should the background refresh kick in", never
# "how long a request is allowed to block". Keep it ABOVE your cron interval.
CACHE_TTL = int(os.environ.get("ZEUS_CACHE_TTL", "1800"))  # seconds, default 30 min
PREWARM = os.environ.get("ZEUS_PREWARM", "1") != "0"

# If a cold worker fails to build its first snapshot (upstream down), don't let
# every subsequent request retry and block on the same dead endpoint.
FAIL_COOLDOWN = int(os.environ.get("ZEUS_FAIL_COOLDOWN", "30"))

# Longest a cold request will wait for an in-flight first refresh before giving
# up and answering with an empty page. Never applies once a snapshot exists.
COLD_WAIT = float(os.environ.get("ZEUS_COLD_WAIT", "5"))


def _cache_key(feed):
    return (os.environ.get("ZEUS_DATA_SOURCE", "simulated").lower(), feed)


def _lock_for(key):
    with _STATE_LOCK:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = _LOCKS[key] = threading.Lock()
        return lock


def _peek(key):
    with _STATE_LOCK:
        return _CACHE.get(key)


def refresh_now(feed="most-played", force=False, wait=None):
    """Rebuild the snapshot from upstream. Single-flight: concurrent callers
    share one fetch instead of each firing their own. Returns the Dataset.

    wait: seconds to wait for an in-flight refresh before giving up and
    returning whatever snapshot exists (None if there is none yet). The cron
    and the prewarm thread pass None and wait as long as it takes.

    On failure (upstream down, empty response) the previous snapshot is kept
    and returned — the API keeps serving last known-good data.
    """
    key = _cache_key(feed)
    source = key[0]
    lock = _lock_for(key)
    acquired = lock.acquire() if wait is None else lock.acquire(timeout=wait)
    if not acquired:
        return _peek(key)   # someone else is on it; don't pile up behind them
    try:
        current = _peek(key)
        # another thread refreshed while we waited on the lock
        if current is not None and not force and current.age < CACHE_TTL:
            return current
        try:
            raw = get_provider(feed=feed).fetch()
        except Exception:
            raw = []
        if not raw:
            with _STATE_LOCK:
                _FAILED_AT[key] = time.time()
            return current  # keep serving whatever we already had
        sounds = [{k: v for k, v in s.items() if not k.startswith("_")} for s in raw]
        dataset = Dataset(sounds, source)
        with _STATE_LOCK:
            _CACHE[key] = dataset
            _FAILED_AT.pop(key, None)
        return dataset
    finally:
        lock.release()


def _refresh_async(feed):
    """Kick a refresh onto a background thread, at most one per key."""
    key = _cache_key(feed)
    with _STATE_LOCK:
        if key in _REFRESHING:
            return
        _REFRESHING.add(key)

    def run():
        try:
            refresh_now(feed=feed)
        finally:
            with _STATE_LOCK:
                _REFRESHING.discard(key)

    threading.Thread(target=run, name="zeus-refresh", daemon=True).start()


def _empty_dataset(source):
    with _STATE_LOCK:
        ds = _EMPTY.get(source)
        if ds is None:
            ds = _EMPTY[source] = Dataset([], source)
        return ds


def get_dataset(feed="most-played"):
    """The read path. Returns an in-memory snapshot; never blocks on upstream
    unless this worker has literally never fetched anything yet."""
    key = _cache_key(feed)
    dataset = _peek(key)
    if dataset is not None:
        if dataset.age >= CACHE_TTL:
            _refresh_async(feed)   # safety net if the cron stops calling
        return dataset
    # Cold worker with a dead upstream: fail fast for FAIL_COOLDOWN seconds
    # instead of making every request wait on the same broken endpoint.
    with _STATE_LOCK:
        failed_at = _FAILED_AT.get(key, 0)
    if time.time() - failed_at < FAIL_COOLDOWN:
        return _empty_dataset(key[0])

    # cold worker: build synchronously, single-flight so N concurrent
    # first-requests trigger ONE upstream fetch, not N.
    return refresh_now(feed=feed, wait=COLD_WAIT) or _empty_dataset(key[0])


def get_sounds(feed="most-played"):
    """Single entry point the API uses. Never changes when sources change.

    feed: 'most-played' (top streamed) or 'viral' (Shazam viral / TikTok trends).
    Only affects the chart source; ignored by simulated/live.

    Returns the SHARED, read-only snapshot list. Callers that need to add or
    change fields must copy the dicts first (see app.py) — this avoids
    rebuilding several hundred dicts on every single request.
    """
    return get_dataset(feed=feed).sounds


def cache_status():
    """Small introspection payload for /health and /internal/refresh."""
    out = {}
    with _STATE_LOCK:
        items = list(_CACHE.items())
    for (source, feed), ds in items:
        out["%s:%s" % (source, feed)] = {
            "sounds": len(ds.sounds),
            "age_seconds": round(ds.age, 1),
            "stale": ds.age >= CACHE_TTL,
            "version": ds.version,
        }
    return {"ttl_seconds": CACHE_TTL, "snapshots": out}


def _prewarm():
    try:
        refresh_now(feed="most-played")
    except Exception:
        pass


if PREWARM:
    # Fill the snapshot while gunicorn is still booting, so the very first
    # real request is already a cache hit. Daemon thread: never blocks boot.
    threading.Thread(target=_prewarm, name="zeus-prewarm", daemon=True).start()
