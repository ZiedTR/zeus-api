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
        "service": "ZEUS API",
        "description": "Objective cross-platform signals for TikTok sounds (A&R radar)",
        "data_source": get_provider().name,
        "endpoints": [
            "GET /sounds            — list sounds (filters + sorting)",
            "GET /sounds/<isrc>     — single sound by ISRC",
            "GET /unsigned          — unsigned artists only",
            "GET /stats             — aggregate counters",
        ],
        "version": "0.1.0",
    })


@app.route("/health")
def health():
    return jsonify({"status": "ok", "source": get_provider().name})


# ---------------------------------------------------------------------------
# Core: list sounds with objective filters and sorting.
# No subjective scoring — raw signals only.
# ---------------------------------------------------------------------------
@app.route("/sounds")
def sounds():
    data = get_sounds()

    # --- filters (all objective) ---
    if request.args.get("unsigned") == "true":
        data = [s for s in data if s.get("signed") is False]

    genre = request.args.get("genre")
    if genre:
        data = [s for s in data if s.get("genre", "").lower() == genre.lower()]

    region = request.args.get("region")
    if region:
        data = [s for s in data if region.upper() in [r.upper() for r in s.get("regions", [])]]

    q = request.args.get("q")
    if q:
        ql = q.lower()
        data = [s for s in data if ql in s.get("title", "").lower() or ql in s.get("artist", "").lower()]

    # --- sorting ---
    # default depends on source: chart data sorts by rank, rich data by TikTok volume
    has_counts = any(s.get("tiktok_videos") is not None for s in data)
    default_sort = "tiktok_videos" if has_counts else "chart_rank"
    sort_by = request.args.get("sort", default_sort)
    sort_map = {
        "tiktok_videos": (lambda s: _int(s.get("tiktok_videos")), True),
        "shazam": (lambda s: _int(s.get("shazam_tags")), True),
        "listeners": (lambda s: _int(s.get("spotify_listeners")), True),
        "velocity": (lambda s: _int(s.get("velocity")), True),
        "growth": (lambda s: _int(s.get("stream_growth_7d")), True),
        "chart_rank": (lambda s: s.get("chart_rank", 9999), False),  # ascending = best rank first
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

    return jsonify({
        "count": len(page),
        "total": total,
        "offset": offset,
        "sort": sort_by,
        "order": order,
        "data": page,
    })


@app.route("/sounds/<isrc>")
def sound_detail(isrc):
    for s in get_sounds():
        if s["isrc"].lower() == isrc.lower():
            return jsonify(s)
    return jsonify({"error": "sound not found", "isrc": isrc}), 404


@app.route("/unsigned")
def unsigned():
    data = [s for s in get_sounds() if s.get("signed") is False]
    data = sorted(data, key=lambda s: _int(s.get("tiktok_videos")) or -s.get("chart_rank", 9999), reverse=True)
    return jsonify({"count": len(data), "data": data})


@app.route("/stats")
def stats():
    data = get_sounds()
    unsigned_count = sum(1 for s in data if s.get("signed") is False)
    signed_count = sum(1 for s in data if s.get("signed") is True)
    total_videos = sum(_int(s.get("tiktok_videos")) for s in data)
    risers = [s for s in data if s.get("velocity")]
    fastest = max(risers, key=lambda s: _int(s.get("velocity")), default=None)
    return jsonify({
        "sounds_tracked": len(data),
        "unsigned": unsigned_count,
        "signed": signed_count,
        "unknown_signed_status": len(data) - unsigned_count - signed_count,
        "total_tiktok_videos": total_videos,
        "fastest_riser": {
            "title": fastest["title"],
            "artist": fastest["artist"],
            "velocity": fastest["velocity"],
        } if fastest else None,
        "data_source": get_provider().name,
    })


if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
