"""Physics road-only Lden for the AU SoundPLAN calibration points.

Why this exists: production blends an EU transfer RF with a physics model, but
the blend arithmetic cancels the physics road term out almost entirely --
measured 2026-07-26, removing ALL 15 dB of building screening moves the score by
0 dB on 5 of 8 test points and at most 0.5 dB on the rest. So today the physics
path computes barrier diffraction for every source and that work reaches the
customer only as a displayed number, never as an input to the score.

Before redesigning the blend we need to know whether physics carries signal the
RF does not. The RF sees ring counts and building DENSITY; it structurally
cannot see direction, line-of-sight screening, or measured AADT. If those carry
independent information, a fitted blend (or physics-as-a-feature) should beat
the RF alone on the same in-city 5-fold gate that scores 0.696 / 3.798 today.

ROAD-ONLY on purpose: the SoundPLAN target is sp_rd_max_{d,e,n}, i.e. road
noise. Comparing it against a physics total that includes rail and aircraft
would be measuring the wrong thing.

Writes data/au_physics_cache.npz aligned index-for-index with the AU block of
data/eu/transfer5_cache.npz (same points, same order -- asserted).

Run: NOISE_TRANSFER=0 .venv/bin/python scripts/build_physics_feature_cache.py
"""
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

OUT = "data/au_physics_cache.npz"


def main():
    # Physics path only: we want the physics road term, not a transfer result.
    os.environ["NOISE_TRANSFER"] = "0"
    from poc_eu_transfer6_geodist import load_au_points
    from property_scores.noise import score as ns

    # The 24 h sqlite result cache is keyed on lat/lng and shared with every
    # other engine copy pointed at this DATA_DIR. Bypass it so these are
    # genuinely computed now.
    ns._cache_get = lambda *a, **k: None
    ns._cache_put = lambda *a, **k: None

    pts = load_au_points()
    old = np.load("data/eu/transfer5_cache.npz", allow_pickle=True)
    assert len(pts) == len(old["yau"]), (
        f"point set drifted: {len(pts)} here vs {len(old['yau'])} in the cache")
    assert np.allclose([p[3] for p in pts], old["yau"]), "targets drifted"

    rd_lden = np.zeros(len(pts))
    road_db = np.zeros(len(pts))
    screening = np.zeros(len(pts))
    n_roads = np.zeros(len(pts))
    ok = np.zeros(len(pts), bool)

    t0 = time.time()
    for i, (city, lat, lng, _tgt) in enumerate(pts):
        try:
            r = ns.noise_score(lat, lng)
        except Exception:
            continue
        rdb = r.get("road_db") or 0.0
        road_db[i] = rdb
        screening[i] = r.get("max_building_screening_db") or 0.0
        n_roads[i] = r.get("road_count") or 0
        if rdb > 0:
            leq = rdb - ns.L10_TO_LEQ_DB
            rd_lden[i] = ns._lden(leq + ns._DAY_ADJ, leq + ns._EVE_ADJ,
                                  leq + ns._NIGHT_ADJ)
        ok[i] = True
        if (i + 1) % 250 == 0:
            el = time.time() - t0
            print(f"  {i+1}/{len(pts)} ({el:.0f}s, eta {el/(i+1)*(len(pts)-i-1):.0f}s)",
                  flush=True)

    np.savez(OUT, rd_lden=rd_lden, road_db=road_db, screening=screening,
             n_roads=n_roads, ok=ok,
             city=np.array([p[0] for p in pts]),
             lat=np.array([p[1] for p in pts]),
             lng=np.array([p[2] for p in pts]),
             y=np.array([p[3] for p in pts]))
    print(f"\nsaved {OUT}: {int(ok.sum())}/{len(pts)} computed, "
          f"{int((rd_lden > 0).sum())} with a road term ({time.time()-t0:.0f}s)")


if __name__ == "__main__":
    raise SystemExit(main())
