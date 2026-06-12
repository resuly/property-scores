"""Quantify the rail-component over-read on the quiet corpus (NSW rail gap).

For every corpus point, runs the local model twice: as-is, and with the rail
sources kicked out (gtfs_rail_near / rail_near monkeypatched to empty). The
difference isolates the rail component's contribution to the final Lden. Where
the measured TOTAL sits below the rail-inclusive model, an implied "rail level
the measurement could at most contain" (energy-subtracting the no-rail model)
bounds the rail over-read per point.

Run (matches deployed prod flags):
  NOISE_TRANSFER=1 NOISE_QUIET_RECAL=1 .venv/bin/python scripts/rail_recal_diagnose.py
Add NOISE_RAIL_RECAL=1 for an "after" run.
"""
import csv
import math
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("NOISE_TRANSFER", "1")

CORPUS = os.path.join(ROOT, "data", "eis_noise", "measured_quiet_corpus.csv")


def _esub(total_db: float, part_db: float) -> float:
    """Energy-subtract part from total (dB). Returns -inf-ish floor at 0."""
    e = 10 ** (total_db / 10) - 10 ** (part_db / 10)
    return 10 * math.log10(e) if e > 0 else 0.0


def main():
    from property_scores.noise import score as sc

    rows = list(csv.DictReader(open(CORPUS)))
    print(f"NOISE_TRANSFER={os.environ.get('NOISE_TRANSFER')} "
          f"NOISE_QUIET_RECAL={os.environ.get('NOISE_QUIET_RECAL', '0')} "
          f"NOISE_RAIL_RECAL={os.environ.get('NOISE_RAIL_RECAL', '0')}")
    hdr = (f"{'site':40} {'meas':>5} {'model':>6} {'norail':>6} {'rail_db':>7} "
           f"{'railctb':>7} {'maxrail':>7} {'over>=':>6} {'dom':>9} {'dom_norail':>10} "
           f"{'type':>5} {'dist':>5} {'svc':>5} {'scr':>4}")
    print(hdr)

    real_gtfs = sc.gtfs_rail_near
    real_rail = sc.rail_near

    for r in rows:
        lat, lng = float(r["lat"]), float(r["lng"])
        meas = float(r["meas_lden"])

        sc.gtfs_rail_near, sc.rail_near = real_gtfs, real_rail
        out = sc.noise_score(lat, lng)

        sc.gtfs_rail_near = lambda *a, **k: []
        sc.rail_near = lambda *a, **k: []
        out_nr = sc.noise_score(lat, lng)

        model, model_nr = out["lden_db"], out_nr["lden_db"]
        rail_db = out.get("rail_db") or 0.0
        # rail contribution to the final Lden mix (energy domain)
        ctb = model - model_nr
        dr = out.get("dominant_rail") or {}
        # The measurement is the TOTAL. The rail Lden contribution the truth can
        # at most contain = energy-subtract the (assumed-ok) no-rail model.
        if rail_db > 0 and model > model_nr + 0.05:
            rail_in_model = _esub(model, model_nr)
            max_rail_true = _esub(meas, model_nr) if meas > model_nr else 0.0
            over = rail_in_model - max_rail_true if max_rail_true > 0 else float("inf")
            over_s = f"{over:>+6.1f}" if over != float("inf") else "  inf"
            mr_s = f"{max_rail_true:>7.1f}" if max_rail_true > 0 else "  <<amb"
        else:
            over_s, mr_s = "     -", "      -"
        print(f"{r['site'][:40]:40} {meas:>5.1f} {model:>6.1f} {model_nr:>6.1f} "
              f"{rail_db if rail_db else '-':>7} {ctb:>+7.1f} {mr_s:>7} {over_s:>6} "
              f"{(out.get('dominant_source') or '-')[:9]:>9} "
              f"{(out_nr.get('dominant_source') or '-')[:10]:>10} "
              f"{dr.get('type', '-'):>5} {dr.get('distance_m', '-'):>5} "
              f"{dr.get('peak_svc_hr', '-'):>5} {dr.get('screening_db', '-'):>4}")

    sc.gtfs_rail_near, sc.rail_near = real_gtfs, real_rail


if __name__ == "__main__":
    main()
