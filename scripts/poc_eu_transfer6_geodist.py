"""POC v6: poc_eu_transfer5 features recomputed with latitude-correct geometry.

Identical point sets, identical targets, identical feature keys and order as
transfer5_cache.npz -- the ONLY difference is how ground distance and the search
windows are computed, so `CACHE=... scripts/calib_eval.py` compares like with
like.

What changes (three instances of one bug: degrees treated as isotropic):

  1. Road distance. v5 did `ST_Distance(geom, pt) * 111320*cos(lat)`, which
     applies the LONGITUDE scale to a mixed-axis degree distance and so
     understates every north-south offset by cos(lat). v6 projects each axis of
     ST_ClosestPoint separately, the form nfdh_near/gtfs_rail_near already use.
  2. Search windows. v5 used fixed degree boxes (roads +/-0.013, POI
     +/-0.006/0.0045), so the ground area swept varied with latitude: the
     "1000 m" road window reached 887 m east-west at NL against 1413 m at
     Darwin. v6 sizes each axis from the metre radius, making the window
     latitude-invariant and an exact superset of the metric filter.
  3. Raster windows. `raster_sample.window_stats` squashes an equal-degree
     window east-west by cos(lat) unless `cos_correct=True` (its docstring
     names this exact situation). v6 passes True.

Buildings and POI DISTANCES were already per-axis in v5 and are unchanged here;
only their bbox is resized. Net effect measured on the features themselves:
road counts fall ~40% at NL/UK against ~2% at Darwin, which is the train/apply
ruler mismatch this run exists to test.

Run: .venv/bin/python scripts/poc_eu_transfer6_geodist.py
Then: CACHE=data/eu/transfer6_geodist_cache.npz MIN_LDEN=30 \
      .venv/bin/python scripts/calib_eval.py
"""
import csv
import math
import os
import re
import sys
import time

import duckdb
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from poc_eu_transfer5 import CLASSES, RINGS, LC_CLASSES, AU_CITIES, DEM, LC, fkeys, lden  # noqa: E402
from property_scores.noise import raster_sample as rs  # noqa: E402

M_LAT = 111_320.0

# SCALE is an EXPLORATORY knob, default 1.0 (a faithful correction of v5).
# Correcting the ruler shrinks every ring on the ground -- at NL a "500 m" ring
# was really an ellipse reaching 815 m north-south, so honest metres drop ~40%
# of the roads it used to count. SCALE>1 enlarges every ring uniformly (staying
# latitude-invariant) to test whether the gate loss is about ring SIZE rather
# than the ruler. Selection over SCALE would be gate-fitting, so anything but
# 1.0 is exploratory only.
SCALE = float(os.environ.get("SCALE", "1.0"))
CACHE = os.environ.get(
    "CACHE_OUT",
    ("data/eu/transfer6_rings_" + os.environ["RINGS_M"].replace(",", "_") + "_cache.npz")
    if os.environ.get("RINGS_M") else
    ("data/eu/transfer6_geodist_cache.npz" if SCALE == 1.0
     else f"data/eu/transfer6_geodist_s{SCALE:g}_cache.npz"))

# Ring KEYS keep v5's names (the cache must stay column-compatible with
# fkeys()); only the metre THRESHOLDS they stand for change.
#
# RINGS_M overrides the thresholds outright (comma-separated, 5 values mapped
# positionally onto the v5 ring names). The v5 set 50/100/200/400/800 was picked
# once in the POC and never tuned, yet a crude uniform x1.3 moved the A/B gate as
# much as the entire geodistance correction -- so it is worth searching. Anything
# selected THIS way is chosen on the gate, i.e. gate-fitted, and must be
# re-validated on held-out cities before it means anything.
_rings_env = os.environ.get("RINGS_M")
if _rings_env:
    _vals = [float(x) for x in _rings_env.split(",")]
    assert len(_vals) == len(RINGS), f"need {len(RINGS)} thresholds, got {len(_vals)}"
    RING_THRESH = dict(zip(RINGS, _vals))
else:
    RING_THRESH = {r: r * SCALE for r in RINGS}
ROAD_R = 1000.0 * SCALE   # metres, matches v5's `d < 1000` at SCALE=1
BLDG_R = 200.0 * SCALE    # largest building ring
POI_R = 500.0 * SCALE     # largest POI ring


def _window(lat: float, radius_m: float) -> tuple[float, float]:
    """Half-width in degrees (lng, lat) of the smallest box containing a metre
    radius at this latitude. Latitude-invariant on the ground, unlike a fixed
    degree box."""
    mpd = M_LAT * math.cos(math.radians(lat))
    return radius_m / mpd, radius_m / M_LAT


DIRECTIONAL = os.environ.get("DIRECTIONAL", "0") == "1"

# Rotation-invariant directional keys. Raw per-compass-quadrant values would
# force the tree to learn that "loud road to the north" and "loud road to the
# south" are the same thing, which it cannot do from this much data. Sorting the
# quadrants instead encodes the ASYMMETRY, which is the acoustically meaningful
# part: one big road on a single side is a very different place from the same
# road count spread evenly around you.
DIR_KEYS = ([f"q_major_near_{i}" for i in range(4)]
            + [f"q_major_n200_{i}" for i in range(4)]
            + ["q_near_spread"])


def fkeys_v2():
    return sorted(fkeys() + DIR_KEYS) if DIRECTIONAL else fkeys()


def _directional(rows_xy, lat, lng, mpd):
    """Per-quadrant major-road geometry, sorted so it carries no compass bias."""
    near = [1000.0] * 4
    cnt = [0] * 4
    for cls, d, cx, cy in rows_xy:
        if cls not in ("motorway", "trunk", "primary", "secondary", "tertiary"):
            continue
        dx = (cx - lng) * mpd
        dy = (cy - lat) * M_LAT
        q = (0 if dx >= 0 and dy >= 0 else 1 if dx >= 0 else 2 if dy < 0 else 3)
        if d < near[q]:
            near[q] = d
        if d <= 200 * SCALE:
            cnt[q] += 1
    near_s = sorted(near)                      # closest quadrant first
    cnt_s = sorted(cnt, reverse=True)          # busiest quadrant first
    f = {}
    for i in range(4):
        f[f"q_major_near_{i}"] = near_s[i]
        f[f"q_major_n200_{i}"] = cnt_s[i]
    # How lopsided the surroundings are: 0 = a road equally close on all sides.
    f["q_near_spread"] = near_s[3] - near_s[0]
    return f


def feats(con, roads_t, bldg_t, poi_t, lat, lng):
    """The 75 v5 features, computed on a true metric ruler (+9 if DIRECTIONAL)."""
    mpd = M_LAT * math.cos(math.radians(lat))

    # --- Roads: per-axis distance to the closest point on the line ---
    dlng, dlat = _window(lat, ROAD_R)
    road_rows = con.execute(f"""
        SELECT class, d, cx, cy FROM (
            SELECT class, ST_X(cp) AS cx, ST_Y(cp) AS cy,
                   SQRT(POW((ST_X(cp)-({lng}))*{mpd},2)
                      + POW((ST_Y(cp)-({lat}))*{M_LAT},2)) AS d
            FROM (
                SELECT class, ST_ClosestPoint(geometry, ST_Point({lng},{lat})) AS cp
                FROM {roads_t}
                WHERE xmin BETWEEN {lng-dlng} AND {lng+dlng}
                  AND ymin BETWEEN {lat-dlat} AND {lat+dlat}
            )
        ) WHERE d < {ROAD_R}
    """).fetchall()
    rows = [(c, d) for c, d, _, _ in road_rows]

    f = {}
    for c in CLASSES:
        ds = [d for cls, d in rows if cls == c]
        f[f"{c}_invd"] = sum(1.0 / max(d, 10) for d in ds)
        f[f"{c}_near"] = min(ds) if ds else 1000.0
        for r in RINGS:
            f[f"{c}_n{r}"] = sum(1 for d in ds if d <= RING_THRESH[r])
    major = ("motorway", "trunk", "primary", "secondary", "tertiary")
    nm = min((d for cls, d in rows if cls in major), default=1000.0)
    f["nearest_major"] = nm
    f["n_roads_200"] = sum(1 for cls, d in rows if d <= 200 * SCALE)
    f["n_roads_500"] = sum(1 for cls, d in rows if d <= 500 * SCALE)

    # --- Buildings: distance already per-axis in v5; only the box is resized ---
    dlng, dlat = _window(lat, BLDG_R)
    b = con.execute(f"""
        SELECT h, SQRT(POW((clng-({lng}))*{mpd},2)+POW((clat-({lat}))*{M_LAT},2)) AS d
        FROM {bldg_t}
        WHERE clng BETWEEN {lng-dlng} AND {lng+dlng}
          AND clat BETWEEN {lat-dlat} AND {lat+dlat}
    """).fetchall()
    h100 = [h for h, d in b if d <= 100 * SCALE]
    f["bldg_n100"] = len(h100)
    f["bldg_n200"] = sum(1 for h, d in b if d <= 200 * SCALE)
    f["bldg_h_mean100"] = float(np.mean(h100)) if h100 else 0.0
    f["bldg_h_max200"] = max((h for h, d in b if d <= 200 * SCALE), default=0.0)
    f["canyon"] = (f["bldg_h_mean100"] / max(nm, 5)) if h100 else 0.0

    # --- POIs ---
    dlng, dlat = _window(lat, POI_R)
    p = con.execute(f"""
        SELECT SQRT(POW((lng-({lng}))*{mpd},2)+POW((lat-({lat}))*{M_LAT},2)) AS d
        FROM {poi_t}
        WHERE lng BETWEEN {lng-dlng} AND {lng+dlng}
          AND lat BETWEEN {lat-dlat} AND {lat+dlat}
    """).fetchall()
    pd = [r[0] for r in p]
    f["poi_n100"] = sum(1 for d in pd if d <= 100 * SCALE)
    f["poi_n300"] = sum(1 for d in pd if d <= 300 * SCALE)
    f["poi_n500"] = sum(1 for d in pd if d <= 500 * SCALE)

    # --- DEM / land cover: cos_correct so the window is metres, not degrees ---
    elev = rs.sample(DEM, lat, lng, default=0.0)
    f["elev"] = elev if not math.isnan(elev) else 0.0
    er = rs.window_stats(DEM, lat, lng, 300 * SCALE, cos_correct=True)
    f["elev_range300"] = (er.get("max", 0) - er.get("mean", 0)) * 2 if er else 0.0
    lc = rs.window_stats(LC, lat, lng, 300 * SCALE, categorical=True,
                         classes=list(LC_CLASSES.keys()), cos_correct=True)
    for code, name in LC_CLASSES.items():
        f[f"lc_{name}_300"] = lc.get(f"frac_{code}", 0.0)
    lc100 = rs.window_stats(LC, lat, lng, 100 * SCALE, categorical=True, classes=[50],
                            cos_correct=True)
    f["lc_built_100"] = lc100.get("frac_50", 0.0)

    if DIRECTIONAL:
        f.update(_directional(road_rows, lat, lng, mpd))
    return f


def load_au_points():
    """Byte-identical to poc_eu_transfer5.load_au_points (same points, same
    order, same targets) so only the features differ between caches."""
    pts = []
    for c in AU_CITIES:
        fn = f"data/ambient_sample/antn_{c}_buildings_.csv"
        if not os.path.exists(fn):
            continue
        rows = list(csv.DictReader(open(fn)))
        if len(rows) > 350:
            rows = rows[::len(rows) // 350][:350]
        for r in rows:
            m = re.search(r"POINT \(([-\d.]+) ([-\d.]+)\)", r["geometry"])
            if not m:
                continue
            la, lo = float(m.group(1)), float(m.group(2))
            t = lden(float(r["sp_rd_max_d"]), float(r["sp_rd_max_e"]), float(r["sp_rd_max_n"]))
            if t > 0:
                pts.append((c, la, lo, t))
    return pts


def main():
    t0 = time.time()
    con = duckdb.connect()
    con.execute("INSTALL spatial; LOAD spatial; SET preserve_insertion_order=false;")
    con.execute("SET memory_limit='6GB'; SET threads=6;")

    au_pts = load_au_points()
    bb = " OR ".join(
        f"(bbox.xmin BETWEEN {min(lo)-0.05} AND {max(lo)+0.05} AND "
        f"bbox.ymin BETWEEN {min(la)-0.05} AND {max(la)+0.05})"
        for c in AU_CITIES
        for la, lo in [([p[1] for p in au_pts if p[0] == c],
                        [p[2] for p in au_pts if p[0] == c])] if la)
    con.execute("CREATE TABLE poi AS SELECT lng,lat FROM read_parquet('data/eu/poi.parquet') ORDER BY lng,lat")
    print(f"  POI loaded ({time.time()-t0:.0f}s)", flush=True)

    KEYS = fkeys_v2()

    def compute(label, country_pts, roads_sql, bldg_sql):
        con.execute("DROP TABLE IF EXISTS rr"); con.execute("DROP TABLE IF EXISTS bb_t")
        con.execute(f"CREATE TABLE rr AS {roads_sql}")
        con.execute(f"CREATE TABLE bb_t AS {bldg_sql}")
        print(f"  {label}: tables ready ({time.time()-t0:.0f}s)", flush=True)
        out = []
        for j, (la, lo, tgt) in enumerate(country_pts):
            f = feats(con, "rr", "bb_t", "poi", la, lo)
            out.append(([f[k] for k in KEYS], tgt))
            if (j + 1) % 2000 == 0:
                print(f"    {j+1}/{len(country_pts)} ({time.time()-t0:.0f}s)", flush=True)
        con.execute("DROP TABLE rr"); con.execute("DROP TABLE bb_t")
        return out

    nl = [(float(r["lat"]), float(r["lng"]), float(r["lden"]))
          for r in csv.DictReader(open("data/eu/nl_train_points.csv"))]
    uk = [(float(r["lat"]), float(r["lng"]), float(r["lden"]))
          for r in csv.DictReader(open("data/uk/uk_train_points.csv"))]

    nlo = compute("NL", nl,
                  "SELECT class,geometry,xmin,ymin FROM read_parquet('data/eu/nl_roads.parquet')",
                  "SELECT h,clng,clat FROM read_parquet('data/eu/nl_buildings.parquet') ORDER BY clng,clat")
    uko = compute("UK", uk,
                  "SELECT class,geometry,xmin,ymin FROM read_parquet('data/uk/uk_roads.parquet')",
                  "SELECT h,clng,clat FROM read_parquet('data/uk/uk_buildings.parquet') ORDER BY clng,clat")

    au3 = [(la, lo, t) for (c, la, lo, t) in au_pts]
    auo = compute("AU", au3,
                  "SELECT class,geometry,bbox.xmin AS xmin,bbox.ymin AS ymin "
                  "FROM read_parquet('data/overture_roads.parquet') "
                  "WHERE class IN ('motorway','trunk','primary','secondary','tertiary',"
                  f"'residential','service','unclassified','living_street') AND ({bb})",
                  "SELECT COALESCE(height,6.0) AS h, ST_X(ST_Centroid(geometry)) AS clng, "
                  "ST_Y(ST_Centroid(geometry)) AS clat "
                  f"FROM read_parquet('data/overture_buildings.parquet') WHERE ({bb})")

    Xnl = np.array([x for x, _ in nlo + uko], float)
    ynl = np.array([t for _, t in nlo + uko], float)
    Xau = np.array([x for x, _ in auo], float)
    yau = np.array([t for _, t in auo], float)
    cau = np.array([c for (c, la, lo, t) in au_pts])
    np.savez(CACHE, Xnl=Xnl, ynl=ynl, Xau=Xau, yau=yau, cau=cau)
    print(f"\nsaved {CACHE}  Xnl={Xnl.shape} Xau={Xau.shape} ({time.time()-t0:.0f}s)")

    old = np.load("data/eu/transfer5_cache.npz", allow_pickle=True)
    assert old["Xnl"].shape[0] == Xnl.shape[0] and old["Xau"].shape[0] == Xau.shape[0], "point sets diverged"
    assert np.allclose(old["ynl"], ynl) and np.allclose(old["yau"], yau), "targets diverged"
    print("point sets and targets identical to v5 -- only the features differ")


if __name__ == "__main__":
    raise SystemExit(main())
