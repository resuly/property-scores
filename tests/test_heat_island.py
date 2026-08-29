"""Heat island caching: the result belongs to the address, not the kilometre.

Regression cover for the 2026-07-25 defect where `_cache_key` rounded to 2 dp
(~1.1 km) and cached the WHOLE result, so the first address computed in a cell
handed its `building_density` and `greenspace_factor` to every other address in
that cell for an hour. Production showed Royal Park parkland values being
served to terrace housing 450 m away.

These tests stub every input, so they need neither the network nor the MODIS /
WorldCover rasters.
"""
import pytest

from property_scores.heat_island import score as hs


def test_score_uses_the_same_two_decimal_local_factors_it_publishes():
    # Kew's disclosed components imply 55. Hidden green=0.326 used to score 54
    # while the payload printed 0.33, so a customer could not reproduce it.
    assert hs._score_from_components(61.0, 0.0, 1.0, 0.326) == 55


@pytest.fixture(autouse=True)
def _clear_caches():
    hs._cache.clear()
    yield
    hs._cache.clear()


# Two coordinates inside ONE round(2) cell: both round to (-37.78, 144.95).
PARK = (-37.7845, 144.9530)
HOUSES = (-37.7801, 144.9548)


def _stub_inputs(monkeypatch, calls=None):
    """Local factors vary by coordinate; MODIS is constant (in coverage)."""
    def density(lat, lng):
        if calls is not None:
            calls.setdefault("density", []).append((lat, lng))
        return 0.47 if (lat, lng) == PARK else 1.0

    def green(lat, lng):
        if calls is not None:
            calls.setdefault("green", []).append((lat, lng))
        return 0.78 if (lat, lng) == PARK else 0.65

    monkeypatch.setattr(hs, "_building_density_proxy", density)
    monkeypatch.setattr(hs, "_greenspace_proxy", green)
    monkeypatch.setattr(hs, "_modis_lst", lambda lat, lng: {
        "point_lst_c": 33.4, "area_lst_c": 34.1,
        "uhi_delta_c": -0.7, "night_lst_c": 15.0, "samples": 1})


def test_neighbour_in_same_grid_cell_gets_its_own_local_factors(monkeypatch):
    """The defect: HOUSES was served PARK's row because they share a cell."""
    _stub_inputs(monkeypatch)
    park = hs.heat_island_score(*PARK)
    houses = hs.heat_island_score(*HOUSES)

    assert (round(PARK[0], 2), round(PARK[1], 2)) == (round(HOUSES[0], 2),
                                                      round(HOUSES[1], 2)), \
        "test points must share a round(2) cell or this proves nothing"

    assert park["building_density"] == 0.47
    assert houses["building_density"] == 1.0
    assert park["greenspace_factor"] == 0.78
    assert houses["greenspace_factor"] == 0.65
    assert not houses.get("cached")
    assert houses["score"] != park["score"]


def test_same_address_repeat_is_served_from_cache(monkeypatch):
    """Removing the smearing must not cost the exact-repeat fast path."""
    calls: dict = {}
    _stub_inputs(monkeypatch, calls)
    first = hs.heat_island_score(*PARK)
    second = hs.heat_island_score(*PARK)

    assert second.get("cached") is True
    assert second["score"] == first["score"]
    assert len(calls["density"]) == 1, "local factors recomputed on an exact repeat"


def test_no_modis_coverage_returns_data_unavailable(monkeypatch):
    """2026-08-02: the Open-Meteo ERA5 fallback was removed (DA Leads is a
    paid commercial product; that endpoint's free tier is non-commercial-use
    only). A MODIS miss must now degrade honestly to "Data unavailable"
    instead of silently estimating from a service we are not licensed to
    call for a paying customer."""
    monkeypatch.setattr(hs, "_building_density_proxy", lambda lat, lng: 1.0)
    monkeypatch.setattr(hs, "_greenspace_proxy", lambda lat, lng: 0.5)
    monkeypatch.setattr(hs, "_modis_lst", lambda lat, lng: None)

    result = hs.heat_island_score(*PARK)

    assert result["score"] is None
    assert result["label"] == "Data unavailable"


# ---------------------------------------------------------------------------
# Waterfront (water-masked MODIS pixel) fallback, 2026-08-05.
#
# Production defect: 1/1 Cavill Avenue, Surfers Paradise QLD 4217 returned
# score:None "Data unavailable" on every call. MODIS LST water-masks whole 1 km
# pixels, so the beachfront pixel is NODATA while two pixels in the 927 m ring
# carry data (raw 31.80999755859375 north, 32.290008544921875 west, mean
# reported as 32.1C). The whole heat score was lost to a mask, not to missing
# data.
# ---------------------------------------------------------------------------

SURFERS = (-28.0027, 153.4296)


class _FakeSampler:
    """Stands in for the raster sampler, addressed by MODIS pixel OFFSET.

    Keys are (dx, dy) on the MODIS sinusoidal grid relative to the subject.
    The lat/lng the code passes in is converted back to an offset here, so the
    sinusoidal round trip in `_nearest_land_pixel` is exercised for real rather
    than stubbed over.
    """

    def __init__(self, origin, day, night=None, window=None, lc=None):
        self.ox, self.oy = hs._wgs84_to_sinusoidal(*origin)
        self.day = day
        self.night = night or {}
        self.window = window
        # Land cover is keyed by offset like everything else. A constant here
        # would make `_point_is_water(lat + 0.5, lng + 0.5)` pass, which is a
        # real bug the tests must be able to see.
        self.lc = lc if isinstance(lc, dict) else ({} if lc is None else {(0, 0): lc})
        self.sampled = []
        self.window_calls = []

    def _offset(self, lat, lng):
        x, y = hs._wgs84_to_sinusoidal(lat, lng)
        return (round((x - self.ox) / hs._MODIS_PIXEL_M),
                round((y - self.oy) / hs._MODIS_PIXEL_M))

    def sample(self, path, lat, lng, default=float("nan")):
        off = self._offset(lat, lng)
        self.sampled.append((path, off))
        if path == hs._DAY_VRT:
            return self.day.get(off, float("nan"))
        if path == hs._NIGHT_VRT:
            return self.night.get(off, float("nan"))
        return self.lc.get(off, float("nan"))

    def window_stats(self, path, lat, lng, radius_m, **kw):
        # Records where and how wide it was asked, so a test can pin the 2 km
        # radius the whole fallback cap is justified against. The real sampler
        # also returns 'max'; score.py does not read it, but the shape should
        # not diverge silently.
        self.window_calls.append((path, self._offset(lat, lng), radius_m))
        if not self.window:
            return {}
        st = dict(self.window)
        st.setdefault("max", st.get("mean"))
        return st


@pytest.fixture
def modis_stub(monkeypatch, tmp_path):
    """Point the module at existing (empty) VRT paths and a fake sampler."""
    day_vrt = tmp_path / "modis_lst_day.vrt"
    night_vrt = tmp_path / "modis_lst_night.vrt"
    day_vrt.write_text("")
    night_vrt.write_text("")
    monkeypatch.setattr(hs, "_DAY_VRT", str(day_vrt))
    monkeypatch.setattr(hs, "_NIGHT_VRT", str(night_vrt))

    from property_scores.common import landcover as lc

    def _install(day, night=None, window=None, lc_value=None, lc_available=True):
        fake = _FakeSampler(SURFERS, day, night, window, lc_value)
        monkeypatch.setattr(lc, "sampler", lambda: fake)
        monkeypatch.setattr(lc, "available", lambda: lc_available)
        monkeypatch.setattr(lc, "LC_VRT", "LC")
        return fake

    return _install


def test_neighbour_rings_are_ordered_and_capped():
    rings = hs._neighbour_rings(hs._MODIS_NEIGHBOUR_MAX_M, hs._MODIS_PIXEL_M)
    dists = [d for d, _ in rings]
    assert dists == sorted(dists), "nearest ring must be tried first"
    assert all(d <= hs._MODIS_NEIGHBOUR_MAX_M for d in dists)
    assert (0, 0) not in [o for _, offs in rings for o in offs]

    first_dist, first_offs = rings[0]
    assert round(first_dist) == 927
    assert set(first_offs) == {(-1, 0), (1, 0), (0, -1), (0, 1)}

    all_offs = {o for _, offs in rings for o in offs}
    assert (2, 0) in all_offs, "1853 m is inside the 2 km cap"
    assert (2, 1) not in all_offs, "2072 m is outside the 2 km cap"
    assert (3, 0) not in all_offs


def test_waterfront_pixel_reads_the_nearest_land_pixels(modis_stub):
    """The production case, sampled off the live mosaic 2026-08-05: the centre
    pixel is NODATA and TWO pixels in the 927 m ring carry data, so the address
    reads their mean.

    The day/night numbers below are the raw sampler values from the server, not
    rounded ones. It matters: the mean of the raw pair is 32.050003, which
    rounds to 32.1, while the mean of their rounded display values (31.8, 32.3)
    is 32.05 and rounds to 32.0.
    """
    fake = modis_stub(day={(0, 1): 31.80999755859375, (-1, 0): 32.290008544921875},
                      night={(0, 1): 22.829986572265625, (-1, 0): 23.080001831054688},
                      window={"mean": 33.347, "count": 10},
                      lc_value={(0, 0): 50})  # 50 = built-up

    out = hs._modis_lst(*SURFERS)

    assert out is not None, "a masked centre pixel must no longer kill the score"
    assert out["point_lst_c"] == 32.1
    assert out["lst_source"] == "nearest_land_pixel"
    assert out["lst_offset_m"] == 927
    assert out["lst_pixels_averaged"] == 2
    assert out["uhi_delta_c"] is None, "borrowed pixels vs their own neighbourhood"
    assert out["area_lst_c"] is None, "no like-for-like area to report either"
    # Reading night at the address (NODATA) or at only one of the two borrowed
    # pixels would give something other than 23.0.
    assert out["night_lst_c"] == 23.0

    # The 2 km cap is justified as "inside the window the area is averaged
    # over", so pin that the window really is asked for 2 km at the address.
    assert fake.window_calls == [(hs._DAY_VRT, (0, 0), 2000)]


def test_equidistant_land_pixels_are_averaged(modis_stub):
    """No compass tie-break: same-distance pixels with data are averaged."""
    modis_stub(day={(-1, 0): 30.0, (0, 1): 34.0, (-2, 0): 20.0},
               window={"mean": 32.0, "count": 8}, lc_value={(0, 0): 50})

    out = hs._modis_lst(*SURFERS)

    assert out["point_lst_c"] == 32.0  # (30+34)/2, the 927 m ring only
    assert out["lst_offset_m"] == 927
    assert out["lst_pixels_averaged"] == 2
    assert out["samples"] == 1, "'samples' counts composites, not pixels"


def test_second_ring_is_used_when_the_first_is_empty(modis_stub):
    """A wider inlet: the 927 m ring is all water, the 1310 m diagonal is not."""
    modis_stub(day={(-1, 1): 30.0}, window={"mean": 30.0, "count": 4},
               lc_value={(0, 0): 50})

    out = hs._modis_lst(*SURFERS)

    assert out["point_lst_c"] == 30.0
    assert out["lst_offset_m"] == 1310


def test_water_cover_is_checked_at_the_address_itself(modis_stub):
    """Guard against reading land cover at the wrong coordinate: only the
    address's own cell is water here, the neighbours are built-up."""
    modis_stub(day={(-1, 0): 32.3}, window={"mean": 33.3, "count": 10},
               lc_value={(0, 0): 80, (-1, 0): 50, (0, 1): 50, (1, 0): 50,
                         (0, -1): 50})

    assert hs._modis_lst(*SURFERS) is None


def test_fallback_does_not_reach_beyond_the_cap(modis_stub):
    """Data only 3 pixels (2.8 km) away is too far to speak for this address."""
    modis_stub(day={(3, 0): 31.0}, window={"mean": 31.0, "count": 1}, lc_value=50)

    assert hs._modis_lst(*SURFERS) is None


def test_no_data_anywhere_in_the_window_skips_the_ring_search(modis_stub):
    """Outside tile coverage (any non-AU coordinate) the 2 km window is empty,
    so the 12 neighbour samples must not be attempted at all."""
    fake = modis_stub(day={}, window=None, lc_value={(0, 0): 50})

    assert hs._modis_lst(*SURFERS) is None
    day_samples = [o for p, o in fake.sampled if p == hs._DAY_VRT]
    assert day_samples == [(0, 0)], f"only the centre should be sampled: {day_samples}"


def test_point_on_water_is_not_given_a_land_temperature(modis_stub):
    """A geocode that landed in the sea must stay 'Data unavailable'."""
    fake = modis_stub(day={(-1, 0): 32.3}, window={"mean": 33.3, "count": 10},
                      lc_value=80)  # 80 = WorldCover water

    assert hs._modis_lst(*SURFERS) is None
    assert any(p == "LC" for p, _ in fake.sampled), "land cover must be consulted"


def test_inland_pixel_path_is_unchanged(modis_stub):
    """The normal path keeps its centre-backed-out UHI arithmetic."""
    modis_stub(day={(0, 0): 33.0}, night={(0, 0): 19.0},
               window={"mean": 33.5, "count": 25}, lc_value=50)

    out = hs._modis_lst(*SURFERS)

    assert out["lst_source"] == "pixel"
    assert "lst_offset_m" not in out
    assert out["point_lst_c"] == 33.0
    # (33.5*25 - 33.0)/24 = 33.52; uhi = 33.0 - 33.52
    assert out["area_lst_c"] == 33.5
    assert out["uhi_delta_c"] == -0.5
    assert out["night_lst_c"] == 19.0


def test_score_on_a_borrowed_pixel_drops_the_uhi_penalty(monkeypatch):
    """Caller side: no UHI penalty and no '+X.XC vs surrounding area' field,
    even if a borrowed reading arrives carrying a delta."""
    monkeypatch.setattr(hs, "_building_density_proxy", lambda lat, lng: 0.5)
    monkeypatch.setattr(hs, "_greenspace_proxy", lambda lat, lng: 0.35)
    # area_lst_c/uhi_delta_c carry values here on purpose: the caller must
    # refuse to publish a comparison even if a reading arrives with one.
    monkeypatch.setattr(hs, "_modis_lst", lambda lat, lng: {
        "point_lst_c": 32.3, "area_lst_c": 33.3, "uhi_delta_c": 5.0,
        "night_lst_c": 21.0, "samples": 1, "lst_pixels_averaged": 2,
        "lst_source": "nearest_land_pixel", "lst_offset_m": 927})
    # Force the water/tree context check to say "ordinary land", so the only
    # thing that can suppress the penalty is the borrowed-pixel rule.
    from property_scores.common import landcover as lc
    monkeypatch.setattr(lc, "fractions", lambda lat, lng, radius_m=500: {80: 0.0, 10: 0.0})

    out = hs.heat_island_score(*SURFERS)

    assert out["score"] is not None
    assert "uhi_delta_c" not in out
    # DA Leads' map computes its own delta from (modis_lst_c - modis_area_c),
    # so withholding uhi_delta_c alone would not stop it rendering one.
    assert "modis_area_c" not in out
    assert out["lst_source"] == "nearest_land_pixel"
    assert out["lst_offset_m"] == 927
    assert out["lst_pixels_averaged"] == 2
    assert "927 m away" in out["disclaimer"]
    # temp_score 63.5 - night penalty 4.5 - density 3.0 + green 0.0 = 56.
    # Applying the +5.0 delta would give 41, so this number is what proves the
    # penalty was dropped rather than merely rounded away.
    assert out["score"] == 56


def test_normal_score_keeps_its_uhi_delta_and_disclaimer(monkeypatch):
    """Guard the other side: the ordinary path must not pick up the waterfront
    wording or lose its UHI field."""
    monkeypatch.setattr(hs, "_building_density_proxy", lambda lat, lng: 0.5)
    monkeypatch.setattr(hs, "_greenspace_proxy", lambda lat, lng: 0.35)
    monkeypatch.setattr(hs, "_modis_lst", lambda lat, lng: {
        "point_lst_c": 33.0, "area_lst_c": 32.0, "uhi_delta_c": 1.0,
        "night_lst_c": 17.0, "samples": 1, "lst_source": "pixel"})
    from property_scores.common import landcover as lc
    monkeypatch.setattr(lc, "fractions", lambda lat, lng, radius_m=500: {80: 0.0, 10: 0.0})

    out = hs.heat_island_score(*SURFERS)

    assert out["uhi_delta_c"] == 1.0
    assert out["lst_source"] == "pixel"
    assert "lst_offset_m" not in out
    assert "water-masked" not in out["disclaimer"]
    # temp_score 60.0 - uhi 3.0 - density 3.0 = 54
    assert out["score"] == 54


def test_neighbourhood_contract_separates_temperature_and_land_cover(monkeypatch,
                                                                    tmp_path):
    monkeypatch.setattr(hs, "_building_density_proxy", lambda lat, lng: 0.5)
    monkeypatch.setattr(hs, "_greenspace_proxy", lambda lat, lng: 0.35)
    monkeypatch.setattr(hs, "_modis_lst", lambda lat, lng: {
        "point_lst_c": 33.0, "area_lst_c": 32.0, "uhi_delta_c": 1.0,
        "night_lst_c": 20.0, "samples": 1, "lst_source": "pixel"})
    from property_scores.common import landcover as lc
    monkeypatch.setattr(lc, "fractions",
                        lambda lat, lng, radius_m=500: {80: 0.0, 10: 0.0})
    monkeypatch.setattr(hs, "_MOSAIC_METADATA",
                        str(tmp_path / "missing-metadata.json"))
    monkeypatch.setattr(hs, "_mosaic_metadata_cache", None)

    out = hs.heat_island_score(*SURFERS)

    assert out["product"] == "neighbourhood_heat"
    assert out["assessment_level"] == "neighbourhood_context"
    assert out["temperature_resolution_m"] == 1000
    assert out["temperature_native_grid_step_m"] == hs._MODIS_PIXEL_M
    assert out["land_cover_resolution_m"] == 10
    assert out["day_night_cooling_c"] == 13.0
    assert out["temperature_vintage"]["status"] == "unverified"
    assert {source["source"] for source in out["sources"]} == {
        "NASA MOD11A2 Version 6.1", "ESA WorldCover",
        "Overture Maps buildings",
    }


def test_mosaic_manifest_is_the_only_vintage_truth(monkeypatch, tmp_path):
    releases = tmp_path / "releases"
    release = releases / "summer-2023-2024-2025-median-test"
    release.mkdir(parents=True)
    active = tmp_path / "current"
    active.symlink_to(release)
    manifest = release / "modis_lst_metadata.json"
    manifest.write_text(
        '{"collection":"modis-11A2-061","seasons":[2023,2024,2025],'
        '"stat":"median","generated_at":"2026-08-30T00:00:00+00:00",'
        '"release_id":"summer-2023-2024-2025-median-test",'
        '"tile_count":12}',
        encoding="utf-8",
    )
    monkeypatch.setattr(hs, "_ACTIVE_MOSAIC_DIR", str(active))
    monkeypatch.setattr(hs, "_MOSAIC_RELEASES_DIR", str(releases))
    monkeypatch.setattr(hs, "_mosaic_metadata_cache", None)

    out = hs._mosaic_vintage()

    assert out["status"] == "verified"
    assert out["seasons"] == [2023, 2024, 2025]
    assert out["stat"] == "median"


def test_legacy_top_level_manifest_cannot_claim_verified_vintage(monkeypatch,
                                                                 tmp_path):
    manifest = tmp_path / "modis_lst_metadata.json"
    manifest.write_text(
        '{"collection":"modis-11A2-061","seasons":[2023,2024,2025],'
        '"stat":"median","generated_at":"2026-08-30T00:00:00+00:00"}',
        encoding="utf-8",
    )
    monkeypatch.setattr(hs, "_ACTIVE_MOSAIC_DIR", str(tmp_path / "missing"))
    monkeypatch.setattr(hs, "_MOSAIC_METADATA", str(manifest))
    monkeypatch.setattr(hs, "_mosaic_metadata_cache", None)

    out = hs._mosaic_vintage()

    assert out["status"] == "unverified"
    assert "not an atomically published generation" in out["note"]


def test_cli_prints_a_borrowed_pixel_result_without_crashing(modis_stub, capsys):
    """`python -m property_scores.heat_island.score` indexed result['modis_area_c']
    directly, which is a KeyError on exactly the addresses this path exists for
    (caught in review; the suite was green because nothing exercised __main__).
    """
    modis_stub(day={(0, 1): 31.80999755859375, (-1, 0): 32.290008544921875},
               night={(0, 1): 22.829986572265625, (-1, 0): 23.080001831054688},
               window={"mean": 33.347, "count": 10}, lc_value={(0, 0): 50})
    hs._cache.clear()

    hs._print_result(hs.heat_island_score(*SURFERS))

    out = capsys.readouterr().out
    assert "Heat Island Score:" in out
    assert "area avg: n/a" in out
    assert "UHI: n/a" in out
    assert "927 m away" in out
    assert "undefined" not in out and "None°C" not in out


def test_disclaimer_says_how_many_pixels_were_averaged(monkeypatch):
    """One pixel in five reads a single pixel, so the sentence must not hard-code
    either number (measured: 31 of 149 recovered addresses)."""
    monkeypatch.setattr(hs, "_building_density_proxy", lambda lat, lng: 0.5)
    monkeypatch.setattr(hs, "_greenspace_proxy", lambda lat, lng: 0.35)
    from property_scores.common import landcover as lc
    monkeypatch.setattr(lc, "fractions", lambda lat, lng, radius_m=500: {80: 0.0, 10: 0.0})

    base = {"point_lst_c": 32.1, "area_lst_c": None, "uhi_delta_c": None,
            "night_lst_c": 21.0, "samples": 1,
            "lst_source": "nearest_land_pixel", "lst_offset_m": 927}

    monkeypatch.setattr(hs, "_modis_lst",
                        lambda lat, lng: {**base, "lst_pixels_averaged": 1})
    hs._cache.clear()
    one = hs.heat_island_score(*SURFERS)["disclaimer"]

    monkeypatch.setattr(hs, "_modis_lst",
                        lambda lat, lng: {**base, "lst_pixels_averaged": 3})
    hs._cache.clear()
    three = hs.heat_island_score(*SURFERS)["disclaimer"]

    assert "the nearest pixel that does" in one
    assert "pixels" not in one.split("read from")[1].split(",")[0]
    assert "the nearest 3 pixels that do" in three


def test_module_has_no_open_meteo_dependency():
    """Regression guard: nothing in this module should be able to reach
    api.open-meteo.com / archive-api.open-meteo.com again by accident."""
    assert not hasattr(hs, "_fetch_summer_temp")
    assert not hasattr(hs, "_summer_temp_grid")
    assert not hasattr(hs, "_era5_cache")
    assert not hasattr(hs, "OPEN_METEO_HIST")
