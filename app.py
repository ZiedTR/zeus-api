"""
ZEUS API — A&R signal radar.
Serves raw, objective cross-platform signals for TikTok sounds.
ZEUS surfaces and sorts the data; the A&R team makes the call.

Stack: Python / Flask. Deploy on Render. Distribute on RapidAPI.
Data comes from providers.get_sounds() — simulated until real sources are connected.

Latency model: every endpoint reads an in-memory snapshot built off the request
path (prewarm at boot + your cron calling /internal/refresh). Nothing here ever
waits on Apple's feed. See DEPLOY.md.
"""

import gzip
import hashlib
import hmac
import os

from flask import Flask, g, jsonify, request

import enrichment
import providers
from providers import get_dataset, get_provider, get_sounds

app = Flask(__name__)

# --- Marketplace proxy verification -----------------------------------------
# ZEUS is listed on multiple marketplaces (RapidAPI, Zyla API Hub, ...). Base
# data (chart endpoints) is free and open to everyone, from any marketplace or
# direct call — it is NEVER blocked here. Only the *premium* Spotify
# enrichment is gated, and only RapidAPI's claim of a paying plan is trusted,
# because only RapidAPI gives us a secret to verify the claim actually came
# through its proxy. Without that check, anyone could forge
# X-RapidAPI-Subscription directly and get premium data for free.
RAPIDAPI_PROXY_SECRET = os.environ.get("ZEUS_RAPIDAPI_PROXY_SECRET", "")

# Which RapidAPI plan names (from X-RapidAPI-Subscription) count as "paying".
# Must match the plan names you configure in RapidAPI Studio.
PREMIUM_PLANS = {
    p.strip().lower()
    for p in os.environ.get("ZEUS_PREMIUM_PLANS", "pro,ultra").split(",")
    if p.strip()
}

# Shared secret your cron sends to /internal/refresh. If unset, the endpoint
# is disabled rather than left open — a refresh is the one expensive operation
# in this service and must not be triggerable by anyone who knows the URL.
REFRESH_TOKEN = os.environ.get("ZEUS_REFRESH_TOKEN", "")

# How long clients / the RapidAPI edge may reuse a free-tier response.
CLIENT_CACHE_SECONDS = int(os.environ.get("ZEUS_CLIENT_CACHE", "120"))
COMPRESS_MIN_BYTES = int(os.environ.get("ZEUS_COMPRESS_MIN", "700"))
# Level 3 hits ~17x on this JSON for ~0.15 ms; level 6 buys 3 more points of
# ratio for 3x the CPU, which is a bad trade on a small Render instance.
GZIP_LEVEL = int(os.environ.get("ZEUS_GZIP_LEVEL", "3"))


def _is_premium_request():
    sub = request.headers.get("X-RapidAPI-Subscription", "")
    if not sub:
        return False
    if RAPIDAPI_PROXY_SECRET:
        provided = request.headers.get("X-RapidAPI-Proxy-Secret", "")
        if not hmac.compare_digest(provided, RAPIDAPI_PROXY_SECRET):
            return False  # claims a paid plan but can't prove it came via RapidAPI
    return sub.strip().lower() in PREMIUM_PLANS


def _maybe_enrich(data):
    """Layer real Spotify signals on chart data, for paying tiers only.

    Free tier: chart data as-is. Paying tiers: same data plus real Spotify
    followers/popularity/genres per artist — at zero marginal data cost.
    """
    if not enrichment.is_enrichment_available() or not _is_premium_request():
        return data, False
    g.zeus_premium = True
    return enrichment.enrich(data), True


def _int(v):
    """Parse a numeric value that may carry % or commas, or be None."""
    if v is None:
        return 0
    try:
        return int(str(v).replace("%", "").replace(",", "").replace("+", ""))
    except (ValueError, TypeError):
        return 0


# ---------------------------------------------------------------------------
# Response pipeline: ETag/304 + gzip + cache headers.
# On a 30 KB /sounds page this is the difference between shipping 30 KB and
# shipping ~4 KB — or 0 bytes when the client already has the current version.
# ---------------------------------------------------------------------------
@app.after_request
def _finalize(resp):
    if resp.direct_passthrough or resp.status_code >= 400:
        return resp

    resp.headers["Vary"] = "Accept-Encoding, X-RapidAPI-Subscription"

    if "Cache-Control" not in resp.headers:
        if getattr(g, "zeus_premium", False):
            # premium payloads differ per subscriber — never let an edge cache them
            resp.headers["Cache-Control"] = "private, no-store"
        else:
            resp.headers["Cache-Control"] = "public, max-age=%d" % CLIENT_CACHE_SECONDS

    body = resp.get_data()
    resp.set_etag(hashlib.md5(body).hexdigest())
    resp = resp.make_conditional(request)
    if resp.status_code == 304:
        return resp

    if (len(body) >= COMPRESS_MIN_BYTES
            and "gzip" in request.headers.get("Accept-Encoding", "")
            and "Content-Encoding" not in resp.headers):
        resp.set_data(gzip.compress(body, GZIP_LEVEL))
        resp.headers["Content-Encoding"] = "gzip"
        resp.headers["Content-Length"] = str(len(resp.get_data()))
    return resp


# ---------------------------------------------------------------------------
# Health / meta
# ---------------------------------------------------------------------------
@app.route("/")
def root():
    return jsonify({
        "service": "ZEUS — TikTok & Shazam Trending Sounds API",
        "tagline": "Real-time trending sounds across multiple countries, from official charts.",
        "data_source": get_provider().name,
        "endpoints": {
            "GET /sounds": "Top streamed sounds (Apple Music chart)",
            "GET /tiktok-trends": "Sounds going viral on social incl. TikTok (Shazam viral chart)",
            "GET /sounds/<id>": "Single sound by id",
            "GET /stats": "Coverage: countries, top genres, totals",
            "GET /unsigned": "Unsigned-artist leads (Pro plan)",
            "GET /health": "Health check",
        },
        "filters": ["country", "genre", "q", "sort", "order", "limit", "offset"],
        "version": "1.1.0",
    })


@app.route("/health")
def health():
    """Liveness only — deliberately does NOT touch the data snapshot, so an
    uptime pinger can't be what keeps (or fails to keep) the cache warm."""
    resp = jsonify({"status": "ok", "source": get_provider().name})
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/internal/refresh", methods=["GET", "POST"])
def internal_refresh():
    """Rebuild the data snapshot. THIS is what your cron should call.

    Auth: send the shared secret as `X-Zeus-Refresh-Token: <token>` or
    `?token=<token>`. Without ZEUS_REFRESH_TOKEN set, the endpoint is off.
    """
    if not REFRESH_TOKEN:
        resp = jsonify({"error": "refresh endpoint disabled",
                        "hint": "set ZEUS_REFRESH_TOKEN to enable it"})
        resp.headers["Cache-Control"] = "no-store"
        return resp, 503

    provided = request.headers.get("X-Zeus-Refresh-Token") or request.args.get("token", "")
    if not hmac.compare_digest(provided, REFRESH_TOKEN):
        resp = jsonify({"error": "unauthorized"})
        resp.headers["Cache-Control"] = "no-store"
        return resp, 401

    force = request.args.get("force", "true") != "false"
    providers.refresh_now(feed="most-played", force=force)
    resp = jsonify({"status": "refreshed", "cache": providers.cache_status()})
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/internal/cache")
def internal_cache():
    """Read-only view of snapshot age/size — handy to confirm the cron works."""
    resp = jsonify(providers.cache_status())
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ---------------------------------------------------------------------------
# Core: list sounds with objective filters and sorting.
# No subjective scoring — raw signals only.
# ---------------------------------------------------------------------------
def _apply_query(data, default_sort=None):
    """Apply objective filters, sorting and pagination from query params."""
    # --- filters ---
    if request.args.get("unsigned") == "true":
        data = [s for s in data if (s.get("signed") is False or s.get("_signed") is False)]

    genre = request.args.get("genre")
    if genre:
        gl = genre.lower()
        data = [s for s in data if s.get("genre", "").lower() == gl]

    region = request.args.get("region") or request.args.get("country")
    if region:
        ru = region.upper()

        def in_region(s):
            for z in (s.get("regions") or s.get("trending_in") or ()):
                if z.upper() == ru:
                    return True
            return False

        data = [s for s in data if in_region(s)]

    q = request.args.get("q")
    if q:
        ql = q.lower()
        data = [s for s in data if ql in s.get("title", "").lower() or ql in s.get("artist", "").lower()]

    # --- sorting ---
    has_counts = any(s.get("tiktok_videos") is not None for s in data)
    if default_sort is None:
        default_sort = "tiktok_videos" if has_counts else "chart_rank"
    sort_by = request.args.get("sort", default_sort)
    sort_map = {
        "tiktok_videos": (lambda s: _int(s.get("tiktok_videos")), True),
        "shazam": (lambda s: _int(s.get("shazam_tags")), True),
        "listeners": (lambda s: _int(s.get("spotify_listeners")), True),
        "velocity": (lambda s: _int(s.get("velocity")), True),
        "growth": (lambda s: _int(s.get("stream_growth_7d")), True),
        "chart_rank": (lambda s: s.get("chart_rank", 9999), False),
        # spread: most countries first, then best rank as tiebreaker
        "spread": (lambda s: (s.get("countries_count", 1), -s.get("chart_rank", 9999)), True),
        # emerging: freshness x reach score (set by /tiktok-trends)
        "emerging": (lambda s: s.get("emerging_score", 0), True),
    }
    key, default_desc = sort_map.get(sort_by, sort_map.get(default_sort, sort_map["chart_rank"]))
    order = request.args.get("order")
    reverse = (order != "asc") if order else default_desc
    data = sorted(data, key=key, reverse=reverse)

    # --- pagination ---
    try:
        limit = min(int(request.args.get("limit", 50)), 200)
    except ValueError:
        limit = 50
    try:
        offset = int(request.args.get("offset", 0))
    except ValueError:
        offset = 0

    total = len(data)
    page = data[offset:offset + limit]
    return {
        "count": len(page),
        "total": total,
        "offset": offset,
        "sort": sort_by,
        "order": order,
        "data": page,
    }


@app.route("/sounds")
def sounds():
    """Top streamed/played sounds (Apple Music chart)."""
    ds = get_dataset(feed="most-played")
    result = _apply_query(ds.sounds)
    result["data"], premium = _maybe_enrich(result["data"])
    result["premium_enriched"] = premium
    if not premium:
        result["upgrade_note"] = ("Upgrade to Pro/Ultra to unlock real Spotify artist "
                                   "signals (followers, popularity, genres) on every track.")
    return jsonify(result)


@app.route("/tiktok-trends")
def tiktok_trends():
    """Recent sounds spreading fast across countries — emerging-hit radar.

    Combines two real signals from live chart data:
      - cross-country spread (how many markets it charts in)
      - freshness (how recently it was released)
    A song released days ago already charting in several countries is a far
    stronger 'breaking out' signal than an established hit sitting everywhere.
    Honest, computed on real data, no scraping. Exact TikTok counts on Pro plan.

    days_since_release / emerging_score are precomputed once per snapshot in
    providers.Dataset, so this endpoint does no date parsing per request.
    """
    ds = get_dataset(feed="most-played")

    min_countries = 2
    try:
        min_countries = max(1, int(request.args.get("min_countries", 2)))
    except ValueError:
        pass

    # copy only the sounds that survive the spread filter — the snapshot itself
    # is shared and must stay untouched
    spreading = []
    for s, extra in zip(ds.sounds, ds.derived):
        if s.get("countries_count", 1) >= min_countries:
            item = dict(s)
            item.update(extra)
            spreading.append(item)

    # --- diversity: max 2 tracks per artist so the radar isn't monopolized ---
    spreading.sort(key=lambda s: s["emerging_score"], reverse=True)
    per_artist = {}
    diversified = []
    for s in spreading:
        a = s.get("artist", "").lower()
        n = per_artist.get(a, 0) + 1
        per_artist[a] = n
        if n <= 2:
            diversified.append(s)

    # standard filters (genre/country/q), keep emerging-score order
    result = _apply_query(diversified, default_sort="emerging")
    result["feed"] = "tiktok-trends (emerging: fresh + spreading)"
    result["note"] = ("Recent sounds spreading across {}+ countries, ranked by "
                      "freshness x reach. Max 2 tracks per artist. "
                      "Exact TikTok metrics on Pro plan.".format(min_countries))
    result["data"], premium = _maybe_enrich(result["data"])
    result["premium_enriched"] = premium
    return jsonify(result)


@app.route("/sounds/<sound_id>")
def sound_detail(sound_id):
    ds = get_dataset()
    match = ds.index.get(sound_id.lower())   # O(1) instead of scanning every sound
    if match is None:
        return jsonify({"error": "sound not found", "id": sound_id}), 404
    enriched, premium = _maybe_enrich([match])
    result = dict(enriched[0])               # copy: never mutate the shared snapshot
    result["premium_enriched"] = premium
    return jsonify(result)


@app.route("/unsigned")
def unsigned():
    data = get_sounds()
    # rich schema: real signed flag. chart schema: status unknown (premium feature)
    enriched = [s for s in data if s.get("signed") is not None]
    if not enriched:
        return jsonify({
            "count": 0,
            "data": [],
            "note": "Unsigned-artist detection requires the Pro plan (licensed enrichment). "
                    "The free chart plan does not expose label/signing status.",
        })
    result = [s for s in enriched if s.get("signed") is False]
    result = sorted(result, key=lambda s: _int(s.get("tiktok_videos")), reverse=True)
    return jsonify({"count": len(result), "data": result})


@app.route("/stats")
def stats():
    """Aggregates are computed once per snapshot, not once per request."""
    ds = get_dataset()
    payload = dict(ds.stats)
    payload["data_source"] = get_provider().name
    return jsonify(payload)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
