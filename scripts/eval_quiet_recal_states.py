"""v2, redesigned: which states should get the quiet-end recalibration?

The first v2 attempt added the physics term to the blend, was chosen on the
SoundPLAN gate, and turned out WORSE against instruments (MAE 7.73 -> 10.32).
That was optimising the wrong objective. This attempt starts from the measured
gate and from a correction that was already validated against instruments.

Evidence this is the right lever:

  * Against the 199-point EIS measured corpus the model over-reads by state:
    NSW -1.1, QLD +2.8, TAS_ACT_NT +4.9, WA +6.6, VIC +9.4 dB.
  * The metric is NOT the cause. Within VIC, LA10 rows bias +9.5 (n=71) and LAeq
    rows +8.8 (n=8) -- a 0.7 dB gap, so the LA10->LAeq conversion is exonerated.
  * `transfer.quiet_relief` exists precisely for this: its docstring records that
    against clean Class-1 truth the model "over-reads set-back suburban homes by
    ~+11 dB", and it removes the affine's lift only where every context gate says
    "ordinary suburban dwelling".
  * It is gated to `QUIET_RECAL_STATES = {"NSW"}` -- and NSW is the ONE state
    that is unbiased against measurements.

So the hypothesis is that the fix is already written and simply not applied where
it is measurably needed. This script tests that instead of assuming it, on the
primary gate, per state, so a state that does not need it cannot be dragged.

    NOISE_TRANSFER=1 NOISE_QUIET_RECAL=1 NOISE_RAIL_RECAL=1 \
      .venv/bin/python scripts/eval_quiet_recal_states.py
"""
import csv
import json
import math
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CORPUS = "data/eis_noise/measured_corpus_v2.csv"
CACHE = "data/eis_noise/_geocode_cache.json"
L10_TO_LEQ = 3.0
# The corpus groups TAS/ACT/NT together; map its labels to score.py state codes.
CORPUS_TO_STATES = {"VIC": {"VIC"}, "NSW": {"NSW"}, "WA": {"WA"}, "QLD": {"QLD"},
                    "TAS_ACT_NT": {"TAS", "ACT", "NT"}}


def meas_lden(day, night, metric):
    day_leq = day - L10_TO_LEQ if metric == "LA10" else day
    return 10 * math.log10((15 * 10 ** (day_leq / 10)
                            + 9 * 10 ** ((night + 10) / 10)) / 24)


def main():
    from property_scores.noise import score as ns
    from property_scores.noise import transfer as tr
    from property_scores.noise.score import noise_score
    ns._cache_get = lambda *a, **k: None
    ns._cache_put = lambda *a, **k: None
    if not tr.QUIET_RECAL_ENABLED:
        print("NOISE_QUIET_RECAL is off; nothing to vary. Set it to 1.")
        return 2

    cache = json.load(open(CACHE))
    rows = []
    for r in csv.DictReader(open(CORPUS)):
        h = cache.get(f"{r['state']}|{r['address']}")
        if h:
            rows.append((r, float(h[0]), float(h[1])))
    print(f"{len(rows)} geocoded measured points\n")

    all_states = {"VIC", "NSW", "WA", "QLD", "TAS", "ACT", "NT", "SA"}
    configs = {
        "current  {NSW}": {"NSW"},
        "+VIC": {"NSW", "VIC"},
        "+VIC+WA": {"NSW", "VIC", "WA"},
        "+VIC+WA+TAS/ACT/NT": {"NSW", "VIC", "WA", "TAS", "ACT", "NT"},
        "ALL states": all_states,
    }

    results = {}
    for label, states in configs.items():
        tr.QUIET_RECAL_STATES = states
        cells = defaultdict(list)
        for r, lat, lng in rows:
            sc = noise_score(lat, lng).get("lden_db")
            if sc is None:
                continue
            d = sc - meas_lden(float(r["meas_day"]), float(r["meas_night"]),
                               r.get("metric", "LAeq"))
            cells[r["state"]].append(d)
        results[label] = cells
        alld = [x for v in cells.values() for x in v]
        print(f"{label:22s} n={len(alld):3d}  bias={sum(alld)/len(alld):+6.2f}  "
              f"MAE={sum(abs(x) for x in alld)/len(alld):5.2f}")

    print(f"\nper-state MAE by configuration:")
    states_seen = sorted(results["current  {NSW}"].keys())
    print(f"{'config':22s}" + "".join(f"{s:>12s}" for s in states_seen))
    for label, cells in results.items():
        line = f"{label:22s}"
        for s in states_seen:
            v = cells.get(s, [])
            line += f"{(sum(abs(x) for x in v)/len(v)) if v else float('nan'):12.2f}"
        print(line)
    print("\nA state that does not need the relief must not get WORSE when it is "
          "switched on -- that is the check that this generalises rather than "
          "trading one state for another.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
