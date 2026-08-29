#!/usr/bin/env python3
"""Compare the current BAL pre-screen with the internal v2 shadow.

The 12 points are DA Leads' public, never-metered sandbox addresses. Before a
point is admitted to v2, this script verifies that it falls inside an Overture
building footprint; a geocoded point outside a building is reported and not
mislabelled as a verified building point.

This script does not call the DA Leads commercial API and does not change any
delivery surface. Set DATA_DIR to the populated property-scores data directory.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from property_scores.bal_prescreen import bal_prescreen
from property_scores.bal_prescreen.v2 import (
    building_point_from_overture,
    preliminary_bal_v2,
)
from property_scores.bushfire.score import _overlay_check
from property_scores.common import terrain


SITES = [
    ("ACT Campbell", "ACT", -35.28579770, 149.14604948),
    ("NSW Katoomba", "NSW", -33.73128831, 150.31528035),
    ("NSW Sydenham", "NSW", -33.91518436, 151.16875651),
    ("NT Darwin CBD", "NT", -12.45618540, 130.83510818),
    ("QLD Brisbane CBD", "QLD", -27.46819284, 153.03047575),
    ("QLD Rocklea", "QLD", -27.53941362, 153.00563112),
    ("QLD Surfers Paradise", "QLD", -28.00198907, 153.43086174),
    ("SA Adelaide CBD", "SA", -34.92492634, 138.59880160),
    ("TAS Hobart CBD", "TAS", -42.87963235, 147.32296684),
    ("VIC Carlton", "VIC", -37.80055584, 144.96342294),
    ("VIC Clayton", "VIC", -37.92452985, 145.12142090),
    ("WA Perth CBD", "WA", -31.95419661, 115.85632071),
]


def compare() -> list[dict]:
    rows = []
    for label, state, lat, lng in SITES:
        identity = building_point_from_overture(lat, lng)
        footprint = ((identity.evidence or {}).get("building_footprint_m2")
                     if identity else None)
        overlay = _overlay_check(state, lat, lng)
        elevation = terrain.elevation(lat, lng)
        v1 = bal_prescreen(lat, lng, state=state, elevation=elevation,
                           overlay=overlay)
        v2 = preliminary_bal_v2(
            lat, lng, subject_identity=identity, state=state, overlay=overlay)
        rows.append({
            "label": label, "state": state, "lat": lat, "lng": lng,
            "building_footprint_m2": footprint,
            "v1": {
                "bal": v1.get("indicative_bal"),
                "range": v1.get("bal_range"),
                "confidence": v1.get("confidence"),
                "vegetation": (v1.get("inputs") or {}).get("vegetation"),
            },
            "v2": v2,
        })
    return rows


def print_markdown(rows: list[dict]) -> None:
    print("| Site | Building m2 | v1 | v2 status | v2 range | Confidence | Limiting | Directions | Obs |")
    print("|---|---:|---|---|---|---|---|---|---:|")
    for row in rows:
        v2 = row["v2"]
        print(
            f"| {row['label']} | {row['building_footprint_m2']} | "
            f"{row['v1']['bal']} | {v2.get('status')} | "
            f"{v2.get('bal_range') or v2.get('preliminary_bal')} | "
            f"{v2.get('confidence') or '-'} | "
            f"{(v2.get('limiting_observation') or {}).get('sector', '-')} | "
            f"{','.join(v2.get('directions_assessed') or []) or '-'} | "
            f"{len(v2.get('observations') or [])} |"
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json-output", type=Path)
    args = parser.parse_args()
    rows = compare()
    print_markdown(rows)
    if args.json_output:
        args.json_output.write_text(
            json.dumps(rows, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8")
        print(f"\nJSON: {args.json_output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
