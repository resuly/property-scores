"""Does ACTUAL AADT explain the model's over-read vs MEASURED noise?

The transfer RF has road CLASS but no traffic VOLUME. Hypothesis: the high-end
flattening (model over-reads its loud end) happens specifically where the nearby
major road's ACTUAL aadt is LOWER than its class implies — i.e. a suburban
arterial the EU model assumes is busy. If so, AADT carries the corrective signal
the model lacks, and a post-adjustment keyed on log(actual_aadt / class_expected)
will close the gap. If residual does NOT track the aadt gap, AADT is not the fix.

This is the experiment the 06-06 POC never ran: it validated AADT against
SoundPLAN (itself the hot target). Here we validate against the 199 measured
points (residuals.csv + geocode cache).
"""
import csv
import json
import math
import os
import statistics
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from property_scores.common.overture import get_db, aadt_near, roads_near  # noqa: E402
from property_scores.noise.score import _estimate_aadt  # class+speed -> expected AADT

RESID = "data/eis_noise/residuals.csv"
GC = "data/eis_noise/_geocode_cache.json"
MAJOR = ("motorway", "trunk", "primary", "secondary", "tertiary")


def corr(xs, ys):
    if len(xs) < 3:
        return float("nan")
    mx, my = statistics.mean(xs), statistics.mean(ys)
    cov = sum((a - mx) * (b - my) for a, b in zip(xs, ys)) / len(xs)
    sd = statistics.pstdev(xs) * statistics.pstdev(ys)
    return cov / sd if sd else 0.0


def main():
    gc = json.load(open(GC))
    db = get_db()
    rows = []
    for r in csv.DictReader(open(RESID)):
        key = f"{r['state']}|{r['address']}"
        if key not in gc:
            continue
        lat, lng = gc[key]
        if lat is None:
            continue
        # nearest road overall + nearest MAJOR road (what the model keys on)
        rn = roads_near(db, lat, lng, radius_m=400)
        if not rn:
            continue
        # roads_near row: (class, dist_m, ...) -> take class + dist
        majors = [(row[0], row[1]) for row in rn if row[0] in MAJOR]
        if majors:
            mclass, mdist = min(majors, key=lambda t: t[1])
        else:
            mclass, mdist = min(((row[0], row[1]) for row in rn), key=lambda t: t[1])
        exp_aadt = _estimate_aadt(mclass, None)
        # nearest ACTUAL aadt record within 300m
        an = aadt_near(db, lat, lng, radius_m=300)
        if an:
            # an row: (aadt, hv_pct, road_name, dist_m, near_lng, near_lat)
            aadt, _hv, _name, adist = an[0][0], an[0][1], an[0][2], an[0][3]
            # take the highest-aadt within 150m as the likely dominant source
            near150 = [x for x in an if x[3] <= 150]
            dom_aadt = max((x[0] for x in near150), default=aadt)
        else:
            aadt, adist, dom_aadt = None, None, None
        rows.append({
            "state": r["state"], "model": float(r["model_lden"]),
            "meas": float(r["measured_lden"]), "res": float(r["residual"]),
            "mclass": mclass, "mdist": mdist, "exp_aadt": exp_aadt,
            "aadt": aadt, "adist": adist, "dom_aadt": dom_aadt,
        })
    db.close()

    have = [r for r in rows if r["dom_aadt"] and r["dom_aadt"] > 0]
    print(f"n total={len(rows)}  with actual AADT hit={len(have)}")

    # log ratio actual/expected: <0 means road quieter than class implies
    for r in have:
        r["lr"] = math.log(r["dom_aadt"] / max(r["exp_aadt"], 1))
        r["laadt"] = math.log(r["dom_aadt"])

    print("\n[H1] residual vs log(actual/class_expected AADT)")
    print(f"  overall            r={corr([r['lr'] for r in have],[r['res'] for r in have]):+.2f}")
    hi = [r for r in have if r["model"] >= 68]
    print(f"  high-model(>=68)   r={corr([r['lr'] for r in hi],[r['res'] for r in hi]):+.2f}  n={len(hi)}")
    print("  (expect NEGATIVE: lower actual-vs-expected AADT -> bigger over-read)")

    print("\n[H2] residual vs log(actual AADT) within high-model(>=68)")
    print(f"  r={corr([r['laadt'] for r in hi],[r['res'] for r in hi]):+.2f}")

    print("\n[binned] high-model(>=68) points by actual-vs-expected AADT ratio:")
    hi_sorted = sorted(hi, key=lambda r: r["lr"])
    k = max(1, len(hi_sorted) // 3)
    for label, grp in [("road QUIETER than class (lr<0)", [r for r in hi if r["lr"] < -0.2]),
                       ("road ~ class (|lr|<=0.2)", [r for r in hi if abs(r["lr"]) <= 0.2]),
                       ("road BUSIER than class (lr>0.2)", [r for r in hi if r["lr"] > 0.2])]:
        if grp:
            print(f"  {label:<34} n={len(grp):>3}  mean residual {statistics.mean(r['res'] for r in grp):+5.1f}"
                  f"  mean actual AADT {statistics.mean(r['dom_aadt'] for r in grp):>7.0f}")

    print("\n[control] does the over-read just track distance to road, not AADT?")
    print(f"  residual vs nearest-major dist (hi):  r={corr([r['mdist'] for r in hi],[r['res'] for r in hi]):+.2f}")

    # write joined table for the post-adjustment step
    out = "data/eis_noise/residuals_aadt.csv"
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"\nwrote {out} (n={len(rows)})")


if __name__ == "__main__":
    main()
