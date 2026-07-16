"""Pre-compute BOM 2016 IFD design-rainfall grid for Australia (1% AEP).

Replaces the ERA5-via-Open-Meteo P95 grid (precompute_era5_p95.py). BOM IFD is
the ARR 2019 national standard design rainfall, licensed CC BY 4.0 (commercial
use + redistribution OK with attribution), so it is clean for the commercial
API — unlike the Open-Meteo free tier (non-commercial ToS).

Data source: BOM Design Rainfall Data System, undocumented multipoint endpoint
the web tool posts to. Native grid 0.025 deg (~2.5 km); we sample a coarser
grid (default 0.25 deg) point-by-point. 1% AEP is a native column.

Attribution (CC BY 4.0), carry into product methodology page:
  "Bureau of Meteorology, (c) Commonwealth of Australia. Licensed under CC BY 4.0."

Output: data/bom_ifd_1pct.parquet (lat, lng, ifd_1pct_1h_mm, ifd_1pct_6h_mm,
        ifd_1pct_24h_mm)
Usage:  python scripts/precompute_bom_ifd.py [--step 0.25] [--test]
"""
import argparse
import json
import re
import sys
import time
import urllib.parse

import numpy as np
import pandas as pd
import requests

ENDPOINT = "https://www.bom.gov.au/water/designRainfalls/revised-ifd/?multipoint"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "Referer": "https://www.bom.gov.au/water/designRainfalls/revised-ifd/",
    "X-Requested-With": "XMLHttpRequest",
}

# duration row id in the IFD HTML table -> our column. ifdDur<minutes>.
# 60 = 1h (flash), 360 = 6h, 1440 = 24h (riverine/daily analog).
DURATIONS = {"ifd_1pct_1h_mm": 60, "ifd_1pct_6h_mm": 360, "ifd_1pct_24h_mm": 1440}

AU_LAT_RANGE = None  # set from --step in main()
AU_LNG_RANGE = None


def _parse_1pct(html: str, dur_min: int) -> float | None:
    """1% AEP depth (mm) for a duration row. 1% AEP is the last (7th) AEP td."""
    m = re.search(rf'id="ifdDur{dur_min}"[^>]*>(.*?)</tr>', html, re.S)
    if not m:
        return None
    cells = re.findall(r'<td[^>]*>(.*?)</td>', m.group(1), re.S)
    vals = [re.sub(r'<[^>]+>', '', c).strip() for c in cells]
    nums = [v for v in vals if re.fullmatch(r'[\d.]+', v)]
    if len(nums) < 7:
        return None
    try:
        return float(nums[6])  # 63.2/50/20/10/5/2/1% -> index 6 = 1% AEP
    except ValueError:
        return None


def fetch_ifd(lat: float, lng: float) -> dict | None:
    multi = urllib.parse.quote(json.dumps([[f"{lat:.4f}", f"{lng:.4f}"]]))
    body = (f"coordinate_type=dd&latitude={lat}&longitude={lng}"
            f"&multi={multi}&sdmin=true&sdhr=true")
    r = requests.post(ENDPOINT, data=body, headers=HEADERS, timeout=60)
    r.raise_for_status()
    html = r.text
    out = {}
    for col, dur in DURATIONS.items():
        out[col] = _parse_1pct(html, dur)
    if all(v is None for v in out.values()):
        return None  # ocean / outside AU coverage
    return out


def _fetch_retry(lat, lng, tries=4):
    for attempt in range(tries):
        try:
            return fetch_ifd(lat, lng)
        except Exception as e:
            if attempt < tries - 1:
                time.sleep(2 ** attempt)
                continue
            print(f"  ({lat:.2f},{lng:.2f}) failed: {e}", file=sys.stderr)
            return None


def run_test():
    """Verify the parser against agent-confirmed values."""
    cases = [
        (-33.815, 151.00, "Parramatta", 58.8),   # 1h 1% AEP verified = 58.8mm
        (-37.8136, 144.9631, "Melbourne CBD", None),
        (-27.4705, 153.0260, "Brisbane CBD", None),
        (-25.0, 135.0, "Simpson Desert (dry inland)", None),
        (-40.0, 148.0, "Bass Strait (ocean)", None),
    ]
    for lat, lng, name, expect in cases:
        d = _fetch_retry(lat, lng)
        v = d.get("ifd_1pct_1h_mm") if d else None
        tag = ""
        if expect is not None:
            tag = " OK" if v and abs(v - expect) < 0.5 else f" EXPECTED ~{expect} !!"
        print(f"  {name:28s} 1h 1%AEP = {v} mm  6h={d.get('ifd_1pct_6h_mm') if d else None} "
              f"24h={d.get('ifd_1pct_24h_mm') if d else None}{tag}")
        time.sleep(0.8)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--step", type=float, default=0.25, help="grid step in degrees")
    ap.add_argument("--test", action="store_true", help="parser sanity check only")
    ap.add_argument("--sleep", type=float, default=0.3)
    ap.add_argument("--workers", type=int, default=3)
    args = ap.parse_args()

    if args.test:
        run_test()
        return

    lats = np.arange(-44.0, -9.0, args.step)
    lngs = np.arange(112.0, 154.0, args.step)
    grid = [(round(float(la), 4), round(float(lo), 4)) for la in lats for lo in lngs]
    total = len(grid)
    print(f"Grid: {total} points at {args.step} deg "
          f"({len(lats)} lat x {len(lngs)} lng). Ocean/desert points return null.")

    from concurrent.futures import ThreadPoolExecutor
    rows = []
    t0 = time.time()
    hits = 0
    done = 0

    def work(pt):
        lat, lng = pt
        d = _fetch_retry(lat, lng)
        time.sleep(args.sleep)
        row = {"lat": lat, "lng": lng,
               "ifd_1pct_1h_mm": None, "ifd_1pct_6h_mm": None, "ifd_1pct_24h_mm": None}
        if d:
            row.update(d)
        return row, bool(d)

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        for row, hit in ex.map(work, grid):
            rows.append(row)
            hits += hit
            done += 1
            if done % 200 == 0 or done == total:
                el = time.time() - t0
                eta = el / done * (total - done)
                print(f"  {done}/{total} ({done*100//total}%) — {hits} land hits — "
                      f"{el:.0f}s, ~{eta:.0f}s left", flush=True)
                # incremental save so a mid-run crash keeps progress
                pd.DataFrame(rows).to_parquet("data/bom_ifd_1pct.parquet", index=False)

    df = pd.DataFrame(rows)
    df.to_parquet("data/bom_ifd_1pct.parquet", index=False)
    valid = df.dropna(subset=["ifd_1pct_1h_mm"])
    print(f"\nDone: {len(valid)} land points with IFD, saved data/bom_ifd_1pct.parquet")
    if len(valid):
        print(f"1h 1% AEP range: {valid['ifd_1pct_1h_mm'].min():.1f} - "
              f"{valid['ifd_1pct_1h_mm'].max():.1f} mm")


if __name__ == "__main__":
    main()
