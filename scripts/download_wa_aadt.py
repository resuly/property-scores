"""Download Main Roads WA "Traffic Digest" AADT and write data/aadt_wa.parquet.

Source: Main Roads WA OpenData RoadAssets_DataPortal, layer 27 (Traffic Digest),
public ArcGIS MapServer, no auth. Point sites with Mon-Sun average daily traffic
(MON_SUN = AADT) and heavy-vehicle percent (PCT_HEAVY_MON_SUN).

Consumed by aadt_near()'s aadt_*.parquet glob. Latest year kept per site.
(Was previously appended into nfdh_aadt_national.parquet; now a first-class
measured-AADT layer like VIC.)
"""

import json
import os
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from property_scores.common.aadt_build import write_aadt_parquet, data_out, clamp_hv  # noqa: E402

LAYER = ("https://gisservices.mainroads.wa.gov.au/arcgis/rest/services/OpenData/"
         "RoadAssets_DataPortal/MapServer/27/query")
PAGE = 2000


def _get(params, retries=4):
    url = LAYER + "?" + urllib.parse.urlencode(params)
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "property-scores/wa-aadt"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"WA request failed: {last}")


def fetch_all():
    # one record per site, keep highest TRAFFIC_YEAR
    best = {}
    offset = 0
    while True:
        d = _get({
            "where": "MON_SUN > 0",
            "outFields": "SITE_NO,ROAD_NAME,MON_SUN,PCT_HEAVY_MON_SUN,TRAFFIC_YEAR",
            "outSR": "4326",
            "orderByFields": "OBJECTID",
            "resultOffset": offset,
            "resultRecordCount": PAGE,
            "f": "geojson",
        })
        feats = d.get("features", [])
        if not feats:
            break
        for ft in feats:
            a = ft.get("properties", {})
            geom = ft.get("geometry") or {}
            coords = geom.get("coordinates")
            if not coords or len(coords) < 2:
                continue
            aadt = a.get("MON_SUN")
            if not aadt or aadt <= 0:
                continue
            site = a.get("SITE_NO")
            yr = a.get("TRAFFIC_YEAR") or 0
            key = site if site is not None else (round(coords[0], 6), round(coords[1], 6))
            if key in best and best[key][0] >= yr:
                continue
            best[key] = (yr, {
                "aadt": int(aadt),
                "hv_pct": clamp_hv(a.get("PCT_HEAVY_MON_SUN")),
                "road_name": (a.get("ROAD_NAME") or "").strip() or None,
                "wkt": f"POINT ({coords[0]} {coords[1]})",
            })
        offset += PAGE
        print(f"  fetched {offset} ({len(best)} sites)", flush=True)
        if len(feats) < PAGE:
            break
    return [v[1] for v in best.values()]


def main():
    rows = fetch_all()
    if not rows:
        print("ERROR: no WA rows", file=sys.stderr)
        return 2
    out = data_out("aadt_wa.parquet")
    n = write_aadt_parquet(rows, out)
    print(f"wrote {n} rows -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
