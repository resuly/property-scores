"""
Download NFDH Harmonised Traffic Counts → parquet
==================================================
Source: spatial.infrastructure.gov.au  (Dept of Infrastructure, Transport)
API guide: https://catalogue.data.infrastructure.gov.au/dataset/harmonised-traffic-counts

Strategy:
  1. Download ALL calendar-year records (NSW, VIC, TAS, QLD) from Layer 1
  2. Download SA-only financial-year records from Layer 2 (SA not in calendar-year)
  3. For each station+direction, keep only the MOST RECENT year
  4. Aggregate both directions into a single AADT per station where possible
  5. Compute heavy_vehicle_pct from 02-bin / 04-bin / 12-bin counters
  6. Save to parquet with point geometry as WKT

Output columns:
  station_id, station_name, road_name, state, lon, lat, geometry_wkt,
  aadt, heavy_vehicle_pct, year, counter_type, direction, source_data
"""

import json
import math
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

import pandas as pd
import requests

BASE = "https://spatial.infrastructure.gov.au/server/rest/services/Hosted/Harmonised_Traffic_Counts/FeatureServer"
PAGE_SIZE = 2000  # server max
OUT_PATH = Path("D:/property-scores-data/nfdh_aadt_national.parquet")

FIELDS_CALENDAR = [
    "esri_oid", "id", "state", "clientid", "src_stnname", "src_stnid",
    "stn_countername", "ctr_id", "roadname", "counter_type", "direction",
    "source_data", "temporal_period",
    "bins01_class01to12", "bins02_class01to02", "bins02_class03to12",
]


def query_layer(layer_id: int, where: str = "1=1", fields: list[str] | None = None) -> list[dict]:
    """Paginate through an ArcGIS FeatureServer layer and return all features."""
    url = f"{BASE}/{layer_id}/query"
    out_fields = ",".join(fields) if fields else "*"
    all_features = []
    offset = 0

    # First get total count
    params = {"where": where, "returnCountOnly": "true", "f": "json"}
    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    total = resp.json().get("count", 0)
    print(f"  Layer {layer_id} | where={where!r} → {total:,} features")

    while offset < total:
        params = {
            "where": where,
            "outFields": out_fields,
            "resultOffset": offset,
            "resultRecordCount": PAGE_SIZE,
            "returnGeometry": "true",
            "f": "json",
        }
        for attempt in range(3):
            try:
                resp = requests.get(url, params=params, timeout=60)
                resp.raise_for_status()
                data = resp.json()
                break
            except Exception as e:
                if attempt == 2:
                    raise
                print(f"    Retry {attempt+1} at offset {offset}: {e}")
                time.sleep(2 * (attempt + 1))

        features = data.get("features", [])
        if not features:
            break

        for feat in features:
            row = feat["attributes"]
            geom = feat.get("geometry", {})
            row["lon"] = geom.get("x")
            row["lat"] = geom.get("y")
            all_features.append(row)

        offset += len(features)
        pct = min(100, offset * 100 / total)
        print(f"    {offset:,} / {total:,}  ({pct:.0f}%)")

    return all_features


def extract_year(period_str: str) -> int | None:
    """Extract year from 'YYYY Calendar Year' or 'YYYY-1/YYYY Financial Year'."""
    if not period_str:
        return None
    parts = period_str.split()
    if "Calendar" in period_str:
        try:
            return int(parts[0])
        except ValueError:
            return None
    elif "Financial" in period_str:
        # "2023-1/2024 Financial Year" → 2024 (the ending year)
        try:
            fy = parts[0]  # "2023-1/2024"
            return int(fy.split("/")[-1])
        except (ValueError, IndexError):
            return None
    return None


def compute_aadt(total_vehicles, year, is_financial_year: bool = False):
    """Compute AADT from total vehicle count for a year."""
    if pd.isna(total_vehicles) or pd.isna(year):
        return None
    total_vehicles = float(total_vehicles)
    year = int(year)
    import calendar
    if is_financial_year:
        days = 365
    else:
        days = 366 if calendar.isleap(year) else 365
    return int(round(total_vehicles / days))


def main():
    print("=" * 60)
    print("NFDH Harmonised Traffic Counts → Parquet")
    print("=" * 60)

    # ── 1. Download calendar-year data (NSW, VIC, TAS, QLD) ──
    print("\n[1/2] Calendar Year layer (NSW, VIC, TAS, QLD)...")
    cal_features = query_layer(1, where="1=1", fields=FIELDS_CALENDAR)
    df_cal = pd.DataFrame(cal_features)
    df_cal["data_source"] = "calendar_year"
    print(f"  → {len(df_cal):,} raw records")

    # ── 2. Download financial-year SA data ──
    print("\n[2/2] Financial Year layer (SA only)...")
    fy_features = query_layer(2, where="state='SA'", fields=FIELDS_CALENDAR)
    df_fy = pd.DataFrame(fy_features)
    df_fy["data_source"] = "financial_year"
    print(f"  → {len(df_fy):,} raw records")

    # ── Combine ──
    df = pd.concat([df_cal, df_fy], ignore_index=True)
    print(f"\nCombined: {len(df):,} records")
    print(f"States: {sorted(df['state'].dropna().unique())}")

    # ── Extract year ──
    df["year"] = df["temporal_period"].apply(extract_year)
    df["is_fy"] = df["data_source"] == "financial_year"

    # ── Compute AADT per record ──
    df["aadt"] = df.apply(
        lambda r: compute_aadt(r["bins01_class01to12"], r["year"], r["is_fy"]),
        axis=1,
    )

    # ── Heavy vehicle percentage ──
    # Only available for 02-bin, 04-bin, 12-bin counters
    df["heavy_vehicle_pct"] = None
    mask = df["bins02_class01to02"].notna() & df["bins02_class03to12"].notna()
    total_02 = df.loc[mask, "bins02_class01to02"] + df.loc[mask, "bins02_class03to12"]
    df.loc[mask & (total_02 > 0), "heavy_vehicle_pct"] = (
        df.loc[mask & (total_02 > 0), "bins02_class03to12"] / total_02[total_02 > 0] * 100
    ).round(1)

    # ── Keep most recent year per station+direction+counter_type ──
    # Use ctr_id as the unique counter identifier, fallback to src_stnid+direction
    df["station_key"] = df.apply(
        lambda r: f"{r['state']}_{r['ctr_id']}" if pd.notna(r['ctr_id']) else f"{r['state']}_{r['src_stnid']}_{r['direction']}",
        axis=1,
    )

    print(f"\nBefore dedup: {len(df):,} records")
    print(f"Unique station keys: {df['station_key'].nunique():,}")

    # Sort by year desc, keep first (most recent)
    df = df.sort_values("year", ascending=False)
    df_latest = df.drop_duplicates(subset=["station_key", "counter_type"], keep="first").copy()
    print(f"After keeping latest year per station+counter_type: {len(df_latest):,}")

    # ── For stations with multiple counter_types, prefer the most detailed one ──
    # Priority: 12-bin > 04-bin > 02-bin > 01-bin (more detail is better)
    type_priority = {"12-bin": 0, "04-bin": 1, "02-bin": 2, "01-bin": 3}
    df_latest.loc[:, "type_priority"] = df_latest["counter_type"].map(type_priority).fillna(9)
    df_latest = df_latest.sort_values("type_priority")
    df_best = df_latest.drop_duplicates(subset=["station_key"], keep="first").copy()
    print(f"After picking best counter_type per station: {len(df_best):,}")

    # ── Build geometry WKT ──
    df_best.loc[:, "geometry_wkt"] = df_best.apply(
        lambda r: f"POINT ({r['lon']} {r['lat']})" if pd.notna(r['lon']) and pd.notna(r['lat']) else None,
        axis=1,
    )

    # ── Select and rename output columns ──
    out = df_best.rename(columns={
        "src_stnname": "station_name",
        "src_stnid": "station_id",
        "roadname": "road_name",
    })[[
        "station_id", "station_name", "road_name", "state",
        "lon", "lat", "geometry_wkt",
        "aadt", "heavy_vehicle_pct", "year",
        "counter_type", "direction", "source_data",
        "clientid", "ctr_id",
    ]].copy()

    # ── Summary stats ──
    print(f"\n{'='*60}")
    print(f"FINAL OUTPUT: {len(out):,} stations")
    print(f"\nPer state:")
    for state, group in out.groupby("state"):
        aadt_valid = group["aadt"].notna().sum()
        hv_valid = group["heavy_vehicle_pct"].notna().sum()
        years = sorted(group["year"].dropna().unique())
        yr_range = f"{int(min(years))}-{int(max(years))}" if years else "?"
        print(f"  {state:5s}: {len(group):6,} stations | {aadt_valid:5,} with AADT | {hv_valid:5,} with HV% | years {yr_range}")

    print(f"\nAADT stats:")
    print(out["aadt"].describe())

    print(f"\nHeavy vehicle % stats (where available):")
    hv = out["heavy_vehicle_pct"].dropna()
    if len(hv):
        print(hv.describe())

    # ── Save ──
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    out.to_parquet(OUT_PATH, index=False, engine="pyarrow")
    size_mb = OUT_PATH.stat().st_size / 1024 / 1024
    print(f"\nSaved → {OUT_PATH}  ({size_mb:.1f} MB)")
    print(f"Columns: {list(out.columns)}")


if __name__ == "__main__":
    main()
