"""Phase-2 (14-day) prototype files: meta_<city>.json + helipads_<city>.json + grid copy, from the relayed Colab CSV/npz.

usage: finalize_14day.py REGION COVERAGE "coverage note"
env:   AA_COLAB_DIR (downloaded *_airports.csv, *_heli_sites.csv, grid_*.npz), AA_OFFICIAL_CSV (parse_airservices.py output),
       AA_DEST (output folder for the prototype files)
"""
import sys, json, os, re, shutil, urllib.request, urllib.parse, csv, math
import pandas as pd

DL = os.environ['AA_COLAB_DIR']
DEST = os.environ['AA_DEST']
OFFICIAL_CSV = os.environ['AA_OFFICIAL_CSV']
CITY = {'MEL': 'melbourne', 'SYD': 'sydney', 'BNE': 'brisbane', 'OOL': 'goldcoast', 'MCY': 'sunshinecoast', 'PER': 'perth', 'ADL': 'adelaide',
        'CBR': 'canberra', 'HBA': 'hobart', 'LST': 'launceston', 'DRW': 'darwin', 'CNS': 'cairns', 'TSV': 'townsville', 'NTL': 'newcastle'}
TZ = {'MEL': 'Australia/Melbourne', 'SYD': 'Australia/Sydney', 'NTL': 'Australia/Sydney', 'CBR': 'Australia/Sydney', 'BNE': 'Australia/Brisbane', 'OOL': 'Australia/Brisbane',
      'MCY': 'Australia/Brisbane', 'CNS': 'Australia/Brisbane', 'TSV': 'Australia/Brisbane', 'PER': 'Australia/Perth', 'ADL': 'Australia/Adelaide',
      'HBA': 'Australia/Hobart', 'LST': 'Australia/Hobart', 'DRW': 'Australia/Darwin'}
OFFICIAL_MONTH = '2025-11'
HOSP = re.compile('hospital|health|medical', re.I)

def official():
    o = pd.read_csv(OFFICIAL_CSV)
    o = o[o.month == OFFICIAL_MONTH].fillna(0).set_index('code')
    return o

def osm_hospital_flags(points):
    """points: list of (lat, lon). Returns list of bools: OSM helipad or hospital polygon/node within 150 m."""
    if not points: return []
    parts = ''.join(f'nwr(around:150,{la},{lo})[aeroway=helipad];nwr(around:150,{la},{lo})[amenity=hospital];' for la, lo in points)
    q = f'[out:json][timeout:90];({parts});out tags center;'
    req = urllib.request.Request('https://overpass-api.de/api/interpreter', data=urllib.parse.urlencode({'data': q}).encode(), headers={'User-Agent': 'flight-sample-research/1.0'})
    els = json.load(urllib.request.urlopen(req, timeout=120))['elements']
    out = []
    for la, lo in points:
        hit = False
        for e in els:
            c = e.get('center') or {'lat': e.get('lat'), 'lon': e.get('lon')}
            if c['lat'] is None: continue
            d = math.hypot((c['lat'] - la) * 111132, (c['lon'] - lo) * 111320 * math.cos(math.radians(la)))
            t = e.get('tags', {})
            name = ' '.join(str(v) for k, v in t.items() if k in ('name', 'operator'))
            if d <= 200 and (t.get('amenity') == 'hospital' or (t.get('aeroway') == 'helipad' and (HOSP.search(name) or 'hospital' in name.lower()))):
                hit = True; break
        out.append(hit)
    return out

def osm_bbox_flags(points):
    """One Overpass request for the bbox of all points: hospital helipads and hospitals; match locally within 200 m."""
    import time
    if not points: return []
    la = [p[0] for p in points]; lo = [p[1] for p in points]
    bb = f'{min(la) - 0.01},{min(lo) - 0.01},{max(la) + 0.01},{max(lo) + 0.01}'
    q = f'[out:json][timeout:120];(nwr[aeroway=helipad]({bb});nwr[amenity=hospital]({bb}););out tags center;'
    els = None
    for attempt in range(5):
        try:
            req = urllib.request.Request('https://overpass-api.de/api/interpreter', data=urllib.parse.urlencode({'data': q}).encode(), headers={'User-Agent': 'flight-sample-research/1.0'})
            els = json.load(urllib.request.urlopen(req, timeout=180))['elements']; break
        except Exception as e:
            print('overpass retry', attempt, e); time.sleep(15 * (attempt + 1))
    if els is None:
        print('OVERPASS FAILED: kind falls back to OurAirports names only'); return [False] * len(points)
    feats = []
    for e in els:
        c = e.get('center') or {'lat': e.get('lat'), 'lon': e.get('lon')}
        if c.get('lat') is None: continue
        t = e.get('tags', {}); name = ' '.join(str(v) for k, v in t.items() if k in ('name', 'operator'))
        if t.get('amenity') == 'hospital' or (t.get('aeroway') == 'helipad' and HOSP.search(name)):
            feats.append((c['lat'], c['lon']))
    print('osm hospital features', len(feats))
    out = []
    for p_la, p_lo in points:
        out.append(any(math.hypot((f_la - p_la) * 111132, (f_lo - p_lo) * 111320 * math.cos(math.radians(p_la))) <= 200 for f_la, f_lo in feats))
    return out

def main(region, coverage, note):
    city = CITY[region]; os.makedirs(DEST, exist_ok=True)
    shutil.copy(os.path.join(DL, f'grid_{city}.npz'), os.path.join(DEST, f'grid_{city}.npz'))
    ap = pd.read_csv(os.path.join(DL, f'{region}_airports.csv')); o = official()
    airports = []
    for _, r in ap.iterrows():
        rec = dict(icao=r.airport, name=r['name'], lat=round(float(r.lat), 6), lon=round(float(r.lon), 6), official_month=OFFICIAL_MONTH,
                   fixed_wing_visible_ratio_low=None, fixed_wing_visible_ratio_high=None, heli_visible_ratio_low=None, heli_visible_ratio_high=None)
        if r.airport in o.index:
            off = o.loc[r.airport]; fw_off = off.total - off.heli; he_off = off.heli
            f = 30.0 / 14.0
            if fw_off > 0:
                rec['fixed_wing_visible_ratio_low'] = round(r.fw_mov_100 * f / fw_off, 3); rec['fixed_wing_visible_ratio_high'] = round(r.fw_mov_250 * f / fw_off, 3)
            if he_off > 0:
                rec['heli_visible_ratio_low'] = round(r.heli_mov_100 * f / he_off, 3); rec['heli_visible_ratio_high'] = round(r.heli_mov_250 * f / he_off, 3)
        airports.append(rec)
    meta = {'city': city, 'date_start': '2026-11-14', 'date_end': '2026-11-26', 'n_days': 13, 'n_weekdays': 9, 'n_weekend_days': 4,
            'radius_m': 1000, 'ceiling_ft_agl': 3000, 'timezone': TZ[region], 'source': 'adsb.lol globe_history (ODbL 1.0)', 'fixture': False,
            'coverage': coverage, 'coverage_note': note, 'airports': airports}
    json.dump(meta, open(os.path.join(DEST, f'meta_{city}.json'), 'w'), indent=1)
    fp = os.path.join(DL, f'{region}_heli_sites.csv')
    if os.path.getsize(fp) < 5:
        json.dump([], open(os.path.join(DEST, f'helipads_{city}.json'), 'w')); print(city, 'no helicopter landing sites'); return
    s = pd.read_csv(fp)
    s = s[((s.arrivals + s.departures) >= 2) & ~s.within_2km_of_aerodrome.astype(bool)].copy()
    # drop low hovers over sea (DEM 0 ft, never reported on ground): not landing sites
    s = s[~((s.elev_ft <= 1) & (s.ground_stops == 0))].copy()
    # a single airborne dip below 100 ft is not a landing site: need a ground report or >=2 separate arrivals
    s = s[(s.ground_stops >= 1) | (s.arrivals >= 2)].copy()
    matched = (s.near_dist_m <= 300) & (s.near_type == 'heliport')
    s['name'] = [n if m else 'Unidentified landing site' for n, m in zip(s.near_name, matched)]
    s['source'] = ['ourairports' if m else 'clustered' for m in matched]
    osm = osm_bbox_flags(list(zip(s.lat, s.lon)))
    s['kind'] = ['hospital' if (HOSP.search(str(n)) and src == 'ourairports') or f else 'other' for n, src, f in zip(s.name, s.source, osm)]
    pads = [dict(name=r['name'], kind=r.kind, source=r.source, lat=float(r.lat), lon=float(r.lon), arrivals=int(r.arrivals), departures=int(r.departures),
                 night_movements=int(r.night_movements), aircraft_count=int(r.aircraft_count), hourly=[int(x) for x in json.loads(r.hourly)])
            for _, r in s.sort_values(['arrivals'], ascending=False).iterrows()]
    json.dump(pads, open(os.path.join(DEST, f'helipads_{city}.json'), 'w'), indent=1)
    sz = sum(os.path.getsize(os.path.join(DEST, f'{p}_{city}.{e}')) for p, e in (('grid', 'npz'), ('meta', 'json'), ('helipads', 'json')))
    print(city, 'files total bytes', sz, 'helipads', len(pads), 'hospital', sum(p['kind'] == 'hospital' for p in pads))

if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2], sys.argv[3])
