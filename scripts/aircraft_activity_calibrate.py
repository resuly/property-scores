"""Check the ADS-B aircraft Lden estimate against official ANEF bands.

    AIRCRAFT_ACTIVITY_DIR=~/limon-data/aircraft-activity-12m \
        .venv/bin/python scripts/aircraft_activity_calibrate.py --anef-dir <dir>

<dir> holds the official contours as GeoJSON, fetched once (read-only public
queries; never committed, the WA layer is licensed for internal use only):

  vic_27.geojson  VicPlan MAEO (MAEO1 = ANEF 25+, MAEO2 = ANEF 20-25)
  vic_22.geojson  VicPlan AEO  (AEO1 = 25+, AEO2 = 20-25)
  wa_77.geojson   WA SLIP SPP 5.1 Perth Airport (feature_type 20..35)
  bne.geojson     Brisbane City Plan ANEF (ovl2_desc "... ANEF) NN")

Every 250 m cell of the city grid whose centre lies in a contour is assigned
the highest ANEF containing it; cells within 3 km outside the outermost
contour form the "<20" ring. For each band it prints the estimate's median
and quartiles against two references:

  ref_as2021 = ANEF + 35   (the usual ANEF ~ DNL - 35 relation; DNL ~ Lden)
  ref_engine = the aircraft Lden this engine already uses for that band
               (35 + _anef_to_penalty(ANEF) + 6.39, a flat Leq in all periods)

ANEF is a FORECAST of future capacity (Melbourne's includes the third
runway), so current traffic should read at or below it. An estimate that sits
above the band is the failure to look for.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from property_scores.aircraft_activity import noise as aan  # noqa: E402
from property_scores.aircraft_activity import store as aas  # noqa: E402
from property_scores.noise.aircraft import _anef_to_penalty  # noqa: E402


def _anef_of(props: dict) -> int | None:
    z = props.get("ZONE_CODE")
    if z:
        return {"MAEO1": 25, "MAEO2": 20, "AEO1": 25, "AEO2": 20}.get(z)
    ft = props.get("feature_type")
    if ft is not None and str(ft).isdigit():
        return int(ft)
    m = re.search(r"ANEF\)?\s*(\d+)", props.get("ovl2_desc") or "")
    return int(m.group(1)) if m else None


def load_contours(anef_dir: Path, names):
    from shapely.geometry import shape
    out = []
    for n in names:
        p = anef_dir / n
        if not p.exists():
            continue
        for f in json.loads(p.read_text()).get("features", []):
            a = _anef_of(f.get("properties") or {})
            if a is None or not f.get("geometry"):
                continue
            out.append((a, shape(f["geometry"]).buffer(0)))
    return out


def band_cells(city: aas.CityGrid, contours, ring_km=3.0):
    from shapely import contains_xy
    from shapely.ops import unary_union
    ii, jj = np.meshgrid(np.arange(city.ny), np.arange(city.nx), indexing="ij")
    lat = city.lat0 + ii * city.dlat
    lng = city.lon0 + jj * city.dlon
    band = np.full(lat.shape, -1, np.int16)
    for a, g in sorted(contours, key=lambda t: t[0]):
        inside = contains_xy(g, lng, lat)
        band[inside] = np.maximum(band[inside], a)
    outer = unary_union([g for _, g in contours])
    deg = ring_km / 111.0
    ring = contains_xy(outer.buffer(deg), lng, lat) & (band < 0)
    band[ring] = 0
    return band


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--anef-dir", required=True, type=Path)
    ap.add_argument("--json-out", type=Path)
    a = ap.parse_args()
    st = aas.get_store()
    plan = {
        "melbourne": ["vic_27.geojson", "vic_22.geojson"],
        "perth": ["wa_77.geojson"],
        "brisbane": ["bne.geojson"],
    }
    report = {}
    for city_key, files in plan.items():
        c = st.cities.get(city_key)
        contours = load_contours(a.anef_dir, files)
        if c is None or not contours:
            print(f"{city_key}: skipped (city or contours missing)")
            continue
        band = band_cells(c, contours)
        print(f"\n=== {city_key} (coverage {c.coverage}) ===")
        print(f"{'band':>6} {'cells':>6} {'est_p25':>8} {'est_med':>8} {'est_p75':>8} "
              f"{'ref_as':>7} {'med-ref_as':>10} {'ref_eng':>8} {'med-ref_eng':>11} {'>=floor':>8}")
        rows = []
        for b in sorted(set(band[band >= 0].tolist())):
            idx = np.argwhere(band == b)
            ests = []
            for i, j in idx:
                e = aan.estimate_lden(c.cell_values(int(i), int(j)), c.coverage)
                ests.append(e["lden_db"] if e else float("nan"))
            ests = np.array(ests, float)
            ok = ests[np.isfinite(ests)]
            if not ok.size:
                continue
            p25, med, p75 = np.percentile(ok, [25, 50, 75])
            ref_as = 35 + b if b else None
            ref_eng = (35 + _anef_to_penalty(b) + aan.LDEN_MINUS_FLAT_LEQ) if b else None
            frac = float((ok >= aan.APPLY_FLOOR_DB).mean())
            label = f"{b}+" if b else "<20"
            print(f"{label:>6} {len(idx):>6} {p25:8.1f} {med:8.1f} {p75:8.1f} "
                  f"{(ref_as or float('nan')):7.1f} {(med - ref_as) if ref_as else float('nan'):10.1f} "
                  f"{(ref_eng or float('nan')):8.1f} {(med - ref_eng) if ref_eng else float('nan'):11.1f} "
                  f"{frac:8.0%}")
            rows.append(dict(band=b, cells=int(len(idx)), p25=round(p25, 1), median=round(med, 1),
                             p75=round(p75, 1), ref_as2021=ref_as,
                             ref_engine=round(ref_eng, 1) if ref_eng else None,
                             share_above_floor=round(frac, 3)))
        report[city_key] = rows
    if a.json_out:
        a.json_out.write_text(json.dumps(report, indent=1))


if __name__ == "__main__":
    main()
