# Colab: python nat_run.py REGION [REGION...]  -> /content/nout/<region>_*.csv, /content/grids/grid_<city>.npz
import sys, json
exec(open('/content/nat_common.py').read())
from scipy.spatial import cKDTree
from scipy.sparse.csgraph import connected_components
import scipy.sparse as sps

CITY = {'MEL': 'melbourne', 'SYD': 'sydney', 'BNE': 'brisbane', 'OOL': 'goldcoast', 'MCY': 'sunshinecoast', 'PER': 'perth', 'ADL': 'adelaide',
        'CBR': 'canberra', 'HBA': 'hobart', 'LST': 'launceston', 'DRW': 'darwin', 'CNS': 'cairns', 'TSV': 'townsville', 'NTL': 'newcastle'}
GRID_BOX = {'MEL': (-38.30, -37.40, 144.40, 145.60)}
for r, (tz, la, lo, _) in REG.items():
    if r not in GRID_BOX:
        dla = 58.5 / 111.0; dlo = 58.5 / (111.32 * math.cos(math.radians(la)))
        GRID_BOX[r] = (la - dla, la + dla, lo - dlo, lo + dlo)
os.makedirs('/content/grids', exist_ok=True)

def window(tz):
    t0 = pd.Timestamp('2026-11-14 00:00', tz=tz).timestamp(); t1 = pd.Timestamp('2026-11-27 00:00', tz=tz).timestamp()
    return t0, t1

def build_grid(df, region, tz):
    lat_min, lat_max, lon_min, lon_max = GRID_BOX[region]
    dlat = 250.0 / 111132.0; dlon = 250.0 / (111320.0 * math.cos(math.radians((lat_min + lat_max) / 2)))
    ny = int(np.floor((lat_max - lat_min) / dlat)) + 1; nx = int(np.floor((lon_max - lon_min) / dlon)) + 1
    lat0, lon0 = lat_min, lon_min
    glat = lat0 + np.arange(ny) * dlat; glon = lon0 + np.arange(nx) * dlon
    GLA, GLO = np.meshgrid(glat, glon, indexing='ij')
    elev_c = dem_ft(GLA, GLO).astype(np.float32)
    t0, t1 = window(tz)
    cat = df.cat.fillna('').values
    keep_ac = ~pd.Series(cat).str.startswith('C').values
    h = df.hex.values; ts = df.ts.values
    sn = np.r_[h[1:] == h[:-1], False]
    nxt = np.r_[np.arange(1, len(df)), len(df) - 1]
    gr = df.ground.values; agl = df.agl.values; elev = df.elev.values
    msl = np.where(gr, elev, df.alt_msl.values)
    ok = sn & (np.r_[np.diff(ts), 0] <= 120) & ~(gr & gr[nxt]) & (np.minimum(agl, agl[nxt]) <= 4500) & keep_ac & (ts[nxt] >= t0 - 60) & (ts <= t1 + 60)
    si = np.flatnonzero(ok)
    la0, lo0, la1, lo1 = df.lat.values[si], df.lon.values[si], df.lat.values[si + 1], df.lon.values[si + 1]
    L = np.hypot((la1 - la0) * 111132, (lo1 - lo0) * 111320 * np.cos(np.radians(la0)))
    k = np.maximum(1, np.ceil(L / 100).astype(np.int64))
    hexcodes, hid = np.unique(h, return_inverse=True)
    heli_ac = df.heli.values; jet_ac = np.isin(cat, ['A3', 'A4', 'A5'])
    # process segments in chunks aligned to aircraft boundaries
    seg_h = hid[si]
    bounds = np.r_[0, np.flatnonzero(np.diff(seg_h)) + 1, len(si)]
    cum = np.cumsum(k)
    ev_cell, ev_t, ev_agl, ev_hid = [], [], [], []
    di = np.arange(-5, 6)
    DI, DJ = np.meshgrid(di, di, indexing='ij'); DI = DI.ravel(); DJ = DJ.ravel()
    start = 0
    while start < len(bounds) - 1:
        end = start + 1
        while end < len(bounds) - 1 and (cum[bounds[end + 1] - 1] - (cum[bounds[start] - 1] if bounds[start] > 0 else 0)) < 250000:
            end += 1
        a, b = bounds[start], bounds[end]
        kk = k[a:b]; rep = np.repeat(np.arange(a, b), kk)
        frac = (np.arange(kk.sum()) - np.repeat(np.cumsum(kk) - kk, kk) + 0.5) / np.repeat(kk, kk)
        s_ = si[rep]
        slat = df.lat.values[s_] + frac * (df.lat.values[s_ + 1] - df.lat.values[s_])
        slon = df.lon.values[s_] + frac * (df.lon.values[s_ + 1] - df.lon.values[s_])
        smsl = msl[s_] + frac * (msl[s_ + 1] - msl[s_])
        st = ts[s_] + frac * (ts[s_ + 1] - ts[s_])
        sh = hid[s_]
        m = (st >= t0) & (st < t1)
        slat, slon, smsl, st, sh = slat[m], slon[m], smsl[m], st[m], sh[m]
        i0 = np.round((slat - lat0) / dlat).astype(np.int64); j0 = np.round((slon - lon0) / dlon).astype(np.int64)
        I = (i0[:, None] + DI[None, :]).ravel(); J = (j0[:, None] + DJ[None, :]).ravel()
        n = len(slat); R = np.repeat(np.arange(n), len(DI))
        inb = (I >= 0) & (I < ny) & (J >= 0) & (J < nx)
        I, J, R = I[inb], J[inb], R[inb]
        dy = (lat0 + I * dlat - slat[R]) * 111132.0; dx = (lon0 + J * dlon - slon[R]) * 111320.0 * np.cos(np.radians(slat[R]))
        d = np.hypot(dx, dy)
        a_c = smsl[R] - elev_c[I, J]
        q = (d <= 1000) & (a_c <= 3000)
        I, J, R, d, a_c = I[q], J[q], R[q], d[q], a_c[q]
        cell = I * nx + J; t = st[R]; hh = sh[R]
        o = np.lexsort((t, hh, cell)); cell, t, hh, d, a_c = cell[o], t[o], hh[o], d[o], a_c[o]
        brk = np.r_[True, (cell[1:] != cell[:-1]) | (hh[1:] != hh[:-1]) | (np.diff(t) > 600)]
        eid = np.cumsum(brk) - 1
        o2 = np.lexsort((d, eid)); first = o2[np.r_[True, eid[o2][1:] != eid[o2][:-1]]]
        ev_cell.append(cell[first].astype(np.int64)); ev_t.append(t[first]); ev_agl.append(a_c[first].astype(np.float32)); ev_hid.append(hh[first])
        start = end
    cellv = np.concatenate(ev_cell); tv = np.concatenate(ev_t); aglv = np.concatenate(ev_agl); hv = np.concatenate(ev_hid)
    # per-aircraft class
    ac = pd.DataFrame(dict(hid=hid, heli=heli_ac, jet=jet_ac)).groupby('hid').agg(heli=('heli', 'max'), jet=('jet', 'max'))
    isheli = ac.heli.values[hv]; isjet = ac.jet.values[hv] & ~isheli; islight = ~isheli & ~isjet
    lt = pd.to_datetime(tv, unit='s', utc=True).tz_convert(tz)
    hr = lt.hour.values; wd = lt.weekday.values
    night = (hr >= 22) | (hr < 6)
    NC = ny * nx; ND = 13.0; NWD = float(sum(pd.Timestamp(f'2026-11-{d:02d}').weekday() < 5 for d in range(14, 27))); NWE = ND - NWD
    def per(mask, div):
        return (np.bincount(cellv[mask], minlength=NC) / div).reshape(ny, nx).astype(np.float32)
    allm = np.ones(len(cellv), bool)
    out = dict(lat0=lat0, lon0=lon0, dlat=dlat, dlon=dlon, ny=ny, nx=nx,
               passes_per_day=per(allm, ND), night_per_night=per(night, ND), weekday_per_day=per(wd < 5, NWD), weekend_per_day=per(wd >= 5, NWE),
               heli_per_day=per(isheli, ND), heli_night_per_night=per(isheli & night, ND), jet_per_day=per(isjet, ND), light_per_day=per(islight, ND))
    hourly = (np.bincount(cellv * 24 + hr, minlength=NC * 24) / ND).reshape(ny, nx, 24).astype(np.float32)
    out['hourly'] = hourly
    # median agl per cell
    o = np.lexsort((aglv, cellv)); cs = cellv[o]; av = aglv[o]
    cnt = np.bincount(cellv, minlength=NC); starts = np.r_[0, np.cumsum(cnt)[:-1]]
    med = np.full(NC, np.nan, np.float32); has = cnt > 0
    lo_i = starts[has] + (cnt[has] - 1) // 2; hi_i = starts[has] + cnt[has] // 2
    med[has] = (av[lo_i] + av[hi_i]) / 2
    out['median_agl_ft'] = med.reshape(ny, nx)
    np.savez_compressed(f'/content/grids/grid_{CITY[region]}.npz', **out)
    print(region, 'grid', ny, nx, 'events', len(cellv), 'max passes/day', float(out['passes_per_day'].max()), flush=True)
    return out

def heli_sites(df, region, tz, X, Y, TR):
    h = df.hex.values; ts = df.ts.values
    sn = np.r_[h[1:] == h[:-1], False]; sp = np.r_[False, h[1:] == h[:-1]]
    heli = df.heli.values; agl = df.agl.values; gr = df.ground.values; src = df.src.astype(str).values
    low = heli & (gr | (agl < 100)); idx = np.flatnonzero(low)
    rows = []
    if idx.size:
        brk = np.r_[True, (np.diff(idx) != 1) | (h[idx[1:]] != h[idx[:-1]]) | (np.diff(ts[idx]) > 600)]
        st = idx[brk]; en = np.r_[idx[np.flatnonzero(brk)[1:] - 1], idx[-1]]
        for s, e in zip(st, en):
            seg = slice(s, e + 1); j = s + int(np.argmin(np.where(gr[seg], -1, agl[seg])))
            arr = bool(sp[s] and ts[s] - ts[s - 1] <= 600 and not gr[s - 1] and agl[s - 1] >= 100)
            dep = bool(sn[e] and ts[e + 1] - ts[e] <= 600 and not gr[e + 1] and agl[e + 1] >= 100)
            if arr or dep:
                rows.append((X[j], Y[j], df.lat.values[j], df.lon.values[j], h[s], df.reg.values[s], ts[s], ts[e], int(arr), int(dep), src[s], bool(gr[seg].any())))
    S = pd.DataFrame(rows, columns=['x', 'y', 'lat', 'lon', 'hex', 'reg', 'ts', 'te', 'arr', 'dep', 'src', 'gany'])
    if S.empty: return S, pd.DataFrame()
    pairs = cKDTree(S[['x', 'y']].values).query_pairs(150, output_type='ndarray'); n = len(S)
    G = sps.coo_matrix((np.ones(len(pairs)), (pairs[:, 0], pairs[:, 1])), shape=(n, n)) if len(pairs) else sps.coo_matrix((n, n))
    S['cl'] = connected_components(G, directed=False)[1]
    # merge fragmented stops: same aircraft, same cluster, next stop starts <=600 s after previous ends -> one landing
    S = S.sort_values(['hex', 'ts']).reset_index(drop=True)
    same = (S.hex.values[1:] == S.hex.values[:-1]) & (S.cl.values[1:] == S.cl.values[:-1]) & ((S.ts.values[1:] - S.te.values[:-1]) <= 600)
    idx_m = np.flatnonzero(same)
    S.loc[idx_m, 'dep'] = 0; S.loc[idx_m + 1, 'arr'] = 0
    S['merged_fragments'] = 0; S.loc[idx_m, 'merged_fragments'] = 1
    S['h_arr'] = pd.to_datetime(S.ts, unit='s', utc=True).dt.tz_convert(tz).dt.hour
    S['h_dep'] = pd.to_datetime(S.te, unit='s', utc=True).dt.tz_convert(tz).dt.hour
    t0, t1 = window(tz)
    box = APT[APT.latitude_deg.between(df.lat.min(), df.lat.max()) & APT.longitude_deg.between(df.lon.min(), df.lon.max()) & (APT.type != 'closed')]
    bx, by = TR.transform(box.longitude_deg.values, box.latitude_deg.values); bx, by = np.asarray(bx), np.asarray(by)
    aero = box.type.isin(['small_airport', 'medium_airport', 'large_airport']).values
    out = []
    for c, g in S.groupby('cl'):
        cx, cy = g.x.median(), g.y.median()
        dd = np.hypot(bx - cx, by - cy)
        k = int(np.argmin(dd)) if len(dd) else None
        near_aero = bool(len(dd) and (dd[aero] <= 2000).any()) if aero.any() else False
        gw = g[(g.ts >= t0) & (g.ts < t1)]
        hourly = np.zeros(24, int)
        for _, r in gw.iterrows():
            if r.arr: hourly[r.h_arr] += 1
            if r.dep: hourly[r.h_dep] += 1
        night = int(hourly[[22, 23, 0, 1, 2, 3, 4, 5]].sum())
        out.append(dict(region=region, cluster=c, lat=round(float(g.lat.median()), 5), lon=round(float(g.lon.median()), 5),
                        arrivals_14d=int(g.arr.sum()), departures_14d=int(g.dep.sum()), aircraft_14d=g.hex.nunique(),
                        arrivals=int(gw.arr.sum()), departures=int(gw.dep.sum()), night_movements=night, aircraft_count=gw.hex.nunique(),
                        hourly=json.dumps(hourly.tolist()), mlat_share=round(float((g.src == 'mlat').mean()), 2),
                        near_ident=(box.iloc[k].ident if k is not None else ''), near_name=(box.iloc[k]['name'] if k is not None else ''),
                        near_type=(box.iloc[k].type if k is not None else ''), near_dist_m=(round(float(dd[k])) if k is not None else None),
                        within_2km_of_aerodrome=near_aero,
                        merged_fragments=int(g.merged_fragments.sum()), ground_stops=int(g.gany.sum()), stops=len(g), elev_ft=float(dem_ft(np.array([g.lat.median()]), np.array([g.lon.median()]))[0]),
                        hems_vhyx_movements=int(g[g.reg.fillna('').str.startswith('VH-YX')][['arr', 'dep']].sum().sum())))
    return S, pd.DataFrame(out)

for region in sys.argv[1:]:
    tz, clat, clon, codes = REG[region]
    df, offmed = load(region)
    TR = Transformer.from_crs(4326, f'+proj=aeqd +lat_0={clat} +lon_0={clon} +datum=WGS84 +units=m', always_xy=True)
    X, Y = TR.transform(df.lon.values, df.lat.values)
    h = df.hex.values; ts = df.ts.values
    sn = np.r_[h[1:] == h[:-1], False]; sp = np.r_[False, h[1:] == h[:-1]]
    A = (X, Y, df.agl.values.astype(float), df.ground.values, df.vrate.values.astype(float), df.gs.values.astype(float), h, ts, sn, sp)
    tl = pd.to_datetime(df.ts, unit='s', utc=True).dt.tz_convert(tz); hour = tl.dt.hour.values
    heli = df.heli.values; src = df.src.astype(str).values; cat = df.cat.fillna('').values
    print(region, 'rows', len(df), 'aircraft', df.hex.nunique(), 'baro offset median', offmed, flush=True)
    if os.environ.get('HELI_ONLY') != '1':
        build_grid(df, region, tz)
    # heli sites
    S, sites = heli_sites(df, region, tz, X, Y, TR)
    sites.to_csv(f'{OUT}/{region}_heli_sites.csv', index=False)
    if os.environ.get('HELI_ONLY') == '1':
        print('HELI_DONE', region, flush=True); continue
    # daily aircraft
    day_rows = []
    full = pd.date_range('2026-11-14', '2026-11-26').date
    main = APT[APT.ident == codes[0]].iloc[0]; mx, my = TR.transform(main.longitude_deg, main.latitude_deg)
    d20 = np.hypot(X - mx, Y - my) <= 37040
    ld = tl.dt.date
    for lab, msk in (('box_all_alt', np.ones(len(df), bool)), ('20nm_all_alt', d20), ('20nm_below3000ftAGL', d20 & (df.agl.values <= 3000))):
        s = df[msk].assign(d=ld[msk].values).groupby('d').hex.nunique().reindex(full, fill_value=0)
        day_rows.append(dict(region=region, scope=lab, mean_per_day=round(s.mean(), 1), min=int(s.min()), max=int(s.max()), days=len(s)))
    pd.DataFrame(day_rows).to_csv(f'{OUT}/{region}_daily_aircraft.csv', index=False)
    # source share
    src_rows = []
    for lab, msk in (('box <=10000ft AGL', np.ones(len(df), bool)), ('20nm <=3000ft AGL', d20 & (df.agl.values <= 3000))):
        for hl in (False, True):
            mm = msk & (heli == hl); vc = pd.Series(src[mm]).value_counts(normalize=True)
            src_rows.append(dict(region=region, scope=lab, heli=hl, n_pts=int(mm.sum()), adsb=round(float(vc.filter(like='adsb').sum()), 4), mlat=round(float(vc.get('mlat', 0)), 4)))
    g = pd.DataFrame(dict(hex=h, heli=heli, ml=(src == 'mlat'))).groupby('hex').agg(heli=('heli', 'max'), ml=('ml', 'mean'))
    for hl in (False, True):
        gg = g[g.heli == hl]
        src_rows.append(dict(region=region, scope='aircraft', heli=hl, n_pts=len(gg), mlat_only_aircraft=int((gg.ml > 0.95).sum())))
    pd.DataFrame(src_rows).to_csv(f'{OUT}/{region}_source_share.csv', index=False)
    # airports
    apt_rows = []
    for code in codes:
        a = APT[APT.ident == code].iloc[0]
        rw = RWY[(RWY.airport_ident == code) & (RWY.closed == 0)]
        ends = [(r[f'{p}_latitude_deg'], r[f'{p}_longitude_deg']) for _, r in rw.iterrows() for p in ('le', 'he') if pd.notna(r[f'{p}_latitude_deg']) and pd.notna(r[f'{p}_longitude_deg'])]
        if ends:
            ex, ey = TR.transform([e[1] for e in ends], [e[0] for e in ends]); ex, ey = np.asarray(ex), np.asarray(ey)
            cx, cy = ex.mean(), ey.mean(); R = float(np.clip(np.hypot(ex - cx, ey - cy).max() + 1500, 2000, 4500))
        else:
            cx, cy = TR.transform(a.longitude_deg, a.latitude_deg); ex = ey = np.array([]); R = 3000.0
        res = dict(region=region, airport=code, name=a['name'], lat=a.latitude_deg, lon=a.longitude_deg, R_m=round(R), n_rwy_ends=len(ends))
        for ml in (250, 100):
            ep = episodes(A, cx, cy, R, 500, ml)
            if len(ep):
                eh = heli[ep.s.values]; esrc = src[ep.s.values]; ehr = hour[ep.s.values]
                res[f'fw_mov_{ml}'] = int(ep.mov[~eh].sum()); res[f'heli_mov_{ml}'] = int(ep.mov[eh].sum())
                if ml == 250:
                    res['fw_mov_mlat_250'] = int(ep.mov[~eh & (esrc == 'mlat')].sum()); res['heli_mov_mlat_250'] = int(ep.mov[eh & (esrc == 'mlat')].sum())
                    res['lowpass_not_counted_250'] = int((~ep.real).sum())
            else:
                res[f'fw_mov_{ml}'] = res[f'heli_mov_{ml}'] = 0
        if len(ex):
            de = np.full(len(X), np.inf)
            for x_, y_ in zip(ex, ey): de = np.minimum(de, np.hypot(X - x_, Y - y_))
            agl = df.agl.values; gr = df.ground.values; vr = df.vrate.values
            idx = np.flatnonzero((de <= 3000) & (gr | (agl < 1000)) & ~heli)
            n_app = n_ok = 0; cls_ok = {'large': [0, 0], 'light': [0, 0]}
            if idx.size:
                brk = np.r_[True, (np.diff(idx) != 1) | (h[idx[1:]] != h[idx[:-1]]) | (np.diff(ts[idx]) > 120)]
                st = idx[brk]; en = np.r_[idx[np.flatnonzero(brk)[1:] - 1], idx[-1]]
                for s, e in zip(st, en):
                    if gr[s]: continue
                    desc = (vr[s] < -100) if not np.isnan(vr[s]) else (agl[e] < agl[s])
                    if not desc: continue
                    ok = bool(gr[s:e + 1].any() or agl[s:e + 1].min() <= 200)
                    n_app += 1; n_ok += ok
                    kk = 'large' if cat[s] in ('A2', 'A3', 'A4', 'A5') else ('light' if cat[s] == 'A1' else None)
                    if kk: cls_ok[kk][0] += 1; cls_ok[kk][1] += ok
            res.update(approaches=n_app, low_cov_ratio=round(n_ok / n_app, 3) if n_app else None,
                       large_approaches=cls_ok['large'][0], large_low_cov_ratio=round(cls_ok['large'][1] / cls_ok['large'][0], 3) if cls_ok['large'][0] else None,
                       light_approaches=cls_ok['light'][0], light_low_cov_ratio=round(cls_ok['light'][1] / cls_ok['light'][0], 3) if cls_ok['light'][0] else None)
        apt_rows.append(res); print(res, flush=True)
    pd.DataFrame(apt_rows).to_csv(f'{OUT}/{region}_airports.csv', index=False)
    print('REGION_DONE', region, flush=True)
    del df, X, Y, A
