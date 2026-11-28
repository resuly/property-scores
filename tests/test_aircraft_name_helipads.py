"""Naming of unnamed helicopter landing sites from OSM (scripts/aircraft_activity/name_helipads.py), on small fixtures."""
from __future__ import annotations

import copy
import importlib.util
import math
import sys
from pathlib import Path

_P = Path(__file__).resolve().parents[1] / "scripts" / "aircraft_activity" / "name_helipads.py"
sys.path.insert(0, str(_P.parent))
import mvt  # noqa: E402

_spec = importlib.util.spec_from_file_location("name_helipads", _P)
nh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(nh)

LAT0, LON0 = -37.80, 145.00
KY = 111132.0
KX = 111320 * math.cos(math.radians(LAT0))


def at(dx_m, dy_m):
    return LAT0 + dy_m / KY, LON0 + dx_m / KX


def pad(name=nh.UNNAMED, **kw):
    p = dict(name=name, kind="other", source="clustered", lat=LAT0, lon=LON0, arrivals=5, departures=4,
             night_movements=7, aircraft_count=3, hourly=[1] * 24)
    p.update(kw)
    return p


_ids = iter(range(1, 10_000))


def node(dx, dy, **tags):
    la, lo = at(dx, dy)
    return {"type": "node", "id": next(_ids), "lat": la, "lon": lo, "tags": tags}


def square(cx, cy, half, **tags):
    pts = [at(cx - half, cy - half), at(cx + half, cy - half), at(cx + half, cy + half), at(cx - half, cy + half)]
    pts.append(pts[0])
    return {"type": "way", "id": next(_ids), "tags": tags, "geometry": [{"lat": a, "lon": b} for a, b in pts]}


def one(elements, p=None, helipad=True):
    """Name one site; by default an unnamed basemap helipad is mapped 10 m away (the evidence rule needs one)."""
    if helipad:
        elements = elements + [{"type": "node", "id": "bm-pad", "lat": at(10, 0)[0], "lon": at(10, 0)[1],
                                "tags": {"aeroway": "helipad"}, "_src": "basemap"}]
    return nh.name_pads([p or pad()], elements)[0]


def test_own_helipad_name_tag_is_used_first():
    r = one([node(20, 0, aeroway="helipad", name="Epworth Helipad"), square(0, 0, 150, amenity="hospital", name="Other Hospital")])
    assert r["name"] == "Epworth Helipad"
    assert r["name_source"] == "osm_helipad_tag"
    assert r["name_dist_m"] == 20


def test_own_helipad_operator_only_gets_helipad_suffix():
    r = one([node(30, 10, aeroway="helipad", operator="Ambulance Victoria")])
    assert r["name"] == "Ambulance Victoria helipad"


def test_unnamed_helipad_inside_hospital_polygon_takes_hospital_name():
    r = one([node(5, 5, aeroway="helipad"), square(40, 0, 100, amenity="hospital", name="Box Hill Hospital")])
    assert r["name"] == "Box Hill Hospital helipad"
    assert r["name_source"] == "osm_hospital_contains"
    assert r["name_kind"] == "hospital"
    assert r["name_dist_m"] == 0


def test_hospital_outline_distance_not_centre():
    # big hospital campus: centre 400 m away but outline 150 m away -> named
    r = one([square(550, 0, 400, healthcare="hospital", name="Big Campus Hospital")])
    assert r["name"] == "Big Campus Hospital helipad"
    assert r["name_dist_m"] == 150


def test_hospital_beyond_radius_is_not_used():
    assert nh.RADIUS["hospital"] == 250
    r = one([node(290, 0, amenity="hospital", name="Far Hospital")])
    assert r["name"] == nh.UNNAMED
    assert r["name_kind"] == "unknown"
    assert r["name_source"] == "none"
    near = one([node(210, 0, amenity="hospital", name="Near Hospital")])
    assert near["name"] == "Near Hospital helipad"


def test_two_hospitals_at_similar_distance_stay_unnamed():
    r = one([node(120, 0, amenity="hospital", name="North Hospital"), node(-160, 0, amenity="hospital", name="South Private Hospital")])
    assert r["name"] == nh.UNNAMED
    assert r["name_source"] == "ambiguous_hospital"
    assert set(r["name_candidates"]) == {"North Hospital", "South Private Hospital"}


def test_containing_hospital_wins_over_a_nearby_second_one():
    r = one([square(0, 0, 60, amenity="hospital", name="Inner Hospital"), node(90, 0, amenity="hospital", name="Neighbour Hospital")])
    assert r["name"] == "Inner Hospital helipad"
    # inside both (e.g. a private hospital mapped inside a public campus): no name
    both = one([square(0, 0, 200, amenity="hospital", name="Campus Hospital"), square(10, 0, 40, amenity="hospital", name="Private Wing Hospital")])
    assert both["name"] == nh.UNNAMED and both["name_source"] == "ambiguous_hospital"


def test_same_hospital_as_node_and_polygon_is_not_ambiguous():
    # outline 100 m away, node 120 m away: two different names here would be ambiguous
    r = one([square(160, 0, 60, amenity="hospital", name="Alfred Hospital"), node(120, 0, amenity="hospital", name="Alfred hospital")])
    assert r["name"] == "Alfred Hospital helipad"


def test_hospital_beats_closer_hotel():
    r = one([node(10, 0, tourism="hotel", name="Crown Towers"), node(200, 0, amenity="hospital", name="City Hospital")])
    assert r["name"] == "City Hospital helipad"


def test_other_category_uses_landing_site_wording():
    r = one([square(0, 0, 50, tourism="resort", name="Sea Resort")])
    assert r["name"] == "Sea Resort landing site"
    assert r["name_kind"] == "other"
    e = one([node(60, 0, amenity="police", name="Police Air Wing")])
    assert e["name_kind"] == "emergency"


def test_multipolygon_relation_containment():
    w = square(0, 0, 80)
    rel = {"type": "relation", "id": 9, "tags": {"type": "multipolygon", "amenity": "hospital", "name": "Rel Hospital"},
           "members": [{"type": "way", "ref": 1, "role": "outer", "geometry": w["geometry"]}]}
    r = one([rel])
    assert r["name"] == "Rel Hospital helipad"
    assert r["name_dist_m"] == 0


def test_existing_name_is_never_changed():
    p = pad(name="Royal Melbourne Hospital Helipad", source="ourairports", kind="hospital")
    r = one([square(0, 0, 100, amenity="hospital", name="Some Other Hospital")], p)
    assert r["name"] == "Royal Melbourne Hospital Helipad"
    assert r["name_source"] == "ourairports"


def test_other_fields_untouched_and_rerun_is_stable():
    els = [square(0, 0, 100, amenity="hospital", name="Box Hill Hospital")]
    src = [pad(), pad(lat=LAT0 + 0.1)]
    before = copy.deepcopy(src)
    out = nh.name_pads(src, els)
    assert src == before
    for a, b in zip(src, out):
        assert {k: v for k, v in b.items() if k != "name" and not k.startswith("name_")} == {k: v for k, v in a.items() if k != "name"}
    assert nh.name_pads(out, els) == out
    # a site named by this step is re-derived when OSM changes, not frozen
    assert nh.name_pads(out, [])[0]["name"] == nh.UNNAMED


def bm(dx, dy, **tags):
    e = node(dx, dy, **tags)
    e["_src"] = "basemap"
    return e


def test_basemap_point_hospital_named_with_distance_in_source():
    r = one([bm(100, 0, amenity="hospital", name="Northern Hospital")])
    assert r["name"] == "Northern Hospital helipad"
    assert r["name_source"] == "basemap_poi_hospital_within_100m"


def test_osm_polygon_beats_basemap_point_and_same_name_merges():
    r = one([square(0, 0, 80, amenity="hospital", name="Austin Hospital"), bm(150, 0, amenity="hospital", name="Austin Hospital")])
    assert r["name_source"] == "osm_hospital_contains"
    r2 = one([square(0, 0, 80, amenity="hospital", name="Austin Hospital"), bm(70, 0, amenity="hospital", name="Mercy Hospital")])
    assert r2["name"] == "Austin Hospital helipad"


def test_basemap_clinic_is_not_a_hospital():
    assert nh.basemap_category_tags("poi", {"class": "hospital", "subclass": "clinic", "name": "Pathology"}) is None
    assert nh.basemap_category_tags("poi", {"class": "hospital", "subclass": "hospital", "name": "X Hospital"}) == {"amenity": "hospital", "name": "X Hospital"}
    assert nh.basemap_category_tags("poi", {"class": "hospital", "subclass": "hospital"}) is None


# ---- a hand-encoded vector tile, so the decoder is checked against bytes it did not produce ----
def _vi(n):
    out = bytearray()
    while True:
        b = n & 0x7F; n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _f(num, wire, payload):
    key = _vi((num << 3) | wire)
    return key + (_vi(payload) if wire == 0 else _vi(len(payload)) + payload)


def _zz(n):
    return (n << 1) ^ (n >> 31)


def _tile(points, layer="poi", extent=4096):
    keys, vals, feats = [], [], b""
    for (px, py), tags in points:
        t = []
        for k, v in tags.items():
            if k not in keys: keys.append(k)
            if v not in vals: vals.append(v)
            t += [keys.index(k), vals.index(v)]
        geom = [(1 << 3) | 1, _zz(px), _zz(py)]
        feats += _f(2, 2, _f(2, 2, b"".join(_vi(x) for x in t)) + _f(3, 0, 1) + _f(4, 2, b"".join(_vi(x) for x in geom)))
    body = _f(15, 0, 2) + _f(1, 2, layer.encode()) + feats
    body += b"".join(_f(3, 2, k.encode()) for k in keys) + b"".join(_f(4, 2, _f(1, 2, v.encode())) for v in vals) + _f(5, 0, extent)
    return _f(3, 2, body)


def test_tile_decode_positions_and_buffer_points():
    z = 14
    x, y = mvt.tile_of(LAT0, LON0, z)
    data = _tile([((2048, 1024), {"class": "hospital", "subclass": "hospital", "name": "Mid Hospital"}),
                  ((2048, 1024), {"class": "hospital", "subclass": "clinic", "name": "A Clinic"}),
                  ((-40, 100), {"class": "hospital", "subclass": "hospital", "name": "Buffer Copy Hospital"})])
    els = nh.basemap_tile_elements(data, z, x, y)
    assert [e["tags"]["name"] for e in els] == ["Mid Hospital"]
    n = 2 ** z
    lon = (x + 0.5) / n * 360 - 180
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 0.25) / n))))
    assert abs(els[0]["lon"] - lon) < 1e-6 and abs(els[0]["lat"] - lat) < 1e-6
    assert els[0]["_src"] == "basemap"


def test_competitor_just_outside_radius_still_blocks():
    # A at 200 m is within 250 m; B at 280 m is outside the radius but close enough to make A unsafe
    r = one([bm(200, 0, amenity="hospital", name="A Hospital"), bm(-280, 0, amenity="hospital", name="B Hospital")])
    assert r["name_source"] == "ambiguous_hospital"
    far = one([bm(200, 0, amenity="hospital", name="A Hospital"), bm(-420, 0, amenity="hospital", name="B Hospital")])
    assert far["name"] == "A Hospital helipad"


def test_next_hospital_150m_farther_is_clear():
    r = one([bm(130, 0, amenity="hospital", name="St George Hospital"), bm(-290, 0, amenity="hospital", name="St George Private Hospital")])
    assert r["name"] == "St George Hospital helipad"
    close = one([bm(130, 0, amenity="hospital", name="St George Hospital"), bm(-270, 0, amenity="hospital", name="St George Private Hospital")])
    assert close["name_source"] == "ambiguous_hospital"


def test_sub_units_fold_into_their_hospital_and_docks_are_ignored():
    r = one([bm(45, 0, amenity="hospital", name="Fiona Stanley Hospital (Emergency Department)"),
             bm(-146, 0, amenity="hospital", name="Fiona Stanley Hospital"),
             bm(0, 80, amenity="hospital", name="Fiona Stanley Hospital Loading Dock"),
             bm(0, -50, amenity="hospital", name="Emergency")])
    assert r["name"] == "Fiona Stanley Hospital helipad"
    assert r["name_dist_m"] == 45


def test_campus_ambiguity_resolved_by_named_osm_helipad():
    els = [bm(51, 0, amenity="hospital", name="Royal Hobart Hospital"), bm(-122, 0, amenity="hospital", name="Hobart Private Hospital")]
    assert one(els)["name_source"] == "ambiguous_hospital"
    r = one(els + [node(2, 0, aeroway="helipad", name="Royal Hobart Hospital", ref="YXHH")])
    assert r["name"] == "Royal Hobart Hospital helipad"
    assert r["name_source"] == "osm_helipad_tag"


def test_only_unnamed_sites_near_a_hospital_get_the_overpass_lookup():
    pads = [pad(), pad(lat=LAT0 + 0.05), pad(name="Known Helipad", source="ourairports", lat=LAT0 + 0.0001), pad(lat=LAT0 + 0.1, kind="hospital")]
    pads.append(pad(lat=LAT0 + 0.2, arrivals=60, departures=40))
    pads.append(pad(lat=LAT0 + 0.3, arrivals=60, departures=39))
    pts = nh.near_hospital_points(pads, [bm(300, 0, amenity="hospital", name="H"), bm(0, 300, amenity="police", name="P")])
    assert pts == sorted([[round(LAT0 + d, 5), round(LON0, 5)] for d in (0, 0.1, 0.2)])


def test_no_mapped_helipad_means_no_facility_name():
    els = [bm(40, 0, amenity="hospital", name="Royal Dental Hospital")]
    r = one(els, helipad=False)
    assert r["name"] == nh.UNNAMED
    assert r["name_source"] == "no_mapped_helipad_hospital"
    assert r["name_candidates"] == ["Royal Dental Hospital"]
    far_pad = els + [bm(nh.HELIPAD_EVIDENCE_M + 30, 0, aeroway="helipad")]
    assert one(far_pad, helipad=False)["name_source"] == "no_mapped_helipad_hospital"
    near_pad = els + [bm(nh.HELIPAD_EVIDENCE_M - 30, 0, aeroway="helipad")]
    assert one(near_pad, helipad=False)["name"] == "Royal Dental Hospital helipad"
    assert nh.HELIPAD_EVIDENCE_M == 150
    # a busy site at a hospital needs no mapped helipad (cluster centre can sit 300 m from the mapped pad); a hotel does
    assert nh.ACTIVE_MOVEMENTS == 20
    assert one(els, p=pad(arrivals=10, departures=10), helipad=False)["name"] == "Royal Dental Hospital helipad"
    assert one(els, p=pad(arrivals=10, departures=9), helipad=False)["name"] == nh.UNNAMED
    assert one([bm(40, 0, tourism="hotel", name="Big Hotel")], p=pad(arrivals=200), helipad=False)["name"] == nh.UNNAMED
    # an airfield needs a mapped helipad or a busy site, like a hospital
    apt = [bm(100, 0, aeroway="aerodrome", name="Rothwell Airfield")]
    assert one(apt, helipad=False)["name"] == nh.UNNAMED
    assert one(apt, p=pad(arrivals=10, departures=10), helipad=False)["name"] == "Rothwell Airfield landing site"


def test_basemap_helipad_outline_becomes_evidence_point():
    z = 14
    x, y = mvt.tile_of(LAT0, LON0, z)
    ring = [(1000, 1000), (1100, 1000), (1100, 1100), (1000, 1100)]
    data = _poly_tile(ring, {"class": "helipad"})
    els = nh.basemap_tile_elements(data, z, x, y)
    assert len(els) == 1 and els[0]["tags"] == {"aeroway": "helipad"}
    n = 2 ** z
    lon = (x + 1050 / 4096) / n * 360 - 180
    assert abs(els[0]["lon"] - lon) < 1e-6


def _poly_tile(ring, tags, layer="aeroway", extent=4096):
    keys = list(tags); vals = list(tags.values())
    t = []
    for k, v in tags.items():
        t += [keys.index(k), vals.index(v)]
    geom = [(1 << 3) | 1, _zz(ring[0][0]), _zz(ring[0][1]), ((len(ring) - 1) << 3) | 2]
    for (ax, ay), (bx, by) in zip(ring, ring[1:]):
        geom += [_zz(bx - ax), _zz(by - ay)]
    geom.append((1 << 3) | 7)
    feat = _f(2, 2, b"".join(_vi(v) for v in t)) + _f(3, 0, 3) + _f(4, 2, b"".join(_vi(v) for v in geom))
    body = _f(15, 0, 2) + _f(1, 2, layer.encode()) + _f(2, 2, feat)
    body += b"".join(_f(3, 2, k.encode()) for k in keys) + b"".join(_f(4, 2, _f(1, 2, v.encode())) for v in vals) + _f(5, 0, extent)
    return _f(3, 2, body)
