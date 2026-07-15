"""Extract GA DEM-H (bare-earth, hydro-enforced, CC BY 4.0) 1-degree tiles
covering the AU Copernicus tiles, from the DEA public S3 national COG.

Idempotent: skips tiles already present. Output -> data/global/dem_h/
Tile naming mirrors the source SW corner: DEMH_S33_E151.tif (covers lat -33..-32, lng 151..152).
Source: dea-public-data.s3.ap-southeast-2 ... demh1sv1_0.tif (national COG, EPSG:4326, ~30m).
"""
import os, re, subprocess, sys, glob

SRC = "/vsicurl/https://dea-public-data.s3.ap-southeast-2.amazonaws.com/projects/elevation/ga_srtm_dem1sv1_0/demh1sv1_0.tif"
DEM_DIR = "data/global/dem"
OUT_DIR = "data/global/dem_h"
os.makedirs(OUT_DIR, exist_ok=True)

# AU Copernicus tiles present -> parse SW corner
cop = glob.glob(os.path.join(DEM_DIR, "Copernicus_DSM_COG_10_S*_E1*_DEM.tif"))
tiles = []
for p in cop:
    m = re.search(r"_S(\d\d)_00_E(\d\d\d)_00_", os.path.basename(p))
    if not m: continue
    la = int(m.group(1)); lo = int(m.group(2))
    if 10 <= la <= 44 and 112 <= lo <= 154:   # AU land envelope
        tiles.append((la, lo))
tiles = sorted(set(tiles))
print(f"AU tiles to cover: {len(tiles)}", flush=True)

ok = skip = fail = 0
for i, (la, lo) in enumerate(tiles, 1):
    out = os.path.join(OUT_DIR, f"DEMH_S{la:02d}_E{lo:03d}.tif")
    if os.path.exists(out) and os.path.getsize(out) > 1000:
        skip += 1; continue
    # SW corner (la south, lo west). -projwin = ulx(W) uly(N) lrx(E) lry(S)
    w, n, e, s = lo, -(la-1), lo+1, -la
    cmd = ["gdal_translate", "-q", "-projwin", str(w), str(n), str(e), str(s),
           "-of", "GTiff", "-co", "COMPRESS=DEFLATE", "-co", "TILED=YES", SRC, out]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        if r.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 1000:
            ok += 1
        else:
            fail += 1; print(f"  FAIL S{la} E{lo}: {r.stderr[-200:]}", flush=True)
            if os.path.exists(out): os.remove(out)
    except Exception as ex:
        fail += 1; print(f"  ERR S{la} E{lo}: {ex}", flush=True)
    if i % 20 == 0:
        print(f"  [{i}/{len(tiles)}] ok={ok} skip={skip} fail={fail}", flush=True)
print(f"DONE ok={ok} skip={skip} fail={fail} total_out={len(glob.glob(os.path.join(OUT_DIR,'*.tif')))}", flush=True)
