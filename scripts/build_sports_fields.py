#!/usr/bin/env python3
"""Build au_sports_fields.parquet from OSM leisure polygons via Overpass.

Council ovals and sports grounds are OSM leisure=* POLYGONS, not commercial
POIs, so Overture places miss most of them ("No sports ovals near us, but its
two houses away", 2026-06-10). This pulls way/relation centroids per state
(out center, tiny responses) for the walkability "sports" scenario.

Output columns: name, lat, lng, leisure, sport, state
Usage: .venv/bin/python scripts/build_sports_fields.py [--states nsw,vic]
"""
import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]

# coarse state bboxes (south,west,north,east); overlap is fine, dedupe below
BBOXES = {
    "nsw": (-37.6, 140.9, -28.1, 153.7),
    "vic": (-39.3, 140.9, -33.9, 150.2),
    "qld": (-29.2, 137.9, -9.9, 153.6),
    "sa":  (-38.2, 129.0, -25.9, 141.1),
    "wa":  (-35.2, 112.9, -13.6, 129.1),
    "tas": (-43.8, 143.5, -39.5, 148.6),
    "nt":  (-26.1, 129.0, -10.9, 138.1),
    "act": (-36.0, 148.7, -35.1, 149.4),
}

LEISURE = ["pitch", "sports_centre", "stadium", "recreation_ground"]


def _query(state: str, bbox: tuple) -> list[dict]:
    s, w, n, e = bbox
    regex = "|".join(LEISURE)
    q = (f'[out:json][timeout:170];('
         f'way["leisure"~"^({regex})$"]({s},{w},{n},{e});'
         f'relation["leisure"~"^({regex})$"]({s},{w},{n},{e});'
         f');out center tags;')
    for mirror in MIRRORS:
        try:
            req = urllib.request.Request(
                mirror, data=urllib.parse.urlencode({"data": q}).encode(),
                headers={"User-Agent": "property-scores/1.0"})
            with urllib.request.urlopen(req, timeout=200) as r:
                data = json.load(r)
            out = []
            for el in data.get("elements", []):
                center = el.get("center") or {}
                lat, lng = center.get("lat"), center.get("lon")
                if lat is None or lng is None:
                    continue
                tags = el.get("tags", {})
                sport = tags.get("sport", "")
                name = tags.get("name") or (
                    f"{sport.split(';')[0].replace('_', ' ').title()} field"
                    if sport else "Sports field")
                out.append({"name": name, "lat": lat, "lng": lng,
                            "leisure": tags.get("leisure", ""),
                            "sport": sport, "state": state})
            return out
        except Exception as e:
            print(f"  {state} via {mirror.split('/')[2]} failed: {e}", file=sys.stderr)
            time.sleep(5)
    return []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--states", default=",".join(BBOXES))
    args = ap.parse_args()

    out_path = Path(__file__).resolve().parents[1] / "data" / "au_sports_fields.parquet"
    rows: list[dict] = []
    for state in [s.strip() for s in args.states.split(",") if s.strip()]:
        print(f"Querying {state}...", file=sys.stderr)
        got = _query(state, BBOXES[state])
        print(f"  {state}: {len(got)} fields", file=sys.stderr)
        rows.extend(got)
        time.sleep(8)

    # dedupe: state bboxes overlap + adjacent courts cluster (~11 m grid)
    best: dict[tuple, dict] = {}
    for r in rows:
        key = (round(r["lat"], 4), round(r["lng"], 4))
        if key not in best or (best[key]["name"] == "Sports field" and r["name"] != "Sports field"):
            best[key] = r
    deduped = list(best.values())
    print(f"Total: {len(rows)} raw, {len(deduped)} deduped", file=sys.stderr)
    if not deduped:
        sys.exit("no fields extracted, refusing to write empty parquet")

    import pandas as pd
    pd.DataFrame(deduped).to_parquet(out_path, index=False)
    print(pd.DataFrame(deduped).groupby("state").size(), file=sys.stderr)
    print(f"Saved {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
