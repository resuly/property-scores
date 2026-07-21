#!/usr/bin/env python3
"""reg-09 QLD: batch-build 1% AEP depth COGs from Brisbane City Council open data
(Opendatasoft API, CC-BY 4.0, no login). Deterministic recipe (cheaper than agents):
  catalog search "flood study" -> 64 datasets -> attachments ->
    overland sub-models: <area>_100yr_d.zip  (1% AEP depth, single .asc)
    creek/river studies: <n>_<creek>_depth_<date>.zip  (all-AEP bundle -> pick 100yr .asc)
  -> gdalwarp -s_srs EPSG:28356 -t_srs EPSG:4326 COG + numeric depth gate + manifest.

Disk-safe: one study at a time, delete its temp immediately. Run:
  python3 brisbane_depth_batch.py <out_dir>
"""
import json, os, re, subprocess, sys, tempfile, zipfile, glob, shutil
import requests, warnings
warnings.filterwarnings("ignore")
for v in ("PROJ_LIB", "PROJ_DATA", "GDAL_DATA"):
    os.environ.pop(v, None)

OUT = sys.argv[1] if len(sys.argv) > 1 else "depth_cogs"
os.makedirs(OUT, exist_ok=True)
PY = "/Users/bwwan3/Documents/GitHub/property-scores/.venv/bin/python"
B = "https://data.brisbane.qld.gov.au/api/explore/v2.1/catalog"
S = requests.Session(); S.headers["User-Agent"] = "Mozilla/5.0 (limon-reg09)"

# 1% AEP = 100yr in Brisbane naming. depth = _d (NOT _dv/_h/_v). exclude future/ultimate/climate.
OVERLAND_DEPTH = re.compile(r"100yr_d\.zip$", re.I)          # <area>_100yr_d.zip
CREEK_DEPTH = re.compile(r"_depth[_.].*\.zip$", re.I)         # <creek>_depth_<date>.zip
# 1% AEP inside creek bundles is a zero-padded ARI: _0100_ (100yr). Also accept 100yr/1%.
ASC_1PCT = re.compile(r"(_0100_|\b0100\b|100yr|1%|1pc|_1p_|1in100)", re.I)
ASC_EXCLUDE = re.compile(r"(future|ultimate|climate|_cc_|_0002_|_0005_|_0010_|_0020_|_0050_|_0200_|_0500_|_2000_|2yr|5yr|10yr|20yr|50yr|500yr|1000yr|2000yr|velocity|_dv|_v\.asc|height|level)", re.I)


def list_datasets():
    r = S.get(f"{B}/datasets", params={"where": 'search("flood study")', "limit": 100}, timeout=60).json()
    return r.get("results", [])


def _att_list(did):
    """Attachments live only on the dedicated endpoint, not the dataset list/detail meta."""
    r = S.get(f"{B}/datasets/{did}/attachments", timeout=60)
    try:
        j = r.json()
    except Exception:
        return []
    lst = j.get("attachments", j) if isinstance(j, dict) else j
    out = []
    for a in (lst or []):
        title = (a.get("metas", {}) or {}).get("title") or a.get("title") or a.get("id") or ""
        url = a.get("href") or a.get("url")
        if url:
            out.append({"title": title, "url": url})
    return out


def pick_attachment(d):
    att = _att_list(d["dataset_id"])
    overland = [a for a in att if OVERLAND_DEPTH.search(a["title"])]
    if overland:
        return overland[0], "overland"
    creek = [a for a in att if CREEK_DEPTH.search(a["title"])
             and not re.search(r"(future|ultimate|climate)", a["title"], re.I)]
    if creek:
        return creek[0], "creek"
    return None, "no-depth-attachment"


def build(d, wd):
    did = d["dataset_id"]
    att, kind = pick_attachment(d)
    if not att:
        return None, kind
    zp = os.path.join(wd, "d.zip")
    with S.get(att["url"], stream=True, timeout=600) as r:
        r.raise_for_status()
        with open(zp, "wb") as f:
            for c in r.iter_content(1 << 20):
                f.write(c)
    try:
        zf = zipfile.ZipFile(zp)
    except zipfile.BadZipFile:
        return None, "bad-zip"
    ascs = [n for n in zf.namelist() if n.lower().endswith(".asc")]
    if not ascs:
        return None, f"no-asc (of {len(zf.namelist())} files)"
    if kind == "overland":
        target = ascs  # single 100yr depth grid
    else:
        target = [n for n in ascs if ASC_1PCT.search(n) and not ASC_EXCLUDE.search(n)]
        if len(target) != 1:
            # try loosest: any asc with 100yr and not excluded
            target = [n for n in ascs if re.search("100yr", n, re.I) and not ASC_EXCLUDE.search(n)]
        if not target:
            return None, f"no 1% AEP depth .asc in creek bundle ({len(ascs)} ascs)"
        target = target[:1]
    exd = os.path.join(wd, "ex")
    for n in target:
        zf.extract(n, exd)
    tif_in = glob.glob(f"{exd}/**/*.asc", recursive=True)[0]
    cog = os.path.join(OUT, f"qld_brisbane_{did.replace('flood-study-','')[:36]}_q100y_depth_4326.tif")
    subprocess.run(["gdalwarp", "-s_srs", "EPSG:28356", "-t_srs", "EPSG:4326", "-r", "bilinear",
                    "-dstnodata", "-9999", "-co", "TILED=YES", "-co", "COMPRESS=DEFLATE", "-overwrite",
                    tif_in, cog], capture_output=True, timeout=1800)
    if not (os.path.exists(cog) and os.path.getsize(cog) > 1000):
        return None, "warp-failed"
    # stats + gate via rasterio
    stat = subprocess.run([PY, "-c",
        "import rasterio,numpy as np,sys;b=rasterio.open(sys.argv[1]).read(1);r=b[(b>0)&(b<1e6)];"
        "import json;print(json.dumps({} if len(r)==0 else {'p50':float(np.percentile(r,50)),"
        "'p99':float(np.percentile(r,99)),'max':float(r.max()),'pct8':100*float((r>8).sum())/len(r),'n':int(len(r))}))",
        cog], capture_output=True, timeout=300, text=True)
    try:
        st = json.loads(stat.stdout.strip())
    except Exception:
        return None, "stats-failed"
    if not st or st["p99"] > 10 or st["pct8"] > 3 or st["max"] > 50:
        os.remove(cog)
        return None, f"failed depth gate (p50={st.get('p50')} p99={st.get('p99')} pct8={st.get('pct8')})"
    gi = json.loads(subprocess.run(["gdalinfo", "-json", cog], capture_output=True, timeout=120).stdout)
    ext = gi.get("wgs84Extent", {}).get("coordinates", [[]])[0]
    xs = [c[0] for c in ext]; ys = [c[1] for c in ext]
    return {"key": f"qld_brisbane_{did[:44]}", "cog": os.path.abspath(cog), "aep": "1% AEP",
            "source": f"Brisbane City Council — {d.get('metas',{}).get('default',{}).get('title', did)}",
            "licence": "CC BY 4.0", "bounds": [min(xs), min(ys), max(xs), max(ys)],
            "p50": round(st["p50"], 2), "p99": round(st["p99"], 2), "pct8": round(st["pct8"], 3)}, "ok"


def main():
    ds = list_datasets()
    print(f"{len(ds)} Brisbane flood-study datasets", flush=True)
    manifest_path = os.path.join(OUT, "manifest.json")
    manifest = json.load(open(manifest_path)) if os.path.exists(manifest_path) else []
    have = {e["cog"] for e in manifest}
    have_dids = {e["key"].replace("qld_brisbane_", "").rstrip() for e in manifest}
    built, log = [], []
    for i, d in enumerate(ds):
        if d["dataset_id"][:44] in have_dids:  # already built on a prior run — don't re-download
            log.append(f"HAVE {d['dataset_id'][:48]}")
            print(f"[{i+1}/{len(ds)}] HAVE {d['dataset_id'][:40]}", flush=True)
            continue
        wd = tempfile.mkdtemp()
        try:
            res, status = build(d, wd)
            if res:
                if res["cog"] not in have:
                    manifest.append({k: res[k] for k in ("key", "cog", "aep", "source", "licence", "bounds")})
                built.append(res)
                log.append(f"OK   {d['dataset_id'][:48]:48} p50={res['p50']} p99={res['p99']}")
            else:
                log.append(f"SKIP {d['dataset_id'][:48]:48} :: {status}")
        except Exception as e:
            log.append(f"ERR  {d['dataset_id'][:48]:48} :: {str(e)[:50]}")
        finally:
            shutil.rmtree(wd, ignore_errors=True)
        json.dump(manifest, open(manifest_path, "w"), indent=1)
        open(os.path.join(OUT, "brisbane_build.log"), "w").write("\n".join(log))
        print(f"[{i+1}/{len(ds)}] {log[-1]}", flush=True)
    print(f"\nDONE: {len(built)} Brisbane depth COGs built (manifest now {len(manifest)} entries)")


if __name__ == "__main__":
    main()
