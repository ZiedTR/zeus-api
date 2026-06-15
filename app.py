"""
ZEUS API — A&R signal radar.
Serves raw, objective cross-platform signals for TikTok sounds.
ZEUS surfaces and sorts the data; the A&R team makes the call.

Stack: Python / Flask. Deploy on Render. Distribute on RapidAPI.
Data comes from providers.get_sounds() — simulated until real sources are connected.
"""

from flask import Flask, jsonify, request
from providers import get_sounds, get_provider

app = Flask(__name__)


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


# ---------------------------------------------------------------------------
# Core: list sounds with objective filters and sorting.
# No subjective scoring — raw signals only.
# ---------------------------------------------------------------------------
def _apply_query(data):
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
    default_sort = "tiktok_videos" if has_counts else "chart_rank"
    sort_by = request.args.get("sort", default_sort)
    sort_map = {
        "tiktok_videos": (lambda s: _int(s.get("tiktok_videos")), True),
        "shazam": (lambda s: _int(s.get("shazam_tags")), True),
        "listeners": (lambda s: _int(s.get("spotify_listeners")), True),
        "velocity": (lambda s: _int(s.get("velocity")), True),
        "growth": (lambda s: _int(s.get("stream_growth_7d")), True),
        "chart_rank": (lambda s: s.get("chart_rank", 9999), False),
    }
    key, default_desc = sort_map.get(sort_by, sort_map[default_sort])
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
    return jsonify(_apply_query(get_sounds(feed="most-played")))


@app.route("/tiktok-trends")
def tiktok_trends():
    """Sounds going viral on social platforms incl. TikTok (Shazam viral chart).

    Source: Apple's official Shazam-driven viral feed — public, legal, no scraping.
    This captures sounds breaking out on TikTok and other social platforms,
    rather than the all-time most-streamed tracks.
    """
    result = _apply_query(get_sounds(feed="viral"))
    result["feed"] = "tiktok-trends (Shazam viral / social)"
    return jsonify(result)


@app.route("/sounds/<sound_id>")
def sound_detail(sound_id):
    for s in get_sounds():
        if (s.get("isrc") or "").lower() == sound_id.lower() or str(s.get("id", "")).lower() == sound_id.lower():
            return jsonify(s)
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
