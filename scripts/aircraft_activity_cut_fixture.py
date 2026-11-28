"""Cut a small test fixture out of the REAL aircraft-activity grids.

    .venv/bin/python scripts/aircraft_activity_cut_fixture.py \
        --src ~/limon-data/aircraft-activity-12m --out tests/fixtures/aircraft_activity_20261128

Tests must read bytes produced by the real pipeline, not arrays written by the
same hands as the reader (a hand-made fixture shares the reader's assumptions
about axis order and cell centres; see the 2026-10-08 tile incident). Each
city is cropped to a window of whole cells; lat0/lon0 are moved to the
window's first cell centre and every other byte is copied as is, so a lookup
in the fixture must return exactly what the full grid returns for the same
coordinate. The window index is recorded in the meta ("fixture_window").
Contents are an extract of the adsb.lol-derived database (ODbL 1.0).
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np

# city -> (centre lat, lng, half-size in cells)
WINDOWS = {
    "melbourne": (-37.7184, 144.847, 6),   # Keilor Park, Melbourne Airport corridor
    "brisbane": (-27.40, 153.08, 3),       # large_aircraft_only city
    "hobart": (-42.88, 147.33, 3),         # insufficient city
}


def cut(src: Path, out: Path, city: str, lat: float, lng: float, half: int):
    with np.load(src / f"grid_{city}.npz") as z:
        lat0, lon0 = float(z["lat0"]), float(z["lon0"])
        dlat, dlon = float(z["dlat"]), float(z["dlon"])
        ci = int(math.floor((lat - lat0) / dlat + 0.5))
        cj = int(math.floor((lng - lon0) / dlon + 0.5))
        i0, j0 = ci - half, cj - half
        n = 2 * half + 1
        payload = {}
        for k in z.files:
            a = z[k]
            if a.ndim >= 2:
                payload[k] = a[i0:i0 + n, j0:j0 + n].copy()
        payload.update(lat0=np.float64(lat0 + i0 * dlat), lon0=np.float64(lon0 + j0 * dlon),
                       dlat=np.float64(dlat), dlon=np.float64(dlon),
                       ny=np.int64(n), nx=np.int64(n))
    out.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(out / f"grid_{city}.npz", **payload)
    for q in ("q1", "q2", "q3", "q4"):
        f = src / f"grid_{city}_{q}.npz"
        if f.exists():
            with np.load(f) as z:
                qp = {k: z[k][i0:i0 + n, j0:j0 + n].copy() for k in z.files if z[k].ndim >= 2}
                qp.update({k: payload[k] for k in ("lat0", "lon0", "dlat", "dlon", "ny", "nx")})
            np.savez_compressed(out / f"grid_{city}_{q}.npz", **qp)
    meta = json.loads((src / f"meta_{city}.json").read_text())
    meta["fixture_window"] = {"i0": i0, "j0": j0, "n": n}
    (out / f"meta_{city}.json").write_text(json.dumps(meta, indent=1))
    hp = src / f"helipads_{city}.json"
    if hp.exists():
        # Only landing sites within 10 km of the window centre (whole records).
        keep = [h for h in json.loads(hp.read_text())
                if math.hypot((h["lat"] - lat) * 111.132,
                              (h["lon"] - lng) * 111.32 * math.cos(math.radians(lat))) <= 10]
        (out / f"helipads_{city}.json").write_text(json.dumps(keep, indent=1))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    a = ap.parse_args()
    for city, (lat, lng, half) in WINDOWS.items():
        cut(a.src.expanduser(), a.out, city, lat, lng, half)
        print("cut", city)


if __name__ == "__main__":
    main()
