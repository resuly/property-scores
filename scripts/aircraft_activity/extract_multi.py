# Colab: one pass over a day's adsb.lol prod archive, filter several regional boxes. Usage: python extract_multi.py DATE
import sys, os, glob, gzip, subprocess, time, math
import orjson, numpy as np, pandas as pd
from multiprocessing import Pool

CENTERS = {  # region: (lat, lon) ; ~60 km half-size boxes
    'SYD': (-33.946, 151.177), 'NTL': (-32.795, 151.834), 'BNE': (-27.384, 153.117), 'OOL': (-28.164, 153.505),
    'MCY': (-26.603, 153.091), 'PER': (-31.940, 115.967), 'ADL': (-34.945, 138.531), 'CBR': (-35.307, 149.195),
    'HBA': (-42.836, 147.510), 'LST': (-41.545, 147.214), 'DRW': (-12.415, 130.877), 'CNS': (-16.886, 145.755),
    'TSV': (-19.252, 146.765),
}
HALF_KM = 60.0
BOXES = {}
for r, (la, lo) in CENTERS.items():
    dla = HALF_KM / 111.0; dlo = HALF_KM / (111.32 * math.cos(math.radians(la)))
    BOXES[r] = (la - dla, la + dla, lo - dlo, lo + dlo)
BOXES['MEL'] = (-38.45, -37.25, 144.25, 145.75)   # Melbourne region (same padded box as phase 1)
ALT_MAX = 13000
B = np.array(list(BOXES.values())); NAMES = list(BOXES.keys())

def parse(path):
    try:
        raw = open(path, 'rb').read()
        if raw[:2] == b'\x1f\x8b':
            raw = gzip.decompress(raw)
        d = orjson.loads(raw)
    except Exception:
        return None
    tr = d.get('trace') or []
    if not tr:
        return None
    lat = np.fromiter((p[1] for p in tr), float, len(tr)); lon = np.fromiter((p[2] for p in tr), float, len(tr))
    inb = (lat[:, None] >= B[:, 0]) & (lat[:, None] <= B[:, 1]) & (lon[:, None] >= B[:, 2]) & (lon[:, None] <= B[:, 3])
    if not inb.any():
        return None
    cats = set(); cat = None
    for p in tr:
        o = p[8] if len(p) > 8 else None
        if o:
            c = o.get('category')
            if c: cats.add(c); cat = c
    base = d['timestamp']; rows = []
    for i in np.flatnonzero(inb.any(1)):
        p = tr[i]; a = p[3]; ground = a == 'ground'
        ab = np.nan if (a is None or ground) else float(a)
        ag = p[10] if len(p) > 10 and p[10] is not None else np.nan
        if not ground:
            lo_ = ag if ag == ag else ab
            if lo_ == lo_ and lo_ > ALT_MAX:
                continue
        for j in np.flatnonzero(inb[i]):
            rows.append((NAMES[j], base + p[0], p[1], p[2], ab, ground, ag, p[4] if p[4] is not None else np.nan,
                         p[7] if p[7] is not None else np.nan, (p[9] if len(p) > 9 else None) or 'none'))
    if not rows:
        return None
    df = pd.DataFrame(rows, columns=['region', 'ts', 'lat', 'lon', 'alt_baro', 'ground', 'alt_geom', 'gs', 'vrate', 'src'])
    df['hex'] = d.get('icao'); df['reg'] = d.get('r'); df['type'] = d.get('t')
    df['cat'] = cat; df['cats'] = ','.join(sorted(cats))
    return df   # owner/operator field deliberately not kept

def main():
    date = sys.argv[1]
    outdir = '/content/npts'; os.makedirs(outdir, exist_ok=True)
    out = f'{outdir}/{date}.parquet'
    if os.path.exists(out):
        print('exists', out); return
    t = time.time(); raw = f'/content/raw/{date}'; os.makedirs(raw, exist_ok=True)
    tag = f'v{date}-planes-readsb-prod-0'
    api = subprocess.run(['curl', '-sL', f'https://api.github.com/repos/adsblol/globe_history_2026/releases/tags/{tag}'], capture_output=True).stdout
    names = sorted(a['name'] for a in orjson.loads(api)['assets'])
    urls = ' '.join(f'https://github.com/adsblol/globe_history_2026/releases/download/{tag}/{n}' for n in names)
    r = subprocess.run(f'cd {raw} && (for u in {urls}; do curl -sfL "$u" || exit 9; done) | tar -xf - --wildcards "*traces/*"', shell=True)
    t1 = time.time()
    files = glob.glob(f'{raw}/**/trace_full_*.json', recursive=True)
    with Pool(8) as pool:
        parts = [x for x in pool.imap_unordered(parse, files, chunksize=200) if x is not None]
    df = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    df['region'] = df.region.astype('category'); df['src'] = df.src.astype('category')
    df.to_parquet(out)
    subprocess.run(['rm', '-rf', raw])
    print(f'{date} files={len(files)} tar_rc={r.returncode} aircraft={len(parts)} rows={len(df)} by_region={df.groupby("region", observed=True).hex.nunique().to_dict()} dl={t1-t:.0f}s parse={time.time()-t1:.0f}s', flush=True)

if __name__ == '__main__':
    main()
