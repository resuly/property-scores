"""Demo runner for the indicative BAL pre-screen (reg-08 verification proof).

Runs the AS 3959 Method 1 pre-screen over a curated set of coordinates spanning
bushfire exposure (deep bush / interface / suburban / urban / grassland) and
cross-checks each against the official state planning overlay. Emits a markdown
table + full JSON so the verification is reproducible.

Run from the property-scores repo root (needs ./data rasters + venv):
    PYTHONPATH=<worktree> .venv/bin/python scripts/bal_prescreen_demo.py
"""

import json
import sys

from property_scores.bal_prescreen import bal_prescreen

# (label, locality/context, lat, lng, rough expectation)
SITES = [
    ("Melbourne CBD",        "dense urban, no bush",              -37.8136, 144.9631, "LOW"),
    ("Geelong CBD",          "regional urban",                    -38.1499, 144.3617, "LOW"),
    ("Glen Waverley",        "established SE Melb suburb",         -37.8800, 145.1650, "LOW"),
    ("Warrandyte",           "Yarra bush-suburb interface (VIC)", -37.7450, 145.2150, "mid/high"),
    ("Kalorama (Dandenongs)","forest interface (VIC BMO)",        -37.8180, 145.3600, "high"),
    ("Anglesea edge",        "coastal heath/forest (VIC)",        -38.4080, 144.1880, "mid/high"),
    ("Lorne",                "steep Otways forest (VIC)",         -38.5400, 143.9750, "high"),
    ("Kinglake township",    "Black Saturday forest (VIC)",       -37.5240, 145.3400, "high"),
    ("Halls Gap",            "Grampians forest (VIC)",            -37.1370, 142.5210, "high"),
    ("Blackheath",           "Blue Mtns forest (NSW)",            -33.6350, 150.2870, "high"),
    ("Lismore VIC plains",   "open grazing/grassland (VIC)",      -37.9560, 143.3400, "LOW (grass)"),
    ("Mount Dandenong",      "in forest (VIC BMO)",               -37.8306, 145.3560, "FZ/40"),
]


def main():
    rows = []
    full = []
    for label, ctx, lat, lng, exp in SITES:
        r = bal_prescreen(lat, lng)
        full.append({"label": label, "context": ctx, "expectation": exp, "result": r})
        bal = r.get("indicative_bal")
        lo, hi = (r.get("bal_range") or [bal, bal])
        conf = r.get("confidence", "-")
        ov = r.get("official_overlay", {}).get("status", "-")
        veg = r.get("inputs", {}).get("vegetation")
        if isinstance(veg, dict):
            dist = veg.get("distance_m")
            vclass = veg.get("as3959_class", "-")
        else:
            dist = "-"
            vclass = "none<100m"
        sl = r.get("inputs", {}).get("slope")
        slope = f"{sl.get('deg')}° {sl.get('direction','')}" if isinstance(sl, dict) else "-"
        rows.append((label, ctx, exp, bal, f"{lo}..{hi}", conf, f"{dist}", vclass, slope, ov))

    # markdown table
    print("| Site | Context | Rough expect | Indicative BAL | Range | Conf | Veg dist (m) | AS3959 class | Slope | Official overlay |")
    print("|------|---------|--------------|----------------|-------|------|--------------|--------------|-------|------------------|")
    for r in rows:
        print("| " + " | ".join(str(x) for x in r) + " |")

    with open("bal_prescreen_demo_output.json", "w") as f:
        json.dump(full, f, indent=2)
    print("\nFull JSON -> bal_prescreen_demo_output.json", file=sys.stderr)


if __name__ == "__main__":
    main()
