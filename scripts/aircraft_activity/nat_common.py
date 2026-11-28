# Colab: national per-region analysis. python nat_analyze.py  -> /content/nout/*.csv
import os, glob, math, re, subprocess
import numpy as np, pandas as pd
from pyproj import Transformer

OUT = '/content/nout'; os.makedirs(OUT, exist_ok=True)
REG = {  # region: (tz, center lat, center lon, [airports])
    'MEL': ('Australia/Melbourne', -37.85, 145.00, ['YMML', 'YMEN', 'YMMB', 'YMAV']),
    'SYD': ('Australia/Sydney', -33.946, 151.177, ['YSSY', 'YSBK', 'YSCN']),
    'NTL': ('Australia/Sydney', -32.795, 151.834, ['YWLM']),
    'BNE': ('Australia/Brisbane', -27.384, 153.117, ['YBBN', 'YBAF']),
    'OOL': ('Australia/Brisbane', -28.164, 153.505, ['YBCG']),
    'MCY': ('Australia/Brisbane', -26.603, 153.091, ['YBSU']),
    'PER': ('Australia/Perth', -31.940, 115.967, ['YPPH', 'YPJT']),
    'ADL': ('Australia/Adelaide', -34.945, 138.531, ['YPAD', 'YPPF']),
    'CBR': ('Australia/Sydney', -35.307, 149.195, ['YSCB']),
    'HBA': ('Australia/Hobart', -42.836, 147.510, ['YMHB', 'YCBG']),
    'LST': ('Australia/Hobart', -41.545, 147.214, ['YMLT']),
    'DRW': ('Australia/Darwin', -12.415, 130.877, ['YPDN']),
    'CNS': ('Australia/Brisbane', -16.886, 145.755, ['YBCS']),
    'TSV': ('Australia/Brisbane', -19.252, 146.765, ['YBTL']),
}
DAYS = 14
HELI_TYPES = set('R22 R44 R66 B06 B06T B206 B407 B427 B429 B412 B212 B505 EC20 EC30 EC35 EC45 EC55 EC75 AS32 AS50 AS55 AS65 AS3B AS35 '
                 'A109 A119 A139 A149 A169 A189 AW09 BK17 H160 H175 S76 S92 S61 MD50 MD52 MD60 H500 EXPL H269 G2CA CABR UH1 H60 NH90 '
                 'TIGR EH10 KMAX BO05 B47G B47J R300 S300 GAZL ALO3 LYNX A129 H47 B214 B430 KA32 MI8 H135 H145 AS65 EC25 BK117'.split())

# ---- reference data ----
os.makedirs('/content/ref', exist_ok=True)
for f in ('airports.csv', 'runways.csv'):
    if not os.path.exists(f'/content/ref/{f}'):
        subprocess.run(f'curl -sfL -o /content/ref/{f} https://davidmegginson.github.io/ourairports-data/{f}', shell=True, check=True)
APT = pd.read_csv('/content/ref/airports.csv', low_memory=False)
RWY = pd.read_csv('/content/ref/runways.csv', low_memory=False)

# ---- DEM (SRTM via AWS terrain tiles, skadi) ----
TILES = {}
def tile(la_floor, lo_floor):
    n = f"{'S' if la_floor < 0 else 'N'}{abs(la_floor):02d}{'E' if lo_floor >= 0 else 'W'}{abs(lo_floor):03d}"
    if n in TILES: return TILES[n]
    p = f'/content/dem/{n}.hgt'; os.makedirs('/content/dem', exist_ok=True)
    if not os.path.exists(p):
        r = subprocess.run(f'curl -sfL https://s3.amazonaws.com/elevation-tiles-prod/skadi/{n[:3]}/{n}.hgt.gz | gunzip > {p}', shell=True)
    try:
        a = np.fromfile(p, '>i2'); s = int(round(np.sqrt(a.size))); a = a.reshape(s, s).astype(np.float32); a[a < -1000] = 0
    except Exception:
        a = None
    TILES[n] = a; return a
def dem_ft(lat, lon):
    lat = np.asarray(lat, float); lon = np.asarray(lon, float); out = np.zeros(lat.shape, np.float32)
    lf = np.floor(lat).astype(int); of = np.floor(lon).astype(int)
    for la0, lo0 in set(zip(lf.ravel().tolist(), of.ravel().tolist())):
        a = tile(la0, lo0); m = (lf == la0) & (of == lo0)
        if a is None: continue   # ocean tile -> 0
        s = a.shape[0]
        r = np.round((la0 + 1 - lat[m]) * (s - 1)).astype(int); c = np.round((lon[m] - lo0) * (s - 1)).astype(int)
        out[m] = a[r, c] * 3.28084
    return out

files = sorted(glob.glob('/content/npts/*.parquet'))

def load(region):
    dfs = [pd.read_parquet(f, filters=[('region', '==', region)]) for f in files]
    df = pd.concat(dfs, ignore_index=True)
    df['tsr'] = df.ts.round(0)
    df = df.drop_duplicates(['hex', 'tsr']).sort_values(['hex', 'ts']).reset_index(drop=True)
    df['hr'] = (df.ts // 3600).astype(np.int64)
    m = df.alt_geom.notna() & df.alt_baro.between(500, 5000) & df.src.astype(str).str.startswith('adsb')
    off = df[m].assign(o=df.alt_geom - df.alt_baro).groupby('hr').o.median()
    off = off.reindex(range(df.hr.min(), df.hr.max() + 1)).interpolate().bfill().ffill().fillna(0)
    df['alt_msl'] = np.where(df.alt_geom.notna(), df.alt_geom, df.alt_baro + df.hr.map(off).astype(float))
    df['elev'] = dem_ft(df.lat.values, df.lon.values)
    df['agl'] = np.where(df.ground, 0.0, df.alt_msl - df.elev)
    df.loc[~df.ground & (df.agl < 0), 'agl'] = 0.0
    df = df[df.ground | (df.agl.notna() & (df.agl <= 10000))].reset_index(drop=True)
    df['heli'] = df.cats.fillna('').str.contains('A7') | df.type.isin(HELI_TYPES)
    # fill missing/A0 category from modal category of the same type (within region)
    known = df[df.cat.notna() & ~df.cat.isin(['A0'])].groupby('hex').agg(type=('type', 'first'), cat=('cat', 'first'))
    tc = known.groupby('type').cat.agg(lambda s: s.mode().iloc[0])
    miss = df.cat.isna() | df.cat.eq('A0'); df.loc[miss, 'cat'] = df.loc[miss, 'type'].map(tc)
    return df, float(off.median())

def episodes(A, cx, cy, R, H, minlow, gap=600):
    X, Y, AGL, GR, VR, GS, h, ts, sn, sp = A
    d = np.hypot(X - cx, Y - cy)
    inz = (d <= R) & (GR | (AGL < H))
    idx = np.flatnonzero(inz)
    if idx.size == 0: return pd.DataFrame(columns=['s', 'e', 'arr', 'dep', 'mov', 'real'])
    brk = np.r_[True, (np.diff(idx) != 1) | (h[idx[1:]] != h[idx[:-1]]) | (np.diff(ts[idx]) > gap)]
    starts = idx[brk]; ends = np.r_[idx[np.flatnonzero(brk)[1:] - 1], idx[-1]]
    rows = []
    for s, e in zip(starts, ends):
        hg = GR[s:e + 1].any(); mn = AGL[s:e + 1].min()
        prev_ok = sp[s] and ts[s] - ts[s - 1] <= gap; next_ok = sn[e] and ts[e + 1] - ts[e] <= gap
        if prev_ok and not GR[s - 1]: arr = 'air'
        elif not prev_ok and not GR[s] and (np.isnan(VR[s]) or VR[s] <= 0) and (AGL[s] > 100 or GS[s] > 40): arr = 'lowstart'
        else: arr = 'none'
        if next_ok and not GR[e + 1]: dep = 'air'
        elif not next_ok and not GR[e] and (np.isnan(VR[e]) or VR[e] >= 0) and (AGL[e] > 100 or GS[e] > 40): dep = 'lowend'
        else: dep = 'none'
        rows.append((s, e, hg, mn, arr, dep))
    ep = pd.DataFrame(rows, columns=['s', 'e', 'has_ground', 'min_agl', 'arr', 'dep'])
    ep['real'] = ep.has_ground | (ep.min_agl < minlow)
    ep['mov'] = ((ep.arr != 'none') & ep.real).astype(int) + ((ep.dep != 'none') & ep.real).astype(int)
    return ep

