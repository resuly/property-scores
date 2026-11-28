"""Aircraft activity store, API routes and the dB estimate.

The fixture is cut from the REAL 12-month grids by
scripts/aircraft_activity_cut_fixture.py (windows of whole cells, lat0/lon0
moved to the window's first cell; every value byte copied). Expected numbers
below were read from the full real grid by plain indexing, independent of the
store: Keilor Park (-37.7184, 144.847) is cell (259, 157) of the 401x422
Melbourne grid, lat0 -38.3, lon0 144.4.
"""
from __future__ import annotations

import math
import os
from pathlib import Path

import numpy as np
import pytest

from property_scores.aircraft_activity import noise as aan
from property_scores.aircraft_activity import store as aas

FIX = Path(__file__).parent / "fixtures" / "aircraft_activity_20261128"
FULL = Path(os.path.expanduser("~/limon-data/aircraft-activity-12m"))
KEILOR = (-37.7184, 144.847)
BRISBANE = (-27.40, 153.08)
HOBART = (-42.88, 147.33)

# Read from grid_melbourne.npz [259, 157] of the full release (2026-11-28).
KEILOR_EXPECTED = {
    "passes_per_day": 165.7910919189453,
    "jet_per_day": 153.10585021972656,
    "light_per_day": 12.442896842956543,
    "heli_per_day": 0.2423398345708847,
    "median_agl_ft": 806.29638671875,
    "night_per_night": 27.961002349853516,
}


@pytest.fixture
def store():
    return aas.Store(FIX)


def test_keilor_park_reads_the_real_cell(store):
    got = store.lookup(*KEILOR)
    assert got["status"] == "ok"
    assert got["city"]["key"] == "melbourne"
    for k, v in KEILOR_EXPECTED.items():
        assert got["metrics"][k] == pytest.approx(v, rel=1e-6), k
    # the cell centre is within half a cell of the query
    assert got["cell"]["offset_m"] < 180
    assert sum(got["hourly"]) == pytest.approx(KEILOR_EXPECTED["passes_per_day"], rel=1e-4)
    assert got["city"]["missing_days_count"] == 6


def test_neighbouring_cells_are_not_interchangeable(store):
    """Guards the i-north / j-east convention: a step of one cell north must
    land on the value one row up in the raw array, not one column across."""
    c = store.cities["melbourne"]
    with np.load(FIX / "grid_melbourne.npz") as z:
        raw = z["passes_per_day"]
    i, j = c.cell_index(*KEILOR)
    north = store.lookup(KEILOR[0] + c.dlat, KEILOR[1])["metrics"]["passes_per_day"]
    east = store.lookup(KEILOR[0], KEILOR[1] + c.dlon)["metrics"]["passes_per_day"]
    assert north == pytest.approx(float(raw[i + 1, j]))
    assert east == pytest.approx(float(raw[i, j + 1]))
    assert raw[i + 1, j] != raw[i, j + 1]


@pytest.mark.skipif(not (FULL / "grid_melbourne.npz").exists(), reason="full release not on this machine")
def test_fixture_matches_full_release_everywhere_in_window(store):
    full = aas.Store(FULL)
    c = store.cities["melbourne"]
    for di in range(c.ny):
        for dj in range(c.nx):
            lat, lng = c.cell_center(di, dj)
            a, b = store.lookup(lat, lng), full.lookup(lat, lng)
            assert a["metrics"] == b["metrics"]
            assert a["quarters"] == b["quarters"]


def test_quarter_range_is_min_max_of_quarter_files(store):
    got = store.lookup(*KEILOR)["quarters"]["passes_per_day"]
    c = store.cities["melbourne"]
    i, j = c.cell_index(*KEILOR)
    vals = []
    for q in ("q1", "q2", "q3", "q4"):
        with np.load(FIX / f"grid_melbourne_{q}.npz") as z:
            vals.append(float(z["passes_per_day"][i, j]))
    assert got["min"] == pytest.approx(min(vals))
    assert got["max"] == pytest.approx(max(vals))
    assert got["n_quarters"] == 4
    assert min(vals) < max(vals)


def test_large_aircraft_only_gives_jets_only(store):
    got = store.lookup(*BRISBANE)
    assert got["status"] == "large_aircraft_only"
    assert set(got["metrics"]) == {"jet_per_day"}
    assert got["hourly"] is None
    assert got["helipads"] == []
    assert got["undercounted"] == ["light", "helicopter"]
    assert set(got["quarters"]) == {"jet_per_day"}
    d = store.cities["brisbane"].describe()
    assert d["metrics"] == ["jet_per_day"] and d["helipads"] == []


def test_insufficient_gives_no_numbers(store):
    got = store.lookup(*HOBART)
    assert got["status"] == "insufficient"
    assert got["metrics"] is None and got["hourly"] is None and got["quarters"] is None
    assert got["helipads"] == []
    d = store.cities["hobart"].describe()
    assert d["metrics"] == [] and d["scales"] == {} and d["helipads"] == []
    assert not store.cities["hobart"].has_metric("passes_per_day")


def test_outside_every_city_is_not_covered(store):
    got = store.lookup(-33.0, 120.0)
    assert got["status"] == "not_covered"


def test_unavailable_when_dir_missing(tmp_path):
    assert not aas.Store(None).available
    assert not aas.Store(tmp_path / "nope").available
    block, est = aan.activity_at(*KEILOR, store=aas.Store(None))
    assert block["status"] == "unavailable" and est is None
    assert block["flights_per_day"] is None


def test_mmap_path_returns_the_same_values(tmp_path):
    import shutil
    import subprocess
    import sys
    d = tmp_path / "data"
    shutil.copytree(FIX, d)
    root = Path(__file__).resolve().parents[1]
    subprocess.run([sys.executable, str(root / "scripts" / "aircraft_activity_unpack.py"),
                    "--dir", str(d)], check=True, capture_output=True)
    mm = aas.Store(d)
    plain = aas.Store(FIX)
    assert mm.cities["melbourne"].arrays().mmapped
    assert not plain.cities["melbourne"].arrays().mmapped
    assert mm.lookup(*KEILOR) == plain.lookup(*KEILOR)
    g1 = mm.cities["melbourne"].grid("passes_per_day", None, 8)
    g2 = plain.cities["melbourne"].grid("passes_per_day", None, 8)
    assert g1 == g2
    assert mm.resident_bytes() == 0 and plain.resident_bytes() > 0


def test_lru_keeps_at_most_max_cities():
    st = aas.Store(FIX, max_cities=1)
    st.lookup(*KEILOR)
    st.lookup(*BRISBANE)
    assert list(st._lru) == ["brisbane"]


def test_grid_block_mean_and_orientation(store):
    c = store.cities["melbourne"]
    with np.load(FIX / "grid_melbourne.npz") as z:
        raw = z["passes_per_day"]
    g = c.grid("passes_per_day", None, 8)   # 13x13 -> stride 2 -> 7x7
    assert (g["rows"], g["cols"], g["stride"]) == (7, 7, 2)
    first = float(np.mean(raw[0:2, 0:2]))
    assert g["values"][0] == pytest.approx(aas.round_sig(first, 3))
    # last row is the northern edge
    last_row_first = float(np.mean(raw[12:14, 0:2]))
    assert g["values"][6 * 7] == pytest.approx(aas.round_sig(last_row_first, 3))


# ---- the dB estimate ---------------------------------------------------------

def _keilor_cell(store):
    c = store.cities["melbourne"]
    return c.cell_values(*c.cell_index(*KEILOR))


def test_estimate_matches_numerical_integration(store):
    """Re-derive the Keilor Park estimate by brute-force numerical integration
    over the sideways offset (not the closed form the module uses)."""
    cell = _keilor_cell(store)
    got = aan.estimate_lden(cell, "ok")
    m = cell["metrics"]
    h = m["median_agl_ft"] * 0.3048
    xs = (np.arange(200000) + 0.5) / 200000 * 1000.0
    factor = float(np.mean(304.8 ** 2 / (h ** 2 + xs ** 2)))
    w = np.array([10.0 if (hr >= 23 or hr < 7) else (10 ** 0.5 if hr >= 19 else 1.0)
                  for hr in range(24)])
    share = float(np.dot(cell["hourly"], w)) / m["passes_per_day"]
    e = sum(m[f"{c}_per_day"] * share * factor * 10 ** (aan.SEL_AT_1000FT_DB[c] / 10)
            for c in ("jet", "light"))
    assert got["lden_db"] == pytest.approx(10 * math.log10(e / 86400), abs=0.05)
    assert 55 < got["lden_db"] < 70  # a busy airport corridor, sanity only
    # period Leqs: energy over 12 / 4 / 8 h, unweighted
    E = [0.0, 0.0, 0.0]
    for hr in range(24):
        per = 2 if (hr >= 23 or hr < 7) else (1 if hr >= 19 else 0)
        for c in ("jet", "light"):
            E[per] += (m[f"{c}_per_day"] * cell["hourly"][hr] / m["passes_per_day"]
                       * factor * 10 ** (aan.SEL_AT_1000FT_DB[c] / 10))
    for key, e_, secs in zip(("leq_day_db", "leq_evening_db", "leq_night_db"), E,
                             (43200, 14400, 28800)):
        assert got[key] == pytest.approx(10 * math.log10(e_ / secs), abs=0.05), key
    assert got["leq_24h_db"] == pytest.approx(10 * math.log10(sum(E) / 86400), abs=0.05)


def test_estimate_responds_to_counts_height_and_class(store):
    cell = _keilor_cell(store)
    base = aan.estimate_lden(cell, "ok")["lden_db"]
    doubled = {**cell, "metrics": {**cell["metrics"],
                                   "jet_per_day": cell["metrics"]["jet_per_day"] * 2,
                                   "light_per_day": cell["metrics"]["light_per_day"] * 2}}
    assert aan.estimate_lden(doubled, "ok")["lden_db"] == pytest.approx(base + 3.0, abs=0.1)
    higher = {**cell, "metrics": {**cell["metrics"], "median_agl_ft": 2500.0}}
    assert aan.estimate_lden(higher, "ok")["lden_db"] < base - 3
    more_heli = {**cell, "metrics": {**cell["metrics"], "heli_per_day": 500.0}}
    assert aan.estimate_lden(more_heli, "ok")["lden_db"] == base  # helicopters never become dB
    jets_only = aan.estimate_lden(cell, "large_aircraft_only")
    full = aan.estimate_lden(cell, "ok")
    assert jets_only["classes"] == ["jet"] and set(jets_only["lden_db_by_class"]) == {"jet"}
    assert jets_only["lden_db"] == full["lden_db_by_class"]["jet"]
    assert "light" in full["lden_db_by_class"]
    assert aan.estimate_lden(cell, "insufficient") is None


def test_activity_block_shapes(store):
    ok, est = aan.activity_at(*KEILOR, store=store)
    assert ok["status"] == "ok" and est is not None
    assert ok["flights_per_day"] == round(KEILOR_EXPECTED["passes_per_day"], 2)
    assert ok["by_type_per_day"]["jet"] == round(KEILOR_EXPECTED["jet_per_day"], 2)
    assert ok["typical_height_ft_agl"] == round(KEILOR_EXPECTED["median_agl_ft"])
    assert ok["quarterly_flights_per_day"]["basis"] == "all"
    assert ok["licence"] == "ODbL-1.0" and "adsb.lol" in ok["attribution"]
    assert ok["affects_score"] is False
    assert ok["period"]["missing_days"] == 6 and ok["period"]["valid_days"] == 359

    bne, est_b = aan.activity_at(*BRISBANE, store=store)
    assert bne["flights_per_day"] is None and bne["flights_per_night"] is None
    assert bne["by_type_per_day"]["light"] is None and bne["by_type_per_day"]["helicopter"] is None
    assert bne["by_type_per_day"]["jet"] is not None
    assert bne["typical_height_ft_agl"] is None
    assert bne["quarterly_flights_per_day"]["basis"] == "jet"
    assert est_b is None or est_b["classes"] == ["jet"]

    hob, est_h = aan.activity_at(*HOBART, store=store)
    assert hob["status"] == "insufficient" and est_h is None
    assert hob["flights_per_day"] is None and hob["by_type_per_day"]["jet"] is None
    # same keys in every state, so an API consumer can rely on the shape
    assert set(ok) == set(bne) == set(hob)


# ---- API routes ----------------------------------------------------------------

@pytest.fixture
def client(monkeypatch):
    from fastapi.testclient import TestClient
    from property_scores.api import main
    monkeypatch.setenv("AIRCRAFT_ACTIVITY_DIR", str(FIX))
    return TestClient(main.app)


def test_routes(client):
    r = client.get("/scores/aircraft-activity/cities")
    assert r.status_code == 200
    keys = {c["key"] for c in r.json()["cities"]}
    assert keys == {"melbourne", "brisbane", "hobart"}
    r = client.get("/scores/aircraft-activity/lookup", params={"lat": KEILOR[0], "lng": KEILOR[1]})
    assert r.status_code == 200 and r.json()["metrics"]["jet_per_day"] == pytest.approx(
        KEILOR_EXPECTED["jet_per_day"], rel=1e-6)
    r = client.get("/scores/aircraft-activity/grid", params={"city": "melbourne", "max": 8})
    assert r.status_code == 200 and r.json()["rows"] == 7
    r = client.get("/scores/aircraft-activity/grid", params={"city": "brisbane", "metric": "passes_per_day"})
    assert r.status_code == 400
    r = client.get("/scores/aircraft-activity/grid", params={"city": "hobart"})
    assert r.status_code == 200 and r.json()["values"] == []
    r = client.get("/scores/aircraft-activity/lookup", params={"lat": "nan", "lng": 1})
    assert r.status_code == 422


def test_routes_unavailable_without_dir(monkeypatch):
    from fastapi.testclient import TestClient
    from property_scores.api import main
    monkeypatch.delenv("AIRCRAFT_ACTIVITY_DIR", raising=False)
    c = TestClient(main.app)
    for path in ("/scores/aircraft-activity/cities",
                 "/scores/aircraft-activity/lookup?lat=-37.7&lng=144.8",
                 "/scores/aircraft-activity/grid?city=melbourne"):
        r = c.get(path)
        assert r.status_code == 503 and r.json()["status"] == "unavailable", path


def test_batch_withholds_counts_unless_export_enabled(monkeypatch):
    from property_scores.api import main
    out = {"noise": {"score": 50, "aircraft_activity": {"status": "ok"},
                     "aircraft_estimate": {"lden_db": 50}}}
    monkeypatch.delenv("AIRCRAFT_ACTIVITY_API_EXPORT", raising=False)
    main._gate_aircraft_activity_export(out)
    assert "aircraft_activity" not in out["noise"]
    assert out["noise"]["score"] == 50 and "aircraft_estimate" in out["noise"]
    out2 = {"noise": {"score": 50, "aircraft_activity": {"status": "ok"}}}
    monkeypatch.setenv("AIRCRAFT_ACTIVITY_API_EXPORT", "1")
    main._gate_aircraft_activity_export(out2)
    assert out2["noise"]["aircraft_activity"] == {"status": "ok"}


def test_helipads_endpoint_filters_and_shapes(client):
    r = client.get("/scores/aircraft-activity/helipads", params={"city": "melbourne"})
    j = r.json()
    assert r.status_code == 200 and j["count"] == j["total_in_city"] > 0
    raw = __import__("json").loads((FIX / "helipads_melbourne.json").read_text())
    # The fixture sites are all unnamed (old file format, no name_* fields):
    # only those with >= 20 movements are public.
    pub = [h for h in raw if h["arrivals"] + h["departures"] >= 20]
    assert 0 < len(pub) < len(raw)
    assert j["count"] == len(pub)
    p = j["helipads"][0]
    assert set(p) >= {"name", "is_hospital", "lat", "lon", "movements", "night_movements"}
    assert p["movements"] == pub[0]["arrivals"] + pub[0]["departures"]
    assert all(x["name"] is None or x["name"] != "Unidentified landing site" for x in j["helipads"])
    top = max(x["movements"] for x in j["helipads"])
    r2 = client.get("/scores/aircraft-activity/helipads", params={"city": "melbourne", "min_movements": top})
    assert all(x["movements"] >= top for x in r2.json()["helipads"]) and r2.json()["count"] >= 1
    w, s, e, n = 144.80, -37.75, 144.90, -37.70
    r3 = client.get("/scores/aircraft-activity/helipads", params={"city": "melbourne", "bbox": f"{w},{s},{e},{n}"})
    assert all(w <= x["lon"] <= e and s <= x["lat"] <= n for x in r3.json()["helipads"])
    assert r3.json()["count"] == sum(1 for h in pub if w <= h["lon"] <= e and s <= h["lat"] <= n)
    r4 = client.get("/scores/aircraft-activity/helipads", params={"city": "brisbane"})
    assert r4.json()["count"] == 0 and "message" in r4.json()
    c = client.get("/scores/aircraft-activity/cities", params={"helipads": "false"}).json()
    mel = [x for x in c["cities"] if x["key"] == "melbourne"][0]
    assert mel["helipads"] is None and mel["helipad_count"] == len(pub)
    hob = [x for x in c["cities"] if x["key"] == "hobart"][0]
    assert hob["airports"] and hob["airports"][0]["observed_movements_12m"] is None


# ---- public helipad threshold and naming ------------------------------------

def _pad(name, kind, arr, dep, dlat, **extra):
    h = {"name": name, "kind": kind, "source": "clustered",
         "lat": KEILOR[0] + dlat, "lon": KEILOR[1], "arrivals": arr, "departures": dep,
         "night_movements": 0, "aircraft_count": 1, "hourly": [0] * 24}
    h.update(extra)
    return h


@pytest.fixture
def named_dir(tmp_path):
    """The fixture with melbourne's helipads replaced by four sites near Keilor
    Park: one in the old format (no name_* fields), three in the named format."""
    import json
    d = tmp_path / "aa"
    d.mkdir()
    for f in FIX.iterdir():
        if f.name != "helipads_melbourne.json":
            (d / f.name).symlink_to(f)
    pads = [
        _pad("Unidentified landing site", "other", 10, 9, 0.001),                  # 19, unnamed, old format
        _pad("Unidentified landing site", "other", 10, 10, 0.002,
             name_source="none", name_kind="unknown", name_dist_m=None),          # 20, unnamed
        _pad("Tiny Pad", "other", 1, 1, 0.003,
             name_source="osm_helipad_tag", name_kind="other", name_dist_m=5),     # 2, named
        _pad("Keilor Hospital Helipad", "other", 20, 20, 0.004,
             name_source="osm_helipad_tag", name_kind="hospital", name_dist_m=0),  # 40, named hospital
    ]
    (d / "helipads_melbourne.json").write_text(json.dumps(pads))
    return d


def test_public_helipads_threshold_and_kind(named_dir):
    c = aas.Store(named_dir).cities["melbourne"]
    assert len(c.helipads) == 4  # the file is kept whole
    got = {p["name"]: p for p in c.public_helipads()}
    assert set(got) == {None, "Tiny Pad", "Keilor Hospital Helipad"}
    assert got[None]["movements"] == 20  # the 19-movement unnamed site is not public
    assert got["Tiny Pad"]["movements"] == 2 and got["Tiny Pad"]["kind"] == "other"
    hosp = got["Keilor Hospital Helipad"]
    assert hosp["kind"] == "hospital" and hosp["is_hospital"] is True
    # min_movements filters further on top of the public rule.
    assert {p["name"] for p in c.public_helipads(min_movements=3)} == {None, "Keilor Hospital Helipad"}
    assert {p["name"] for p in c.public_helipads(min_movements=21)} == {"Keilor Hospital Helipad"}
    d = c.describe()
    assert d["helipad_count"] == 3 and len(d["helipads"]) == 3


def test_lookup_nearby_helipads_use_public_rule(named_dir):
    got = aas.Store(named_dir).lookup(*KEILOR)
    pads = got["helipads"]
    assert sorted(p["movements"] for p in pads) == [2, 20, 40]
    assert [p["kind"] for p in pads if p["name"] == "Keilor Hospital Helipad"] == ["hospital"]


def test_helipads_routes_use_public_rule(named_dir, monkeypatch):
    from fastapi.testclient import TestClient
    from property_scores.api import main
    monkeypatch.setenv("AIRCRAFT_ACTIVITY_DIR", str(named_dir))
    cl = TestClient(main.app)
    j = cl.get("/scores/aircraft-activity/helipads", params={"city": "melbourne"}).json()
    assert j["total_in_city"] == j["count"] == 3
    j = cl.get("/scores/aircraft-activity/helipads",
               params={"city": "melbourne", "min_movements": 20}).json()
    assert j["total_in_city"] == 3 and j["count"] == 2
    mel = [x for x in cl.get("/scores/aircraft-activity/cities").json()["cities"]
           if x["key"] == "melbourne"][0]
    assert mel["helipad_count"] == 3 and len(mel["helipads"]) == 3
    lk = cl.get("/scores/aircraft-activity/lookup", params={"lat": KEILOR[0], "lng": KEILOR[1]}).json()
    assert len(lk["helipads"]) == 3
