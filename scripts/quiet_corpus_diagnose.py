"""Decompose the NSW quiet-residential over-read (handoff §8 step 2).

For every corpus point, recompute the transfer RF raw + its 75 features, then
split the over-read (model - measured) into:

  rf_share    = raw - meas            RF error if calibration were identity
                (captures "RF reads the motorway corridor hot": geometry says
                loud, the noise walls / setback say otherwise)
  cal_share   = affine(raw) - raw     what the NSW affine ADDS on top of raw
                (slope 0.8776 / intercept 16.55 fit on SoundPLAN urban facades)
  mix_share   = remainder             quiet-blend + rail/aircraft remix effects

and prints needed_add = meas - raw, i.e. what a correct calibration should add
per point. Sorting needed_add against receptor-context features inside the
conflicting raw band (~60-66, where set-back homes and kerbside sensors share
the same raw) surfaces which feature separates them.

Usage: .venv/bin/python scripts/quiet_corpus_diagnose.py
"""
import csv
import os
import statistics
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("NOISE_TRANSFER", "1")

CORPUS = os.path.join(ROOT, "data", "eis_noise", "measured_quiet_corpus.csv")

# receptor setback = distance to the nearest motorised road of ANY class
_NEAR_KEYS = ["motorway_near", "trunk_near", "primary_near", "secondary_near",
              "tertiary_near", "residential_near", "service_near",
              "unclassified_near"]


def main():
    from property_scores.common.overture import get_db
    from property_scores.noise import transfer as tr
    from property_scores.noise.score import noise_score

    if not tr._load():
        sys.exit("transfer model unavailable")
    cal = tr._CALIB["states"]["NSW"]
    db = get_db()

    rows = list(csv.DictReader(open(CORPUS)))
    out = []
    for r in rows:
        lat, lng = float(r["lat"]), float(r["lng"])
        f, raster_ok = tr.transfer_feats(db, lat, lng)
        X = [[f[k] for k in tr._FEATURE_KEYS]]
        raw = float(tr._RF.predict(X)[0])
        meas = float(r["meas_lden"])
        score_out = noise_score(lat, lng)
        model = score_out["lden_db"]
        out.append({
            "site": r["site"], "role": r["role"], "qual": r["quality"],
            "meas": meas, "raw": raw, "model": model,
            "affine": cal["slope"] * raw + cal["intercept"],
            "needed_add": meas - raw,
            "near_any": min(f[k] for k in _NEAR_KEYS),
            "nearest_major": f["nearest_major"],
            "poi100": f["poi_n100"], "poi300": f["poi_n300"],
            "built300": f["lc_built_300"], "bldg100": f["bldg_n100"],
            "rail_db": score_out.get("rail_db") or 0,
            "road_db": score_out.get("road_db") or 0,
        })

    print(f"{'site':42} {'role':>16} {'meas':>5} {'raw':>5} {'affine':>6} "
          f"{'model':>6} {'need+':>6} {'nearR':>5} {'nearMaj':>7} {'poi100':>6} "
          f"{'blt.3':>5} {'rail':>5}")
    for o in sorted(out, key=lambda x: x["raw"]):
        print(f"{o['site'][:42]:42} {o['role']:>16} {o['meas']:>5.1f} "
              f"{o['raw']:>5.1f} {o['affine']:>6.1f} {o['model']:>6.1f} "
              f"{o['needed_add']:>+6.1f} {o['near_any']:>5.0f} "
              f"{o['nearest_major']:>7.0f} {o['poi100']:>6.0f} "
              f"{o['built300']:>5.2f} {o['rail_db']:>5.1f}")

    res = [o for o in out if o["role"] == "quiet_residential" and o["qual"] == "exact"]
    loud = [o for o in out if o["role"] == "loud_roadside"]

    def decomp(label, pts):
        tot = statistics.mean(o["model"] - o["meas"] for o in pts)
        rf = statistics.mean(o["raw"] - o["meas"] for o in pts)
        ca = statistics.mean(o["affine"] - o["raw"] for o in pts)
        print(f"  {label:24} n={len(pts)}  total {tot:+6.2f} = "
              f"rf_share {rf:+6.2f} + cal_share {ca:+6.2f} + mix "
              f"{tot - rf - ca:+6.2f}")

    print("\n== over-read decomposition (model - meas) ==")
    decomp("residential (exact)", res)
    decomp("loud_roadside", loud)

    print("\n== needed_add (meas - raw): what calibration SHOULD add ==")
    for label, pts in (("residential", res), ("loud_roadside", loud)):
        vals = [o["needed_add"] for o in pts]
        print(f"  {label:24} mean {statistics.mean(vals):+6.2f}  "
              f"range [{min(vals):+.1f}, {max(vals):+.1f}]")

    print("\n== conflict band raw 58-67: homes vs kerbside, by receptor feature ==")
    band = sorted((o for o in out if 58 <= o["raw"] <= 67),
                  key=lambda x: x["needed_add"])
    for o in band:
        print(f"  {o['site'][:40]:40} {o['role'][:14]:14} need {o['needed_add']:+5.1f} "
              f"nearR {o['near_any']:>4.0f} nearMaj {o['nearest_major']:>5.0f} "
              f"poi100 {o['poi100']:>3.0f} poi300 {o['poi300']:>4.0f} "
              f"built {o['built300']:.2f} bldg100 {o['bldg100']:>3.0f}")


if __name__ == "__main__":
    main()
