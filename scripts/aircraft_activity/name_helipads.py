"""Give names to unnamed helicopter landing sites from OpenStreetMap: the facility the site sits at or beside.

usage: name_helipads.py --src DIR --dest DIR --basemap-cache DIR [--osm-cache DIR [--overpass]] [--city NAME ...] [--offline]

  --src            directory with helipads_<city>.json (year_finalize.py output); never written
  --dest           output directory: helipads_<city>.json only, same records plus name_source / name_kind / name_dist_m
                   (and name_candidates when ambiguous); must differ from --src
  --basemap-cache  per z14 tile, the kept features of our basemap (tiles.limontech.net, OpenMapTiles schema from OSM),
                   in a folder per archive version; cached tiles are not re-fetched
  --osm-cache      optional Overpass extracts per 0.25 deg tile (osm_<city>_<i>_<j>.json) merged in; polygons give
                   containment, so the same facility from a polygon wins over the basemap point
  --overpass       fetch missing Overpass tiles (the public instance is slow and often 502: off by default)
  --overpass-near  look up hospital polygons and helipad tags in Overpass only around sites with a basemap hospital
                   within 400 m or >= 100 movements (tens of points per city): resolves hospital campuses where
                   points alone are ambiguous, and names busy helipads whose OSM object has a name.
                   Cached as osm_near_<city>.json (with the point list); a failed lookup leaves that city basemap-only
  --offline        use caches only

Idempotent: the same src + caches give byte-identical output, and running it on its own output changes nothing
(sites named by this step are recognised by name_source osm_* / basemap_* and re-derived; other names are kept).

Rules, in order (a landing site is an ADS-B cluster point, not an OSM object; distances are from that point):
  0. A site that already has a name (OurAirports heliport within 300 m) keeps it: name_source=ourairports.
  1. Own tags (Overpass only; basemap helipads carry no name): an OSM aeroway=helipad|heliport within SELF_M with a
     name (else operator) -> osm_helipad_tag.
  2. Facility: inside its polygon (Overpass) or within the radius of its point (basemap POI) / outline.
     Hospital first (amenity=hospital, healthcare=hospital; basemap poi subclass=hospital, never clinic; 250 m),
     then airport (aerodrome, 300 m), emergency (police / fire / ambulance / SES, 100 m), other (hotel / resort,
     and from Overpass also military, TV studio, mine / quarry; 100 m).
     Ambiguity: every differently named facility of that category within radius + 150 m competes; the nearest is
     used only when it is within 60 m (0 = inside) and the next is at least 3x as far, or the next is at least 150 m
     farther. Otherwise no name (name_source=ambiguous_<cat>, name_candidates). The same facility counts once (same
     name; "<name> (Emergency Department)" folds into "<name>"); loading docks, car parks, a bare "Emergency" are
     not facilities. name_source: basemap_poi_<cat>_within_<N>m, osm_<cat>_contains, osm_<cat>_within_<N>m.
     Evidence: hospital / emergency / other names are used only when an OSM or basemap helipad / heliport is mapped
     within 150 m of the site, or (hospitals and airfields only) the site has >= 20 movements in the period: a
     two-movement cluster near a CBD hotel, a dental hospital or a seaplane base is not their helipad. Otherwise no
     name (name_source=no_mapped_helipad_<cat>, name_candidates).
  3. Nothing -> name stays "Unidentified landing site", name_kind=unknown. Names are never invented.

Names: "<hospital> helipad", "<other facility> landing site". The existing kind / source / counts are not changed.
Data: OSM via our basemap tiles and optionally Overpass, ODbL 1.0 "(c) OpenStreetMap contributors".
"""
import argparse, json, math, os, re, sys, time, urllib.parse, urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mvt  # noqa: E402

UNNAMED = 'Unidentified landing site'
SELF_M = 120
RADIUS = {'hospital': 250, 'airport': 300, 'emergency': 100, 'other': 100}
ORDER = ('hospital', 'airport', 'emergency', 'other')
FETCH_PAD_DEG = 0.01
TILE_DEG = 0.25
KEEP_M = 1000   # cache keeps OSM objects within this distance of some landing site
OVERPASS = 'https://overpass-api.de/api/interpreter'
TILEJSON = 'https://tiles.limontech.net/limon.json'
HELIPAD_EVIDENCE_M = 150   # a facility gives its name only if a helipad is mapped this close to the site
ACTIVE_MOVEMENTS = 20   # ... or, for hospitals and airfields only, the site has at least this many movements in the period
BUSY_LOOKUP = 100   # sites this busy also get the targeted Overpass lookup for helipad tags
CACHE_FORMAT = 'v2'   # basemap cache layout (v2 keeps aeroway helipads)
COMPETE_EXTRA_M = 150   # a facility this much beyond the radius still makes the nearest one ambiguous
BASEMAP_MARGIN_M = 400   # also fetch neighbour z14 tiles when a site is this close to a tile edge


def category(t):
    """Facility category of an OSM tag dict, or None. 'self' = a helipad / heliport object."""
    if t.get('aeroway') in ('helipad', 'heliport'):
        return 'self'
    if t.get('amenity') == 'hospital' or t.get('healthcare') == 'hospital':
        return 'hospital'
    if t.get('aeroway') == 'aerodrome':
        return 'airport'
    if t.get('amenity') in ('police', 'fire_station', 'ambulance_station') or t.get('emergency') in ('ambulance_station', 'ses_station'):
        return 'emergency'
    if (t.get('landuse') in ('military', 'quarry') or t.get('military') or t.get('studio') or t.get('tourism') in ('hotel', 'resort')
            or t.get('leisure') == 'resort' or t.get('industrial') == 'mine'):
        return 'other'
    return None


def query(bbox):
    s, w, n, e = bbox
    bb = f'{s},{w},{n},{e}'
    sel = ['nwr["aeroway"~"^(helipad|heliport|aerodrome)$"]', 'nwr["amenity"~"^(hospital|police|fire_station|ambulance_station)$"]',
           'nwr["healthcare"="hospital"]', 'nwr["emergency"~"^(ambulance_station|ses_station)$"]', 'nwr["landuse"~"^(military|quarry)$"]',
           'nwr["military"]', 'nwr["studio"]', 'nwr["tourism"~"^(hotel|resort)$"]', 'nwr["leisure"="resort"]', 'nwr["industrial"="mine"]']
    return '[out:json][timeout:300];(' + ''.join(f'{x}({bb});' for x in sel) + ');out tags geom;'


class Proj:
    """Local equirectangular metres around a reference latitude (error < 2 % over a city box)."""
    def __init__(self, lat0):
        self.kx = 111320 * math.cos(math.radians(lat0)); self.ky = 111132.0

    def __call__(self, lat, lon):
        return (lon * self.kx, lat * self.ky)


def _rings(el):
    """Closed rings (lists of (lat, lon)) of a way / multipolygon relation with geometry, else []."""
    if el['type'] == 'way':
        g = [(p['lat'], p['lon']) for p in el.get('geometry') or []]
        return [g] if len(g) >= 4 and g[0] == g[-1] else []
    if el['type'] == 'relation':
        # join outer member ways end to end into closed rings
        segs = [[(p['lat'], p['lon']) for p in m.get('geometry') or []] for m in el.get('members', [])
                if m.get('type') == 'way' and m.get('role', 'outer') in ('outer', '') and m.get('geometry')]
        rings = []
        while segs:
            cur = segs.pop(0)
            changed = True
            while cur[0] != cur[-1] and changed:
                changed = False
                for i, s in enumerate(segs):
                    if s[0] == cur[-1]: cur += s[1:]
                    elif s[-1] == cur[-1]: cur += s[::-1][1:]
                    elif s[-1] == cur[0]: cur = s + cur[1:]
                    elif s[0] == cur[0]: cur = s[::-1] + cur[1:]
                    else: continue
                    segs.pop(i); changed = True; break
            if len(cur) >= 4 and cur[0] == cur[-1]:
                rings.append(cur)
        return rings
    return []


def _point(el):
    if el['type'] == 'node':
        return (el['lat'], el['lon'])
    pts = [(p['lat'], p['lon']) for p in el.get('geometry') or [] if p]
    for m in el.get('members', []) or []:
        pts += [(p['lat'], p['lon']) for p in m.get('geometry') or [] if p]
    if not pts:
        return None
    return (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))


def _inside(x, y, ring):
    c = False
    for (x1, y1), (x2, y2) in zip(ring, ring[1:]):
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            c = not c
    return c


def _seg_dist(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    L = dx * dx + dy * dy
    t = 0 if L == 0 else max(0, min(1, ((px - ax) * dx + (py - ay) * dy) / L))
    return math.hypot(px - ax - t * dx, py - ay - t * dy)


class Feature:
    def __init__(self, el, proj):
        self.tags = el.get('tags', {}); self.cat = category(self.tags); self.src = 'basemap' if el.get('_src') == 'basemap' else 'osm'
        self.id = f"{el['type']}/{el['id']}"
        self.rings = [[proj(la, lo) for la, lo in r] for r in _rings(el)]
        p = _point(el)
        self.pt = proj(*p) if p else None

    def dist(self, x, y):
        """0 inside a polygon, else metres to the outline (polygons) or to the point (nodes / open ways)."""
        if self.rings:
            if any(_inside(x, y, r) for r in self.rings):
                return 0.0
            return min(_seg_dist(x, y, *a, *b) for r in self.rings for a, b in zip(r, r[1:]))
        if self.pt is None:
            return math.inf
        return math.hypot(x - self.pt[0], y - self.pt[1])

    def name(self):
        return (self.tags.get('name') or self.tags.get('official_name') or '').strip()


def _norm(s):
    return re.sub(r'[^a-z0-9]+', ' ', s.lower()).strip()


def facility_name(cat, nm):
    if cat == 'hospital':
        return nm if re.search(r'heli(pad|port)', nm, re.I) else f'{nm} helipad'
    return nm if re.search(r'heli(pad|port)|landing', nm, re.I) else f'{nm} landing site'


NOT_A_FACILITY = re.compile(r'^(the )?(emergency( department)?|ed|hospital|helipad|main entrance)$|loading dock|car ?park|parking|entrance|drop ?off|bus stop', re.I)


def pick(cands, radius):
    """cands: (dist_m, name, src) up to radius + COMPETE_EXTRA_M. One clear winner within radius -> (name, dist, src);
    else ('AMBIGUOUS', [names], None) or None when nothing is within radius.

    Same facility counts once: equal names after normalising, and a name that extends another one
    ("Fiona Stanley Hospital (Emergency Department)" -> "Fiona Stanley Hospital"); docks, car parks, bare
    "Emergency" are dropped. The nearest wins when no other facility is within the competitor window, or it is
    within 60 m and the next is at least 3x as far, or the next is at least 150 m farther."""
    best = {}
    for d, nm, src in cands:
        if NOT_A_FACILITY.search(nm.strip()):
            continue
        k = _norm(nm)
        if k not in best or (d, src) < best[k][:1] + (best[k][2],):
            best[k] = (d, nm, src)
    for k in sorted(best, key=len):   # fold "<base> <suffix>" into "<base>"
        if k not in best:
            continue
        for k2 in [k2 for k2 in best if k2 != k and (k2.startswith(k + ' ') or k2.startswith('the ' + k + ' '))]:
            if best[k2][0] < best[k][0]:
                best[k] = (best[k2][0], best[k][1], best[k2][2])
            del best[k2]
    ranked = sorted(best.values())
    if not ranked or ranked[0][0] > radius:
        return None
    if len(ranked) == 1:
        return ranked[0][1], ranked[0][0], ranked[0][2]
    (d1, n1, s1), (d2, _, _) = ranked[0], ranked[1]
    if (d1 <= 60 and d2 >= 3 * max(d1, 1)) or d2 - d1 >= 150:
        return n1, d1, s1
    return 'AMBIGUOUS', [n for _, n, _ in ranked], None


def _own(src):
    return bool(src) and (src.startswith('osm_') or src.startswith('basemap_'))


def _source(src, cat, d):
    if src == 'basemap':
        return f'basemap_poi_{cat}_within_{round(d)}m'
    return f'osm_{cat}_contains' if d == 0 else f'osm_{cat}_within_{round(d)}m'


def name_site(pad, feats, proj):
    """Return the fields to add to one landing site record (does not mutate pad)."""
    nm = pad.get('name')
    if nm and nm != UNNAMED and not _own(pad.get('name_source')):
        return {'name': nm, 'name_source': pad.get('name_source') or pad.get('source') or 'existing',
                'name_kind': 'hospital' if pad.get('kind') == 'hospital' else 'other', 'name_dist_m': pad.get('name_dist_m')}
    x, y = proj(pad['lat'], pad['lon'])
    near = [(f.dist(x, y), f) for f in feats]
    # 1. own helipad tags (OSM only: basemap helipads carry no name)
    own = [(d, (f.name() or (f.tags.get('operator') or '').strip()), f) for d, f in near if f.cat == 'self' and d <= SELF_M + COMPETE_EXTRA_M]
    own = [(d, n if re.search(r'heli(pad|port)|landing', n, re.I) else f'{n} helipad', f.src) for d, n, f in own if n]
    if own:
        r = pick(own, SELF_M)
        if r and r[0] != 'AMBIGUOUS':
            hosp = bool(re.search('hospital|medical|health', r[0], re.I))
            return {'name': r[0], 'name_source': f'{r[2]}_helipad_tag', 'name_kind': 'hospital' if hosp else 'other', 'name_dist_m': round(r[1])}
    # 2. facility, by category order; OSM polygons and basemap points of the same name merge (nearest distance wins)
    helipad_near = any(f.cat == 'self' and d <= HELIPAD_EVIDENCE_M for d, f in near)
    for cat in ORDER:
        c = [(d, f.name(), f.src) for d, f in near if f.cat == cat and d <= RADIUS[cat] + COMPETE_EXTRA_M and f.name()]
        r = pick(c, RADIUS[cat])
        if r is None:
            continue
        if r[0] == 'AMBIGUOUS':
            return {'name': UNNAMED, 'name_source': f'ambiguous_{cat}', 'name_kind': 'unknown', 'name_dist_m': None,
                    'name_candidates': r[1][:4]}
        busy = (pad.get('arrivals') or 0) + (pad.get('departures') or 0) >= ACTIVE_MOVEMENTS
        if not helipad_near and not (cat in ('hospital', 'airport') and busy):
            return {'name': UNNAMED, 'name_source': f'no_mapped_helipad_{cat}', 'name_kind': 'unknown', 'name_dist_m': None,
                    'name_candidates': [r[0]]}
        kind = cat if cat in ('hospital', 'airport', 'emergency') else 'other'
        return {'name': facility_name(cat, r[0]), 'name_source': _source(r[2], cat, r[1]), 'name_kind': kind, 'name_dist_m': round(r[1])}
    return {'name': UNNAMED, 'name_source': 'none', 'name_kind': 'unknown', 'name_dist_m': None}


def name_pads(pads, elements):
    """Named copies of pads (same order). elements: Overpass-style elements (out tags geom); '_src': 'basemap' marks
    elements converted from basemap tiles, anything else counts as OSM (Overpass)."""
    if not pads:
        return []
    proj = Proj(sum(p['lat'] for p in pads) / len(pads))
    feats = [f for f in (Feature(e, proj) for e in elements) if f.cat]
    out = []
    for p in pads:
        q = {k: v for k, v in p.items() if k not in ('name_source', 'name_kind', 'name_dist_m', 'name_candidates')}
        add = name_site(p, feats, proj)
        q['name'] = add.pop('name')
        q.update(add)
        out.append(q)
    return out


# ---- basemap tiles (OpenMapTiles schema, tiles.limontech.net) ----
def basemap_category_tags(layer, t):
    """OSM-style tags for a basemap feature worth keeping, else None. Clinics are not hospitals.
    Helipads (aeroway layer) carry no name in the basemap: kept only as evidence that a helipad is mapped there."""
    if layer == 'aeroway' and t.get('class') in ('helipad', 'heliport'):
        return {'aeroway': t['class']}
    nm = (t.get('name') or t.get('name_en') or '').strip()
    if not nm:
        return None
    if layer == 'poi':
        cl, sc = t.get('class'), t.get('subclass')
        if cl == 'hospital' and sc == 'hospital':
            return {'amenity': 'hospital', 'name': nm}
        if sc in ('police', 'fire_station', 'ambulance_station'):
            return {'amenity': sc, 'name': nm}
        if sc == 'ses_station':
            return {'emergency': sc, 'name': nm}
        if sc in ('hotel', 'resort'):
            return {'tourism': sc, 'name': nm}
        if sc == 'heliport':
            return {'aeroway': 'heliport', 'name': nm}
        return None
    if layer == 'aerodrome_label':
        return {'aeroway': 'aerodrome', 'name': nm}
    return None


def basemap_tile_elements(data, z, x, y):
    """Overpass-style node elements for the kept POIs / aerodrome labels / helipads of one decoded tile."""
    out = []
    for layer, feats in mvt.decode(data).items():
        for k, f in enumerate(feats):
            tags = basemap_category_tags(layer, f['tags'])
            if not tags or not f['geom'] or not f['geom'][0] or (f['type'] != 1 and layer != 'aeroway'):
                continue
            if f['type'] == 1:
                px, py = f['geom'][0][0]
            else:   # helipad outline / line: mean of the first part's vertices
                g = f['geom'][0][:-1] if len(f['geom'][0]) > 1 and f['geom'][0][0] == f['geom'][0][-1] else f['geom'][0]
                px, py = sum(q[0] for q in g) / len(g), sum(q[1] for q in g) / len(g)
            if not (0 <= px < f['extent'] and 0 <= py < f['extent']):
                continue   # buffer copy of a neighbour tile's point
            la, lo = mvt.to_lonlat(z, x, y, px, py, f['extent'])
            out.append({'type': 'node', 'id': f'bm{z}/{x}/{y}/{layer}/{k}', 'lat': round(la, 7), 'lon': round(lo, 7), 'tags': tags, '_src': 'basemap'})
    return out


def basemap_tiles_for(pads, margin_m=BASEMAP_MARGIN_M, z=14):
    tiles = set()
    for p in pads:
        dlat = margin_m / 111132; dlon = margin_m / (111320 * math.cos(math.radians(p['lat'])))
        for a in (-dlat, 0, dlat):
            for b in (-dlon, 0, dlon):
                tiles.add((z,) + mvt.tile_of(p['lat'] + a, p['lon'] + b, z))
    return sorted(tiles)


def basemap_elements(pads, cache_dir, template, offline=False, ua='tile-client/1.0'):
    els = []
    os.makedirs(cache_dir, exist_ok=True)
    for z, x, y in basemap_tiles_for(pads):
        cache = os.path.join(cache_dir, f'{z}_{x}_{y}.json')
        if not os.path.exists(cache):
            if offline:
                sys.exit(f'no basemap cache {cache} and --offline')
            url = template.replace('{z}', str(z)).replace('{x}', str(x)).replace('{y}', str(y))
            for attempt in range(4):
                try:
                    resp = urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': ua}), timeout=60)
                    data = resp.read() if resp.status == 200 else b''
                    break
                except Exception as e:
                    print('tile retry', attempt, z, x, y, e, file=sys.stderr); time.sleep(5 * (attempt + 1))
            else:
                raise RuntimeError(f'tile {z}/{x}/{y} failed')
            tmp = cache + '.part'
            json.dump(basemap_tile_elements(data, z, x, y) if data else [], open(tmp, 'w'))
            os.replace(tmp, cache)
            time.sleep(0.05)
        els += json.load(open(cache))
    return els


def fetch(bbox, ua='osm-feature-lookup/1.0', q=None, attempts=5):
    q = q or query(bbox)
    for attempt in range(attempts):
        try:
            req = urllib.request.Request(OVERPASS, data=urllib.parse.urlencode({'data': q}).encode(), headers={'User-Agent': ua})
            return json.load(urllib.request.urlopen(req, timeout=400))['elements']
        except Exception as e:
            print('overpass retry', attempt, e, file=sys.stderr); time.sleep(30 * (attempt + 1))
    raise RuntimeError('Overpass failed')


def near_hospital_points(pads, elements):
    """Sites still to be named that have a basemap hospital within radius + competitor window, that year_finalize
    already flagged kind=hospital, or that have >= BUSY_LOOKUP movements: only these get the targeted Overpass
    lookup (the basemap carries no helipad names, and the busiest unnamed sites are the most visible)."""
    if not pads:
        return []
    proj = Proj(sum(p['lat'] for p in pads) / len(pads))
    hs = [f for f in (Feature(e, proj) for e in elements) if f.cat == 'hospital']
    lim = RADIUS['hospital'] + COMPETE_EXTRA_M
    out = []
    for p in pads:
        if p.get('name') and p['name'] != UNNAMED and not _own(p.get('name_source')):
            continue
        x, y = proj(p['lat'], p['lon'])
        busy = (p.get('arrivals') or 0) + (p.get('departures') or 0) >= BUSY_LOOKUP
        if p.get('kind') == 'hospital' or busy or any(h.dist(x, y) <= lim for h in hs):
            out.append([round(p['lat'], 5), round(p['lon'], 5)])
    return sorted(out)


def near_query(points):
    """Hospital polygons and helipads (with their tags) around a few points: small, unlike a whole-city query."""
    r = RADIUS['hospital'] + COMPETE_EXTRA_M
    parts = ''.join(f'nwr(around:{r},{la},{lo})["amenity"="hospital"];nwr(around:{r},{la},{lo})["healthcare"="hospital"];'
                    f'nwr(around:{SELF_M + COMPETE_EXTRA_M},{la},{lo})["aeroway"~"^(helipad|heliport)$"];' for la, lo in points)
    return f'[out:json][timeout:180];({parts});out tags geom;'


def keep_near(elements, pads):
    """Drop OSM objects farther than KEEP_M from every landing site (cache size)."""
    if not pads:
        return []
    proj = Proj(sum(p['lat'] for p in pads) / len(pads))
    pts = [proj(p['lat'], p['lon']) for p in pads]
    out = []
    for e in elements:
        f = Feature(e, proj)
        if f.cat and any(f.dist(x, y) <= KEEP_M for x, y in pts):
            out.append(e)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--src', required=True); ap.add_argument('--dest', required=True)
    ap.add_argument('--basemap-cache', required=True, help='per-tile cache of kept basemap features (one folder per archive version)')
    ap.add_argument('--tilejson', default=TILEJSON)
    ap.add_argument('--osm-cache', help='optional Overpass tile caches (osm_<city>_<i>_<j>.json) to merge; polygons win')
    ap.add_argument('--overpass', action='store_true', help='fetch missing Overpass tiles (public instance: slow, often 502)')
    ap.add_argument('--overpass-near', action='store_true', help='one small Overpass lookup (hospital polygons, helipad '
                    'tags) around only the sites with a basemap hospital nearby; cached as osm_near_<city>.json')
    ap.add_argument('--city', action='append'); ap.add_argument('--offline', action='store_true')
    a = ap.parse_args()
    if os.path.realpath(a.src) == os.path.realpath(a.dest):
        sys.exit('--dest must differ from --src')
    os.makedirs(a.dest, exist_ok=True)
    if a.offline:
        vers = sorted(d for d in os.listdir(a.basemap_cache) if os.path.isdir(os.path.join(a.basemap_cache, d)) and d.endswith('.' + CACHE_FORMAT))
        if len(vers) != 1:
            sys.exit(f'--offline needs exactly one archive folder in {a.basemap_cache}, found {vers}')
        version, template = vers[0], None
    else:
        tj = json.load(urllib.request.urlopen(urllib.request.Request(a.tilejson, headers={'User-Agent': 'tile-client/1.0'}), timeout=60))
        template = tj['tiles'][0]
        version = re.sub(r'[^A-Za-z0-9_.-]', '_', urllib.parse.parse_qs(urllib.parse.urlparse(template).query).get('v', ['current'])[0]) + '.' + CACHE_FORMAT
    print('basemap archive', version)
    cities = a.city or sorted(f[len('helipads_'):-5] for f in os.listdir(a.src) if f.startswith('helipads_') and f.endswith('.json'))
    fetched = False
    for city in cities:
        pads = json.load(open(os.path.join(a.src, f'helipads_{city}.json')))
        els = {}
        for e in basemap_elements(pads, os.path.join(a.basemap_cache, version), template, offline=a.offline):
            els[str(e['id'])] = e
        n_osm_tiles = 0
        if a.osm_cache:
            tiles = {}
            for p in pads:   # one Overpass query per TILE_DEG tile that holds landing sites (a whole-city query times out)
                tiles.setdefault((math.floor(p['lat'] / TILE_DEG), math.floor(p['lon'] / TILE_DEG)), []).append(p)
            for (ti, tj), tp in sorted(tiles.items()):
                cache = os.path.join(a.osm_cache, f'osm_{city}_{ti}_{tj}.json')
                if not os.path.exists(cache):
                    if not a.overpass or a.offline:
                        continue
                    if fetched:
                        time.sleep(10)
                    la = [p['lat'] for p in tp]; lo = [p['lon'] for p in tp]
                    bbox = (round(min(la) - FETCH_PAD_DEG, 4), round(min(lo) - FETCH_PAD_DEG, 4), round(max(la) + FETCH_PAD_DEG, 4), round(max(lo) + FETCH_PAD_DEG, 4))
                    raw = fetch(bbox); fetched = True
                    kept = keep_near(raw, tp)
                    tmp = cache + '.part'
                    json.dump({'bbox': bbox, 'fetched_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'n_raw': len(raw),
                               'licence': 'ODbL 1.0, (c) OpenStreetMap contributors', 'elements': kept}, open(tmp, 'w'))
                    os.replace(tmp, cache)
                n_osm_tiles += 1
                for e in json.load(open(cache))['elements']:
                    els[f"{e['type']}/{e['id']}"] = e
        n_near = None
        if a.osm_cache:
            pts = near_hospital_points(pads, list(els.values()))
            cache = os.path.join(a.osm_cache, f'osm_near_{city}.json')
            old = json.load(open(cache)) if os.path.exists(cache) else None
            if pts and a.overpass_near and not a.offline and (old is None or old['points'] != pts):
                try:
                    raw = []
                    for k in range(0, len(pts), 20):
                        raw += fetch(None, q=near_query(pts[k:k + 20]), attempts=2); time.sleep(5)
                    tmp = cache + '.part'
                    json.dump({'points': pts, 'fetched_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                               'licence': 'ODbL 1.0, (c) OpenStreetMap contributors', 'elements': raw}, open(tmp, 'w'))
                    os.replace(tmp, cache); old = json.load(open(cache))
                except RuntimeError:
                    print(city, 'near-hospital Overpass lookup failed; basemap only for this city', file=sys.stderr)
            if old is not None and pts:
                n_near = f"{sum(1 for q in pts if q in old['points'])}/{len(pts)}"
                for e in old['elements']:
                    els[f"{e['type']}/{e['id']}"] = e
        named = name_pads(pads, [els[k] for k in sorted(els)])
        with open(os.path.join(a.dest, f'helipads_{city}.json'), 'w') as fh:
            json.dump(named, fh, indent=1)
        n0 = sum(1 for p in pads if p.get('name') and p['name'] != UNNAMED); n1 = sum(1 for p in named if p['name'] != UNNAMED)
        print(city, 'sites', len(pads), 'named before', n0, 'after', n1, 'overpass tiles merged', n_osm_tiles,
              'near-hospital sites with Overpass detail', n_near)


if __name__ == '__main__':
    main()
