#!/usr/bin/env python3
"""Anchor sweep: score a stratified AU address sample to re-anchor scales.

Re-anchoring input for heat-island / view-quality / solar (2026-06 audit
#34): the current 0-100 mappings were never calibrated against the actual
Australian distribution (Paddington QLD reads 13/100 "Extreme Heat";
Brisbane PVOUT 1585 reads "Moderate"). This sweep produces that
distribution so anchor choices can be reviewed instead of guessed.

Input CSV columns: state,suburb,lat,lng (header required).
Output CSV: one row per address with score + the raw driver fields,
appended incrementally; already-scored (lat,lng) pairs are skipped on
re-run, so the sweep is resumable.

Usage (on the box that runs the API, hitting localhost):
  python3 scripts/anchor_sweep.py --sample /tmp/anchor_sample.csv \
      --out /tmp/anchor_sweep_results.csv [--base http://127.0.0.1:8099]
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
import urllib.request
from pathlib import Path

HEAT_PATH = "/scores/heat-island"
VIEW_PATH = "/scores/view-quality"
SOLAR_PATH = "/scores/solar"


def _get(base: str, path: str, lat: float, lng: float, timeout: int = 90) -> dict | None:
    url = f"{base}{path}?lat={lat}&lng={lng}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f"  ERR {path} {lat},{lng}: {e}", file=sys.stderr)
        return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--base", default="http://127.0.0.1:8099")
    ap.add_argument("--sleep", type=float, default=0.5,
                    help="pause between addresses; keep the box responsive")
    args = ap.parse_args()

    rows = list(csv.DictReader(open(args.sample)))
    out_path = Path(args.out)
    done: set[tuple[str, str]] = set()
    if out_path.exists():
        for r in csv.DictReader(open(out_path)):
            done.add((r["lat"], r["lng"]))

    fields = ["state", "suburb", "lat", "lng",
              "heat_score", "heat_label", "heat_uhi_delta_c", "heat_lst_c", "heat_source",
              "view_score", "view_label", "view_degraded",
              "solar_score", "solar_label", "solar_pvout"]
    new_file = not out_path.exists()
    fh = open(out_path, "a", newline="")
    w = csv.DictWriter(fh, fieldnames=fields)
    if new_file:
        w.writeheader()

    for i, r in enumerate(rows, 1):
        if (r["lat"], r["lng"]) in done:
            continue
        lat, lng = float(r["lat"]), float(r["lng"])
        heat = _get(args.base, HEAT_PATH, lat, lng) or {}
        view = _get(args.base, VIEW_PATH, lat, lng) or {}
        solar = _get(args.base, SOLAR_PATH, lat, lng) or {}
        w.writerow({
            "state": r["state"], "suburb": r["suburb"],
            "lat": r["lat"], "lng": r["lng"],
            "heat_score": heat.get("score"),
            "heat_label": heat.get("label"),
            "heat_uhi_delta_c": heat.get("uhi_delta_c"),
            "heat_lst_c": heat.get("modis_lst_c"),
            "heat_source": heat.get("source"),
            "view_score": view.get("score"),
            "view_label": view.get("label"),
            "view_degraded": view.get("degraded"),
            "solar_score": solar.get("score"),
            "solar_label": solar.get("label"),
            "solar_pvout": solar.get("pvout_kwh_kwp_year"),
        })
        fh.flush()
        print(f"[{i}/{len(rows)}] {r['state']} {r['suburb']}: "
              f"heat={heat.get('score')} view={view.get('score')} solar={solar.get('score')}",
              file=sys.stderr)
        time.sleep(args.sleep)

    fh.close()
    print(f"done -> {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
