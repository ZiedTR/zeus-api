"""
ZEUS API — A&R signal radar.
Serves raw, objective cross-platform signals for TikTok sounds.
ZEUS surfaces and sorts the data; the A&R team makes the call.

Stack: Python / Flask. Deploy on Render. Distribute on RapidAPI.
Data comes from providers.get_sounds() — simulated until real sources are connected.
"""

import hmac
import os

from flask import Flask, jsonify, request

import enrichment
from providers import get_sounds, get_provider

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
        "version": "1.0.0",
    })


@app.route("/health")
def health():
    return jsonify({"status": "ok", "source": get_provider().name})


@app.route("/debug-headers")
def debug_headers():
    """Temporary: echoes incoming request headers, to see what each
    marketplace (RapidAPI, Zyla, ...) actually forwards to the backend —
    e.g. whether Zyla sends any subscription/plan header we could gate on.
    Remove once that's confirmed."""
    return jsonify({k: v for k, v in request.headers.items()})


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
        data = [s for s in data if s.get("genre", "").lower() == genre.lower()]

    region = request.args.get("region") or request.args.get("country")
    if region:
        def in_region(s):
            zones = s.get("regions") or s.get("trending_in") or []
            return region.upper() in [z.upper() for z in zones]
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
    result = _apply_query(get_sounds(feed="most-played"))
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
    """
    from datetime import datetime

    data = get_sounds(feed="most-played")

    min_countries = 2
    try:
        min_countries = max(1, int(request.args.get("min_countries", 2)))
    except ValueError:
        pass
    spreading = [s for s in data if s.get("countries_count", 1) >= min_countries]

    # --- freshness score: newer releases score higher ---
    today = datetime.utcnow().date()
    def days_old(s):
        rd = s.get("release_date", "")
        try:
            d = datetime.strptime(rd[:10], "%Y-%m-%d").date()
            return max(0, (today - d).days)
        except (ValueError, TypeError):
            return 9999  # unknown date -> treat as old

    def emerging_score(s):
        spread = s.get("countries_count", 1)
        age = days_old(s)
        # freshness multiplier: <=7d strongest, decays to ~1 after ~90 days
        if age <= 7:
            fresh = 3.0
        elif age <= 30:
            fresh = 2.0
        elif age <= 90:
            fresh = 1.3
        else:
            fresh = 1.0
        return spread * fresh

    for s in spreading:
        s["days_since_release"] = days_old(s)
        s["emerging_score"] = round(emerging_score(s), 1)

    # --- diversity: max 2 tracks per artist so the radar isn't monopolized ---
    spreading.sort(key=lambda s: s["emerging_score"], reverse=True)
    per_artist = {}
    diversified = []
    for s in spreading:
        a = s.get("artist", "").lower()
        per_artist[a] = per_artist.get(a, 0) + 1
        if per_artist[a] <= 2:
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
    for s in get_sounds():
        if (s.get("isrc") or "").lower() == sound_id.lower() or str(s.get("id", "")).lower() == sound_id.lower():
            enriched, premium = _maybe_enrich([s])
            result = enriched[0]
            result["premium_enriched"] = premium
            return jsonify(result)
    return jsonify({"error": "sound not found", "id": sound_id}), 404


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
    data = get_sounds()
    countries = set()
    genres = {}
    for s in data:
        for c in (s.get("trending_in") or s.get("regions") or []):
            countries.add(c)
        g = s.get("genre", "Unknown")
        genres[g] = genres.get(g, 0) + 1
    top_genres = sorted(genres.items(), key=lambda x: x[1], reverse=True)[:5]
    return jsonify({
        "sounds_tracked": len(data),
        "countries_covered": sorted(countries),
        "top_genres": [{"genre": g, "count": n} for g, n in top_genres],
        "data_source": get_provider().name,
        "updated": data[0].get("detected") if data else None,
    })


if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
