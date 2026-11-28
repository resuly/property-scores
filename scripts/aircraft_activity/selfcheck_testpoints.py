"""Compare a city grid at known test points with an earlier point-based result (R=1000 m rows).

usage: selfcheck_testpoints.py GRID_NPZ TESTPOINTS_CSV OUT_CSV
"""
import sys, numpy as np, pandas as pd, os
g = np.load(sys.argv[1])
lat0, lon0, dlat, dlon, ny, nx = [float(g[k]) for k in ('lat0', 'lon0', 'dlat', 'dlon', 'ny', 'nx')]
tp = pd.read_csv(sys.argv[2])
tp = tp[tp.R == 1000]
rows = []
for _, r in tp.iterrows():
    i = int(round((r.lat - lat0) / dlat)); j = int(round((r.lon - lon0) / dlon))
    rows.append(dict(point=r.point, phase1_per_day=r.per_day, grid_per_day=round(float(g['passes_per_day'][i, j]), 2),
                     phase1_night=r.night_per_night, grid_night=round(float(g['night_per_night'][i, j]), 2),
                     phase1_heli=r.heli_per_day, grid_heli=round(float(g['heli_per_day'][i, j]), 2),
                     phase1_median_agl=r.median_agl_ft, grid_median_agl=round(float(g['median_agl_ft'][i, j])),
                     hourly_sum_minus_total=round(float(g['hourly'][i, j].sum() - g['passes_per_day'][i, j]), 4),
                     cell_offset_m=round(float(np.hypot((lat0 + i * dlat - r.lat) * 111132, (lon0 + j * dlon - r.lon) * 111320 * np.cos(np.radians(r.lat)))))))
d =pd.DataFrame(rows); d['ratio'] = (d.grid_per_day / d.phase1_per_day).round(3)
d.to_csv(sys.argv[3], index=False)
pd.set_option('display.width', 250); print(d.to_string())
print('shape', g['passes_per_day'].shape, g['hourly'].shape, 'dtype', g['passes_per_day'].dtype, 'nan median cells', int(np.isnan(g['median_agl_ft']).sum()),
      'max |hourly sum - total|', float(np.abs(g['hourly'].sum(2) - g['passes_per_day']).max()))
