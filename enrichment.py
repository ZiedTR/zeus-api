"""
ZEUS — Spotify enrichment layer.

Adds real Spotify artist signals (followers, popularity, genres) on top of
chart data, for paying RapidAPI tiers only. Uses Spotify's official Client
Credentials flow, which is free with no volume-based billing — so there is
zero marginal data cost per paying customer, only Spotify's own rate limits
(mitigated here with a 6h per-artist cache).

Requires SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET env vars (free, created
at https://developer.spotify.com/dashboard). If unset, enrichment is skipped
and sounds are returned unchanged — nothing breaks.
"""

import base64
import concurrent.futures
import os
import threading
import time

import requests

_TOKEN_LOCK = threading.Lock()
_TOKEN_CACHE = {"token": None, "expires_at": 0}

_ARTIST_CACHE = {}
_ARTIST_CACHE_LOCK = threading.Lock()
ARTIST_CACHE_TTL = int(os.environ.get("ZEUS_SPOTIFY_CACHE_TTL", "21600"))  # 6h


def is_enrichment_available():
    return bool(os.environ.get("SPOTIFY_CLIENT_ID")) and bool(os.environ.get("SPOTIFY_CLIENT_SECRET"))


def _get_token():
    client_id = os.environ.get("SPOTIFY_CLIENT_ID")
    client_secret = os.environ.get("SPOTIFY_CLIENT_SECRET")
    if not client_id or not client_secret:
        return None

    with _TOKEN_LOCK:
        if _TOKEN_CACHE["token"] and time.time() < _TOKEN_CACHE["expires_at"]:
            return _TOKEN_CACHE["token"]

        auth = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        try:
            resp = requests.post(
                "https://accounts.spotify.com/api/token",
                headers={"Authorization": f"Basic {auth}"},
                data={"grant_type": "client_credentials"},
                timeout=5,
            )
            resp.raise_for_status()
            body = resp.json()
        except Exception:
            return None

        _TOKEN_CACHE["token"] = body.get("access_token")
        _TOKEN_CACHE["expires_at"] = time.time() + body.get("expires_in", 3600) - 60
        return _TOKEN_CACHE["token"]


def _lookup_artist(name, token):
    key = name.lower().strip()
    with _ARTIST_CACHE_LOCK:
        cached = _ARTIST_CACHE.get(key)
    if cached and (time.time() - cached[0]) < ARTIST_CACHE_TTL:
        return cached[1]

    data = None
    try:
        resp = requests.get(
            "https://api.spotify.com/v1/search",
            headers={"Authorization": f"Bearer {token}"},
            params={"q": name, "type": "artist", "limit": 1},
            timeout=5,
        )
        resp.raise_for_status()
        items = resp.json().get("artists", {}).get("items", [])
        if items:
            a = items[0]
            data = {
                "spotify_followers": a.get("followers", {}).get("total"),
                "spotify_popularity": a.get("popularity"),
                "spotify_genres": a.get("genres", []),
                "spotify_artist_url": a.get("external_urls", {}).get("spotify"),
            }
    except Exception:
        data = None

    with _ARTIST_CACHE_LOCK:
        _ARTIST_CACHE[key] = (time.time(), data)
    return data


def enrich(sounds):
    """Return a new list with real Spotify artist signals merged in.

    Dedupes by artist (a page of 50 tracks is rarely 50 distinct artists),
    fetches in parallel, and reuses the 6h cache on repeat calls.
    """
    token = _get_token()
    if not token:
        return sounds

    artists = sorted({s.get("artist", "") for s in sounds if s.get("artist")})
    if not artists:
        return sounds

    results = {}

    def work(name):
        return name, _lookup_artist(name, token)

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(10, len(artists))) as pool:
        for name, data in pool.map(work, artists):
            if data:
                results[name] = data

    enriched = []
    for s in sounds:
        s2 = dict(s)
        extra = results.get(s.get("artist", ""))
        if extra:
            s2.update(extra)
        enriched.append(s2)
    return enriched
