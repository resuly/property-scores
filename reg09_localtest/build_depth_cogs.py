#!/usr/bin/env python3
"""reg-09 batch: turn the open CC-BY depth-grid datasets into 1% AEP depth COGs
+ a manifest study_depth loads. This is the "苦活" made repeatable — per-study
depth-file finding, CRS repair, mosaic → EPSG:4326 COG. Honest about failures:
studies name things differently, so some need manual handling; those are logged,
not faked.

Reads corpus candidate list, writes COGs to OUT_DIR and OUT_DIR/manifest.json.
Downloads then deletes each zip (peak disk = one study at a time). No production
touched. Run: python3 build_depth_cogs.py <candidates.json> <out_dir>
"""
import json, os, re, subprocess, sys, tempfile, zipfile, glob, shutil
import requests, warnings
warnings.filterwarnings("ignore")
for v in ("PROJ_LIB", "PROJ_DATA", "GDAL_DATA"):
    os.environ.pop(v, None)

CAND = sys.argv[1] if len(sys.argv) > 1 else "corpus/depth_grid_candidates.json"
OUT = sys.argv[2] if len(sys.argv) > 2 else "depth_cogs"
os.makedirs(OUT, exist_ok=True)
BASE = "https://flooddata.ses.nsw.gov.au"
S = requests.Session(); S.headers["User-Agent"] = "limon-reg09/1"

# 1% AEP depth raster. Studies name depth differently: Central Coast `_Depth.tif`,
# Coffs/TUFLOW `_100y_..._d_Max.tif` (_d_=depth, _h_=level, _V_=velocity, _Z*=hazard).
# `\b` does NOT sit between `d` and `_` (both word chars), so `_d\b` missed `_d_Max` —
# use `_d[_.]` instead. Match depth + a 1% AEP token, exclude other events/derivatives.
DEPTH_RE = re.compile(r"(depth|_wd[_.]|_d[_.]|_dmax|_dep|dmax)", re.I)
AEP1_RE = re.compile(r"(q100|[^0-9]100y|1in100|1%|1pc|100yr|1_?aep|_100_)", re.I)
EXCLUDE_RE = re.compile(r"(pmf|1000y|30pc|slr|climate|_cc_|option|_opt|velocit|_vs_|vsq|diff)", re.I)


def dl_url(name):
    r = S.get(f"{BASE}/api/3/action/package_show", params={"id": name}, timeout=60, verify=False).json()["result"]
    spatial = None
    try:
        spatial = json.loads(r.get("spatial")) if r.get("spatial") else None
    except Exception:
        pass
    for res in r["resources"]:
        if (res.get("format") or "").lower() in ("zip", "applicationzip", "compressed zip folder"):
            return f"{BASE}/dataset/{r['name']}/resource/{res['id']}/download", spatial
    return None, spatial


def centroid_lng(spatial):
    try:
        coords = spatial["coordinates"][0]
        xs = [c[0] for c in coords]
        return sum(xs) / len(xs)
    except Exception:
        return 151.0  # default coastal NSW -> zone 56


def build_one(entry, wd):
    name = entry["url"].split("/dataset/")[1]
    url, spatial = dl_url(name)
    if not url:
        return None, "no-zip-resource"
    zp = os.path.join(wd, "d.zip")
    with S.get(url, stream=True, timeout=600, verify=False) as r:
        r.raise_for_status()
        with open(zp, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
    try:
        zf = zipfile.ZipFile(zp)
    except zipfile.BadZipFile:
        return None, "bad-zip"
    names = zf.namelist()
    depth = [n for n in names if n.lower().endswith(".tif")
             and DEPTH_RE.search(n) and AEP1_RE.search(n) and not EXCLUDE_RE.search(n)]
    if not depth:
        return None, f"no-1pct-depth-tif (of {sum(n.lower().endswith('.tif') for n in names)} tifs)"
    exd = os.path.join(wd, "ex")
    for n in depth:
        zf.extract(n, exd)
    tifs = glob.glob(f"{exd}/**/*.tif", recursive=True)
    vrt = os.path.join(wd, "m.vrt")
    subprocess.run(["gdalbuildvrt", "-srcnodata", "-9999.99", "-vrtnodata", "-9999.99", vrt] + tifs,
                   capture_output=True, timeout=600)
    cog = os.path.join(OUT, f"{entry['council'].lower().replace(' ','_').replace('-','_')}_{name[:30]}_q100y_depth_4326.tif")
    lng = centroid_lng(spatial)
    zone = 55 if lng < 150 else 56
    # try auto CRS first, then forced MGA zone if it fails
    for s_srs in (None, f"EPSG:283{zone}"):
        cmd = ["gdalwarp", "-t_srs", "EPSG:4326", "-r", "bilinear", "-dstnodata", "-9999.99",
               "-co", "TILED=YES", "-co", "COMPRESS=DEFLATE", "-overwrite"]
        if s_srs:
            cmd += ["-s_srs", s_srs]
        cmd += [vrt, cog]
        p = subprocess.run(cmd, capture_output=True, timeout=1200)
        if os.path.exists(cog) and os.path.getsize(cog) > 1000:
            break
    if not (os.path.exists(cog) and os.path.getsize(cog) > 1000):
        return None, "warp-failed"
    # bounds from gdalinfo -json
    gi = json.loads(subprocess.run(["gdalinfo", "-json", cog], capture_output=True, timeout=120).stdout)
    ext = gi.get("wgs84Extent", {}).get("coordinates", [[]])[0]
    xs = [c[0] for c in ext]; ys = [c[1] for c in ext]
    bounds = [min(xs), min(ys), max(xs), max(ys)]
    return {
        "key": name[:50], "cog": os.path.abspath(cog), "aep": "1% AEP",
        "source": f"{entry['council']} — {entry['title']}", "licence": "CC BY 4.0",
        "bounds": bounds, "depth_tifs": len(depth),
    }, "ok"


def main():
    cands = json.load(open(CAND))
    manifest, log = [], []
    for i, e in enumerate(cands):
        wd = tempfile.mkdtemp()
        try:
            res, status = build_one(e, wd)
            if res:
                manifest.append(res)
                log.append(f"OK   {e['council']:24} {e['title'][:40]} -> {res['depth_tifs']} tif")
            else:
                log.append(f"SKIP {e['council']:24} {e['title'][:40]} :: {status}")
        except Exception as ex:
            log.append(f"ERR  {e['council']:24} {e['title'][:40]} :: {str(ex)[:60]}")
        finally:
            shutil.rmtree(wd, ignore_errors=True)
        json.dump(manifest, open(os.path.join(OUT, "manifest.json"), "w"), indent=1)
        open(os.path.join(OUT, "build.log"), "w").write("\n".join(log))
        print(f"[{i+1}/{len(cands)}] {log[-1]}", flush=True)
    print(f"\nDONE: {len(manifest)} COGs built, {len(cands)-len(manifest)} skipped/failed")
    print(f"manifest: {OUT}/manifest.json  log: {OUT}/build.log")


if __name__ == "__main__":
    main()
