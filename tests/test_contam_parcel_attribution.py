import json
import math
import time
from threading import Event
from types import SimpleNamespace

from property_scores.contamination import parcel_attribution as pa


def _polygon(west, south, east, north):
    return json.dumps({"type": "Polygon", "coordinates": [[
        [west, south], [east, south], [east, north], [west, north],
        [west, south],
    ]]})


class _FakeCursor:
    def __init__(self, rows):
        self.rows = rows
        self.params = []

    def execute(self, sql, params):
        self.params.append((sql, params))
        return self

    def fetchall(self):
        return self.rows


def test_evidence_points_use_strict_contains_without_snap(monkeypatch):
    cursor = _FakeCursor([
        ("target", _polygon(144.959, -37.811, 144.961, -37.809)),
        ("neighbour", _polygon(144.961, -37.811, 144.963, -37.809)),
    ])
    monkeypatch.setattr(pa, "_get_conn", lambda: cursor)
    flags = pa.same_parcel_flags(
        "VIC", -37.81, 144.96,
        [(-37.8101, 144.9601), (-37.8101, 144.962)],
    )
    assert flags == [True, False]
    assert len(cursor.params) == 1
    assert "ST_Intersects" in cursor.params[0][0]
    assert "ST_DWithin" not in cursor.params[0][0]


def test_same_pfi_geometry_slices_are_one_parcel(monkeypatch):
    cursor = _FakeCursor([
        ("split", _polygon(144.959, -37.811, 144.9605, -37.809)),
        ("split", _polygon(144.9605, -37.811, 144.962, -37.809)),
    ])
    monkeypatch.setattr(pa, "_get_conn", lambda: cursor)
    assert pa.same_parcel_flags(
        "VIC", -37.81, 144.96, [(-37.81, 144.961)]
    ) == [True]


def test_snap_uses_real_metres_and_selects_nearest_parcel(monkeypatch):
    lat, lng = -37.81, 144.96
    lng_per_m = 1 / (111_320 * math.cos(math.radians(lat)))
    # The address lies between two lots: the east lot is 22 m away, while the
    # west lot containing the evidence point is 24 m away. Real-metre ordering
    # must select the east target and reject evidence in the west neighbour.
    cursor = _FakeCursor([
        ("a-west-evidence", _polygon(
            lng - 27 * lng_per_m, lat - 0.001,
            lng - 24 * lng_per_m, lat + 0.001)),
        ("z-east-target", _polygon(
            lng + 22 * lng_per_m, lat - 0.001,
            lng + 24 * lng_per_m, lat + 0.001)),
    ])
    monkeypatch.setattr(pa, "_get_conn", lambda: cursor)
    evidence = (lat, lng - 25 * lng_per_m)

    assert pa.same_parcel_flags("VIC", lat, lng, [evidence]) == [False]

    # The SQL prefilter itself must cover a true 25 m east-west at Melbourne,
    # not the 19.8 m produced by reusing the latitude degree conversion.
    _, west, _, east, _ = cursor.params[0][1]
    assert (east - lng) / lng_per_m >= 24.999
    assert (lng - west) / lng_per_m >= 24.999


def test_missing_target_parcel_is_unavailable_not_clean(monkeypatch):
    cursor = _FakeCursor([])
    monkeypatch.setattr(pa, "_get_conn", lambda: cursor)
    assert pa.same_parcel_flags(
        "VIC", -37.81, 144.96, [(-37.8101, 144.9601)]
    ) is None


def test_database_error_falls_back(monkeypatch):
    def unavailable():
        raise FileNotFoundError("parcels.duckdb")

    monkeypatch.setattr(pa, "_get_conn", unavailable)
    assert pa.same_parcel_flags(
        "VIC", -37.81, 144.96, [(-37.8101, 144.9601)]
    ) is None


def test_inode_change_schedules_off_request_refresh(monkeypatch):
    pa._conn, pa._conn_ino = object(), 1
    scheduled = []
    monkeypatch.setattr(pa.os, "stat", lambda path: SimpleNamespace(st_ino=2))
    monkeypatch.setattr(pa, "_schedule_refresh", scheduled.append)

    try:
        pa._get_conn()
    except RuntimeError as exc:
        assert "warming" in str(exc)
    else:  # pragma: no cover - explicit failure message
        raise AssertionError("stale parcel generation was served")
    assert scheduled == [2]

    pa._conn, pa._conn_ino = None, None


def test_expired_budget_does_not_open_database(monkeypatch):
    def should_not_open():
        raise AssertionError("expired budget opened DuckDB")

    monkeypatch.setattr(pa, "_get_conn", should_not_open)
    assert pa.same_parcel_flags(
        "VIC", -37.81, 144.96, [(-37.8101, 144.9601)], timeout_s=0
    ) is None


def test_running_query_is_interrupted_at_budget(monkeypatch):
    released = Event()

    class _BlockingCursor:
        def __init__(self):
            self.interrupted = False

        def interrupt(self):
            self.interrupted = True
            released.set()

        def execute(self, sql, params):
            released.wait(1)
            raise RuntimeError("interrupted")

    cursor = _BlockingCursor()
    monkeypatch.setattr(pa, "_get_conn", lambda: cursor)
    started = time.monotonic()

    result = pa.same_parcel_flags(
        "VIC", -37.81, 144.96, [(-37.8101, 144.9601)], timeout_s=0.03)

    assert result is None
    assert cursor.interrupted is True
    assert time.monotonic() - started < 0.3


def test_lock_wait_respects_budget(monkeypatch):
    acquired = pa._query_lock.acquire(timeout=0.1)
    assert acquired
    started = time.monotonic()
    try:
        result = pa.same_parcel_flags(
            "VIC", -37.81, 144.96,
            [(-37.8101, 144.9601)], timeout_s=0.03)
    finally:
        pa._query_lock.release()

    assert result is None
    assert time.monotonic() - started < 0.15
