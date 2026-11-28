# Local: combine per-region CSVs into national tables.
import os, glob, pandas as pd, numpy as np
# usage: summary_14day.py COLAB_DIR OUT_DIR   (OUT_DIR must hold airservices_rows.csv)
import sys
D, O = sys.argv[1], sys.argv[2]
o = pd.read_csv(os.path.join(O, 'airservices_rows.csv')).fillna(0)
ap = pd.concat([pd.read_csv(f) for f in sorted(glob.glob(f'{D}/*_airports.csv'))], ignore_index=True)
f = 30.0 / 14.0
rows = []
for _, r in ap.iterrows():
    rec = dict(region=r.region, airport=r.airport, fw_14d_250=r.fw_mov_250, fw_14d_100=r.fw_mov_100, heli_14d_250=r.heli_mov_250, heli_14d_100=r.heli_mov_100,
               fw_mlat_share=round(r.fw_mov_mlat_250 / r.fw_mov_250, 3) if r.fw_mov_250 else np.nan,
               appr=r.get('approaches'), appr_le200=r.get('low_cov_ratio'), appr_large=r.get('large_low_cov_ratio'), appr_light=r.get('light_low_cov_ratio'))
    for m in ('2025-11', '2026-07'):
        x = o[(o.month == m) & (o.code == r.airport)]
        if x.empty: continue
        x = x.iloc[0]; fw = x.total - x.heli; he = x.heli; tag = m.replace('-', '')
        rec[f'off_fw_{tag}'] = int(fw); rec[f'off_heli_{tag}'] = int(he)
        rec[f'fw_ratio_{tag}'] = f"{r.fw_mov_100 * f / fw:.0%}-{r.fw_mov_250 * f / fw:.0%}" if fw else ''
        rec[f'heli_ratio_{tag}'] = f"{r.heli_mov_100 * f / he:.0%}-{r.heli_mov_250 * f / he:.0%}" if he else ('n/a (official 0)' if r.heli_mov_250 == 0 else f'official 0, seen {r.heli_mov_250}')
        rec[f'large_ratio_{tag}'] = f"{0:.0%}"
    rows.append(rec)
T = pd.DataFrame(rows); T.drop(columns=[c for c in T.columns if c.startswith('large_ratio')], inplace=True)
T.to_csv(os.path.join(O, 'airports_vs_official.csv'), index=False)
pd.set_option('display.width', 300); pd.set_option('display.max_columns', 40)
print(T.to_string())
src = pd.concat([pd.read_csv(f) for f in sorted(glob.glob(f'{D}/*_source_share.csv'))]); src.to_csv(os.path.join(O, 'source_share.csv'), index=False)
print(src[src.scope != 'aircraft'].pivot_table(index='region', columns=['scope', 'heli'], values='mlat').round(3).to_string())
print(src[src.scope == 'aircraft'].pivot_table(index='region', columns='heli', values=['n_pts', 'mlat_only_aircraft']).to_string())
day = pd.concat([pd.read_csv(f) for f in sorted(glob.glob(f'{D}/*_daily_aircraft.csv'))]); day.to_csv(os.path.join(O, 'daily_aircraft.csv'), index=False)
print(day.pivot_table(index='region', columns='scope', values=['mean_per_day', 'min', 'max']).to_string())
