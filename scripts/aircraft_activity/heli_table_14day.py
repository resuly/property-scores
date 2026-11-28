import pandas as pd, glob, os
# usage: heli_table_14day.py COLAB_DIR OUT_CSV
import sys
D = sys.argv[1]; rows=[]; empty=[]
for f in sorted(glob.glob(f'{D}/*_heli_sites.csv')):
    if os.path.getsize(f) < 5: empty.append(os.path.basename(f)[:3]); continue
    s=pd.read_csv(f)
    s['mov14']=s.arrivals_14d+s.departures_14d
    s=s[(s.mov14>=2)&~s.within_2km_of_aerodrome.astype(bool)&~((s.elev_ft<=1)&(s.ground_stops==0))&((s.ground_stops>=1)|(s.arrivals_14d>=2))].copy()
    s['ourairports_heliport']=[n if (d<=300 and t=='heliport') else '' for n,d,t in zip(s.near_name,s.near_dist_m,s.near_type)]
    s['hospital_by_name']=s.ourairports_heliport.str.contains('hospital|health|medical',case=False)
    s['night_14d_window13']=s.night_movements
    rows.append(s[['region','lat','lon','arrivals_14d','departures_14d','mov14','aircraft_14d','night_14d_window13','ourairports_heliport','hospital_by_name','ground_stops','mlat_share']])
T=pd.concat(rows); T.to_csv(sys.argv[2],index=False)
print('empty regions', empty)
print(T.groupby('region').agg(sites=('lat','size'),hospital_named=('hospital_by_name','sum'),mov=('mov14','sum')).to_string())
pd.set_option('display.width',250)
print(T[T.hospital_by_name].sort_values(['region','mov14'],ascending=[True,False]).to_string())
print(T[~T.hospital_by_name].sort_values('mov14',ascending=False).groupby('region').head(3).to_string())
