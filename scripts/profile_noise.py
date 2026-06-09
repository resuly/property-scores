"""Pin down the EXACT bottleneck of noise_score with cProfile + wall timing.

Run with NOISE_TRANSFER set in the process env BEFORE python starts (read at import).
"""
import cProfile
import io
import os
import pstats
import statistics
import time

LAT, LNG = -37.81, 144.96

from property_scores.noise.score import noise_score, _TRANSFER_ENABLED  # noqa: E402


def warmup():
    """One full call to load RF, open duckdb, prime aircraft lru_cache, open rasters."""
    t0 = time.perf_counter()
    r = noise_score(LAT, LNG)
    dt = (time.perf_counter() - t0) * 1000
    return dt, r


def wall_timing(n=10):
    cold_ms, r = warmup()  # this is the COLD first call
    warm = []
    for _ in range(n):
        t0 = time.perf_counter()
        noise_score(LAT, LNG)
        warm.append((time.perf_counter() - t0) * 1000)
    return cold_ms, warm, r


def profile_one():
    # warm up first so RF/duckdb/raster/aircraft-cache are all primed
    warmup()
    warmup()  # second warmup to be safe (raster _OPEN cache, etc.)
    pr = cProfile.Profile()
    pr.enable()
    noise_score(LAT, LNG)
    pr.disable()
    return pr


def cumtime_of(stats, name_substrings):
    """Sum cumulative time (s) of all profiled functions whose (file, line, func)
    label contains any of the given substrings. Returns max cumtime among matches
    to avoid double counting nested wrappers; caller picks the right granularity."""
    total = 0.0
    matched = []
    for func, (cc, nc, tt, ct, callers) in stats.stats.items():
        fname, lineno, funcname = func
        label = f"{fname}:{funcname}"
        for s in name_substrings:
            if s in label:
                matched.append((label, ct, tt))
                break
    return matched


def func_cumtime(stats, file_sub, func_name):
    """Exact cumtime (s) for a specific function by file substring + func name.
    Sums across all matching keys (usually one)."""
    tot = 0.0
    found = False
    for func, (cc, nc, tt, ct, callers) in stats.stats.items():
        fname, lineno, funcname = func
        if func_name == funcname and (file_sub is None or file_sub in fname):
            tot += ct
            found = True
    return tot if found else None


def main():
    transfer = os.environ.get("NOISE_TRANSFER", "0")
    print(f"==== NOISE_TRANSFER={transfer}  (_TRANSFER_ENABLED={_TRANSFER_ENABLED}) ====")

    # ---- wall timing ----
    cold_ms, warm, r = wall_timing(10)
    warm_sorted = sorted(warm)
    print(f"\n[WALL] cold (first call): {cold_ms:.1f} ms")
    print(f"[WALL] warm calls (ms): {[round(x,1) for x in warm]}")
    print(f"[WALL] warm median: {statistics.median(warm):.1f} ms  min: {min(warm):.1f}  max: {max(warm):.1f}")
    print(f"[RESULT] lden_db={r.get('lden_db')} physics_lden_db={r.get('physics_lden_db')} "
          f"lden_source={r.get('lden_source')} road_count={r.get('road_count')} "
          f"transfer_raw={r.get('transfer_raw')} dominant={r.get('dominant_source')}")

    # ---- cProfile ----
    pr = profile_one()
    s = io.StringIO()
    st = pstats.Stats(pr, stream=s)
    total_time = st.total_tt
    print(f"\n[PROFILE] total profiled time for ONE warm call: {total_time*1000:.1f} ms")

    st.sort_stats("cumulative")
    s.truncate(0); s.seek(0)
    st.print_stats(25)
    print("\n========== TOP 25 BY CUMULATIVE ==========")
    print(s.getvalue())

    s2 = io.StringIO()
    st2 = pstats.Stats(pr, stream=s2)
    st2.sort_stats("tottime")
    st2.print_stats(15)
    print("\n========== TOP 15 BY TOTTIME ==========")
    print(s2.getvalue())

    # ---- stage attribution (cumtime per key function) ----
    print("\n========== STAGE ATTRIBUTION (cumtime ms) ==========")
    stages = [
        ("overture roads (roads_near)",        "overture", "roads_near"),
        ("buildings (buildings_in_radius)",    "buildings", "buildings_in_radius"),
        ("buildings_near (overture)",          "overture", "buildings_near"),
        ("aadt parquet (aadt_near)",           "overture", "aadt_near"),
        ("nfdh (nfdh_near)",                   "overture", "nfdh_near"),
        ("rail/gtfs (gtfs_rail_near)",         "overture", "gtfs_rail_near"),
        ("rail overture (rail_near)",          "overture", "rail_near"),
        ("water (water_near)",                 "overture", "water_near"),
        ("pois (pois_near)",                   "overture", "pois_near"),
        ("aircraft (aircraft_noise_penalty)",  "aircraft", "aircraft_noise_penalty"),
        ("aircraft cached (_aircraft_cached)", "aircraft", "_aircraft_cached"),
        ("terrain (terrain_attenuation)",      "terrain", "terrain_attenuation"),
        ("transfer_lden",                      "transfer", "transfer_lden"),
        ("transfer_feats",                     "transfer", "transfer_feats"),
        ("RF predict",                         None, "predict"),
        ("raster sample",                      "raster_sample", "sample"),
        ("raster window_stats",                "raster_sample", "window_stats"),
        ("get_db",                             "overture", "get_db"),
        ("duckdb connect",                     None, "connect"),
        ("barrier_attenuation",                "buildings", "barrier_attenuation"),
        ("buildings_to_arrays",                "buildings", "buildings_to_arrays"),
    ]
    for label, fsub, fn in stages:
        ct = func_cumtime(st, fsub, fn)
        if ct is not None:
            print(f"  {label:42s} {ct*1000:8.2f} ms   ({ct/total_time*100:5.1f}%)")
        else:
            print(f"  {label:42s}      n/a")

    # All duckdb .sql / .execute / .fetchall time
    print("\n  --- DuckDB internals (cumtime) ---")
    for fn in ("sql", "execute", "fetchall", "load_extension", "install_extension"):
        ct = func_cumtime(st, None, fn)
        if ct is not None:
            print(f"  {('duckdb.'+fn):42s} {ct*1000:8.2f} ms   ({ct/total_time*100:5.1f}%)")


if __name__ == "__main__":
    main()
