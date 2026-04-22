"""Run full Melbourne NoiseCapture validation, output to file."""
import json, math, time, sys, os

os.environ['DUCKDB_NO_PROGRESS_BAR'] = '1'

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from property_scores.noise.score import noise_score

OUT = os.path.join(os.path.dirname(__file__), '..', 'validation_results.txt')

areas = []
for fname in [
    'D:/property-scores-data/noisecapture/melbourne/Australia_Victoria_Melbourne - Inner.areas.geojson',
    "D:/property-scores-data/noisecapture/melbourne/Australia_Victoria_Melbourne - S'bank-D'lands.areas.geojson",
]:
    with open(fname) as f:
        data = json.load(f)
    for feat in data['features']:
        p = feat['properties']
        g = feat['geometry']
        if g['type'] != 'Polygon':
            continue
        cs = g['coordinates'][0]
        clng = sum(c[0] for c in cs) / len(cs)
        clat = sum(c[1] for c in cs) / len(cs)
        laeq = p.get('laeq')
        count = p.get('measure_count', 0)
        if laeq and count >= 3:
            areas.append((clat, clng, float(laeq), count))

with open(OUT, 'w') as out:
    out.write(f"Melbourne NoiseCapture Validation ({len(areas)} hexagons, min 3 measurements)\n")
    out.write("=" * 70 + "\n\n")

    errors = []
    t0 = time.time()
    for i, (lat, lng, measured, count) in enumerate(areas):
        try:
            r = noise_score(lat, lng)
            predicted = r['estimated_db']
            error = predicted - measured
            errors.append(error)
            out.write(f"{i+1:3d} ({lat:.4f},{lng:.4f}) pred={predicted:5.1f} meas={measured:5.1f} err={error:+5.1f} score={r['score']:3d} n={count}\n")
            if (i + 1) % 50 == 0:
                out.flush()
                elapsed = time.time() - t0
                rate = (i + 1) / elapsed
                remaining = (len(areas) - i - 1) / rate
                print(f"  {i+1}/{len(areas)} ({elapsed:.0f}s elapsed, ~{remaining:.0f}s remaining)", flush=True)
        except Exception as e:
            out.write(f"{i+1:3d} ({lat:.4f},{lng:.4f}) ERROR: {e}\n")

    dt = time.time() - t0
    n = len(errors)
    if n == 0:
        out.write("\nNo valid comparisons.\n")
        sys.exit(1)

    bias = sum(errors) / n
    mae = sum(abs(e) for e in errors) / n
    rmse = math.sqrt(sum(e ** 2 for e in errors) / n)
    w5 = sum(1 for e in errors if abs(e) <= 5) / n * 100
    w10 = sum(1 for e in errors if abs(e) <= 10) / n * 100
    median_err = sorted(errors)[n // 2]

    out.write(f"\n{'=' * 70}\n")
    out.write(f"RESULTS ({n} hexagons, {dt:.0f}s)\n")
    out.write(f"{'=' * 70}\n")
    out.write(f"Bias (mean error):  {bias:+.1f} dB\n")
    out.write(f"Median error:       {median_err:+.1f} dB\n")
    out.write(f"MAE:                {mae:.1f} dB\n")
    out.write(f"RMSE:               {rmse:.1f} dB\n")
    out.write(f"Within 5 dB:        {w5:.0f}% ({sum(1 for e in errors if abs(e) <= 5)}/{n})\n")
    out.write(f"Within 10 dB:       {w10:.0f}% ({sum(1 for e in errors if abs(e) <= 10)}/{n})\n")

    # Breakdown by error bucket
    buckets = [(0, 3), (3, 5), (5, 8), (8, 10), (10, 15), (15, 99)]
    out.write(f"\nError distribution:\n")
    for lo, hi in buckets:
        cnt = sum(1 for e in errors if lo <= abs(e) < hi)
        out.write(f"  |error| {lo:2d}-{hi:2d} dB: {cnt:3d} ({cnt/n*100:.0f}%)\n")

    # Worst predictions
    indexed = [(errors[i], areas[i]) for i in range(n)]
    indexed.sort(key=lambda x: abs(x[0]), reverse=True)
    out.write(f"\nWorst 10:\n")
    for err, (lat, lng, meas, cnt) in indexed[:10]:
        out.write(f"  ({lat:.4f},{lng:.4f}) err={err:+.1f} meas={meas:.1f} n={cnt}\n")
    out.write(f"\nBest 10:\n")
    for err, (lat, lng, meas, cnt) in indexed[-10:]:
        out.write(f"  ({lat:.4f},{lng:.4f}) err={err:+.1f} meas={meas:.1f} n={cnt}\n")

    out.write(f"\nDone in {dt:.0f}s ({dt/n:.1f}s per hexagon)\n")

print(f"\nResults written to {OUT}")
print(f"Bias={bias:+.1f} MAE={mae:.1f} RMSE={rmse:.1f} Within5={w5:.0f}% Within10={w10:.0f}%")
