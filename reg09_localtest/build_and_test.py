#!/usr/bin/env python3
"""reg-09 local before/after: bake Newcastle graded flood hazard into a LOCAL
features.duckdb slice and show the flood score grading H1 nuisance vs H5 floodway,
where the current binary library would score both the same. No production DB touched."""
import json, os, sys, urllib.request, ssl, duckdb

ctx=ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE
DB=os.environ["FEATURES_DB"]
FS="https://services-ap1.arcgis.com/CNeUPE1voVc22gNk/arcgis/rest/services/Newcastle_Floodplain_Risk_Managment_1AEP_Hazard/FeatureServer"
# Newcastle CBD / Hunter bbox (xmin,ymin,xmax,ymax WGS84) to keep the slice small
BBOX="151.65,-32.98,151.82,-32.85"
PROV={"study":"City of Newcastle Floodplain Risk Management (1% AEP hazard)","year":2020,
      "aep":"1% AEP","licence":"CC BY 4.0 (council open data)"}

def fetch(layer):
    feats=[]; off=0
    while True:
        u=(f"{FS}/{layer}/query?where=1%3D1&outFields=Hazard&geometry={BBOX}"
           f"&geometryType=esriGeometryEnvelope&inSR=4326&spatialRel=esriSpatialRelIntersects"
           f"&outSR=4326&f=geojson&resultOffset={off}&resultRecordCount=1000")
        d=json.load(urllib.request.urlopen(urllib.request.Request(u,headers={"User-Agent":"l/1"}),timeout=60,context=ctx))
        fs=d.get("features",[]); feats+=fs
        if len(fs)<1000: break
        off+=1000
    return feats

con=duckdb.connect(DB); con.execute("INSTALL spatial;LOAD spatial")
con.execute("DROP TABLE IF EXISTS features")
con.execute("CREATE TABLE features(source VARCHAR, state VARCHAR, category VARCHAR, geom GEOMETRY, props VARCHAR)")
n=0
for layer in ("3","4","5"):
    for ft in fetch(layer):
        hz=ft.get("properties",{}).get("Hazard")
        if hz is None: continue
        props=dict(PROV); props["hazard_class"]=f"H{int(hz)}"
        con.execute("INSERT INTO features VALUES ('nsw_newcastle_flood_hazard','nsw','flood',"
                    "ST_GeomFromGeoJSON(?), ?)",[json.dumps(ft["geometry"]), json.dumps(props)])
        n+=1
print(f"baked {n} Newcastle graded-hazard polygons into local slice {DB}")
# distribution
rows=con.execute("SELECT json_extract_string(props,'$.hazard_class') h, count(*) FROM features GROUP BY 1 ORDER BY 1").fetchall()
print("hazard class distribution:", dict(rows))
# pick a representative point inside an H1 polygon and an H5 polygon
def point_for(hcls):
    r=con.execute("SELECT ST_X(ST_PointOnSurface(geom)), ST_Y(ST_PointOnSurface(geom)) FROM features "
                  "WHERE json_extract_string(props,'$.hazard_class')=? LIMIT 1",[hcls]).fetchone()
    return r
PTS={h:point_for(h) for h in ("H1","H2","H5","H6")}
con.close()

# --- run the score at each point (patched code on PYTHONPATH) ---
from property_scores.flood import score as fs_mod, local_overlays as lo
SEV=fs_mod.SEVERITY_SCORES
print("\n=== BEFORE (binary extent) vs AFTER (graded hazard) — overlay contribution ===")
print(f"binary flood extent -> severity 'flood' -> overlay_score band {SEV['flood']}  (same for every hit)")
for hcls in ("H1","H2","H5","H6"):
    pt=PTS.get(hcls)
    if not pt: print(f"  no {hcls} polygon in bbox"); continue
    lng,lat=pt
    chk=lo.check("nsw",lat,lng)  # clean, no network
    kind=chk.get("worst"); band=SEV.get(kind)
    hz=chk.get("hazard",{})
    print(f"\n  {hcls} point ({lat:.5f},{lng:.5f}):")
    print(f"    AFTER graded: worst_severity={kind} band={band}  hazard={hz.get('hazard_class')} '{hz.get('description')}'")
    print(f"    provenance: {hz.get('source')} ({hz.get('year')}), {hz.get('licence')}")

print("\n=== FULL flood_score (with JRC/HAND/IFD live) — final number + surfaced hazard ===")
for hcls in ("H1","H6"):
    pt=PTS.get(hcls)
    if not pt: continue
    lng,lat=pt
    try:
        r=fs_mod.flood_score(lat,lng)
        fh=r.get("flood_hazard",{})
        print(f"  {hcls} point: score={r['score']} ({r['label']}) | flood_hazard={fh.get('class')} "
              f"prov={fh.get('provenance')} | summary={r.get('flood_hazard_summary')}")
    except Exception as e:
        print(f"  {hcls} flood_score err (likely network): {str(e)[:80]}")
