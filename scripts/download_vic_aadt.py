"""
Download VIC declared-road AADT (traffic volume) and write the ground-truth
parquet consumed by property_scores.common.overture.aadt_near().

Source: DataVic / Department of Transport and Planning "Traffic Volume" layer,
public ArcGIS Online hosted FeatureServer (no auth, CC-BY 4.0, data year 2020).
~74k polyline segments across the Victorian declared road network (freeways +
arterials) — the layer that lets the noise model tell a busy arterial from a
quiet back street.

Output schema (exactly what aadt_near's SQL requires):
  aadt       INTEGER   all-vehicles AADT for the segment (ALLVEHS_AA)
  hv_pct     DOUBLE    heavy-vehicle FRACTION 0..1 (NOT percent — score.py *100)
  road_name  VARCHAR
  geometry   GEOMETRY  native LINESTRING/POINT in WGS84
  xmin,ymin  DOUBLE    flat bbox min-corner (segment), bbox prefilter columns

Run:  .venv/bin/python scripts/download_vic_aadt.py
"""

import json
import os
import sys
import time
import urllib.parse
import urllib.request

import duckdb

LAYER = ("https://services2.arcgis.com/18ajPSI0b3ppsmMt/ArcGIS/rest/services/"
         "Traffic_Volume/FeatureServer/0/query")
PAGE = 2000
OUT_FILE = "aadt_vic.parquet"  # read by aadt_near()'s aadt_*.parquet glob


def _data_dir() -> str:
    d = os.environ.get("DATA_DIR")
    if d:
        return d
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(here, "data")


def _get(params: dict, retries: int = 4) -> dict:
    url = LAYER + "?" + urllib.parse.urlencode(params)
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "property-scores/vic-aadt"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"request failed after {retries} tries: {last}")


def _count() -> int:
    d = _get({"where": "ALLVEHS_AA > 0", "returnCountOnly": "true", "f": "json"})
    return int(d.get("count", 0))


def _coords_to_wkt(geom: dict) -> str | None:
    """esri geojson geometry -> WKT (LINESTRING / MULTILINESTRING / POINT)."""
    if not geom:
        return None
    gtype = geom.get("type")
    coords = geom.get("coordinates")
    if not coords:
        return None

    def pt(c):
        return f"{c[0]} {c[1]}"

    if gtype == "LineString":
        pts = [c for c in coords if len(c) >= 2]
        if len(pts) >= 2:
            return "LINESTRING (" + ", ".join(pt(c) for c in pts) + ")"
        if len(pts) == 1:
            return f"POINT ({pt(pts[0])})"
        return None
    if gtype == "MultiLineString":
        parts = []
        for line in coords:
            pts = [c for c in line if len(c) >= 2]
            if len(pts) >= 2:
                parts.append("(" + ", ".join(pt(c) for c in pts) + ")")
        if parts:
            return "MULTILINESTRING (" + ", ".join(parts) + ")"
        # collapse to a single point if only stubs
        for line in coords:
            if line:
                return f"POINT ({pt(line[0])})"
        return None
    if gtype == "Point":
        return f"POINT ({pt(coords)})"
    return None


def fetch_all() -> list[dict]:
    total = _count()
    print(f"VIC Traffic_Volume segments with ALLVEHS_AA>0: {total}", flush=True)
    rows: list[dict] = []
    offset = 0
    while True:
        d = _get({
            "where": "ALLVEHS_AA > 0",
            "outFields": "ROAD_NAME,ALLVEHS_AA,TWO_WAY_AA,TWO_WAY__1",
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
            wkt = _coords_to_wkt(ft.get("geometry"))
            if wkt is None:
                continue
            allveh = a.get("ALLVEHS_AA")
            twoway = a.get("TWO_WAY_AA")
            hv_raw = a.get("TWO_WAY__1")  # two-way commercial/heavy AADT (derives HV fraction)
            aadt = allveh if (allveh and allveh > 0) else twoway
            if not aadt or aadt <= 0:
                continue
            # heavy-vehicle fraction as 0..1, clamped to a sane band; default 5%
            hv = 0.05
            try:
                if hv_raw and twoway and twoway > 0:
                    r = float(hv_raw) / float(twoway)
                    if 0.0 < r < 0.35:
                        hv = round(r, 4)
            except (TypeError, ValueError):
                pass
            rows.append({
                "aadt": int(aadt),
                "hv_pct": float(hv),
                "road_name": (a.get("ROAD_NAME") or "").strip() or None,
                "wkt": wkt,
            })
        offset += PAGE
        print(f"  fetched {offset} ({len(rows)} usable)", flush=True)
        if len(feats) < PAGE:
            break
    return rows


def write_parquet(rows: list[dict], out_path: str) -> None:
    con = duckdb.connect()
    con.execute("INSTALL spatial; LOAD spatial;")
    con.execute("""
        CREATE TABLE staging (
            aadt INTEGER, hv_pct DOUBLE, road_name VARCHAR, wkt VARCHAR
        )
    """)
    con.executemany(
        "INSERT INTO staging VALUES (?, ?, ?, ?)",
        [(r["aadt"], r["hv_pct"], r["road_name"], r["wkt"]) for r in rows],
    )
    tmp = out_path + ".tmp.parquet"
    con.execute(f"""
        COPY (
            WITH g AS (
                SELECT aadt, hv_pct, road_name, ST_GeomFromText(wkt) AS geometry
                FROM staging WHERE wkt IS NOT NULL
            )
            SELECT aadt, hv_pct, road_name, geometry,
                   ST_XMin(geometry) AS xmin,
                   ST_YMin(geometry) AS ymin
            FROM g
        ) TO '{tmp}' (FORMAT PARQUET)
    """)
    os.replace(tmp, out_path)
    n = con.execute(f"SELECT count(*) FROM read_parquet('{out_path}')").fetchone()[0]
    print(f"wrote {n} rows -> {out_path}", flush=True)


def main() -> int:
    rows = fetch_all()
    if not rows:
        print("ERROR: no rows fetched", file=sys.stderr)
        return 2
    out_path = os.path.join(_data_dir(), OUT_FILE)
    os.makedirs(_data_dir(), exist_ok=True)
    write_parquet(rows, out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
