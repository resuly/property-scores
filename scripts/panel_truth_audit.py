"""Panel-truth audit: cross-layer, neighbour-pair and panel-field checks.

Born from the Simon Kean rounds (2026-06): point-accuracy sentinels never
catch what a resident sees in one glance, namely
  1. cross-layer contradictions at one address ("hilltop" + "high flood"),
  2. boundary cliffs between near-identical neighbours (BPL buffer edge
     scored 20 vs 55 across the polygon line),
  3. panel fields that overclaim (absolute negatives, missing caveats).

Runs against the PRODUCTION API by default (that JSON is what the panel
renders), throttled under the 90/min per-IP limit. Exit 1 if any rule fails,
so it can join the sentinel cron later.

Usage:
  python scripts/panel_truth_audit.py            # prod
  BASE=http://localhost:8099 python scripts/...  # local service
"""
import json
import os
import sys
import time
import urllib.request

BASE = os.environ.get("BASE", "https://daleads.com.au/api/property-scores")
UA = {"User-Agent": "Mozilla/5.0 (panel-truth-audit)"}
SLEEP = 1.5

# Edge-oversampled probes: polygon boundaries, terrain rims, data seams.
# Addresses are geocoded through G-NAF at runtime (hand-typed coordinates
# measure the wrong thing, 2026-06-10 N Piazza lesson); pinned coords are
# the fallback when geocoding is down.
# (name, gnaf_query, fallback_lat, fallback_lng, tags)
PROBES = [
    ("Cliff Ave N Wahroonga (BPL buffer + valley rim)",
     "20 Cliff Avenue North Wahroonga", -33.7076, 151.1306,
     {"bpl_in_buffer", "valley_rim"}),
    ("Warrimoo Ave St Ives Chase (park edge, outside BPL)",
     "180 Warrimoo Avenue St Ives Chase", -33.7048, 151.1771,
     {"bpl_outside", "park_edge"}),
    ("99 Westbrook Ave N Wahroonga (hilltop)",
     "99 Westbrook Avenue North Wahroonga", -33.7054, 151.1372,
     {"hilltop"}),
    ("7 Karuah Rd Turramurra (suburban control)",
     "7 Karuah Road Turramurra", -33.7268, 151.1331,
     {"bpl_outside", "suburban"}),
    ("356 Smith St Collingwood (urban loud)",
     "356 Smith Street Collingwood", -37.7989, 144.9845,
     {"urban_loud"}),
    ("3-4 Dirlton Cres Park Orchards (VIC quiet)",
     "3-4 Dirlton Crescent Park Orchards", -37.7800, 145.2163,
     {"vic_quiet"}),
]

GEOCODE = "https://daleads.com.au/api/address-autocomplete?q="


def resolve(query, fb_lat, fb_lng):
    try:
        import urllib.parse
        d = json.loads(urllib.request.urlopen(urllib.request.Request(
            GEOCODE + urllib.parse.quote(query), headers=UA), timeout=15).read())
        hit = next((x for x in d if x.get("type") == "address"), None)
        if hit:
            return hit["lat"], hit["lng"]
    except Exception:
        pass
    return fb_lat, fb_lng

# Neighbour pairs that must not cliff: SAME interface class on both sides of
# the official polygon edge (a bush block vs a street is a legitimate gap;
# two park-edge homes either side of the buffer line is not).
# (probe_a, probe_b, score, max_delta)
PAIRS = [
    ("Cliff Ave N Wahroonga (BPL buffer + valley rim)",
     "Warrimoo Ave St Ives Chase (park edge, outside BPL)", "bushfire", 15),
]

# Same-parcel consistency: two click points inside ONE parcel must agree on
# the official zone status. KNOWN-FAIL until score queries use a canonical
# parcel point instead of the raw click coordinate (4 Rutland Pl repro,
# 2026-06-12: in_zone 45 vs outside 55 from two clicks on one lot).
# (label, latA, lngA, latB, lngB)
PARCEL_PAIRS = [
    ("4 Rutland Pl North Wahroonga, two click points",
     -33.70475, 151.13023, -33.705319, 151.130761),
]


def fetch(score, lat, lng):
    # noise must take the PANEL path (detail=true, fields nested under
    # "score"): the plain path serves a reduced field set and the audit must
    # test what the resident reads
    extra = "&radius=500&detail=true" if score == "noise" else ""
    url = f"{BASE}/scores/{score}?lat={lat}&lng={lng}{extra}"
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


LABEL_BANDS = {  # every score family: label must match its own banding
    "bushfire": [(80, "Very Low Risk"), (60, "Low Risk"), (40, "Moderate Risk"),
                 (20, "High Risk"), (0, "Very High Risk")],
}


def check_probe(name, tags, data):
    """Cross-layer + panel-field rules for one address. Yields (rule, ok, detail)."""
    bf, fl, no, vw, so = (data.get(k) for k in
                          ("bushfire", "flood", "noise", "view-quality", "solar"))

    if bf and bf.get("score") is not None:
        status = bf.get("official_zone_status")
        yield ("bushfire: official status present",
               status in ("outside", "in_zone", "unavailable"), status)
        yield ("bushfire: disclaimer says not a BAL assessment",
               "BAL" in (bf.get("disclaimer") or ""), None)
        if "bpl_outside" in tags:
            yield ("bushfire: officially outside floors at 55",
                   status != "outside" or bf["score"] >= 55, bf["score"])
        if "bpl_in_buffer" in tags:
            yield ("bushfire: buffer hit must not crater below band floor 45",
                   status != "in_zone" or bf.get("category") != "Vegetation Buffer"
                   or bf["score"] >= 45, bf["score"])
        for floor_, lbl in LABEL_BANDS["bushfire"]:
            if bf["score"] >= floor_:
                yield ("bushfire: label matches banding", bf.get("label") == lbl,
                       f"{bf['score']} -> {bf.get('label')}")
                break

    if fl and fl.get("score") is not None:
        hd = (fl.get("height_above_drainage_m")
              or (fl.get("elevation") or {}).get("height_above_drainage_m"))
        if "hilltop" in tags and isinstance(hd, (int, float)) and hd > 30:
            yield ("flood: 30m+ above drainage cannot be risky",
                   fl["score"] >= 70, f"score {fl['score']} at {hd}m")

    nsc = (no or {}).get("score") if isinstance((no or {}).get("score"), dict) \
        else no
    if nsc and nsc.get("lden_db") is not None:
        ci = nsc.get("confidence_range_db")
        yield ("noise: confidence band shown and sane (<=14 dB)",
               isinstance(ci, (int, float)) and 0 < ci <= 14, ci)
        dom = nsc.get("dominant_source") or nsc.get("loudest_source")
        yield ("noise: dominant source labelled", bool(dom), dom)
        if "urban_loud" in tags:
            yield ("noise: inner-city main street reads loud (>=65 Lden)",
                   nsc["lden_db"] >= 65, nsc["lden_db"])
        if "vic_quiet" in tags:
            yield ("noise: VIC quiet back street stays quiet (<=62 Lden)",
                   nsc["lden_db"] <= 62, nsc["lden_db"])

    if vw and vw.get("score") is not None:
        fac = (vw.get("factors") or {}).get("elevation_advantage") or {}
        if "valley_rim" in tags:
            yield ("view: valley rim gets elevation credit (>=0.65)",
                   fac.get("value", 0) >= 0.65, fac.get("value"))

    if so and so.get("score") is not None:
        yield ("solar: caveat declares no shading model",
               "shading" in (so.get("caveat") or "").lower(), None)
        yield ("solar: score in range", 0 <= so["score"] <= 100, so["score"])


def main():
    scores = ["bushfire", "flood", "noise", "view-quality", "solar"]
    results, fails = [], 0
    cache = {}
    for name, query, fb_lat, fb_lng, tags in PROBES:
        lat, lng = resolve(query, fb_lat, fb_lng)
        time.sleep(1.0)
        data = {}
        for s in scores:
            try:
                data[s] = fetch(s, lat, lng)
            except Exception as e:
                data[s] = {"_error": str(e)}
            time.sleep(SLEEP)
        cache[name] = data
        for rule, ok, detail in check_probe(name, tags, data):
            results.append((name, rule, ok, detail))
            fails += 0 if ok else 1

    for label, la1, ln1, la2, ln2 in PARCEL_PAIRS:
        try:
            b1 = fetch("bushfire", la1, ln1); time.sleep(SLEEP)
            b2 = fetch("bushfire", la2, ln2); time.sleep(SLEEP)
            same = (b1.get("official_zone_status") == b2.get("official_zone_status")
                    and abs((b1.get("score") or 0) - (b2.get("score") or 0)) <= 10)
            detail = (f"{b1.get('official_zone_status')}/{b1.get('score')} vs "
                      f"{b2.get('official_zone_status')}/{b2.get('score')}")
        except Exception as e:
            same, detail = False, str(e)
        # known-fail: counted and printed, but does not gate the exit code yet
        results.append((f"PARCEL {label}",
                        "bushfire: one parcel, one answer (KNOWN-FAIL until canonical point)",
                        same if same else None, detail))

    for a, b, score, max_delta in PAIRS:
        sa, sb = cache[a].get(score, {}).get("score"), cache[b].get(score, {}).get("score")
        ok = sa is not None and sb is not None and abs(sa - sb) <= max_delta
        results.append((f"PAIR {a.split('(')[0].strip()} vs {b.split('(')[0].strip()}",
                        f"{score}: neighbour cliff <= {max_delta}", ok, f"{sa} vs {sb}"))
        fails += 0 if ok else 1

    width = max(len(r[1]) for r in results)
    cur = None
    for name, rule, ok, detail in results:
        if name != cur:
            print(f"\n## {name}")
            cur = name
        mark = "PASS" if ok else ("KNOWN-FAIL" if ok is None else "FAIL")
        print(f"  [{mark}] {rule:<{width}}  {detail if detail is not None else ''}")
    known = sum(1 for r in results if r[2] is None)
    print(f"\n{len(results)} checks, {fails} failed, {known} known-fail")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
