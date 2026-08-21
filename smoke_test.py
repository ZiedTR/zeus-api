#!/usr/bin/env python3
"""
ZEUS — contract smoke test.

Run it against the LIVE API before deploying, then again after. It compares the
*contract* (status codes, top-level keys, per-item field names and types,
ordering, pagination invariants) rather than the values, so it does not raise
false alarms when Apple's chart simply changed between the two runs.

    python3 smoke_test.py --base https://<your-app>.onrender.com --save before.json
    # ... deploy ...
    python3 smoke_test.py --base https://<your-app>.onrender.com --check before.json

Exit code 0 = contract unchanged. 1 = something a client could notice.
Only dependency: requests.
"""
import argparse, json, statistics, sys, time
import requests

CASES = [
    ("root",            "/"),
    ("health",          "/health"),
    ("stats",           "/stats"),
    ("sounds",          "/sounds"),
    ("sounds_limit5",   "/sounds?limit=5"),
    ("sounds_limit200", "/sounds?limit=200"),
    ("sounds_offset",   "/sounds?offset=10&limit=7"),
    ("sounds_genre",    "/sounds?genre=Pop"),
    ("sounds_q",        "/sounds?q=a"),
    ("sounds_country",  "/sounds?country=FR"),
    ("sounds_spread",   "/sounds?sort=spread"),
    ("sounds_asc",      "/sounds?sort=chart_rank&order=asc"),
    ("sounds_badsort",  "/sounds?sort=bogus"),
    ("sounds_badpage",  "/sounds?limit=abc&offset=xyz"),
    ("sounds_unsigned", "/sounds?unsigned=true"),
    ("trends",          "/tiktok-trends"),
    ("trends_min1",     "/tiktok-trends?min_countries=1"),
    ("trends_min4",     "/tiktok-trends?min_countries=4"),
    ("unsigned",        "/unsigned"),
    ("notfound",        "/sounds/definitely-not-a-real-id"),
]

SAMPLES = 3


def shape(v, depth=0):
    """Type skeleton of a value: names and types, never values."""
    if depth > 4:
        return "..."
    if isinstance(v, dict):
        return {k: shape(v[k], depth + 1) for k in sorted(v)}
    if isinstance(v, list):
        return ["<empty>"] if not v else [shape(v[0], depth + 1)]
    if v is None:
        return "null"
    return type(v).__name__


def probe(base, path):
    lats = []
    r = None
    for _ in range(SAMPLES):
        t = time.perf_counter()
        r = requests.get(base + path, headers={"Accept-Encoding": "gzip"}, timeout=60)
        lats.append((time.perf_counter() - t) * 1000)
    try:
        body = r.json()
    except Exception:
        body = {"__not_json__": r.text[:200]}
    return {
        "status": r.status_code,
        "shape": shape(body),
        "wire_bytes": int(r.headers.get("Content-Length") or len(r.content)),
        "encoding": r.headers.get("Content-Encoding", "identity"),
        "etag": bool(r.headers.get("ETag")),
        "latency_ms": round(statistics.median(lats), 1),
        "_body": body,
    }


def invariants(name, path, res):
    """Things that must hold no matter what the chart says today."""
    out = []
    b = res["_body"]
    if not isinstance(b, dict):
        return out
    if "data" in b and isinstance(b["data"], list):
        if "count" in b and b["count"] != len(b["data"]):
            out.append("%s: count=%s mais %d elements" % (name, b["count"], len(b["data"])))
        if "total" in b and isinstance(b["total"], int) and b["total"] < len(b["data"]):
            out.append("%s: total=%s < elements=%d" % (name, b["total"], len(b["data"])))
        if "limit=5" in path and len(b["data"]) > 5:
            out.append("%s: limit=5 ignore (%d elements)" % (name, len(b["data"])))
        if path == "/sounds" and len(b["data"]) > 1:
            ranks = [s.get("chart_rank") for s in b["data"] if isinstance(s.get("chart_rank"), int)]
            if ranks and ranks != sorted(ranks):
                out.append("%s: tri par defaut chart_rank non croissant" % name)
        if b.get("premium_enriched") is True:
            out.append("%s: premium_enriched=true sans abonnement -> gating casse" % name)
    return out


def conditional_check(base):
    """A repeat request with If-None-Match must not return a different body."""
    r1 = requests.get(base + "/sounds?limit=50", headers={"Accept-Encoding": "gzip"}, timeout=60)
    et = r1.headers.get("ETag")
    if not et:
        return "pas d'ETag (normal avant le patch)", True
    r2 = requests.get(base + "/sounds?limit=50",
                      headers={"Accept-Encoding": "gzip", "If-None-Match": et}, timeout=60)
    if r2.status_code == 304:
        return "304 correct (0 octet renvoye)", True
    if r2.status_code == 200 and r2.json() == r1.json():
        return "200 avec corps identique (acceptable)", True
    return "REPONSE INCOHERENTE: HTTP %d" % r2.status_code, False


def run(base):
    snap, problems = {}, []
    for name, path in CASES:
        try:
            res = probe(base, path)
        except Exception as e:
            problems.append("%s: requete impossible (%s)" % (name, e))
            continue
        problems += invariants(name, path, res)
        res.pop("_body")
        snap[name] = res
    msg, ok = conditional_check(base)
    snap["__conditional__"] = msg
    if not ok:
        problems.append("requete conditionnelle: " + msg)
    return snap, problems


def compare(before, after):
    diffs = []
    for name in sorted(set(before) | set(after)):
        if name.startswith("__"):
            continue
        b, a = before.get(name), after.get(name)
        if b is None:
            diffs.append(("NOUVEAU", name, "", "endpoint absent avant"))
            continue
        if a is None:
            diffs.append(("SUPPRIME", name, "", "endpoint absent apres"))
            continue
        if b["status"] != a["status"]:
            diffs.append(("CASSE", name, "HTTP", "%s -> %s" % (b["status"], a["status"])))
        if b["shape"] != a["shape"]:
            diffs.append(("CASSE", name, "schema",
                          "champs/types differents (voir --dump)"))
    return diffs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True, help="ex: https://zeus.onrender.com")
    ap.add_argument("--save", help="ecrire la reference dans ce fichier")
    ap.add_argument("--check", help="comparer a cette reference")
    ap.add_argument("--dump", action="store_true", help="afficher les schemas complets")
    args = ap.parse_args()
    base = args.base.rstrip("/")

    snap, problems = run(base)

    print("== %s ==" % base)
    for name, res in snap.items():
        if name.startswith("__"):
            continue
        print("  %-18s HTTP %s  %6d o (%s)  %7.1f ms%s"
              % (name, res["status"], res["wire_bytes"], res["encoding"],
                 res["latency_ms"], "  ETag" if res["etag"] else ""))
    print("  requete conditionnelle : %s" % snap["__conditional__"])
    tot = sum(r["wire_bytes"] for n, r in snap.items() if not n.startswith("__"))
    print("  total octets sur le fil : %d" % tot)

    if problems:
        print("\nINVARIANTS VIOLES :")
        for p in problems:
            print("  - " + p)

    if args.save:
        json.dump(snap, open(args.save, "w"), indent=1, sort_keys=True)
        print("\nreference ecrite dans %s" % args.save)

    failed = bool(problems)
    if args.check:
        before = json.load(open(args.check))
        diffs = compare(before, snap)
        print("\n== comparaison avec %s ==" % args.check)
        if not diffs:
            print("  contrat identique sur %d endpoints" % (len(snap) - 1))
        for kind, name, field, detail in diffs:
            print("  [%s] %-18s %-8s %s" % (kind, name, field, detail))
            if kind == "CASSE":
                failed = True
        if args.dump:
            for kind, name, field, _ in diffs:
                if field == "schema":
                    print("\n--- %s AVANT ---\n%s" % (name, json.dumps(before[name]["shape"], indent=1)))
                    print("--- %s APRES ---\n%s" % (name, json.dumps(snap[name]["shape"], indent=1)))
        print("\n  latences (mediane, ms) :")
        for name in sorted(snap):
            if name.startswith("__") or name not in before:
                continue
            print("    %-18s %7.1f -> %7.1f" % (name, before[name]["latency_ms"], snap[name]["latency_ms"]))

    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
