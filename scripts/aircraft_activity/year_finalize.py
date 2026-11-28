"""Build the 12-month prototype files (grid/meta/helipads + quarterly grids) for one city from assembled Colab output.

usage: year_finalize.py REGION --raw DIR --official CSV --dest DIR [--dest DIR ...] [--phase2-meta DIR]

  --raw        directory holding the assembled output (grid_<city>*.npz, <REGION>_heli_sites.csv,
               <REGION>_mov_monthly.csv, summary.json) as relayed from the VM
  --official   Airservices monthly rows (parse_airservices.py output)
  --dest       output directory (repeatable: e.g. the local prototype folder and the Drive grids-12m folder)
  --phase2-meta  directory with the 14-day meta_<city>.json whose coverage label/note is carried over
"""
import argparse, json, os, re, shutil, math, urllib.request, urllib.parse, time
import pandas as pd

CITY = {'MEL': 'melbourne', 'SYD': 'sydney', 'BNE': 'brisbane', 'OOL': 'goldcoast', 'MCY': 'sunshinecoast', 'PER': 'perth', 'ADL': 'adelaide',
        'CBR': 'canberra', 'HBA': 'hobart', 'LST': 'launceston', 'DRW': 'darwin', 'CNS': 'cairns', 'TSV': 'townsville', 'NTL': 'newcastle'}
TZ = {'MEL': 'Australia/Melbourne', 'SYD': 'Australia/Sydney', 'NTL': 'Australia/Sydney', 'CBR': 'Australia/Sydney', 'BNE': 'Australia/Brisbane',
      'OOL': 'Australia/Brisbane', 'MCY': 'Australia/Brisbane', 'CNS': 'Australia/Brisbane', 'TSV': 'Australia/Brisbane', 'PER': 'Australia/Perth',
      'ADL': 'Australia/Adelaide', 'HBA': 'Australia/Hobart', 'LST': 'Australia/Hobart', 'DRW': 'Australia/Darwin'}
AIRPORT_NAMES = {}
HOSP = re.compile('hospital|health|medical', re.I)

def osm_hospital_flags(points):
    """One Overpass request for the bbox of all points; True when a hospital, or a helipad whose name/operator mentions a hospital, lies within 200 m."""
    if not points: return []
    la = [p[0] for p in points]; lo = [p[1] for p in points]
    bb = f'{min(la) - 0.01},{min(lo) - 0.01},{max(la) + 0.01},{max(lo) + 0.01}'
    q = f'[out:json][timeout:120];(nwr[aeroway=helipad]({bb});nwr[amenity=hospital]({bb}););out tags center;'
    for attempt in range(5):
        try:
            req = urllib.request.Request('https://overpass-api.de/api/interpreter', data=urllib.parse.urlencode({'data': q}).encode(),
                                         headers={'User-Agent': 'aircraft-activity-pipeline/1.0'})
            els = json.load(urllib.request.urlopen(req, timeout=180))['elements']; break
        except Exception as e:
            print('overpass retry', attempt, e); time.sleep(15 * (attempt + 1))
    else:
        print('OVERPASS FAILED: kind falls back to OurAirports names'); return [False] * len(points)
    feats = []
    for e in els:
        c = e.get('center') or {'lat': e.get('lat'), 'lon': e.get('lon')}
        if c.get('lat') is None: continue
        t = e.get('tags', {}); name = ' '.join(str(v) for k, v in t.items() if k in ('name', 'operator'))
        if t.get('amenity') == 'hospital' or (t.get('aeroway') == 'helipad' and HOSP.search(name)):
            feats.append((c['lat'], c['lon']))
    return [any(math.hypot((a - p) * 111132, (b - q_) * 111320 * math.cos(math.radians(p))) <= 200 for a, b in feats) for p, q_ in points]

def main():
    ap = argparse.ArgumentParser(); ap.add_argument('region'); ap.add_argument('--raw', required=True); ap.add_argument('--official', required=True)
    ap.add_argument('--dest', action='append', required=True); ap.add_argument('--phase2-meta')
    a = ap.parse_args(); R = a.region; city = CITY[R]
    summ = json.load(open(os.path.join(a.raw, 'summary.json')))[R]
    good_days = sorted(set(summ.get('good_days', [])))
    months = summ['months']
    # --- airports: visible ratio vs Airservices, summed over months that have an official report ---
    off = pd.read_csv(a.official).fillna(0)
    mv = pd.read_csv(os.path.join(a.raw, f'{R}_mov_monthly.csv'))
    mstat = summ.get('month_days', {})
    airports = []
    for code in sorted(mv.airport.unique()):
        rec = dict(icao=code, name=None, lat=None, lon=None, official_month=None, fixed_wing_visible_ratio_low=None, fixed_wing_visible_ratio_high=None,
                   heli_visible_ratio_low=None, heli_visible_ratio_high=None)
        used = []; ours = {('fw', 100): 0.0, ('fw', 250): 0.0, ('he', 100): 0.0, ('he', 250): 0.0}; offi = {'fw': 0.0, 'he': 0.0}
        for m in months:
            o = off[(off.month == m) & (off.code == code)]
            if o.empty or m not in mstat or mstat[m]['good'] == 0: continue
            scale = mstat[m]['calendar'] / mstat[m]['good']   # official month is the calendar month; our month may miss days
            if mstat[m]['local_in_month'] < mstat[m]['calendar']: continue   # partial first/last month of the window
            o = o.iloc[0]; used.append(m)
            offi['fw'] += o.total - o.heli; offi['he'] += o.heli
            for hl, k in ((False, 'fw'), (True, 'he')):
                for ml in (100, 250):
                    ours[(k, ml)] += mv[(mv.airport == code) & (mv.month == m) & (mv.heli == hl) & (mv.minlow == ml)].mov.sum() * scale
        if used:
            rec['official_month'] = f'{used[0]}..{used[-1]} ({len(used)} months)'
            if offi['fw'] > 0:
                rec['fixed_wing_visible_ratio_low'] = round(ours[('fw', 100)] / offi['fw'], 3); rec['fixed_wing_visible_ratio_high'] = round(ours[('fw', 250)] / offi['fw'], 3)
            if offi['he'] > 0:
                rec['heli_visible_ratio_low'] = round(ours[('he', 100)] / offi['he'], 3); rec['heli_visible_ratio_high'] = round(ours[('he', 250)] / offi['he'], 3)
        airports.append(rec)
    apt_meta = summ.get('airports', {})
    for rec in airports:
        info = apt_meta.get(rec['icao'], {})
        rec.update({k: info.get(k) for k in ('name', 'lat', 'lon') if info.get(k) is not None})
    cov, note = None, None
    if a.phase2_meta and os.path.exists(os.path.join(a.phase2_meta, f'meta_{city}.json')):
        p2 = json.load(open(os.path.join(a.phase2_meta, f'meta_{city}.json'))); cov, note = p2['coverage'], p2['coverage_note']
        for rec in airports:
            m2 = next((x for x in p2['airports'] if x['icao'] == rec['icao']), None)
            if m2: rec.update({k: m2[k] for k in ('name', 'lat', 'lon') if rec.get(k) is None})
    meta = {'city': city, 'date_start': good_days[0] if good_days else None, 'date_end': good_days[-1] if good_days else None,
            'n_days': summ['n_days'], 'n_weekdays': summ['n_weekdays'], 'n_weekend_days': summ['n_weekend_days'],
            'radius_m': 1000, 'ceiling_ft_agl': 3000, 'timezone': TZ[R], 'source': 'adsb.lol globe_history (ODbL 1.0)', 'fixture': False,
            'coverage': cov, 'coverage_note': (note or '') + ' Coverage label from the 14-day validation (2026-11-13..26); airport ratios below are 12-month.',
            'sampling': 'every available day (no subsampling)', 'window': '2025-11-27..2026-11-26 local dates',
            'months_included': months, 'partial': bool(summ.get('partial', False)), 'missing_days': summ['missing_days'],
            'quarters': {q: {'months': v['months'], 'n_days': v['n_days']} for q, v in summ.get('quarters', {}).items()},
            'quarter_definition': 'calendar quarters of the local date (q1 Jan-Mar, q2 Apr-Jun, q3 Jul-Sep, q4 Oct-Dec; q4 mixes 2025-11-27..12-31 with 2026-10-01..11-26)',
            'airports': airports}
    # --- helipads ---
    hs = os.path.join(a.raw, f'{R}_heli_sites.csv'); pads = []
    if os.path.exists(hs) and os.path.getsize(hs) > 5:
        s = pd.read_csv(hs)
        s = s[((s.arrivals + s.departures) >= 2) & ~s.within_2km_of_aerodrome.astype(bool)]
        s = s[~((s.elev_ft <= 1) & (s.ground_stops == 0))]
        s = s[(s.ground_stops >= 1) | (s.arrivals >= 2)].copy()
        matched = (s.near_dist_m <= 300) & (s.near_type == 'heliport')
        s['name'] = [n if m else 'Unidentified landing site' for n, m in zip(s.near_name, matched)]
        s['source'] = ['ourairports' if m else 'clustered' for m in matched]
        osm = osm_hospital_flags(list(zip(s.lat, s.lon)))
        s['kind'] = ['hospital' if (src == 'ourairports' and HOSP.search(str(n))) or f else 'other' for n, src, f in zip(s.name, s.source, osm)]
        pads = [dict(name=r['name'], kind=r.kind, source=r.source, lat=float(r.lat), lon=float(r.lon), arrivals=int(r.arrivals), departures=int(r.departures),
                     night_movements=int(r.night_movements), aircraft_count=int(r.aircraft_count), hourly=[int(x) for x in json.loads(r.hourly)])
                for _, r in s.sort_values('arrivals', ascending=False).iterrows()]
    for d in a.dest:
        os.makedirs(d, exist_ok=True)
        for f in os.listdir(a.raw):
            if f.startswith(f'grid_{city}') and f.endswith('.npz'):
                shutil.copy(os.path.join(a.raw, f), os.path.join(d, f))
        json.dump(meta, open(os.path.join(d, f'meta_{city}.json'), 'w'), indent=1)
        json.dump(pads, open(os.path.join(d, f'helipads_{city}.json'), 'w'), indent=1)
    print(city, 'n_days', meta['n_days'], 'months', len(months), 'missing', len(meta['missing_days']), 'helipads', len(pads),
          'airports', [(x['icao'], x['fixed_wing_visible_ratio_low'], x['fixed_wing_visible_ratio_high']) for x in airports])

if __name__ == '__main__':
    main()
