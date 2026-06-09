"""Geocode harvested EIS measured noise points (G-NAF) + compare to our model.

Input CSV columns: state,logger,address,meas_day,meas_night,metric
  metric: 'LAeq' (day already LAeq) | 'LA10' (VIC: day is LA10(18hr) -> -3 to LAeq)
          | 'snapshot' (QLD attended short reading, low confidence)

Converts measured Day/Night to an approx Lden and reports per-state + overall
MAE/bias. First MEASURED, multi-state validation of the model vs reality.
Caveats: address->building-centroid geocoding loses logger facade position;
day/night->Lden is approximate; QLD snapshots are not continuous LAeq.
"""
import csv
import math
import sys
import json
import urllib.parse
import urllib.request

from property_scores.noise.score import noise_score

GEOCODE = "https://daleads.com.au/api/address-autocomplete?q="
STATE_HINT = {"VIC": "VIC", "WA": "WA", "QLD": "QLD", "NSW": "NSW", "TAS_ACT_NT": ""}
L10_TO_LEQ = 3.0  # LA10 road traffic -> LAeq offset


def geocode(address, state):
    q = address.replace("- ", "-").strip()
    hint = STATE_HINT.get(state, "")
    if hint and hint.lower() not in q.lower():
        q = f"{q} {hint}"
    url = GEOCODE + urllib.parse.quote(q)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ps-eis"})
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.load(r)
        rows = d if isinstance(d, list) else (d.get("results") or d.get("suggestions") or [])
        if rows:
            return float(rows[0]["lat"]), float(rows[0]["lng"])
    except Exception as e:
        print(f"  geocode fail [{q}]: {e}", file=sys.stderr)
    return None, None


def meas_lden(day, night, metric):
    day_leq = day - L10_TO_LEQ if metric == "LA10" else day
    return 10 * math.log10((15 * 10 ** (day_leq / 10) + 9 * 10 ** ((night + 10) / 10)) / 24)


def main():
    path = sys.argv[1]
    from collections import defaultdict
    by = defaultdict(list)
    rows = list(csv.DictReader(open(path)))
    print(f"{'state':<11}{'addr':<32}{'meas_Lden':>10}{'model':>8}{'diff':>7}{'metric':>9}")
    for row in rows:
        lat, lng = geocode(row["address"], row["state"])
        if lat is None:
            continue
        r = noise_score(lat, lng)
        mod = r.get("lden_db")
        if mod is None:
            continue
        ml = meas_lden(float(row["meas_day"]), float(row["meas_night"]), row.get("metric", "LAeq"))
        diff = mod - ml
        by[row["state"]].append(diff)
        print(f"{row['state']:<11}{row['address'][:30]:<32}{ml:>10.1f}{mod:>8.1f}{diff:>+7.1f}{row.get('metric',''):>9}")
    print("\n===== per-state (model minus measured Lden) =====")
    alld = []
    for st, ds in by.items():
        alld += ds
        mae = sum(abs(x) for x in ds) / len(ds)
        print(f"  {st:<11} n={len(ds):>3}  bias={sum(ds)/len(ds):+6.1f}  MAE={mae:5.2f}")
    if alld:
        print(f"  {'ALL':<11} n={len(alld):>3}  bias={sum(alld)/len(alld):+6.1f}  MAE={sum(abs(x) for x in alld)/len(alld):5.2f}")


if __name__ == "__main__":
    main()
