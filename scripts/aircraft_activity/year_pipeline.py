# Colab year pipeline for MEL + SYD. Two roles:
#   python year_pipeline.py fetch  START END   -> downloader, keeps <= AHEAD extracted days ready in /content/yraw/<date>
#   python year_pipeline.py process START END  -> processes ready days in order, writes /content/year/day/<date>_*.parquet, logs /content/year/done.csv
# Dates are UTC archive dates YYYY.MM.DD. Plan (prod/staging/missing) from /content/year/day_plan.csv.
import sys, os, glob, gzip, time, math, json, subprocess, hashlib, shutil, datetime as dt
import numpy as np, pandas as pd, orjson
from multiprocessing import Pool

Y = '/content/year'; RAW = '/content/yraw'; os.makedirs(f'{Y}/day', exist_ok=True); os.makedirs(RAW, exist_ok=True)
AHEAD = 2
CENTERS = {
    'SYD': (-33.946, 151.177), 'NTL': (-32.795, 151.834), 'BNE': (-27.384, 153.117), 'OOL': (-28.164, 153.505),
    'MCY': (-26.603, 153.091), 'PER': (-31.940, 115.967), 'ADL': (-34.945, 138.531), 'CBR': (-35.307, 149.195),
    'HBA': (-42.836, 147.510), 'LST': (-41.545, 147.214), 'DRW': (-12.415, 130.877), 'CNS': (-16.886, 145.755),
    'TSV': (-19.252, 146.765)}
BOXES = {'MEL': (-38.45, -37.25, 144.25, 145.75)}
for r_, (la_, lo_) in CENTERS.items():
    dla = 60 / 111.0; dlo = 60 / (111.32 * math.cos(math.radians(la_)))
    BOXES[r_] = (la_ - dla, la_ + dla, lo_ - dlo, lo_ + dlo)
B = np.array(list(BOXES.values())); NAMES = list(BOXES.keys())
ALT_MAX = 13000

def plan():
    p = pd.read_csv(f'{Y}/day_plan.csv'); return dict(zip(p.date, p.kind))

def plan_repo():
    # a few late-December archives are published in the next year's repository
    p = pd.read_csv(f'{Y}/day_plan.csv')
    return dict(zip(p.date, p.repo)) if 'repo' in p.columns else {}

def daterange(a, b):
    d0 = dt.datetime.strptime(a, '%Y.%m.%d').date(); d1 = dt.datetime.strptime(b, '%Y.%m.%d').date()
    return [(d0 + dt.timedelta(i)).strftime('%Y.%m.%d') for i in range((d1 - d0).days + 1)]

def is_done(d):
    f = f'{Y}/day/{d}.done'
    return os.path.exists(f) and open(f).read().strip() in ('OK', 'ROLLED', 'MISSING')

def fetch(a, b):
    P = plan(); PR = plan_repo()
    for d in daterange(a, b):
        if is_done(d) or os.path.exists(f'{RAW}/{d}/READY'): continue
        # keep at most AHEAD extracted days on disk, but never block the day the processor needs next
        while len(glob.glob(f'{RAW}/*/READY')) >= AHEAD and d != next(x for x in daterange(a, b) if not is_done(x)):
            time.sleep(5)
        kind = P.get(d, 'MISSING')
        os.makedirs(f'{RAW}/{d}', exist_ok=True)
        if kind == 'MISSING':
            open(f'{RAW}/{d}/READY', 'w').write('MISSING'); continue
        repo = PR.get(d, 'globe_history_' + d[:4]); t = time.time(); rc = 1
        # some archives fail to untar ("Skipping to next header"); after 2 tries use the other instance's archive
        for attempt, kind_try in enumerate([kind, kind, 'staging-0' if kind == 'prod-0' else 'prod-0']):
            tag = f'v{d}-planes-readsb-{kind_try}'
            api = subprocess.run(['curl', '-sL', f'https://api.github.com/repos/adsblol/{repo}/releases/tags/{tag}'], capture_output=True).stdout
            try:
                names = sorted(x['name'] for x in orjson.loads(api)['assets'])
            except Exception:
                time.sleep(30); continue
            urls = ' '.join(f'https://github.com/adsblol/{repo}/releases/download/{tag}/{n}' for n in names)
            subprocess.run(f'rm -rf {RAW}/{d}/traces', shell=True)
            if shutil.which('aria2c'):
                # 16 connections per file: several times faster than one curl stream on VMs far from GitHub's CDN
                rc = subprocess.run(f'cd {RAW}/{d} && aria2c -q -x16 -s16 -k8M --max-tries=5 --retry-wait=10 -Z {urls} && cat {" ".join(names)} | tar -xf - --wildcards "*traces/*"; r=$?; rm -f {" ".join(names)}; exit $r', shell=True).returncode
            else:
                rc = subprocess.run(f'cd {RAW}/{d} && (for u in {urls}; do curl -sfL --retry 3 "$u" || exit 9; done) | tar -xf - --wildcards "*traces/*"', shell=True).returncode
            if rc == 0:
                kind = kind_try; break
            time.sleep(30)
        open(f'{RAW}/{d}/READY', 'w').write(f'{kind} rc={rc} dl={time.time() - t:.0f}')

def parse(path):
    try:
        raw = open(path, 'rb').read()
        if raw[:2] == b'\x1f\x8b': raw = gzip.decompress(raw)
        d = orjson.loads(raw)
    except Exception:
        return 'ERR'
    tr = d.get('trace') or []
    if not tr: return None
    lat = np.fromiter((p[1] for p in tr), float, len(tr)); lon = np.fromiter((p[2] for p in tr), float, len(tr))
    inb = (lat[:, None] >= B[:, 0]) & (lat[:, None] <= B[:, 1]) & (lon[:, None] >= B[:, 2]) & (lon[:, None] <= B[:, 3])
    if not inb.any(): return None
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
            l_ = ag if ag == ag else ab
            if l_ == l_ and l_ > ALT_MAX: continue
        for j in np.flatnonzero(inb[i]):
            rows.append((NAMES[j], base + p[0], p[1], p[2], ab, ground, ag, p[4] if p[4] is not None else np.nan,
                         p[7] if p[7] is not None else np.nan, (p[9] if len(p) > 9 else None) or 'none'))
    if not rows: return None
    df = pd.DataFrame(rows, columns=['region', 'ts', 'lat', 'lon', 'alt_baro', 'ground', 'alt_geom', 'gs', 'vrate', 'src'])
    df['hex'] = d.get('icao'); df['type'] = d.get('t'); df['cat'] = cat; df['cats'] = ','.join(sorted(cats))
    return df

# ---------------- per-day processing ----------------
def setup_common():
    g = {}
    exec(open('/content/nat_common.py').read(), g)
    return g

GRID_BOX = {'MEL': (-38.30, -37.40, 144.40, 145.60)}
for r_, (la_, lo_) in CENTERS.items():
    dla = 58.5 / 111.0; dlo = 58.5 / (111.32 * math.cos(math.radians(la_)))
    GRID_BOX[r_] = (la_ - dla, la_ + dla, lo_ - dlo, lo_ + dlo)
REGIONS = ['MEL', 'SYD', 'BNE', 'OOL', 'MCY', 'PER', 'ADL', 'CBR', 'HBA', 'LST', 'DRW', 'CNS', 'TSV', 'NTL']
TZ = {'MEL': 'Australia/Melbourne', 'SYD': 'Australia/Sydney', 'NTL': 'Australia/Sydney', 'CBR': 'Australia/Sydney', 'BNE': 'Australia/Brisbane',
      'OOL': 'Australia/Brisbane', 'MCY': 'Australia/Brisbane', 'CNS': 'Australia/Brisbane', 'TSV': 'Australia/Brisbane', 'PER': 'Australia/Perth',
      'ADL': 'Australia/Adelaide', 'HBA': 'Australia/Hobart', 'LST': 'Australia/Hobart', 'DRW': 'Australia/Darwin'}
AIRPORTS = {'MEL': ['YMML', 'YMEN', 'YMMB', 'YMAV'], 'SYD': ['YSSY', 'YSBK', 'YSCN'], 'NTL': ['YWLM'], 'BNE': ['YBBN', 'YBAF'], 'OOL': ['YBCG'],
            'MCY': ['YBSU'], 'PER': ['YPPH', 'YPJT'], 'ADL': ['YPAD', 'YPPF'], 'CBR': ['YSCB'], 'HBA': ['YMHB', 'YCBG'], 'LST': ['YMLT'],
            'DRW': ['YPDN'], 'CNS': ['YBCS'], 'TSV': ['YBTL']}
CTR = dict(CENTERS); CTR['MEL'] = (-37.85, 145.00)
D0 = dt.date(2025, 11, 1)

def grid_geom(region):
    lat_min, lat_max, lon_min, lon_max = GRID_BOX[region]
    dlat = 250.0 / 111132.0; dlon = 250.0 / (111320.0 * math.cos(math.radians((lat_min + lat_max) / 2)))
    ny = int(np.floor((lat_max - lat_min) / dlat)) + 1; nx = int(np.floor((lon_max - lon_min) / dlon)) + 1
    return lat_min, lon_min, dlat, dlon, ny, nx

def prep(df, G, typecat):
    df = df.copy(); df['tsr'] = df.ts.round(0)
    df = df.drop_duplicates(['hex', 'tsr']).sort_values(['hex', 'ts']).reset_index(drop=True)
    df['hr'] = (df.ts // 3600).astype(np.int64)
    m = df.alt_geom.notna() & df.alt_baro.between(500, 5000) & df.src.astype(str).str.startswith('adsb')
    off = df[m].assign(o=df.alt_geom - df.alt_baro).groupby('hr').o.median()
    if len(off):
        off = off.reindex(range(df.hr.min(), df.hr.max() + 1)).interpolate().bfill().ffill()
        df['off'] = df.hr.map(off).astype(float).fillna(0)
    else:
        df['off'] = 0.0
    df['alt_msl'] = np.where(df.alt_geom.notna(), df.alt_geom, df.alt_baro + df.off)
    df['elev'] = G['dem_ft'](df.lat.values, df.lon.values)
    df['agl'] = np.where(df.ground, 0.0, df.alt_msl - df.elev)
    df.loc[~df.ground & (df.agl < 0), 'agl'] = 0.0
    df = df[df.ground | (df.agl.notna() & (df.agl <= 10000))].reset_index(drop=True)
    df['heli'] = df.cats.fillna('').str.contains('A7') | df.type.isin(G['HELI_TYPES'])
    known = df[df.cat.notna() & ~df.cat.isin(['A0'])].groupby('type').cat.agg(lambda s: s.mode().iloc[0])
    for k, v in known.items():
        if isinstance(k, str): typecat.setdefault(k, v)
    miss = df.cat.isna() | df.cat.eq('A0')
    df.loc[miss, 'cat'] = df.loc[miss, 'type'].map(typecat)
    return df

def grid_events(df, region):
    """Return DataFrame of events: cell, t, agl, cls (0 heli,1 jet,2 light). Same logic as phase-2 build_grid."""
    lat0, lon0, dlat, dlon, ny, nx = grid_geom(region)
    G = GCOMMON
    key = f'elev_{region}'
    if key not in ELEV:
        glat = lat0 + np.arange(ny) * dlat; glon = lon0 + np.arange(nx) * dlon
        GLA, GLO = np.meshgrid(glat, glon, indexing='ij'); ELEV[key] = G['dem_ft'](GLA, GLO).astype(np.float32)
    elev_c = ELEV[key]
    cat = df.cat.fillna('').values
    keep_ac = ~pd.Series(cat).str.startswith('C').values
    h = df.hex.values; ts = df.ts.values
    sn = np.r_[h[1:] == h[:-1], False]; nxt = np.r_[np.arange(1, len(df)), len(df) - 1]
    gr = df.ground.values; agl = df.agl.values; elev = df.elev.values
    msl = np.where(gr, elev, df.alt_msl.values)
    ok = sn & (np.r_[np.diff(ts), 0] <= 120) & ~(gr & gr[nxt]) & (np.minimum(agl, agl[nxt]) <= 4500) & keep_ac
    si = np.flatnonzero(ok)
    if si.size == 0: return pd.DataFrame(columns=['cell', 't', 'agl', 'cls'])
    la0, lo0_, la1, lo1 = df.lat.values[si], df.lon.values[si], df.lat.values[si + 1], df.lon.values[si + 1]
    L = np.hypot((la1 - la0) * 111132, (lo1 - lo0_) * 111320 * np.cos(np.radians(la0)))
    k = np.maximum(1, np.ceil(L / 100).astype(np.int64))
    _, hid = np.unique(h, return_inverse=True)
    acls = pd.DataFrame(dict(hid=hid, heli=df.heli.values, jet=np.isin(cat, ['A3', 'A4', 'A5']))).groupby('hid').agg(heli=('heli', 'max'), jet=('jet', 'max'))
    clsv = np.where(acls.heli.values, 0, np.where(acls.jet.values, 1, 2)).astype(np.int8)
    seg_h = hid[si]; bounds = np.r_[0, np.flatnonzero(np.diff(seg_h)) + 1, len(si)]; cum = np.cumsum(k)
    DI, DJ = np.meshgrid(np.arange(-5, 6), np.arange(-5, 6), indexing='ij'); DI = DI.ravel(); DJ = DJ.ravel()
    out = []; start = 0
    while start < len(bounds) - 1:
        end = start + 1
        base_c = cum[bounds[start] - 1] if bounds[start] > 0 else 0
        while end < len(bounds) - 1 and (cum[bounds[end + 1] - 1] - base_c) < 250000: end += 1
        a, b = bounds[start], bounds[end]
        kk = k[a:b]; rep = np.repeat(np.arange(a, b), kk)
        frac = (np.arange(kk.sum()) - np.repeat(np.cumsum(kk) - kk, kk) + 0.5) / np.repeat(kk, kk)
        s_ = si[rep]
        slat = df.lat.values[s_] + frac * (df.lat.values[s_ + 1] - df.lat.values[s_]); slon = df.lon.values[s_] + frac * (df.lon.values[s_ + 1] - df.lon.values[s_])
        smsl = msl[s_] + frac * (msl[s_ + 1] - msl[s_]); st = ts[s_] + frac * (ts[s_ + 1] - ts[s_]); sh = hid[s_]
        i0 = np.round((slat - lat0) / dlat).astype(np.int64); j0 = np.round((slon - lon0) / dlon).astype(np.int64)
        I = (i0[:, None] + DI[None, :]).ravel(); J = (j0[:, None] + DJ[None, :]).ravel(); R = np.repeat(np.arange(len(slat)), len(DI))
        inb = (I >= 0) & (I < ny) & (J >= 0) & (J < nx); I, J, R = I[inb], J[inb], R[inb]
        dy = (lat0 + I * dlat - slat[R]) * 111132.0; dx = (lon0 + J * dlon - slon[R]) * 111320.0 * np.cos(np.radians(slat[R]))
        d = np.hypot(dx, dy); a_c = smsl[R] - elev_c[I, J]
        q = (d <= 1000) & (a_c <= 3000); I, J, R, d, a_c = I[q], J[q], R[q], d[q], a_c[q]
        cell = I * nx + J; t = st[R]; hh = sh[R]
        if cell.size == 0:
            start = end; continue
        o = np.lexsort((t, hh, cell)); cell, t, hh, d, a_c = cell[o], t[o], hh[o], d[o], a_c[o]
        brk = np.r_[True, (cell[1:] != cell[:-1]) | (hh[1:] != hh[:-1]) | (np.diff(t) > 600)]
        eid = np.cumsum(brk) - 1
        o2 = np.lexsort((d, eid)); first = o2[np.r_[True, eid[o2][1:] != eid[o2][:-1]]]
        out.append(pd.DataFrame(dict(cell=cell[first].astype(np.int32), t=t[first], agl=a_c[first].astype(np.float32), cls=clsv[hh[first]])))
        start = end
    if not out: return pd.DataFrame(columns=['cell', 't', 'agl', 'cls'])
    return pd.concat(out, ignore_index=True)

def _bearing(x0, y0, x1, y1):
    return (math.degrees(math.atan2(x1 - x0, y1 - y0)) + 360) % 360

def runway_ops(ep, X, Y_, gr, heli, ldv, ends, region, code):
    """Assign each counted arrival/departure of an airport episode to a runway end by its ground track below 500 ft."""
    out = []
    for r in ep[ep.real].itertuples():
        s_, e_ = int(r.s), int(r.e)
        gidx = np.flatnonzero(gr[s_:e_ + 1]) + s_
        for op, ok_, a_, b_ in (('arr', r.arr != 'none', s_, (gidx[0] if len(gidx) else e_)), ('dep', r.dep != 'none', (gidx[-1] if len(gidx) else s_), e_)):
            if not ok_:
                continue
            if b_ - a_ < 1:
                a_, b_ = s_, e_
            if b_ - a_ < 1 or math.hypot(X[b_] - X[a_], Y_[b_] - Y_[a_]) < 300:
                rw = 'unk'
            else:
                hd = _bearing(X[a_], Y_[a_], X[b_], Y_[b_])
                best = min(ends, key=lambda t: abs((hd - t[1] + 180) % 360 - 180)) if ends else None
                rw = best[0] if best is not None and abs((hd - best[1] + 180) % 360 - 180) <= 35 else 'unk'
            out.append(dict(region=region, airport=code, ld=int(ldv[s_]), heli=bool(heli[s_]), rwy=rw, op=op))
    return out

def hexhash(hx):
    return int(hashlib.sha1(('salt-flight-sample:' + str(hx)).encode()).hexdigest()[:12], 16)

def process_day(d):
    t0 = time.time()
    ready = open(f'{RAW}/{d}/READY').read()
    if ready.startswith('MISSING') or 'rc=0' not in ready:
        return dict(date=d, status='MISSING' if ready.startswith('MISSING') else 'DOWNLOAD_FAILED', info=ready)
    files = glob.glob(f'{RAW}/{d}/**/trace_full_*.json', recursive=True)
    with Pool(8) as pool:
        res = list(pool.imap_unordered(parse, files, chunksize=200))
    nerr = sum(1 for x in res if isinstance(x, str))
    parts = [x for x in res if isinstance(x, pd.DataFrame)]
    raw = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    t1 = time.time(); info = dict(date=d, status='OK', kind=ready.split()[0], files=len(files), parse_errors=nerr, parse_s=round(t1 - t0))
    global RAWDF
    RAWDF = raw
    if len(raw):
        kn = raw[raw.cat.notna() & ~raw.cat.isin(['A0'])].groupby('type').cat.agg(lambda x: x.mode().iloc[0])
        for k_, v_ in kn.items():
            if isinstance(k_, str): TYPECAT.setdefault(k_, v_)
    with Pool(6) as pool:
        for r_info in pool.imap_unordered(region_job, [(d, r) for r in REGIONS]):
            info.update(r_info)
    info['total_s'] = round(time.time() - t0)
    json.dump(TYPECAT, open(f'{Y}/typecat.json', 'w'))
    return info

def write_empty(d, region):
    pd.DataFrame(dict(cell=pd.Series([], dtype='int32'), ld=pd.Series([], dtype='int16'), hour=pd.Series([], dtype='int8'), cls=pd.Series([], dtype='int8'), n=pd.Series([], dtype='int32'))).to_parquet(f'{Y}/day/{d}_{region}_grid.parquet')
    pd.DataFrame(dict(cell=pd.Series([], dtype='int32'), ld=pd.Series([], dtype='int16'), bin=pd.Series([], dtype='int8'), n=pd.Series([], dtype='int32'))).to_parquet(f'{Y}/day/{d}_{region}_hist.parquet')
    for k in ('mov', 'appr', 'stops', 'ac'):
        open(f'{Y}/day/{d}_{region}_{k}.csv', 'w').close()

def region_job(arg):
    d, region = arg; info = {}
    G = GCOMMON; raw = RAWDF
    if True:
        df = raw[raw.region == region] if len(raw) else raw
        if df.empty:
            info[f'{region}_aircraft'] = 0; write_empty(d, region); return info
        df = prep(df, G, TYPECAT); tz = TZ[region]
        info[f'{region}_aircraft'] = int(df.hex.nunique()); info[f'{region}_rows'] = len(df)
        if len(df) < 2:
            write_empty(d, region); return info
        # grid events -> sparse aggregates keyed by local date
        ev = grid_events(df, region)
        if ev.empty:
            ev = pd.DataFrame(dict(cell=np.array([], np.int32), t=np.array([], float), agl=np.array([], np.float32), cls=np.array([], np.int8)))
        lt = pd.to_datetime(ev.t, unit='s', utc=True).dt.tz_convert(tz)
        ev['ld'] = ((lt.dt.tz_localize(None).dt.normalize() - pd.Timestamp(D0)).dt.days if len(ev) else pd.Series([], dtype=int)).astype(np.int16)
        ev['hour'] = lt.dt.hour.astype(np.int8)
        agg = ev.groupby(['cell', 'ld', 'hour', 'cls'], observed=True).size().rename('n').reset_index()
        agg['n'] = agg.n.astype(np.int32); agg.to_parquet(f'{Y}/day/{d}_{region}_grid.parquet')
        ev['bin'] = np.clip(ev.agl // 100, 0, 29).astype(np.int8)
        hist = ev.groupby(['cell', 'ld', 'bin']).size().rename('n').reset_index(); hist['n'] = hist.n.astype(np.int32)
        hist.to_parquet(f'{Y}/day/{d}_{region}_hist.parquet')
        info[f'{region}_events'] = len(ev)
        # airport movements per local date
        clat, clon = CTR[region]
        from pyproj import Transformer
        TR = Transformer.from_crs(4326, f'+proj=aeqd +lat_0={clat} +lon_0={clon} +datum=WGS84 +units=m', always_xy=True)
        X, Y_ = TR.transform(df.lon.values, df.lat.values)
        h = df.hex.values; ts = df.ts.values
        sn = np.r_[h[1:] == h[:-1], False]; sp = np.r_[False, h[1:] == h[:-1]]
        A = (X, Y_, df.agl.values.astype(float), df.ground.values, df.vrate.values.astype(float), df.gs.values.astype(float), h, ts, sn, sp)
        heli = df.heli.values
        ldv = (pd.to_datetime(df.ts, unit='s', utc=True).dt.tz_convert(tz).dt.tz_localize(None).dt.normalize() - pd.Timestamp(D0)).dt.days.values
        mrows = []; arows = []; rrows = []
        for code in AIRPORTS[region]:
            cx, cy, R, ex, ey = APTGEOM[(region, code)]
            if len(ex):
                de = np.full(len(X), np.inf)
                for x_, y_ in zip(ex, ey): de = np.minimum(de, np.hypot(X - x_, Y_ - y_))
                agl_ = df.agl.values; gr_ = df.ground.values; vr_ = df.vrate.values; catv = df.cat.fillna('').values
                idx = np.flatnonzero((de <= 3000) & (gr_ | (agl_ < 1000)) & ~heli)
                if idx.size:
                    brk = np.r_[True, (np.diff(idx) != 1) | (h[idx[1:]] != h[idx[:-1]]) | (np.diff(ts[idx]) > 120)]
                    st_ = idx[brk]; en_ = np.r_[idx[np.flatnonzero(brk)[1:] - 1], idx[-1]]
                    for s_, e_ in zip(st_, en_):
                        if gr_[s_]: continue
                        desc = (vr_[s_] < -100) if not np.isnan(vr_[s_]) else (agl_[e_] < agl_[s_])
                        if not desc: continue
                        okc = bool(gr_[s_:e_ + 1].any() or agl_[s_:e_ + 1].min() <= 200)
                        big = catv[s_] in ('A2', 'A3', 'A4', 'A5')
                        arows.append(dict(region=region, airport=code, ld=int(ldv[s_]), large=big, ok=okc))
            for ml in (250, 100):
                ep = G['episodes'](A, cx, cy, R, 500, ml)
                if ml == 250 and len(ep):
                    rrows.extend(runway_ops(ep, X, Y_, df.ground.values, heli, ldv, RWYEND.get(code, []), region, code))
                if len(ep):
                    e = pd.DataFrame(dict(ld=ldv[ep.s.values], heli=heli[ep.s.values], mov=ep.mov.values))
                    for (l_, hl), n in e.groupby(['ld', 'heli']).mov.sum().items():
                        mrows.append(dict(region=region, airport=code, ld=int(l_), heli=bool(hl), minlow=ml, mov=int(n)))
        pd.DataFrame(mrows).to_csv(f'{Y}/day/{d}_{region}_mov.csv', index=False)
        pd.DataFrame(arows).to_csv(f'{Y}/day/{d}_{region}_appr.csv', index=False)
        (pd.DataFrame(rrows).groupby(['region', 'airport', 'ld', 'heli', 'rwy', 'op']).size().rename('n').reset_index() if rrows else pd.DataFrame()).to_csv(f'{Y}/day/{d}_{region}_rwy.csv', index=False)
        # helicopter stops
        agl = df.agl.values; gr = df.ground.values
        low = heli & (gr | (agl < 100)); idx = np.flatnonzero(low); srows = []
        if idx.size:
            brk = np.r_[True, (np.diff(idx) != 1) | (h[idx[1:]] != h[idx[:-1]]) | (np.diff(ts[idx]) > 600)]
            st = idx[brk]; en = np.r_[idx[np.flatnonzero(brk)[1:] - 1], idx[-1]]
            for s, e in zip(st, en):
                seg = slice(s, e + 1); j = s + int(np.argmin(np.where(gr[seg], -1, agl[seg])))
                arr = bool(sp[s] and ts[s] - ts[s - 1] <= 600 and not gr[s - 1] and agl[s - 1] >= 100)
                dep = bool(sn[e] and ts[e + 1] - ts[e] <= 600 and not gr[e + 1] and agl[e + 1] >= 100)
                if arr or dep:
                    srows.append(dict(hh=hexhash(h[s]), lat=df.lat.values[j], lon=df.lon.values[j], ts=ts[s], te=ts[e], arr=int(arr), dep=int(dep),
                                      gany=bool(gr[seg].any()), elev=float(df.elev.values[j])))
        pd.DataFrame(srows).to_csv(f'{Y}/day/{d}_{region}_stops.csv', index=False)
        # daily distinct aircraft per local date (in box)
        pd.DataFrame(dict(ld=ldv, hh=h)).groupby('ld').hh.nunique().rename('aircraft').reset_index().assign(region=region).to_csv(f'{Y}/day/{d}_{region}_ac.csv', index=False)
    return info

def process(a, b):
    global GCOMMON, TYPECAT, ELEV, APTGEOM, RWYEND
    RWYEND = {}
    GCOMMON = setup_common(); ELEV = {}
    TYPECAT = json.load(open(f'{Y}/typecat.json')) if os.path.exists(f'{Y}/typecat.json') else {}
    from pyproj import Transformer
    APTGEOM = {}
    for region, codes in AIRPORTS.items():
        clat, clon = CTR[region]
        TR = Transformer.from_crs(4326, f'+proj=aeqd +lat_0={clat} +lon_0={clon} +datum=WGS84 +units=m', always_xy=True)
        for code in codes:
            rw = GCOMMON['RWY'][(GCOMMON['RWY'].airport_ident == code) & (GCOMMON['RWY'].closed == 0)]
            ends = [(r[f'{p}_latitude_deg'], r[f'{p}_longitude_deg']) for _, r in rw.iterrows() for p in ('le', 'he') if pd.notna(r[f'{p}_latitude_deg'])]
            ends = [e for e in ends if pd.notna(e[1])]
            if not ends:
                a_ = GCOMMON['APT'][GCOMMON['APT'].ident == code].iloc[0]
                cx, cy = TR.transform(a_.longitude_deg, a_.latitude_deg); APTGEOM[(region, code)] = (cx, cy, 3000.0, np.array([]), np.array([])); continue
            ex, ey = TR.transform([e[1] for e in ends], [e[0] for e in ends]); ex, ey = np.asarray(ex), np.asarray(ey)
            cx, cy = ex.mean(), ey.mean(); APTGEOM[(region, code)] = (cx, cy, float(np.clip(np.hypot(ex - cx, ey - cy).max() + 1500, 2000, 4500)), ex, ey)
            re_ = []
            for _, r in rw.iterrows():
                if pd.isna(r.le_latitude_deg) or pd.isna(r.he_latitude_deg): continue
                lx, ly = TR.transform(r.le_longitude_deg, r.le_latitude_deg); hx_, hy_ = TR.transform(r.he_longitude_deg, r.he_latitude_deg)
                brg = _bearing(lx, ly, hx_, hy_)            # direction of travel when using the le end (e.g. 16)
                re_.append((str(r.le_ident), brg)); re_.append((str(r.he_ident), (brg + 180) % 360))
            RWYEND[code] = re_
    for region in REGIONS:
        lat0, lon0, dlat, dlon, ny, nx = grid_geom(region)
        GLA, GLO = np.meshgrid(lat0 + np.arange(ny) * dlat, lon0 + np.arange(nx) * dlon, indexing='ij')
        ELEV[f'elev_{region}'] = GCOMMON['dem_ft'](GLA, GLO).astype(np.float32)
    for d in daterange(a, b):
        if is_done(d): continue
        waited = 0
        while not os.path.exists(f'{RAW}/{d}/READY'):
            time.sleep(5); waited += 5
            ahead = any(os.path.basename(x) > d for x in glob.glob(f'{RAW}/*'))
            if waited >= 60 and ahead and not os.path.exists(f'{RAW}/{d}'):
                # the fetcher is already working on later days and nobody is downloading this one: fetch it here
                fetch(d, d); waited = 0
        for retry in range(3):   # a failed download gets fetched again here before the day is given up
            r_ = open(f'{RAW}/{d}/READY').read()
            if r_.startswith('MISSING') or 'rc=0' in r_: break
            time.sleep(60 * (retry + 1)); os.remove(f'{RAW}/{d}/READY'); fetch(d, d)
        try:
            info = process_day(d)
        except Exception as e:
            import traceback; info = dict(date=d, status='PROCESS_ERROR', info=repr(e)[:300]); traceback.print_exc()
        with open(f'{Y}/done.jsonl', 'a') as f: f.write(json.dumps(info) + '\n')
        open(f'{Y}/day/{d}.done', 'w').write(info['status'])
        subprocess.run(['rm', '-rf', f'{RAW}/{d}'])
        print(json.dumps(info), flush=True)

if __name__ == '__main__':
    role, a, b = sys.argv[1], sys.argv[2], sys.argv[3]
    if role == 'fetch': fetch(a, b)
    else: process(a, b)
