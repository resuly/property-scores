# Colab: roll processed UTC-day files into local-month aggregates. python year_rollup.py  (idempotent; rolls every month whose UTC days are all done)
import os, glob, json, datetime as dt
import numpy as np, pandas as pd
Y = '/content/year'; M = f'{Y}/month'; os.makedirs(M, exist_ok=True)
D0 = dt.date(2025, 11, 1)
L_START, L_END = dt.date(2025, 11, 27), dt.date(2026, 11, 26)
TZ = {'MEL': 'Australia/Melbourne', 'SYD': 'Australia/Sydney', 'NTL': 'Australia/Sydney', 'CBR': 'Australia/Sydney', 'BNE': 'Australia/Brisbane',
      'OOL': 'Australia/Brisbane', 'MCY': 'Australia/Brisbane', 'CNS': 'Australia/Brisbane', 'TSV': 'Australia/Brisbane', 'PER': 'Australia/Perth',
      'ADL': 'Australia/Adelaide', 'HBA': 'Australia/Hobart', 'LST': 'Australia/Hobart', 'DRW': 'Australia/Darwin'}
REGIONS = ['MEL', 'SYD', 'BNE', 'OOL', 'MCY', 'PER', 'ADL', 'CBR', 'HBA', 'LST', 'DRW', 'CNS', 'TSV', 'NTL']

def utc_str(d): return d.strftime('%Y.%m.%d')

def status():
    plan = pd.read_csv(f'{Y}/day_plan.csv'); med = plan[plan.bytes > 0].bytes.median()
    done = {}
    if os.path.exists(f'{Y}/done.jsonl'):
        for line in open(f'{Y}/done.jsonl'):
            r = json.loads(line); done[r['date']] = r
    bad_utc = {}
    for _, p in plan.iterrows():
        r = done.get(p.date)
        if p.kind == 'MISSING': bad_utc[p.date] = 'no release'
        elif p.bytes < 0.5 * med: bad_utc[p.date] = f'partial archive ({p.kind}, {p.bytes / 1e9:.2f} GB vs median {med / 1e9:.2f})'
        elif r is not None and r['status'] != 'OK': bad_utc[p.date] = r['status']
    missing_local = {}
    for u, why in bad_utc.items():
        ud = dt.datetime.strptime(u, '%Y.%m.%d').date()
        for L in (ud, ud + dt.timedelta(1)):
            if L_START <= L <= L_END: missing_local[L.isoformat()] = f'UTC archive {u}: {why}'
    return done, bad_utc, missing_local

def month_list():
    out = []; d = L_START.replace(day=1)
    while d <= L_END:
        out.append(d); d = (d.replace(day=28) + dt.timedelta(4)).replace(day=1)
    return out

def main():
    done, bad_utc, missing_local = status()
    json.dump(dict(bad_utc=bad_utc, missing_local=missing_local), open(f'{Y}/missing.json', 'w'), indent=1)
    rolled = []
    for m0 in month_list():
        m1 = (m0.replace(day=28) + dt.timedelta(4)).replace(day=1) - dt.timedelta(1)
        a, b = max(m0, L_START), min(m1, L_END)
        tag = m0.strftime('%Y-%m')
        need = [utc_str(a - dt.timedelta(1) + dt.timedelta(i)) for i in range((b - a).days + 2)]
        if os.path.exists(f'{M}/{tag}/DONE'):
            prev = json.load(open(f'{M}/{tag}/meta.json'))
            lds_ = [(a + dt.timedelta(i)).isoformat() for i in range((b - a).days + 1)]
            now_missing = [L for L in lds_ if L in missing_local]
            if sorted(now_missing) == sorted(prev['missing_days']): continue
            os.remove(f'{M}/{tag}/DONE')          # a dropped day has since been processed: roll the month again
        if not all(u in done for u in need): continue
        os.makedirs(f'{M}/{tag}', exist_ok=True)
        lds = [(a + dt.timedelta(i)) for i in range((b - a).days + 1)]
        good = [L for L in lds if L.isoformat() not in missing_local]
        good_idx = set((L - D0).days for L in good)
        wd = {(L - D0).days: int(L.weekday() < 5) for L in lds}
        for region in REGIONS:
            files = [f'{Y}/day/{u}_{region}_grid.parquet' for u in need if os.path.exists(f'{Y}/day/{u}_{region}_grid.parquet')]
            if files:
                g = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
                g = g[g.ld.isin(good_idx)]
                g['wd'] = g.ld.map(wd).astype(np.int8)
                g.groupby(['cell', 'wd', 'hour', 'cls']).n.sum().reset_index().to_parquet(f'{M}/{tag}/{region}_grid.parquet', compression='zstd')
                hf = [f.replace('_grid.', '_hist.') for f in files]
                hst = pd.concat([pd.read_parquet(f) for f in hf if os.path.exists(f)], ignore_index=True)
                hst = hst[hst.ld.isin(good_idx)]
                hst.groupby(['cell', 'bin']).n.sum().reset_index().to_parquet(f'{M}/{tag}/{region}_hist.parquet', compression='zstd')
            for kind in ('mov', 'appr', 'ac', 'stops'):
                parts = []
                for u in need:
                    f = f'{Y}/day/{u}_{region}_{kind}.csv'
                    if os.path.exists(f) and os.path.getsize(f) > 5:
                        x = pd.read_csv(f); parts.append(x)
                if parts:
                    x = pd.concat(parts, ignore_index=True)
                    if kind == 'stops':
                        lt = pd.to_datetime(x.ts, unit='s', utc=True).dt.tz_convert(TZ[region]).dt.tz_localize(None).dt.normalize()
                        x['ld'] = (lt - pd.Timestamp(D0)).dt.days
                        x = x[x.ld.isin(good_idx)]
                    else:
                        x = x[x.ld.isin([(L - D0).days for L in lds])]
                    x.to_csv(f'{M}/{tag}/{region}_{kind}.csv', index=False)
        json.dump(dict(month=tag, local_days=[L.isoformat() for L in lds], good_days=[L.isoformat() for L in good],
                       missing_days=[L.isoformat() for L in lds if L.isoformat() in missing_local], utc_days=need), open(f'{M}/{tag}/meta.json', 'w'), indent=1)
        open(f'{M}/{tag}/DONE', 'w').write('ok'); rolled.append(tag)
    print('rolled', rolled)

if __name__ == '__main__':
    main()
