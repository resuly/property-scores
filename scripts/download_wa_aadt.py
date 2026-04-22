"""Download Main Roads WA traffic digest data and append to NFDH national AADT."""

import json
import sys
import os
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pyarrow as pa
import pyarrow.parquet as pq
import requests

WA_ENDPOINT = (
    "https://gisservices.mainroads.wa.gov.au/arcgis/rest/services"
    "/OpenData/RoadAssets_DataPortal/MapServer/27/query"
)

NFDH_PATH = os.path.join(os.environ.get("DATA_DIR", "D:/property-scores-data"),
                          "nfdh_aadt_national.parquet")


def download_wa_aadt():
    all_features = []
    offset = 0
    batch_size = 2000

    while True:
        params = {
            "where": "TRAFFIC_YEAR='2024/25'",
            "outFields": "SITE_NO,ROAD_NAME,LOCATION_DESC,MON_SUN,PCT_HEAVY_MON_SUN,LG_NAME",
            "returnGeometry": "true",
            "outSR": "4326",
            "f": "json",
            "resultOffset": offset,
            "resultRecordCount": batch_size,
        }
        resp = requests.get(WA_ENDPOINT, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        features = data.get("features", [])
        if not features:
            break
        all_features.extend(features)
        print(f"  Downloaded {len(all_features)} WA stations...")
        offset += batch_size

        if len(features) < batch_size:
            break

    print(f"Total WA stations (2024/25): {len(all_features)}")
    return all_features


def features_to_rows(features):
    rows = []
    for feat in features:
        attrs = feat.get("attributes", {})
        geom = feat.get("geometry", {})

        aadt = attrs.get("MON_SUN")
        if not aadt or aadt <= 0:
            continue

        rows.append({
            "station_id": str(attrs.get("SITE_NO", "")),
            "station_name": attrs.get("LOCATION_DESC", ""),
            "road_name": attrs.get("ROAD_NAME", ""),
            "state": "WA",
            "lon": geom.get("x"),
            "lat": geom.get("y"),
            "geometry_wkt": f"POINT ({geom.get('x')} {geom.get('y')})" if geom.get("x") else None,
            "aadt": float(aadt),
            "heavy_vehicle_pct": float(attrs.get("PCT_HEAVY_MON_SUN") or 0),
            "year": 2025,
            "counter_type": "Class",
            "direction": None,
            "source_data": "mainroads_wa",
            "clientid": "mainroads_wa",
            "ctr_id": 0,
        })
    return rows


def merge_with_nfdh(wa_rows):
    existing = pq.read_table(NFDH_PATH)
    existing_states = set(existing.column("state").to_pylist())
    print(f"Existing NFDH: {len(existing)} rows, states: {existing_states}")

    if "WA" in existing_states:
        print("WA data already in NFDH — removing old WA rows first")
        mask = pa.compute.not_equal(existing.column("state"), "WA")
        existing = existing.filter(mask)
        print(f"After removing old WA: {len(existing)} rows")

    wa_dict = {col: [r[col] for r in wa_rows] for col in existing.column_names}
    wa_table = pa.table(wa_dict).cast(existing.schema)
    combined = pa.concat_tables([existing, wa_table])
    pq.write_table(combined, NFDH_PATH)
    print(f"Combined: {len(combined)} rows")

    state_counts = {}
    for s in combined.column("state").to_pylist():
        state_counts[s] = state_counts.get(s, 0) + 1
    for s, n in sorted(state_counts.items()):
        print(f"  {s}: {n}")


if __name__ == "__main__":
    print("Downloading Main Roads WA AADT (2024/25)...")
    features = download_wa_aadt()

    print("Converting to rows...")
    rows = features_to_rows(features)
    print(f"Valid WA rows: {len(rows)}")

    print("Merging with NFDH national dataset...")
    merge_with_nfdh(rows)
    print("Done.")
