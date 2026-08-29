#!/usr/bin/env python3
"""Validate the v2 shadow range against public professional BAL reports.

This is an effect probe, not a unit test or a claim of statistical validation.
The fixture records the identity quality of each report point. Exit non-zero if
a professional result falls outside the model's stated range.
"""

from __future__ import annotations

import json
from pathlib import Path

from property_scores.bal_prescreen.v2 import (
    building_point_from_professional_report,
    preliminary_bal_v2,
)


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/bal_professional_anchors.json"
BAL_ORDER = ["BAL-LOW", "BAL-12.5", "BAL-19", "BAL-29", "BAL-40", "BAL-FZ"]


def _inside(expected: str, band: list[str] | None) -> bool:
    if not band or expected not in BAL_ORDER:
        return False
    lo, hi = (BAL_ORDER.index(value) for value in band)
    rank = BAL_ORDER.index(expected)
    return lo <= rank <= hi


def main() -> int:
    anchors = json.loads(FIXTURE.read_text(encoding="utf-8"))
    exact = covered = eligible = 0
    print("| Anchor | Professional | v2 point | v2 range | Confidence | Covered |")
    print("|---|---|---|---|---|---|")
    for anchor in anchors:
        if not anchor.get("eligible_for_range_validation"):
            print(f"| {anchor['id']} | {anchor['expected_bal']} | excluded | - | - | "
                  f"{anchor['exclusion_reason']} |")
            continue
        eligible += 1
        identity = building_point_from_professional_report(
            anchor["lat"], anchor["lng"], report_url=anchor["source_url"],
            coordinate_evidence=anchor["point_basis"])
        result = preliminary_bal_v2(
            anchor["lat"], anchor["lng"], state=anchor["state"],
            subject_identity=identity)
        expected = anchor["expected_bal"]
        point = result.get("preliminary_bal")
        band = result.get("bal_range")
        is_covered = _inside(expected, band)
        exact += point == expected
        covered += is_covered
        print(f"| {anchor['id']} | {expected} | {point} | {band} | "
              f"{result.get('confidence')} | {'yes' if is_covered else 'NO'} |")
    print(f"\nEligible anchors: {eligible}/{len(anchors)}")
    print(f"Point exact: {exact}/{eligible}")
    print(f"Range coverage: {covered}/{eligible}")
    return 0 if eligible > 0 and covered == eligible else 1


if __name__ == "__main__":
    raise SystemExit(main())
