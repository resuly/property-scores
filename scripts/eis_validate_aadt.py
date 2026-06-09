"""Score the 199 measured points through the REAL noise_score() and report
MAE/bias per state + loud subset. Run twice (NOISE_AADT_ADJUST=0 then =1) to
confirm the production code reproduces the offline eval and protects loud.

  NOISE_TRANSFER=1 NOISE_AADT_ADJUST=0 .venv/bin/python scripts/eis_validate_aadt.py
  NOISE_TRANSFER=1 NOISE_AADT_ADJUST=1 .venv/bin/python scripts/eis_validate_aadt.py
"""
import csv
import json
import os
import statistics
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
from eis_noise_compare import meas_lden  # noqa: E402
from property_scores.noise.score import noise_score, _AADT_ADJUST_ENABLED, NOISE_MODEL_VERSION  # noqa: E402

CORPUS = "data/eis_noise/measured_corpus_v2.csv"
GC = "data/eis_noise/_geocode_cache.json"


def main():
    gc = json.load(open(GC))
    rows = []
    for r in csv.DictReader(open(CORPUS)):
        key = f"{r['state']}|{r['address']}"
        if key not in gc:
            continue
        lat, lng = gc[key]
        if lat is None:
            continue
        ns = noise_score(lat, lng)
        mod = ns.get("lden_db")
        if mod is None:
            continue
        ml = meas_lden(float(r["meas_day"]), float(r["meas_night"]), r.get("metric", "LAeq"))
        rows.append({"state": r["state"], "model": mod, "meas": round(ml, 2),
                     "res": round(mod - ml, 2)})

    def mae(rs):
        return statistics.mean(abs(r["res"]) for r in rs)

    def bias(rs):
        return statistics.mean(r["res"] for r in rs)

    print(f"AADT_ADJUST={'ON' if _AADT_ADJUST_ENABLED else 'OFF'}  version={NOISE_MODEL_VERSION}  n={len(rows)}")
    print(f"  overall    bias {bias(rows):+.2f}  MAE {mae(rows):.2f}")
    loud = [r for r in rows if r["meas"] >= 72]
    print(f"  loud(>=72) bias {bias(loud):+.2f}  MAE {mae(loud):.2f}  n={len(loud)}")
    from collections import defaultdict
    bys = defaultdict(list)
    for r in rows:
        bys[r["state"]].append(r)
    for s in sorted(bys):
        g = bys[s]
        print(f"    {s:11} n={len(g):>3}  bias {bias(g):+5.2f}  MAE {mae(g):.2f}")


if __name__ == "__main__":
    main()
