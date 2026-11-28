# Colab: assemble annual (or partial) grids from monthly rollups.  python year_assemble.py REGION [REGION ...]
import os, sys, glob, json, math, datetime as dt
import numpy as np, pandas as pd
sys.argv += []  # noqa
Y = '/content/year'; M = f'{Y}/month'; OUT = f'{Y}/out'; os.makedirs(OUT, exist_ok=True)
exec(open('/content/year_pipeline.py').read().split("def plan():")[0])   # CENTERS/BOXES
src = open('/content/year_pipeline.py').read()
exec(src[src.index("GRID_BOX = {'MEL'"):src.index("D0 = dt.date")])
D0 = dt.date(2025, 11, 1)
CITY = {'MEL': 'melbourne', 'SYD': 'sydney', 'BNE': 'brisbane', 'OOL': 'goldcoast', 'MCY': 'sunshinecoast', 'PER': 'perth', 'ADL': 'adelaide',
        'CBR': 'canberra', 'HBA': 'hobart', 'LST': 'launceston', 'DRW': 'darwin', 'CNS': 'cairns', 'TSV': 'townsville', 'NTL': 'newcastle'}
QUARTER = {1: 'q1', 2: 'q1', 3: 'q1', 4: 'q2', 5: 'q2', 6: 'q2', 7: 'q3', 8: 'q3', 9: 'q3', 10: 'q4', 11: 'q4', 12: 'q4'}
NIGHT = [22, 23, 0, 1, 2, 3, 4, 5]

def geom(region):
    lat_min, lat_max, lon_min, lon_max = GRID_BOX[region]
    dlat = 250.0 / 111132.0; dlon = 250.0 / (111320.0 * math.cos(math.radians((lat_min + lat_max) / 2)))
    return lat_min, lon_min, dlat, dlon, int(np.floor((lat_max - lat_min) / dlat)) + 1, int(np.floor((lon_max - lon_min) / dlon)) + 1

def months_done():
    return sorted(os.path.basename(os.path.dirname(p)) for p in glob.glob(f'{M}/*/DONE'))

def assemble(region, months, tag=''):
    lat0, lon0, dlat, dlon, ny, nx = geom(region); NC = ny * nx
    metas = [json.load(open(f'{M}/{m}/meta.json')) for m in months]
    good = [d for mm in metas for d in mm['good_days']]; missing = [d for mm in metas for d in mm['missing_days']]
    ND = len(good); NWD = sum(dt.date.fromisoformat(d).weekday() < 5 for d in good); NWE = ND - NWD
    cnt = np.zeros((NC, 24, 3, 2), np.float64); hist = np.zeros((NC, 30), np.float64)
    for m in months:
        f = f'{M}/{m}/{region}_grid.parquet'
        if os.path.exists(f):
            g = pd.read_parquet(f)
            if len(g):   # regions with no low traffic in a month leave an empty file
                g = g.astype('int64'); np.add.at(cnt, (g.cell.values, g.hour.values, g.cls.values, g.wd.values), g.n.values)
            hh = pd.read_parquet(f.replace('_grid.', '_hist.'))
            if len(hh):
                hh = hh.astype('int64'); np.add.at(hist, (hh.cell.values, hh.bin.values), hh.n.values)
    if ND == 0: return None
    tot = cnt.sum((2, 3))  # NC x 24
    sh = lambda a: (a / max(ND, 1)).reshape(ny, nx).astype(np.float32)
    out = dict(lat0=lat0, lon0=lon0, dlat=dlat, dlon=dlon, ny=ny, nx=nx,
               passes_per_day=sh(tot.sum(1)), night_per_night=sh(tot[:, NIGHT].sum(1)),
               weekday_per_day=(cnt[:, :, :, 1].sum((1, 2)) / max(NWD, 1)).reshape(ny, nx).astype(np.float32),
               weekend_per_day=(cnt[:, :, :, 0].sum((1, 2)) / max(NWE, 1)).reshape(ny, nx).astype(np.float32),
               heli_per_day=sh(cnt[:, :, 0, :].sum((1, 2))), heli_night_per_night=sh(cnt[:, NIGHT, 0, :].sum((1, 2))),
               jet_per_day=sh(cnt[:, :, 1, :].sum((1, 2))), light_per_day=sh(cnt[:, :, 2, :].sum((1, 2))))
    out['hourly'] = (tot / ND).reshape(ny, nx, 24).astype(np.float32)
    n = hist.sum(1); med = np.full(NC, np.nan, np.float32); hv = n > 0
    cum = np.cumsum(hist, 1); half = n / 2
    b = np.argmax(cum >= half[:, None], axis=1)
    before = np.where(b > 0, cum[np.arange(NC), np.maximum(b - 1, 0)], 0)
    inbin = hist[np.arange(NC), b]
    medv = b * 100 + 100 * np.where(inbin > 0, (half - before) / np.maximum(inbin, 1), 0.5)
    med[hv] = medv[hv]; out['median_agl_ft'] = med.reshape(ny, nx)
    city = CITY[region]
    np.savez_compressed(f'{OUT}/grid_{city}{tag}.npz', **out)
    import calendar
    md = {}
    for mm in metas:
        y_, m_ = int(mm['month'][:4]), int(mm['month'][5:])
        md[mm['month']] = dict(good=len(mm['good_days']), local_in_month=len(mm['local_days']), calendar=calendar.monthrange(y_, m_)[1])
    return dict(n_days=ND, n_weekdays=NWD, n_weekend_days=NWE, missing_days=missing, months=months, ny=ny, nx=nx,
                good_days=sorted(good), month_days=md, partial=(months[-1] != '2026-11'))

def quarters(region, months):
    lat0, lon0, dlat, dlon, ny, nx = geom(region); res = {}
    for q in ('q1', 'q2', 'q3', 'q4'):
        ms = [m for m in months if QUARTER[int(m[5:])] == q]
        if not ms: continue
        metas = [json.load(open(f'{M}/{m}/meta.json')) for m in ms]
        good = [d for mm in metas for d in mm['good_days']]; ND = len(good)
        cnt = np.zeros((ny * nx, 24, 3), np.float64)
        for m in ms:
            f = f'{M}/{m}/{region}_grid.parquet'
            if os.path.exists(f):
                g = pd.read_parquet(f)
                if len(g):
                    g = g.astype('int64'); np.add.at(cnt, (g.cell.values, g.hour.values, g.cls.values), g.n.values)
        if ND == 0: continue
        sh = lambda a: (a / ND).reshape(ny, nx).astype(np.float32)
        np.savez_compressed(f'{OUT}/grid_{CITY[region]}_{q}.npz', lat0=lat0, lon0=lon0, dlat=dlat, dlon=dlon, ny=ny, nx=nx,
                            passes_per_day=sh(cnt.sum((1, 2))), night_per_night=sh(cnt[:, NIGHT, :].sum((1, 2))),
                            jet_per_day=sh(cnt[:, :, 1].sum(1)), heli_per_day=sh(cnt[:, :, 0].sum(1)))
        res[q] = dict(months=ms, n_days=ND, missing_days=[d for mm in metas for d in mm['missing_days']])
    return res

def tables(region, months):
    """monthly airport movements, approach low-coverage, daily aircraft, and helicopter landing-site clusters"""
    out = {}
    for kind in ('mov', 'appr', 'ac', 'stops'):
        parts = []
        for m in months:
            f = f'{M}/{m}/{region}_{kind}.csv'
            if os.path.exists(f) and os.path.getsize(f) > 5:
                x = pd.read_csv(f); x['month'] = m; parts.append(x)
        out[kind] = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    # runway-end usage (from the per-UTC-day files, which are kept on the VM and in Drive)
    rparts = []
    for f in glob.glob(f'{Y}/day/*_{region}_rwy.csv'):
        if os.path.getsize(f) > 5: rparts.append(pd.read_csv(f))
    if rparts:
        rw = pd.concat(rparts, ignore_index=True)
        rw['month'] = [(D0 + dt.timedelta(int(x))).strftime('%Y-%m') for x in rw.ld]
        good = {d for m in months for d in json.load(open(f'{M}/{m}/meta.json'))['good_days']}
        rw = rw[[(D0 + dt.timedelta(int(x))).isoformat() in good for x in rw.ld]]
        rw.groupby(['airport', 'month', 'heli', 'rwy', 'op']).n.sum().reset_index().to_csv(f'{OUT}/{region}_rwy_monthly.csv', index=False)
        rw.groupby('ld').size().rename('n').reset_index().to_csv(f'{OUT}/{region}_rwy_days.csv', index=False)
    mov = out['mov']
    if len(mov): mov.groupby(['airport', 'month', 'heli', 'minlow']).mov.sum().reset_index().to_csv(f'{OUT}/{region}_mov_monthly.csv', index=False)
    ap = out['appr']
    if len(ap): ap.groupby(['airport', 'month', 'large']).ok.agg(['size', 'sum']).reset_index().rename(columns={'size': 'approaches', 'sum': 'reach_le200'}).to_csv(f'{OUT}/{region}_appr_monthly.csv', index=False)
    ac = out['ac']
    if len(ac): ac.groupby(['month', 'ld']).aircraft.sum().reset_index().to_csv(f'{OUT}/{region}_aircraft_daily.csv', index=False)
    S = out['stops']
    if len(S):
        from scipy.spatial import cKDTree
        from scipy.sparse.csgraph import connected_components
        import scipy.sparse as sps
        from pyproj import Transformer
        clat, clon = CTR[region]
        TR = Transformer.from_crs(4326, f'+proj=aeqd +lat_0={clat} +lon_0={clon} +datum=WGS84 +units=m', always_xy=True)
        S['x'], S['y'] = TR.transform(S.lon.values, S.lat.values)
        pairs = cKDTree(S[['x', 'y']].values).query_pairs(150, output_type='ndarray'); n = len(S)
        G = sps.coo_matrix((np.ones(len(pairs)), (pairs[:, 0], pairs[:, 1])), shape=(n, n)) if len(pairs) else sps.coo_matrix((n, n))
        S['cl'] = connected_components(G, directed=False)[1]
        S = S.sort_values(['hh', 'ts']).reset_index(drop=True)
        same = (S.hh.values[1:] == S.hh.values[:-1]) & (S.cl.values[1:] == S.cl.values[:-1]) & ((S.ts.values[1:] - S.te.values[:-1]) <= 600)
        im = np.flatnonzero(same); S.loc[im, 'dep'] = 0; S.loc[im + 1, 'arr'] = 0
        tz = TZ[region]
        S['h_arr'] = pd.to_datetime(S.ts, unit='s', utc=True).dt.tz_convert(tz).dt.hour; S['h_dep'] = pd.to_datetime(S.te, unit='s', utc=True).dt.tz_convert(tz).dt.hour
        APT = pd.read_csv('/content/ref/airports.csv', low_memory=False)
        box = APT[APT.latitude_deg.between(S.lat.min() - .05, S.lat.max() + .05) & APT.longitude_deg.between(S.lon.min() - .05, S.lon.max() + .05) & (APT.type != 'closed')]
        bx, by = TR.transform(box.longitude_deg.values, box.latitude_deg.values); bx, by = np.asarray(bx), np.asarray(by)
        aero = box.type.isin(['small_airport', 'medium_airport', 'large_airport']).values
        rows = []; mrows = []
        for c, g in S.groupby('cl'):
            mv = int(g.arr.sum() + g.dep.sum())
            if mv < 2: continue
            cx, cy = g.x.median(), g.y.median(); dd = np.hypot(bx - cx, by - cy)
            k = int(np.argmin(dd)) if len(dd) else None
            hourly = np.zeros(24, int)
            np.add.at(hourly, g.h_arr.values[g.arr.values == 1], 1); np.add.at(hourly, g.h_dep.values[g.dep.values == 1], 1)
            rows.append(dict(cluster=c, lat=round(float(g.lat.median()), 5), lon=round(float(g.lon.median()), 5), arrivals=int(g.arr.sum()), departures=int(g.dep.sum()),
                             night_movements=int(hourly[NIGHT].sum()), aircraft_count=int(g.hh.nunique()), hourly=json.dumps(hourly.tolist()),
                             near_ident=box.iloc[k].ident if k is not None else '', near_name=box.iloc[k]['name'] if k is not None else '',
                             near_type=box.iloc[k].type if k is not None else '', near_dist_m=round(float(dd[k])) if k is not None else None,
                             within_2km_of_aerodrome=bool(len(dd) and aero.any() and (dd[aero] <= 2000).any()),
                             ground_stops=int(g.gany.sum()), stops=len(g), elev_ft=float(g.elev.median())))
            for m, gm in g.groupby('month'):
                mrows.append(dict(cluster=c, month=m, arrivals=int(gm.arr.sum()), departures=int(gm.dep.sum())))
        pd.DataFrame(rows).to_csv(f'{OUT}/{region}_heli_sites.csv', index=False)
        pd.DataFrame(mrows).to_csv(f'{OUT}/{region}_heli_sites_monthly.csv', index=False)

if __name__ == '__main__':
    ms = months_done()
    summary = {}
    for region in sys.argv[1:]:
        info = assemble(region, ms)
        if info is None: continue
        info['quarters'] = quarters(region, ms)
        tables(region, ms)
        APT = pd.read_csv('/content/ref/airports.csv', low_memory=False).set_index('ident')
        info['airports'] = {c: dict(name=APT.loc[c, 'name'], lat=float(APT.loc[c, 'latitude_deg']), lon=float(APT.loc[c, 'longitude_deg'])) for c in AIRPORTS[region] if c in APT.index}
        summary[region] = info
        print(region, info['n_days'], 'days', len(info['missing_days']), 'missing', flush=True)
    prev = json.load(open(f'{OUT}/summary.json')) if os.path.exists(f'{OUT}/summary.json') else {}
    prev.update(summary); json.dump(prev, open(f'{OUT}/summary.json', 'w'), indent=1)
