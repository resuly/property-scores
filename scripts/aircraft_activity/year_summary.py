"""Summary tables for the 12-month run.

usage: year_summary.py --raw DIR --official CSV --out DIR [--grid13 DIR] [--testpoints CSV] [--maeo GEOJSON]

  --raw         relayed assembled output (grid_*.npz, *_mov_monthly.csv, *_appr_monthly.csv, *_heli_sites*.csv, summary.json)
  --official    Airservices monthly rows (parse_airservices.py)
  --grid13      folder with the 14-day prototype grids (grid_<city>.npz) for the 13-day vs 12-month comparison
  --testpoints  earlier point results (testpoints.csv, R=1000 rows) for Melbourne
  --maeo        VicPlan MAEO GeoJSON (MAEO1/MAEO2 polygons) for the Melbourne equal-area IoU
Writes CSVs into --out.
"""
import argparse, glob, json, math, os
import numpy as np, pandas as pd

CITY = {'MEL': 'melbourne', 'SYD': 'sydney', 'BNE': 'brisbane', 'OOL': 'goldcoast', 'MCY': 'sunshinecoast', 'PER': 'perth', 'ADL': 'adelaide',
        'CBR': 'canberra', 'HBA': 'hobart', 'LST': 'launceston', 'DRW': 'darwin', 'CNS': 'cairns', 'TSV': 'townsville', 'NTL': 'newcastle'}
MAIN = {'MEL': 'YMML', 'SYD': 'YSSY', 'BNE': 'YBBN', 'OOL': 'YBCG', 'MCY': 'YBSU', 'PER': 'YPPH', 'ADL': 'YPAD', 'CBR': 'YSCB',
        'HBA': 'YMHB', 'LST': 'YMLT', 'DRW': 'YPDN', 'CNS': 'YBCS', 'TSV': 'YBTL', 'NTL': 'YWLM'}
# extra Melbourne/Sydney probe points for runway-direction seasonality (approach/departure corridors)
PROBES = {'MEL': {'YMML_34_south_KeilorEast': (-37.7380, 144.8506), 'YMML_16_north_Bulla': (-37.6100, 144.8280),
                  'YMML_27_east_Airport_West': (-37.6650, 144.8950), 'YMML_09_west_Plumpton': (-37.6560, 144.7600)},
          'SYD': {'YSSY_16_north_StPeters': (-33.9100, 151.1760), 'YSSY_34_south_Kurnell_bay': (-34.0050, 151.1900),
                  'YSSY_07_west_Kingsgrove': (-33.9390, 151.1000), 'YSSY_25_east_Botany': (-33.9480, 151.2050)}}

def at(g, lat, lon, key):
    i = int(round((lat - float(g['lat0'])) / float(g['dlat']))); j = int(round((lon - float(g['lon0'])) / float(g['dlon'])))
    return float(g[key][i, j]) if 0 <= i < int(g['ny']) and 0 <= j < int(g['nx']) else float('nan')

def main():
    ap = argparse.ArgumentParser()
    for k in ('--raw', '--official', '--out'): ap.add_argument(k, required=True)
    ap.add_argument('--grid13'); ap.add_argument('--testpoints'); ap.add_argument('--maeo')
    a = ap.parse_args(); os.makedirs(a.out, exist_ok=True)
    summ = json.load(open(os.path.join(a.raw, 'summary.json')))
    off = pd.read_csv(a.official).fillna(0)
    # 1. monthly movements vs Airservices, every airport we count
    rows = []
    for R, info in summ.items():
        f = os.path.join(a.raw, f'{R}_mov_monthly.csv')
        if not os.path.exists(f): continue
        mv = pd.read_csv(f); md = info.get('month_days', {})
        for (code, m), g in mv.groupby(['airport', 'month']):
            d = md.get(m, {}); good = d.get('good', 0); cal = d.get('calendar', 0); loc = d.get('local_in_month', 0)
            rec = dict(region=R, airport=code, month=m, good_days=good, days_in_window=loc, calendar_days=cal)
            for hl, k in ((False, 'fw'), (True, 'heli')):
                for ml in (100, 250):
                    rec[f'{k}_{ml}'] = int(g[(g.heli == hl) & (g.minlow == ml)].mov.sum())
            o = off[(off.month == m) & (off.code == code)]
            if len(o) and good and loc == cal:
                o = o.iloc[0]; sc = cal / good
                rec['official_fw'] = int(o.total - o.heli); rec['official_heli'] = int(o.heli)
                if rec['official_fw']:
                    rec['fw_ratio_100'] = round(rec['fw_100'] * sc / rec['official_fw'], 3); rec['fw_ratio_250'] = round(rec['fw_250'] * sc / rec['official_fw'], 3)
                if rec['official_heli']:
                    rec['heli_ratio_100'] = round(rec['heli_100'] * sc / rec['official_heli'], 3); rec['heli_ratio_250'] = round(rec['heli_250'] * sc / rec['official_heli'], 3)
            rows.append(rec)
    pd.DataFrame(rows).to_csv(os.path.join(a.out, 'airport_movements_monthly_vs_airservices.csv'), index=False)
    # 2. monthly approach low-coverage
    rows = []
    for R in summ:
        f = os.path.join(a.raw, f'{R}_appr_monthly.csv')
        if not os.path.exists(f): continue
        x = pd.read_csv(f)
        for (code, m), g in x.groupby(['airport', 'month']):
            n = int(g.approaches.sum()); ok = int(g.reach_le200.sum())
            gl = g[g.large]; rows.append(dict(region=R, airport=code, month=m, approaches=n, reach_le200=ok, ratio=round(ok / n, 3) if n else None,
                                              large_approaches=int(gl.approaches.sum()), large_ratio=round(gl.reach_le200.sum() / gl.approaches.sum(), 3) if gl.approaches.sum() else None))
    pd.DataFrame(rows).to_csv(os.path.join(a.out, 'approach_low_coverage_monthly.csv'), index=False)
    # 3. Melbourne test points: 12-month vs 13-day grid vs earlier point method
    if a.testpoints and os.path.exists(os.path.join(a.raw, 'grid_melbourne.npz')):
        g12 = np.load(os.path.join(a.raw, 'grid_melbourne.npz'))
        g13 = np.load(os.path.join(a.grid13, 'grid_melbourne.npz')) if a.grid13 else None
        tp = pd.read_csv(a.testpoints); tp = tp[tp.R == 1000]; rows = []
        for _, r in tp.iterrows():
            rec = dict(point=r.point, lat=r.lat, lon=r.lon, point_method_14d_per_day=r.per_day)
            for key in ('passes_per_day', 'night_per_night', 'jet_per_day', 'heli_per_day', 'light_per_day', 'median_agl_ft'):
                rec[f'{key}_12m'] = round(at(g12, r.lat, r.lon, key), 2)
                if g13 is not None: rec[f'{key}_13d'] = round(at(g13, r.lat, r.lon, key), 2)
            for q in ('q1', 'q2', 'q3', 'q4'):
                f = os.path.join(a.raw, f'grid_melbourne_{q}.npz')
                if os.path.exists(f): rec[f'passes_per_day_{q}'] = round(at(np.load(f), r.lat, r.lon, 'passes_per_day'), 2)
            rec['ratio_12m_over_13d'] = round(rec['passes_per_day_12m'] / rec['passes_per_day_13d'], 3) if g13 is not None and rec['passes_per_day_13d'] else None
            rows.append(rec)
        pd.DataFrame(rows).to_csv(os.path.join(a.out, 'melbourne_testpoints_12m_vs_13d.csv'), index=False)
    # 4. runway-direction probes per quarter
    rows = []
    for R, pts in PROBES.items():
        c = CITY[R]
        for name, (la, lo) in pts.items():
            rec = dict(region=R, probe=name, lat=la, lon=lo)
            for tag, fn in [('12m', f'grid_{c}.npz')] + [(q, f'grid_{c}_{q}.npz') for q in ('q1', 'q2', 'q3', 'q4')]:
                f = os.path.join(a.raw, fn)
                if os.path.exists(f):
                    g = np.load(f); rec[f'jet_per_day_{tag}'] = round(at(g, la, lo, 'jet_per_day'), 2); rec[f'night_per_night_{tag}'] = round(at(g, la, lo, 'night_per_night'), 2)
            if a.grid13 and os.path.exists(os.path.join(a.grid13, f'grid_{c}.npz')):
                rec['jet_per_day_13d'] = round(at(np.load(os.path.join(a.grid13, f'grid_{c}.npz')), la, lo, 'jet_per_day'), 2)
            rows.append(rec)
    pd.DataFrame(rows).to_csv(os.path.join(a.out, 'runway_direction_probes_by_quarter.csv'), index=False)
    # 5. MAEO equal-area IoU (Melbourne jets), 12-month vs 13-day, raw and 1.5 km smoothed
    if a.maeo:
        from shapely.geometry import shape
        from shapely.ops import unary_union
        from shapely import contains_xy
        from scipy.ndimage import gaussian_filter
        m = json.load(open(a.maeo))
        U = unary_union([shape(f['geometry']) for f in m['features']])
        rows = []
        for tag, f in [('12m', os.path.join(a.raw, 'grid_melbourne.npz'))] + ([('13d', os.path.join(a.grid13, 'grid_melbourne.npz'))] if a.grid13 else []) + \
                      [(q, os.path.join(a.raw, f'grid_melbourne_{q}.npz')) for q in ('q1', 'q2', 'q3', 'q4')]:
            if not os.path.exists(f): continue
            g = np.load(f); ny, nx = int(g['ny']), int(g['nx'])
            LA = float(g['lat0']) + np.arange(ny)[:, None] * float(g['dlat']) + 0 * np.arange(nx)[None, :]
            LO = float(g['lon0']) + np.arange(nx)[None, :] * float(g['dlon']) + 0 * np.arange(ny)[:, None]
            inside = contains_xy(U, LO, LA)
            dom = np.hypot((LA - -37.6655) * 111132, (LO - 144.8366) * 111320 * math.cos(math.radians(-37.6655))) <= 25000
            for sm in (0, 1500):
                D = g['jet_per_day'].astype(float)
                if sm: D = gaussian_filter(D, sm / 250.0)
                n = int((inside & dom).sum()); thr = np.sort(D[dom])[::-1][n - 1]; hi = (D >= thr) & dom
                rows.append(dict(grid=tag, smoothing_sigma_m=sm, maeo_cells=n, equal_area_threshold=round(float(thr), 3),
                                 iou=round(float((hi & inside).sum() / (hi | (inside & dom)).sum()), 3),
                                 median_inside=round(float(np.median(D[inside & dom])), 3), median_outside=round(float(np.median(D[~inside & dom])), 3)))
        pd.DataFrame(rows).to_csv(os.path.join(a.out, 'melbourne_maeo_iou_12m_vs_13d.csv'), index=False)
    # 6. hospital pad monthly movements (named OurAirports heliports + unidentified clusters with >= 10 movements)
    rows = []
    for R in summ:
        fs, fm = os.path.join(a.raw, f'{R}_heli_sites.csv'), os.path.join(a.raw, f'{R}_heli_sites_monthly.csv')
        if not (os.path.exists(fs) and os.path.exists(fm)) or os.path.getsize(fs) < 5: continue
        s = pd.read_csv(fs); mm = pd.read_csv(fm)
        s['name'] = np.where((s.near_dist_m <= 300) & (s.near_type == 'heliport'), s.near_name, 'Unidentified landing site')
        keep = s[(s.name.str.contains('Hospital|Health|Medical', case=False)) | ((s.arrivals + s.departures) >= 10) & ~s.within_2km_of_aerodrome]
        x = mm.merge(keep[['cluster', 'name', 'lat', 'lon']], on='cluster')
        x['region'] = R; rows.append(x)
    if rows:
        pd.concat(rows).to_csv(os.path.join(a.out, 'helipad_monthly_movements.csv'), index=False)
    # 7. runway-end usage by month (fixed-wing arrivals + departures), main airports and Melbourne/Sydney secondaries
    rows = []
    for R in summ:
        f = os.path.join(a.raw, f'{R}_rwy_monthly.csv')
        if not os.path.exists(f): continue
        x = pd.read_csv(f); x = x[~x.heli]
        for (code, m), g in x.groupby(['airport', 'month']):
            tot = g.n.sum()
            for (rw, op), n in g.groupby(['rwy', 'op']).n.sum().items():
                rows.append(dict(region=R, airport=code, month=m, rwy=str(rw), op=op, n=int(n), share_of_month=round(n / tot, 4)))
    if rows:
        rr = pd.DataFrame(rows); rr.to_csv(os.path.join(a.out, 'runway_end_usage_monthly.csv'), index=False)
        # compact pivot: share of all fixed-wing movements per runway end, per month
        pv = rr.groupby(['airport', 'month', 'rwy']).n.sum().reset_index()
        pv['share'] = pv.n / pv.groupby(['airport', 'month']).n.transform('sum')
        pv.pivot_table(index=['airport', 'month'], columns='rwy', values='share', fill_value=0).round(3).to_csv(os.path.join(a.out, 'runway_end_share_monthly_pivot.csv'))
    print('summary written to', a.out)

if __name__ == '__main__':
    main()
