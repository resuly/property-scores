#!/usr/bin/env python3
import json, time, urllib.request

BASE = "https://services5.arcgis.com/ZSYwjtv8RKVhkXIL/arcgis/rest/services"
SVCS = [
    "MooneeValley_Flood_impacted_properties_Current",
    "Glen_Eira_Flood_impacted_properties_Current",
    "Merri_Bek_Flood_impacted_properties_Current",
    "Yarra_Flood_impacted_properties_Current",
    "Banyule_Flood_impacted_properties_Current",
    "Brimbank_Flood_impacted_properties_Current",
    "Hobsons_Bay_Flood_impacted_properties_Current",
    "Darebin_Flood_impacted_properties_Current",
    "Parcel_Flooded_Combined_LMAR_2",
]

def get(url, tries=3):
    for i in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                return json.loads(r.read())
        except Exception as e:
            if i == tries - 1:
                raise
            time.sleep(2 * (i + 1))

out = {}
for svc in SVCS:
    info = get(f"{BASE}/{svc}/FeatureServer?f=json")
    lid = info["layers"][0]["id"]
    meta = get(f"{BASE}/{svc}/FeatureServer/{lid}?f=json")
    cnt = get(f"{BASE}/{svc}/FeatureServer/{lid}/query?where=1%3D1&returnCountOnly=true&f=json")["count"]
    flds = [f["name"] for f in meta.get("fields", [])
            if "FL_" in f["name"].upper() or "DEPTH" in f["name"].upper() or "PCT" in f["name"].upper()]
    print(f"{svc}  layer={lid}  count={cnt}  maxRec={meta.get('maxRecordCount')}")
    print(f"   fields: {flds}")
    out[svc] = {"layer": lid, "count": cnt, "fields": flds, "maxRec": meta.get("maxRecordCount")}
    time.sleep(0.5)

with open("/private/tmp/claude-502/-Users-bwwan3-Documents-GitHub-limon-ops/98ee46b2-b919-4858-9d0c-9058240f1f31/scratchpad/wf_work/services_probe.json", "w") as f:
    json.dump(out, f, indent=1)
