#!/opt/homebrew/bin/python3
"""VIC 'Angle 5' synthetic flood depth — production build.

depth = Melbourne Water 1% AEP flood LEVEL (mAHD, per-parcel FL_1_PCT_MAX)
        minus GA 5m LiDAR bare-earth DTM (AHD, decimetre Int16 local window).

Quality fixes vs Moonee Valley proof:
  (a) polygons > 2 ha dropped (single MAX level stamped across big ground
      gradients produced fake 30-50 m depths);
  (b) DTM = GA 5 Metre DEM of Australia derived from LiDAR (bare earth),
      not 30 m Copernicus DSM.
Maribyrnong: real parcel depth field rasterized directly (no synthesis).
"""
import json, math, os, shutil, subprocess, sys, tempfile, time, urllib.request

for v in ("PROJ_LIB", "PROJ_DATA", "GDAL_DATA"):
    os.environ.pop(v, None)

import numpy as np
from osgeo import gdal, ogr, osr
gdal.UseExceptions()
ogr.UseExceptions()
osr.UseExceptions()

SCRATCH = "/private/tmp/claude-502/-Users-bwwan3-Documents-GitHub-limon-ops/98ee46b2-b919-4858-9d0c-9058240f1f31/scratchpad"
OUT_DIR = f"{SCRATCH}/wf_out/cogs"
WORK = f"{SCRATCH}/wf_work"
DTM_DM = f"{WORK}/melb_lidar5m_dm.tif"   # Int16 decimetres, EPSG:4326, nodata -32768
MANIFEST = f"{OUT_DIR}/manifest.json"
BASE = "https://services5.arcgis.com/ZSYwjtv8RKVhkXIL/arcgis/rest/services"
MAX_AREA_M2 = 20000.0   # 2 ha
NODATA = -9999.0

LGAS = [
    # key, service, layer, field, display, mode
    ("mooneevalley", "MooneeValley_Flood_impacted_properties_Current", 6,  "FL_1_PCT_MAX", "Moonee Valley", "synth"),
    ("gleneira",     "Glen_Eira_Flood_impacted_properties_Current",    5,  "FL_1_PCT_MAX", "Glen Eira",     "synth"),
    ("merribek",     "Merri_Bek_Flood_impacted_properties_Current",    4,  "FL_1_PCT_MAX", "Merri-bek",     "synth"),
    ("yarra",        "Yarra_Flood_impacted_properties_Current",        9,  "FL_1_PCT_MAX", "Yarra",         "synth"),
    ("banyule",      "Banyule_Flood_impacted_properties_Current",      33, "FL_1_PCT_MAX", "Banyule",       "synth"),
    ("brimbank",     "Brimbank_Flood_impacted_properties_Current",     3,  "FL_1_PCT_MAX", "Brimbank",      "synth"),
    ("hobsonsbay",   "Hobsons_Bay_Flood_impacted_properties_Current",  67, "FL_1_PCT_MAX", "Hobsons Bay",   "synth"),
    ("darebin",      "Darebin_Flood_impacted_properties_Current",      11, "FL_1_PCT_M",   "Darebin",       "synth"),
    ("maribyrnong",  "Parcel_Flooded_Combined_LMAR_2",                 8,  "DEPTH_MAX_1_PCT", "Maribyrnong", "depth"),
]


def http_json(url, tries=4):
    for i in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=120) as r:
                return json.loads(r.read())
        except Exception as e:
            if i == tries - 1:
                raise
            time.sleep(3 * (i + 1))


def fetch_features(svc, layer, field):
    """Page the full layer as geojson in EPSG:7855. Returns list of (geom_json, value)."""
    feats, offset = [], 0
    while True:
        url = (f"{BASE}/{svc}/FeatureServer/{layer}/query?where=1%3D1"
               f"&outFields={field}&outSR=7855&f=geojson"
               f"&resultOffset={offset}&resultRecordCount=2000")
        d = http_json(url)
        page = d.get("features", [])
        for f in page:
            feats.append((f.get("geometry"), (f.get("properties") or {}).get(field)))
        offset += len(page)
        if len(page) < 2000:
            break
    return feats


def snap(v, up):
    return math.ceil(v / 5.0) * 5.0 if up else math.floor(v / 5.0) * 5.0


def run(cmd):
    subprocess.run(cmd, check=True, capture_output=True, text=True)


def build_lga(key, svc, layer, field, display, mode, tmp):
    res = {"key": key, "display": display, "mode": mode}
    feats = fetch_features(svc, layer, field)
    res["n_total"] = len(feats)

    srs = osr.SpatialReference(); srs.ImportFromEPSG(7855)
    drv = ogr.GetDriverByName("GPKG")
    gpkg = f"{tmp}/{key}.gpkg"
    ds = drv.CreateDataSource(gpkg)
    lyr = ds.CreateLayer("polys", srs=srs, geom_type=ogr.wkbMultiPolygon)
    lyr.CreateField(ogr.FieldDefn("val", ogr.OFTReal))

    n_null = n_big = n_badgeom = n_kept = 0
    minx = miny = 1e12; maxx = maxy = -1e12
    for gj, val in feats:
        if val is None or not isinstance(val, (int, float)) or not (-10.0 < float(val) < 1000.0):
            n_null += 1
            continue
        try:
            geom = ogr.CreateGeometryFromJson(json.dumps(gj))
            if geom is None or geom.IsEmpty():
                raise ValueError
        except Exception:
            n_badgeom += 1
            continue
        area = geom.GetArea()
        if area > MAX_AREA_M2:
            n_big += 1
            continue
        f = ogr.Feature(lyr.GetLayerDefn())
        f.SetField("val", float(val))
        f.SetGeometry(geom)
        lyr.CreateFeature(f)
        env = geom.GetEnvelope()  # minx maxx miny maxy
        minx = min(minx, env[0]); maxx = max(maxx, env[1])
        miny = min(miny, env[2]); maxy = max(maxy, env[3])
        n_kept += 1
    ds = None
    res.update(kept=n_kept, dropped_gt2ha=n_big, dropped_null=n_null, dropped_badgeom=n_badgeom)
    if n_kept == 0:
        res["status"] = "FAILED: no usable polygons"
        return res

    te = (snap(minx - 10, False), snap(miny - 10, False), snap(maxx + 10, True), snap(maxy + 10, True))
    level_tif = f"{tmp}/{key}_level.tif"
    run(["gdal_rasterize", "-a", "val", "-tr", "5", "5",
         "-te", *[str(v) for v in te], "-ot", "Float32",
         "-init", str(NODATA), "-a_nodata", str(NODATA),
         "-co", "COMPRESS=DEFLATE", gpkg, level_tif])

    if mode == "synth":
        dtm_tif = f"{tmp}/{key}_dtm.tif"
        run(["gdalwarp", "-t_srs", "EPSG:7855", "-te", *[str(v) for v in te],
             "-tr", "5", "5", "-r", "bilinear", "-ot", "Float32",
             "-srcnodata", "-32768", "-dstnodata", "-32768",
             "-co", "COMPRESS=DEFLATE", DTM_DM, dtm_tif])
        lv = gdal.Open(level_tif).ReadAsArray()
        dm = gdal.Open(dtm_tif).ReadAsArray()
        wet = lv != NODATA
        dtm_ok = dm != -32768
        depth = np.full(lv.shape, NODATA, dtype=np.float32)
        m = wet & dtm_ok
        depth[m] = lv[m] - dm[m] / 10.0
        depth[depth <= 0] = NODATA
        res["wet_cells_level"] = int(wet.sum())
        res["dtm_gap_pct_of_wet"] = round(float((wet & ~dtm_ok).sum()) / max(1, wet.sum()) * 100, 2)
    else:
        lv = gdal.Open(level_tif).ReadAsArray()
        depth = lv.astype(np.float32)
        depth[depth <= 0] = NODATA
        res["wet_cells_level"] = int((lv != NODATA).sum())
        res["dtm_gap_pct_of_wet"] = 0.0

    depth_tif = f"{tmp}/{key}_depth7855.tif"
    ref = gdal.Open(level_tif)
    out = gdal.GetDriverByName("GTiff").Create(
        depth_tif, ref.RasterXSize, ref.RasterYSize, 1, gdal.GDT_Float32,
        options=["COMPRESS=DEFLATE", "TILED=YES"])
    out.SetGeoTransform(ref.GetGeoTransform())
    out.SetProjection(ref.GetProjection())
    b = out.GetRasterBand(1); b.WriteArray(depth); b.SetNoDataValue(NODATA)
    out.FlushCache(); out = None; ref = None

    if key == "maribyrnong":
        cog = f"{OUT_DIR}/vic_melbwater_maribyrnong_parcel_q100y_depth_4326.tif"
    else:
        cog = f"{OUT_DIR}/vic_synth_{key}_q100y_depth_4326.tif"
    if os.path.exists(cog):
        os.remove(cog)
    run(["gdalwarp", "-t_srs", "EPSG:4326", "-r", "near", "-of", "COG",
         "-co", "COMPRESS=DEFLATE", depth_tif, cog])

    # stats on final COG wet cells
    dsF = gdal.Open(cog)
    a = dsF.ReadAsArray()
    nd = dsF.GetRasterBand(1).GetNoDataValue()
    gt = dsF.GetGeoTransform()
    bounds = [gt[0], gt[3] + gt[5] * dsF.RasterYSize, gt[0] + gt[1] * dsF.RasterXSize, gt[3]]
    wetv = a[(a != nd) & np.isfinite(a)]
    dsF = None
    if wetv.size == 0:
        res["status"] = "FAILED: no wet cells after depth calc"
        os.remove(cog)
        return res
    p50 = float(np.percentile(wetv, 50)); p99 = float(np.percentile(wetv, 99))
    mx = float(wetv.max()); pgt8 = float((wetv > 8).mean() * 100)
    res.update(p50=round(p50, 2), p99=round(p99, 2), max=round(mx, 2),
               pct_gt8m=round(pgt8, 3), wet_cells_final=int(wetv.size),
               bounds=[round(b_, 7) for b_ in bounds], cog=cog)
    if p99 <= 10 and pgt8 <= 3 and mx <= 50:
        res["status"] = "PASS"
    else:
        res["status"] = f"GATED-OUT (p99={p99:.2f} %>8m={pgt8:.2f} max={mx:.1f})"
        gated = cog.replace(".tif", ".GATED.tif")
        shutil.move(cog, f"{WORK}/{os.path.basename(gated)}")
        res["cog"] = f"{WORK}/{os.path.basename(gated)} (not shipped)"
    return res


def main():
    results = []
    for key, svc, layer, field, display, mode in LGAS:
        tmp = tempfile.mkdtemp(prefix=f"vic_{key}_", dir=WORK)
        t0 = time.time()
        try:
            r = build_lga(key, svc, layer, field, display, mode, tmp)
        except Exception as e:
            r = {"key": key, "display": display, "status": f"FAILED: {type(e).__name__}: {e}"}
        r["secs"] = round(time.time() - t0, 1)
        results.append(r)
        print(json.dumps(r), flush=True)
        shutil.rmtree(tmp, ignore_errors=True)

    # manifest read-modify-write (keep window tiny; parallel sessions append too)
    passing = [r for r in results if r.get("status") == "PASS"]
    m = json.load(open(MANIFEST)) if os.path.exists(MANIFEST) else []
    keys_new = set()
    entries = []
    for r in passing:
        stem = os.path.basename(r["cog"]).replace(".tif", "")
        if r["mode"] == "depth":
            src = f"Melbourne Water — {r['display']} (parcel depth)"
        else:
            src = f"Melbourne Water — {r['display']} (synthetic level-DEM)"
        entries.append({
            "key": stem, "cog": r["cog"], "aep": "1% AEP", "source": src,
            "licence": "CC BY-SA 4.0 (MW hub default) — VERIFY before resale",
            "bounds": r["bounds"],
        })
        keys_new.add(stem)
    m = [e for e in m if e.get("key") not in keys_new] + entries
    tmpf = MANIFEST + ".tmp_vic"
    with open(tmpf, "w") as f:
        json.dump(m, f, indent=1)
    os.replace(tmpf, MANIFEST)
    print(f"MANIFEST now {len(m)} entries (+{len(entries)} VIC)", flush=True)


if __name__ == "__main__":
    main()
