"""
Process Australian state GTFS data into standardised rail/tram parquets.

Produces two outputs matching the PTV parquet schema:
  - shapes:    shape_id, route_type, lat, lng, sequence, state
  - frequency: route_id, route_name, route_long_name, route_type,
               shape_id, peak_services_per_hour, offpeak_services_per_hour, state

Route type mapping (GTFS standard + extended):
  0  = Tram / Light Rail (standard)
  1  = Subway / Metro (standard)
  2  = Rail / Train (standard)
  106 = Underground Railway (extended, used by some agencies for rail)
  401 = Metro Service (extended, used by TfNSW for Sydney Metro)
  900 = Tram / Light Rail (extended, used by TfNSW for Sydney Light Rail)

All non-standard types are mapped to standard 0/1/2 in output.

Peak hours:   7-8 AM (2 hours: depart at 07:xx or 08:xx)
Off-peak:     10 AM - 2 PM (5 hours: depart at 10-14:xx)
Frequency:    count distinct trips / number of hours

Usage:
  python scripts/process_state_gtfs.py --state nsw   # single state
  python scripts/process_state_gtfs.py --all          # all states
  python scripts/process_state_gtfs.py --combine       # combine all into national parquets
"""

import argparse
import os
import sys

import duckdb

DATA_DIR = "D:/property-scores-data"
GTFS_DIR = f"{DATA_DIR}/gtfs"

# ── State configuration ─────────────────────────────────────────────
# Each state defines:
#   gtfs_path:    path to extracted GTFS data dir
#   rail_types:   dict mapping GTFS route_type -> output standard type
#                 0 = tram/light rail, 1 = metro, 2 = rail/train
#   exclude_names: patterns to exclude (replacement buses etc.)

STATE_CONFIG = {
    "vic": {
        "gtfs_path": None,  # VIC uses existing PTV parquets
        "rail_types": {},
        "note": "VIC processed separately via analyze_gtfs_v2.py; included in combine step from existing parquets",
    },
    "nsw": {
        "gtfs_path": f"{GTFS_DIR}/nsw/data",
        "rail_types": {
            "2": 2,     # Sydney Trains + NSW TrainLink (actual rail)
            "401": 1,   # Sydney Metro
            "900": 0,   # Sydney Light Rail
        },
        "exclude_names": ["replacement", "shuttle bus"],
    },
    "qld": {
        "gtfs_path": f"{GTFS_DIR}/qld/data",
        "rail_types": {
            "0": 0,     # Gold Coast Light Rail
            "2": 2,     # Queensland Rail
        },
        "exclude_names": ["replacement", "rail bus"],
    },
    "wa": {
        "gtfs_path": f"{GTFS_DIR}/wa/data",
        "rail_types": {
            "2": 2,     # Transperth trains
        },
        "exclude_names": ["replacement", "bus"],
    },
    "sa": {
        "gtfs_path": f"{GTFS_DIR}/sa/data",
        "rail_types": {
            "0": 0,     # Glenelg tram + Botanic + Festival
            "2": 2,     # Adelaide Metro trains
        },
        "exclude_names": ["replacement"],
    },
    "act": {
        "gtfs_path": f"{GTFS_DIR}/act/data",
        "rail_types": {},
        "note": "ACT light rail not in GTFS feed (only buses as type 3); skipped",
    },
}


def clean(col: str) -> str:
    """SQL expression to trim quotes from a GTFS CSV column."""
    return f"TRIM(BOTH '\"' FROM {col})"


def process_state(db: duckdb.DuckDBPyConnection, state: str) -> dict:
    """Process one state's GTFS into shapes + frequency tables.

    Returns dict with counts or None if state is skipped.
    """
    cfg = STATE_CONFIG.get(state)
    if not cfg:
        print(f"  [SKIP] Unknown state: {state}")
        return None

    if not cfg.get("rail_types"):
        print(f"  [SKIP] {state.upper()}: {cfg.get('note', 'no rail types configured')}")
        return None

    base = cfg["gtfs_path"]
    if not os.path.isdir(base):
        print(f"  [ERROR] GTFS data not found at {base}")
        return None

    rail_types = cfg["rail_types"]
    exclude = cfg.get("exclude_names", [])

    print(f"  Processing {state.upper()}...")
    print(f"    GTFS path: {base}")
    print(f"    Rail types: {rail_types}")

    pfx = state  # table prefix to avoid collisions

    # ── Load GTFS tables ────────────────────────────────────────────
    db.execute(f"""
        CREATE OR REPLACE TABLE {pfx}_calendar AS
        SELECT * FROM read_csv_auto('{base}/calendar.txt', header=true, all_varchar=true)
    """)
    db.execute(f"""
        CREATE OR REPLACE TABLE {pfx}_routes AS
        SELECT * FROM read_csv_auto('{base}/routes.txt', header=true, all_varchar=true)
    """)
    db.execute(f"""
        CREATE OR REPLACE TABLE {pfx}_trips AS
        SELECT * FROM read_csv_auto('{base}/trips.txt', header=true, all_varchar=true)
    """)
    db.execute(f"""
        CREATE OR REPLACE TABLE {pfx}_stop_times AS
        SELECT
            {clean('trip_id')} as trip_id,
            {clean('departure_time')} as departure_time,
            CAST({clean('stop_sequence')} AS INTEGER) as stop_sequence
        FROM read_csv_auto('{base}/stop_times.txt', header=true, all_varchar=true)
    """)

    # Check for calendar_dates.txt (optional but common)
    cal_dates_path = f"{base}/calendar_dates.txt"
    has_cal_dates = os.path.exists(cal_dates_path)
    if has_cal_dates:
        db.execute(f"""
            CREATE OR REPLACE TABLE {pfx}_cal_dates AS
            SELECT * FROM read_csv_auto('{cal_dates_path}', header=true, all_varchar=true)
        """)

    # ── Filter to rail/tram routes only ─────────────────────────────
    type_list = ", ".join(f"'{t}'" for t in rail_types.keys())

    # Build exclusion WHERE clause
    excl_clauses = []
    for pat in exclude:
        excl_clauses.append(
            f"AND LOWER(COALESCE({clean('route_short_name')}, '')) NOT LIKE '%{pat}%'"
        )
        excl_clauses.append(
            f"AND LOWER(COALESCE({clean('route_long_name')}, '')) NOT LIKE '%{pat}%'"
        )
    excl_sql = "\n".join(excl_clauses)

    db.execute(f"""
        CREATE OR REPLACE TABLE {pfx}_rail_routes AS
        SELECT
            {clean('route_id')} as route_id,
            COALESCE({clean('route_short_name')}, '') as route_short_name,
            COALESCE({clean('route_long_name')}, '') as route_long_name,
            {clean('route_type')} as route_type_raw
        FROM {pfx}_routes
        WHERE {clean('route_type')} IN ({type_list})
        {excl_sql}
    """)

    route_cnt = db.execute(f"SELECT COUNT(*) FROM {pfx}_rail_routes").fetchone()[0]
    print(f"    Rail/tram routes after filter: {route_cnt}")

    if route_cnt == 0:
        return None

    # ── Find reference Wednesday services (date-aware) ────────────
    # Strategy: pick a specific target Wednesday, then find all service_ids
    # active on that date using calendar + calendar_dates.
    # This avoids double-counting from overlapping calendar periods.

    # Find the first Wednesday that falls within any calendar period
    target_wed = db.execute(f"""
        WITH bounds AS (
            SELECT
                MIN(CAST({clean('start_date')} AS INTEGER)) as min_sd,
                MAX(CAST({clean('end_date')} AS INTEGER)) as max_ed
            FROM {pfx}_calendar
            WHERE {clean('wednesday')} = '1'
        ),
        -- Generate candidate dates from start_date onwards
        candidates AS (
            SELECT CAST({clean('start_date')} AS INTEGER) as d
            FROM {pfx}_calendar
            WHERE {clean('wednesday')} = '1'
            UNION
            SELECT CAST({clean('end_date')} AS INTEGER) FROM {pfx}_calendar
        )
        -- Pick the earliest start_date that has wednesday=1
        -- Then find a Wednesday 7 days into that period (stable mid-period)
        SELECT MIN(CAST({clean('start_date')} AS INTEGER)) as first_start
        FROM {pfx}_calendar
        WHERE {clean('wednesday')} = '1'
    """).fetchone()

    if target_wed and target_wed[0]:
        # Convert YYYYMMDD integer to find first Wednesday after it
        first_start = str(target_wed[0])
        from datetime import datetime, timedelta
        start_dt = datetime.strptime(first_start, "%Y%m%d")
        # Find next Wednesday (weekday 2)
        days_ahead = (2 - start_dt.weekday()) % 7
        if days_ahead == 0 and start_dt.weekday() != 2:
            days_ahead = 7
        wed_dt = start_dt + timedelta(days=days_ahead)
        # Move one week forward for stability (avoid first-day edge effects)
        wed_dt = wed_dt + timedelta(days=7)
        target_date = wed_dt.strftime("%Y%m%d")
        print(f"    Target Wednesday: {target_date}")
    else:
        print(f"    [ERROR] No calendar periods with wednesday=1")
        return None

    # Build active service_ids for the target date:
    # 1. calendar: start_date <= target AND end_date >= target AND wednesday=1
    # 2. Minus calendar_dates exceptions (exception_type=2)
    # 3. Plus calendar_dates additions (exception_type=1)
    cal_dates_minus = ""
    cal_dates_plus = ""
    if has_cal_dates:
        cal_dates_minus = f"""
            AND {clean('service_id')} NOT IN (
                SELECT {clean('service_id')} FROM {pfx}_cal_dates
                WHERE {clean('date')} = '{target_date}'
                  AND {clean('exception_type')} = '2'
            )
        """
        cal_dates_plus = f"""
            UNION
            SELECT DISTINCT {clean('service_id')} as service_id
            FROM {pfx}_cal_dates
            WHERE {clean('date')} = '{target_date}'
              AND {clean('exception_type')} = '1'
        """

    db.execute(f"""
        CREATE OR REPLACE TABLE {pfx}_wed_services AS
        SELECT DISTINCT {clean('service_id')} as service_id
        FROM {pfx}_calendar
        WHERE {clean('wednesday')} = '1'
          AND CAST({clean('start_date')} AS INTEGER) <= {target_date}
          AND CAST({clean('end_date')} AS INTEGER) >= {target_date}
          {cal_dates_minus}
        {cal_dates_plus}
    """)

    wed_cnt = db.execute(f"SELECT COUNT(*) FROM {pfx}_wed_services").fetchone()[0]
    print(f"    Active service_ids on {target_date}: {wed_cnt}")

    if wed_cnt == 0:
        print(f"    [WARN] No Wednesday services for {target_date}, trying any weekday...")
        for day in ["tuesday", "thursday", "monday", "friday"]:
            day_num = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4}[day]
            alt_dt = wed_dt + timedelta(days=(day_num - 2))
            alt_date = alt_dt.strftime("%Y%m%d")

            cal_dates_minus_alt = ""
            cal_dates_plus_alt = ""
            if has_cal_dates:
                cal_dates_minus_alt = f"""
                    AND {clean('service_id')} NOT IN (
                        SELECT {clean('service_id')} FROM {pfx}_cal_dates
                        WHERE {clean('date')} = '{alt_date}'
                          AND {clean('exception_type')} = '2'
                    )
                """
                cal_dates_plus_alt = f"""
                    UNION
                    SELECT DISTINCT {clean('service_id')} as service_id
                    FROM {pfx}_cal_dates
                    WHERE {clean('date')} = '{alt_date}'
                      AND {clean('exception_type')} = '1'
                """

            db.execute(f"""
                CREATE OR REPLACE TABLE {pfx}_wed_services AS
                SELECT DISTINCT {clean('service_id')} as service_id
                FROM {pfx}_calendar
                WHERE {clean(day)} = '1'
                  AND CAST({clean('start_date')} AS INTEGER) <= {alt_date}
                  AND CAST({clean('end_date')} AS INTEGER) >= {alt_date}
                  {cal_dates_minus_alt}
                {cal_dates_plus_alt}
            """)
            wed_cnt = db.execute(
                f"SELECT COUNT(*) FROM {pfx}_wed_services"
            ).fetchone()[0]
            if wed_cnt > 0:
                print(f"    Using {day} ({alt_date}) services instead: {wed_cnt}")
                break

    if wed_cnt == 0:
        print(f"    [ERROR] No weekday services found at all")
        return None

    # ── Get trip departures (first stop only) for rail routes ───────
    db.execute(f"""
        CREATE OR REPLACE TABLE {pfx}_departures AS
        SELECT
            {clean('t.route_id')} as route_id,
            {clean('t.trip_id')} as trip_id,
            {clean('t.shape_id')} as shape_id,
            st.departure_time,
            CAST(SPLIT_PART(st.departure_time, ':', 1) AS INTEGER) as dep_hour
        FROM {pfx}_trips t
        JOIN {pfx}_wed_services ws
            ON {clean('t.service_id')} = ws.service_id
        JOIN {pfx}_stop_times st
            ON {clean('t.trip_id')} = st.trip_id AND st.stop_sequence = 1
        WHERE {clean('t.route_id')} IN (SELECT route_id FROM {pfx}_rail_routes)
    """)

    trip_cnt = db.execute(f"SELECT COUNT(*) FROM {pfx}_departures").fetchone()[0]
    print(f"    Wednesday rail/tram trips: {trip_cnt}")

    # If very few trips from first-stop join, try stop_sequence = minimum per trip
    if trip_cnt < route_cnt:
        print(f"    [INFO] Trying min stop_sequence per trip (some feeds start at 0 or >1)...")
        db.execute(f"""
            CREATE OR REPLACE TABLE {pfx}_departures AS
            WITH first_stops AS (
                SELECT trip_id, MIN(stop_sequence) as min_seq
                FROM {pfx}_stop_times
                GROUP BY trip_id
            )
            SELECT
                {clean('t.route_id')} as route_id,
                {clean('t.trip_id')} as trip_id,
                {clean('t.shape_id')} as shape_id,
                st.departure_time,
                CAST(SPLIT_PART(st.departure_time, ':', 1) AS INTEGER) as dep_hour
            FROM {pfx}_trips t
            JOIN {pfx}_wed_services ws
                ON {clean('t.service_id')} = ws.service_id
            JOIN first_stops fs
                ON {clean('t.trip_id')} = fs.trip_id
            JOIN {pfx}_stop_times st
                ON {clean('t.trip_id')} = st.trip_id AND st.stop_sequence = fs.min_seq
            WHERE {clean('t.route_id')} IN (SELECT route_id FROM {pfx}_rail_routes)
        """)
        trip_cnt = db.execute(f"SELECT COUNT(*) FROM {pfx}_departures").fetchone()[0]
        print(f"    After min-seq fallback: {trip_cnt} trips")

    # ── Build type mapping CASE expression ──────────────────────────
    case_parts = []
    for raw_type, std_type in rail_types.items():
        case_parts.append(f"WHEN '{raw_type}' THEN {std_type}")
    type_case = f"CASE rr.route_type_raw {' '.join(case_parts)} ELSE 2 END"

    # ── Calculate frequency per route ───────────────────────────────
    # Peak: dep_hour IN (7, 8)  → 2 hours
    # Off-peak: dep_hour IN (10, 11, 12, 13, 14)  → 5 hours
    db.execute(f"""
        CREATE OR REPLACE TABLE {pfx}_route_freq AS
        WITH peak AS (
            SELECT route_id,
                   COUNT(*) * 1.0 / 2.0 as services_per_hour
            FROM {pfx}_departures
            WHERE dep_hour IN (7, 8)
            GROUP BY route_id
        ),
        offpeak AS (
            SELECT route_id,
                   COUNT(*) * 1.0 / 5.0 as services_per_hour
            FROM {pfx}_departures
            WHERE dep_hour IN (10, 11, 12, 13, 14)
            GROUP BY route_id
        )
        SELECT
            rr.route_id,
            rr.route_short_name as route_name,
            rr.route_long_name,
            {type_case} as route_type,
            COALESCE(p.services_per_hour, 0) as peak_services_per_hour,
            COALESCE(o.services_per_hour, 0) as offpeak_services_per_hour
        FROM {pfx}_rail_routes rr
        LEFT JOIN peak p ON rr.route_id = p.route_id
        LEFT JOIN offpeak o ON rr.route_id = o.route_id
    """)

    # ── Pick most-used shape per route ──────────────────────────────
    db.execute(f"""
        CREATE OR REPLACE TABLE {pfx}_route_shapes AS
        WITH ranked AS (
            SELECT
                {clean('route_id')} as route_id,
                {clean('shape_id')} as shape_id,
                COUNT(*) as cnt,
                ROW_NUMBER() OVER (
                    PARTITION BY {clean('route_id')}
                    ORDER BY COUNT(*) DESC
                ) as rn
            FROM {pfx}_trips
            WHERE {clean('route_id')} IN (SELECT route_id FROM {pfx}_rail_routes)
            GROUP BY 1, 2
        )
        SELECT route_id, shape_id FROM ranked WHERE rn = 1
    """)

    # ── Build frequency output table ────────────────────────────────
    db.execute(f"""
        CREATE OR REPLACE TABLE {pfx}_freq_out AS
        SELECT
            rf.route_id,
            rf.route_name,
            rf.route_long_name,
            rf.route_type,
            rs.shape_id,
            ROUND(rf.peak_services_per_hour, 2) as peak_services_per_hour,
            ROUND(rf.offpeak_services_per_hour, 2) as offpeak_services_per_hour,
            '{state}' as state
        FROM {pfx}_route_freq rf
        LEFT JOIN {pfx}_route_shapes rs ON rf.route_id = rs.route_id
        WHERE rf.peak_services_per_hour > 0 OR rf.offpeak_services_per_hour > 0
    """)

    freq_cnt = db.execute(f"SELECT COUNT(*) FROM {pfx}_freq_out").fetchone()[0]

    # ── Build shapes output table ───────────────────────────────────
    db.execute(f"""
        CREATE OR REPLACE TABLE {pfx}_shapes_out AS
        SELECT
            {clean('s.shape_id')} as shape_id,
            fo.route_type,
            CAST({clean('s.shape_pt_lat')} AS DOUBLE) as lat,
            CAST({clean('s.shape_pt_lon')} AS DOUBLE) as lng,
            CAST({clean('s.shape_pt_sequence')} AS INTEGER) as sequence,
            '{state}' as state
        FROM read_csv_auto('{base}/shapes.txt', header=true, all_varchar=true) s
        JOIN (
            SELECT DISTINCT shape_id, route_type FROM {pfx}_freq_out
            WHERE shape_id IS NOT NULL
        ) fo ON {clean('s.shape_id')} = fo.shape_id
    """)

    shape_cnt = db.execute(f"SELECT COUNT(*) FROM {pfx}_shapes_out").fetchone()[0]

    # ── Export per-state parquets ────────────────────────────────────
    freq_path = f"{GTFS_DIR}/{state}/{state}_rail_frequency.parquet"
    shapes_path = f"{GTFS_DIR}/{state}/{state}_rail_shapes.parquet"

    db.execute(f"COPY {pfx}_freq_out TO '{freq_path}' (FORMAT PARQUET)")
    db.execute(f"COPY {pfx}_shapes_out TO '{shapes_path}' (FORMAT PARQUET)")

    print(f"    Output: {freq_cnt} routes, {shape_cnt} shape points")
    print(f"    -> {freq_path}")
    print(f"    -> {shapes_path}")

    # ── Print summary ───────────────────────────────────────────────
    summary = db.execute(f"""
        SELECT route_type,
               COUNT(*) as routes,
               ROUND(AVG(peak_services_per_hour), 1) as avg_peak,
               ROUND(AVG(offpeak_services_per_hour), 1) as avg_offpeak
        FROM {pfx}_freq_out
        GROUP BY route_type
        ORDER BY route_type
    """).fetchall()

    type_names = {0: "Tram/LR", 1: "Metro", 2: "Rail"}
    for row in summary:
        tname = type_names.get(row[0], f"type{row[0]}")
        print(f"    {tname}: {row[1]} routes, peak avg {row[2]}/hr, offpeak avg {row[3]}/hr")

    return {"freq_count": freq_cnt, "shape_count": shape_cnt}


def combine_national(db: duckdb.DuckDBPyConnection):
    """Combine all state parquets + existing PTV into national files."""
    print("\n" + "=" * 70)
    print("COMBINING NATIONAL PARQUETS")
    print("=" * 70)

    # ── Frequency ───────────────────────────────────────────────────
    freq_parts = []

    # VIC from existing PTV parquets
    ptv_freq = f"{DATA_DIR}/ptv_rail_frequency.parquet"
    if os.path.exists(ptv_freq):
        freq_parts.append(f"""
            SELECT route_id, route_name, route_long_name, route_type,
                   shape_id, peak_services_per_hour, offpeak_services_per_hour,
                   'vic' as state
            FROM read_parquet('{ptv_freq}')
        """)
        cnt = db.execute(
            f"SELECT COUNT(*) FROM read_parquet('{ptv_freq}')"
        ).fetchone()[0]
        print(f"  VIC (PTV): {cnt} routes")
    else:
        print(f"  [WARN] VIC PTV parquet not found at {ptv_freq}")

    # Other states
    for state in ["nsw", "qld", "wa", "sa"]:
        path = f"{GTFS_DIR}/{state}/{state}_rail_frequency.parquet"
        if os.path.exists(path):
            freq_parts.append(f"""
                SELECT route_id, route_name, route_long_name, route_type,
                       shape_id, peak_services_per_hour, offpeak_services_per_hour,
                       state
                FROM read_parquet('{path}')
            """)
            cnt = db.execute(
                f"SELECT COUNT(*) FROM read_parquet('{path}')"
            ).fetchone()[0]
            print(f"  {state.upper()}: {cnt} routes")
        else:
            print(f"  [SKIP] {state.upper()}: no parquet at {path}")

    if not freq_parts:
        print("  [ERROR] No frequency data to combine")
        return

    freq_sql = " UNION ALL ".join(freq_parts)
    national_freq = f"{DATA_DIR}/au_rail_frequency.parquet"
    db.execute(f"COPY ({freq_sql}) TO '{national_freq}' (FORMAT PARQUET)")
    total = db.execute(f"SELECT COUNT(*) FROM ({freq_sql})").fetchone()[0]
    size_mb = os.path.getsize(national_freq) / 1024 / 1024
    print(f"\n  au_rail_frequency.parquet: {total} routes, {size_mb:.1f} MB")

    # ── Shapes ──────────────────────────────────────────────────────
    shape_parts = []

    # VIC from existing PTV parquets
    ptv_shapes = f"{DATA_DIR}/ptv_rail_shapes.parquet"
    if os.path.exists(ptv_shapes):
        shape_parts.append(f"""
            SELECT shape_id, route_type, lat, lng, sequence,
                   'vic' as state
            FROM read_parquet('{ptv_shapes}')
        """)
        cnt = db.execute(
            f"SELECT COUNT(*) FROM read_parquet('{ptv_shapes}')"
        ).fetchone()[0]
        print(f"  VIC (PTV) shapes: {cnt} points")
    else:
        print(f"  [WARN] VIC PTV shapes not found at {ptv_shapes}")

    for state in ["nsw", "qld", "wa", "sa"]:
        path = f"{GTFS_DIR}/{state}/{state}_rail_shapes.parquet"
        if os.path.exists(path):
            shape_parts.append(f"""
                SELECT shape_id, route_type, lat, lng, sequence, state
                FROM read_parquet('{path}')
            """)
            cnt = db.execute(
                f"SELECT COUNT(*) FROM read_parquet('{path}')"
            ).fetchone()[0]
            print(f"  {state.upper()} shapes: {cnt} points")
        else:
            print(f"  [SKIP] {state.upper()} shapes: no parquet at {path}")

    if not shape_parts:
        print("  [ERROR] No shape data to combine")
        return

    shapes_sql = " UNION ALL ".join(shape_parts)
    national_shapes = f"{DATA_DIR}/au_rail_shapes.parquet"
    db.execute(f"COPY ({shapes_sql}) TO '{national_shapes}' (FORMAT PARQUET)")
    total = db.execute(f"SELECT COUNT(*) FROM ({shapes_sql})").fetchone()[0]
    size_mb = os.path.getsize(national_shapes) / 1024 / 1024
    print(f"  au_rail_shapes.parquet: {total} points, {size_mb:.1f} MB")

    # ── Summary by state ────────────────────────────────────────────
    print(f"\n{'='*70}")
    print("NATIONAL SUMMARY")
    print(f"{'='*70}")

    type_names = {0: "Tram/LR", 1: "Metro", 2: "Rail"}

    summary = db.execute(f"""
        SELECT state,
               route_type,
               COUNT(*) as routes,
               ROUND(AVG(peak_services_per_hour), 1) as avg_peak,
               ROUND(MEDIAN(peak_services_per_hour), 1) as med_peak,
               ROUND(AVG(offpeak_services_per_hour), 1) as avg_offpeak
        FROM ({freq_sql})
        GROUP BY state, route_type
        ORDER BY state, route_type
    """).fetchall()

    print(f"\n  {'State':<6} {'Type':<10} {'Routes':>6} {'Peak avg':>9} {'Peak med':>9} {'OffPk avg':>10}")
    print(f"  {'-'*6} {'-'*10} {'-'*6} {'-'*9} {'-'*9} {'-'*10}")
    for row in summary:
        tname = type_names.get(row[1], f"type{row[1]}")
        print(
            f"  {row[0].upper():<6} {tname:<10} {row[2]:>6} {row[3]:>9.1f} {row[4]:>9.1f} {row[5]:>10.1f}"
        )

    total_routes = db.execute(f"SELECT COUNT(*) FROM ({freq_sql})").fetchone()[0]
    total_shapes = db.execute(f"SELECT COUNT(*) FROM ({shapes_sql})").fetchone()[0]
    print(f"\n  TOTAL: {total_routes} routes, {total_shapes} shape points")


def main():
    parser = argparse.ArgumentParser(
        description="Process Australian state GTFS data into rail/tram parquets"
    )
    parser.add_argument(
        "--state",
        choices=list(STATE_CONFIG.keys()),
        help="Process a single state",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Process all states",
    )
    parser.add_argument(
        "--combine",
        action="store_true",
        help="Combine all state parquets into national files",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show per-route detail",
    )

    args = parser.parse_args()

    if not any([args.state, args.all, args.combine]):
        parser.print_help()
        sys.exit(1)

    db = duckdb.connect()

    try:
        if args.state:
            print(f"{'='*70}")
            print(f"Processing {args.state.upper()}")
            print(f"{'='*70}")
            result = process_state(db, args.state)
            if result:
                print(f"\nDone: {result['freq_count']} routes, {result['shape_count']} shape points")

        if args.all:
            print(f"{'='*70}")
            print("Processing ALL states")
            print(f"{'='*70}")
            results = {}
            for state in STATE_CONFIG:
                if state == "vic":
                    continue  # VIC uses existing PTV parquets
                print(f"\n--- {state.upper()} ---")
                result = process_state(db, state)
                if result:
                    results[state] = result

            print(f"\n{'='*70}")
            print("PER-STATE RESULTS")
            print(f"{'='*70}")
            for state, result in results.items():
                print(f"  {state.upper()}: {result['freq_count']} routes, {result['shape_count']} shape points")

        if args.combine or args.all:
            combine_national(db)

    finally:
        db.close()


if __name__ == "__main__":
    main()
