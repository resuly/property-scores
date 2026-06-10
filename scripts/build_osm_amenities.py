#!/usr/bin/env python3
"""Build au_osm_amenities.parquet: public amenities Overture misses.

Overture places are commercial POIs ("things with a Facebook page"), so
public infrastructure recall is full of holes (2026-06-11 audit: playground
26% miss, dog park 44%, pool 39%; Brunswick Baths' own front door showed
"Swimming Pool ... Not found within 1.5km"). OSM is the system of record
for these:

  playground      leisure=playground
  dog_park        leisure=dog_park
  swimming_pool   leisure=sports_centre/water_park with sport~swimming, plus
                  leisure=swimming_pool only when access is public/customers
                  or it carries a name (bare unnamed swimming_pool ways are
                  overwhelmingly private backyard pools and would flood the
                  category with false positives)
  beach           natural=beach (also fixes the mislocated Overture beach POIs)

Reuses the per-state bbox + mirror fallback pattern from build_sports_fields
(NSW split into 4 sub-boxes, the statewide query 504s on public mirrors).

Output columns: name, lat, lng, category, state
Usage: .venv/bin/python scripts/build_osm_amenities.py [--states nsw,vic]
"""
import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_sports_fields import BBOXES, MIRRORS  # noqa: E402

NSW_CHUNKS = [
    (-34.4, 150.0, -32.8, 152.0),
    (-37.6, 144.0, -34.4, 150.5),
    (-37.6, 140.9, -34.4, 144.0),
    (-34.4, 140.9, -32.8, 150.0),
    (-32.8, 140.9, -28.1, 153.7),
]


def _overpass(q: str) -> list[dict]:
    for mirror in MIRRORS:
        try:
            req = urllib.request.Request(
                mirror, data=urllib.parse.urlencode({"data": q}).encode(),
                headers={"User-Agent": "property-scores/1.0"})
            with urllib.request.urlopen(req, timeout=200) as r:
                return json.load(r).get("elements", [])
        except Exception as e:
            print(f"  mirror {mirror.split('/')[2]} failed: {e}", file=sys.stderr)
            time.sleep(5)
    return []


def _classify(tags: dict) -> str | None:
    leisure = tags.get("leisure", "")
    if leisure == "playground":
        return "playground"
    if leisure == "dog_park":
        return "dog_park"
    if tags.get("natural") == "beach":
        return "beach"
    sport = tags.get("sport", "")
    if leisure in ("sports_centre", "water_park") and "swimming" in sport:
        return "swimming_pool"
    if leisure == "swimming_pool":
        access = tags.get("access", "")
        if access in ("private", "no", "customers_only"):
            return None
        if access in ("public", "customers", "yes") or tags.get("name"):
            return "swimming_pool"
    return None


def _query(state: str, bbox: tuple) -> list[dict]:
    s, w, n, e = bbox
    q = (f'[out:json][timeout:170];('
         f'nwr["leisure"~"^(playground|dog_park|swimming_pool|water_park|sports_centre)$"]({s},{w},{n},{e});'
         f'nwr["natural"="beach"]({s},{w},{n},{e});'
         f');out center tags;')
    out = []
    for el in _overpass(q):
        tags = el.get("tags", {})
        cat = _classify(tags)
        if not cat:
            continue
        if el.get("type") == "node":
            lat, lng = el.get("lat"), el.get("lon")
        else:
            c = el.get("center") or {}
            lat, lng = c.get("lat"), c.get("lon")
        if lat is None or lng is None:
            continue
        out.append({"name": tags.get("name") or cat.replace("_", " ").title(),
                    "lat": lat, "lng": lng, "category": cat, "state": state})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--states", default=",".join(BBOXES))
    args = ap.parse_args()
    out_path = Path(__file__).resolve().parents[1] / "data" / "au_osm_amenities.parquet"

    rows: list[dict] = []
    for state in [s.strip() for s in args.states.split(",") if s.strip()]:
        boxes = NSW_CHUNKS if state == "nsw" else [BBOXES[state]]
        got: list[dict] = []
        for bbox in boxes:
            got.extend(_query(state, bbox))
            time.sleep(8)
        print(f"  {state}: {len(got)}", file=sys.stderr)
        rows.extend(got)

    best: dict[tuple, dict] = {}
    for r in rows:
        key = (round(r["lat"], 4), round(r["lng"], 4), r["category"])
        if key not in best or (best[key]["name"].istitle() and r["name"] and not r["name"].istitle()):
            best[key] = r
    deduped = list(best.values())
    print(f"Total: {len(rows)} raw, {len(deduped)} deduped", file=sys.stderr)
    if not deduped:
        sys.exit("no amenities extracted, refusing to write empty parquet")

    import pandas as pd
    df = pd.DataFrame(deduped)
    df.to_parquet(out_path, index=False)
    print(df.groupby("category").size(), file=sys.stderr)
    print(f"Saved {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
