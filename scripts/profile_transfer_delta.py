"""Clean warm-median comparison + transfer component isolation.

Run twice (NOISE_TRANSFER=1 and =0) externally; also internally times the
transfer sub-steps (feats vs RF.predict) on a persistent connection.
"""
import os
import statistics
import time

LAT, LNG = -37.81, 144.96

from property_scores.noise.score import noise_score, _TRANSFER_ENABLED  # noqa: E402


def warm_median(n=20):
    noise_score(LAT, LNG)  # cold
    noise_score(LAT, LNG)  # extra warm
    xs = []
    for _ in range(n):
        a = time.perf_counter()
        noise_score(LAT, LNG)
        xs.append((time.perf_counter() - a) * 1000)
    return xs


xs = warm_median(20)
xs_sorted = sorted(xs)
print(f"NOISE_TRANSFER={os.environ.get('NOISE_TRANSFER','0')}  _TRANSFER_ENABLED={_TRANSFER_ENABLED}")
print(f"  n=20 warm ms: median={statistics.median(xs):.1f}  mean={statistics.mean(xs):.1f}  "
      f"min={min(xs):.1f}  p25={xs_sorted[4]:.1f}  p75={xs_sorted[14]:.1f}  max={max(xs):.1f}")

# If transfer on, time the sub-steps on a persistent connection
if _TRANSFER_ENABLED:
    from property_scores.common.overture import get_db
    from property_scores.noise import transfer as T
    from property_scores.common.au_state import detect_state
    db = get_db()
    state = detect_state(LAT, LNG)
    T._load()
    # warm
    T.transfer_feats(db, LAT, LNG)
    # time feats
    fs = []
    for _ in range(10):
        a = time.perf_counter()
        T.transfer_feats(db, LAT, LNG)
        fs.append((time.perf_counter()-a)*1000)
    # time predict alone
    f, ok = T.transfer_feats(db, LAT, LNG)
    X = [[f[k] for k in T._FEATURE_KEYS]]
    T._RF.predict(X)  # warm
    ps = []
    for _ in range(10):
        a = time.perf_counter()
        T._RF.predict(X)
        ps.append((time.perf_counter()-a)*1000)
    # time full transfer_lden
    ts = []
    for _ in range(10):
        a = time.perf_counter()
        T.transfer_lden(db, LAT, LNG, state)
        ts.append((time.perf_counter()-a)*1000)
    print(f"  transfer_feats (duckdb roads+bldg+poi + raster) median {statistics.median(fs):.1f} ms")
    print(f"  RF.predict (300 trees, 75 feats)              median {statistics.median(ps):.1f} ms")
    print(f"  transfer_lden total                           median {statistics.median(ts):.1f} ms")
