"""Build the quiet-end measured-truth corpus -> data/eis_noise/measured_quiet_corpus.csv.

The 2026-06-10 NorthConnex validation proved the live model over-reads quiet NSW
residential by ~+11 dB while staying ±1 on loud roadside. This corpus freezes the
clean measured anchors for the recalibration's dual-anchor before/after check:

  1. NorthConnex ONAR (Wilkinson Murray 13245-O Ver J, Tables 7-1/7-2): Class-1
     LAeq day/eve/night at NW-Sydney residential receivers -> Lden (clean
     conversion, unlike RBL/L90). Geocoded via the daleads G-NAF autocomplete with
     suburb+state filtering (taking the first hit returns wrong localities, e.g.
     "45 Bareena Ave Wahroonga" -> Canley Vale).
  2. Lake Macquarie EMS IoT (15-min dBA): day/eve/night energy means -> Lden.
     Clean loud roadside anchors (Pacific Hwy / Charlestown). Sensors pinned at a
     hardware noise floor (min==p10 and tiny day-night swing) are excluded: the
     floor fakes "accurate" quiet readings (the 2026-06-10 red herring).
  3. Ballarat noise-observations: VIC parkland point(s), same aggregation.

Output columns:
  state,site,address,lat,lng,quality,role,meas_day,meas_eve,meas_night,
  meas_lden,metric,source,note
role:    quiet_residential | arterial | loud_roadside | iot_quiet | parkland
quality: exact | adjacent_suburb | street (geocode confidence; NorthConnex rows)

Anchors downstream (scripts/quiet_corpus_validate.py):
  - residential anchor = quiet_residential with quality=exact (n=9, the +10.9 set)
  - loud anchor        = loud_roadside
  iot_quiet/parkland ride along for context only (low-cost IoT, ±2-3 dB).

Usage:
  curl -s --max-time 500 "https://data.lakemac.com.au/api/v2/catalog/datasets/environmental-monitoring-system-realtime/exports/csv" -o /tmp/lakemac.csv
  .venv/bin/python scripts/build_quiet_corpus.py [--lakemac /tmp/lakemac.csv]
"""
import argparse
import csv
import json
import math
import os
import re
import sys
import time
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "eis_noise", "measured_quiet_corpus.csv")
BALLARAT = os.path.join(ROOT, "data", "ballarat_noise_observations.csv")

# NorthConnex Table 7-1 (logger->address) + 7-2 (measured LAeq Day/Eve/Night).
# road=True marks Pennant Hills Rd arterial loggers.
NCX_ROWS = [
    ("45 Bareena Avenue", "Wahroonga", 61, 60, 56, False),
    ("4 Douglas Avenue", "Wahroonga", 60, 59, 57, False),
    ("118A Coonanbarra Road", "Wahroonga", 57, 54, 52, False),
    ("18 Woniora Avenue", "Wahroonga", 62, 58, 54, False),
    ("12 Trelawney Street", "Thornleigh", 45, 43, 38, False),
    ("6 Trelawney Street", "Thornleigh", 59, 56, 52, False),
    ("6 Duffy Avenue", "Thornleigh", 60, 57, 55, False),
    ("1A Killaloe Avenue", "Pennant Hills", 54, 51, 50, False),
    ("18 Wilson Road", "Pennant Hills", 53, 50, 47, False),
    ("35 Coral Tree Drive", "Carlingford", 55, 53, 50, False),
    ("223 Pennant Hills Road", "Carlingford", 57, 57, 51, True),
    ("440 Pennant Hills Road", "Pennant Hills", 66, 65, 64, True),
    ("606 Pennant Hills Road", "Beecroft", 52, 51, 44, True),
]
NCX_SOURCE = "NorthConnex ONAR WM13245-O Table 7-1/7-2 (Class-1 LAeq, free-field 1.5m)"

# IoT exclusion rules (diagnosed on the 2026-06-10 export):
#   floor: quiet sites sit on the hardware noise floor all night, so the night
#   p25 collapses onto the sensor minimum (Gari St 57/57, Whitebridge 57/57,
#   Charlestown PS 58/57+1) while real roadside keeps >1 dB of night headroom.
#   The floored night reading (+10 Lden penalty) fakes a loud Lden at genuinely
#   quiet sites -- the 2026-06-10 red herring.
#   junk: sensors with a handful of samples (Art Gallery Jetty n=154 day 85 dB,
#   Speers Point Pool n=30, no night) are test/broken units.
FLOOR_NIGHT_P25_DB = 1.0   # night p25 within this of sensor min => floored
MIN_SAMPLES = 1000         # below this the sensor is test/broken
LOUD_ROADSIDE_LDEN = 65.0  # kept IoT sensors at/above this Lden anchor the loud end


def lden(day, eve, night):
    return 10 * math.log10((12 * 10 ** (day / 10) + 4 * 10 ** ((eve + 5) / 10)
                            + 8 * 10 ** ((night + 10) / 10)) / 24)


def _api(query):
    url = ("https://daleads.com.au/api/address-autocomplete?q="
           + urllib.parse.quote(query))
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            d = json.load(urllib.request.urlopen(req, timeout=20))
            return d if isinstance(d, list) else []
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(10 * (attempt + 1))
                continue
            raise
    return []


def _haversine_km(lat1, lng1, lat2, lng2):
    p = math.pi / 180
    a = (0.5 - math.cos((lat2 - lat1) * p) / 2
         + math.cos(lat1 * p) * math.cos(lat2 * p)
         * (1 - math.cos((lng2 - lng1) * p)) / 2)
    return 12742 * math.asin(math.sqrt(a))


def geocode_ncx(street_addr, suburb):
    """Suburb-filtered G-NAF geocode with quality grading.

    exact            house number (plain / unit "18/14-18" / range "14-18")
                     + full street + suburb all match
    adjacent_suburb  number + full street match, G-NAF locality differs but the
                     point is within 5 km of the street's in-suburb centroid
                     (e.g. 606 Pennant Hills Rd: EIS says Beecroft, G-NAF says
                     West Pennant Hills -- same physical address)
    street           no numbered match; street-centroid within the suburb
    """
    num_m = re.match(r"(\S+)\s+(.*)", street_addr)
    house_no, street = num_m.group(1), num_m.group(2)
    street_l = street.lower()
    no_digits = re.sub(r"\D", "", house_no)
    no_int = int(no_digits) if no_digits else None

    def number_match(disp):
        """plain '18 ', unit '18/...', or range '14-18 ' containing the number."""
        d = disp.lower()
        if d.startswith(house_no.lower() + " "):
            return "plain"
        if d.startswith(house_no.lower() + "/"):
            return "unit"
        m = re.match(r"(\d+)-(\d+)\s", d)
        if m and no_int is not None and \
                int(m.group(1)) <= no_int <= int(m.group(2)):
            return "range"
        return None

    items = _api(f"{street_addr} {suburb} NSW")
    time.sleep(3)
    items2 = _api(f"{street} {suburb}")
    time.sleep(3)
    centroid = next(
        ((it["lat"], it["lng"]) for it in items2
         if (it.get("suburb") or "").lower() == suburb.lower()
         and street_l in (it.get("display") or "").lower()), None)

    for it in items:  # 1. number + full street + suburb
        kind = number_match(it.get("display") or "")
        if kind and street_l in (it.get("display") or "").lower() and \
                (it.get("suburb") or "").lower() == suburb.lower():
            note = "" if kind == "plain" else f"{kind} address: {it.get('display')}"
            return it["lat"], it["lng"], "exact", note
    for it in items:  # 2. number + full street, adjacent locality (sanity 5 km)
        kind = number_match(it.get("display") or "")
        if kind and street_l in (it.get("display") or "").lower() and \
                it.get("state") == "NSW" and centroid and \
                _haversine_km(it["lat"], it["lng"], *centroid) < 5:
            return (it["lat"], it["lng"], "adjacent_suburb",
                    f"G-NAF locality {it.get('suburb')} (EIS says {suburb})")
    if centroid:  # 3. street centroid in suburb
        return (centroid[0], centroid[1], "street",
                f"street centroid (no numbered G-NAF match for {house_no})")
    return None, None, "fail", "no G-NAF match"


def northconnex_rows():
    rows = []
    for street_addr, suburb, dy, ev, ni, road in NCX_ROWS:
        lat, lng, quality, note = geocode_ncx(street_addr, suburb)
        if lat is None:
            print(f"  !! geocode FAIL: {street_addr} {suburb}", file=sys.stderr)
            continue
        rows.append({
            "state": "NSW", "site": f"NCX {street_addr} {suburb}",
            "address": f"{street_addr}, {suburb} NSW",
            "lat": round(float(lat), 8), "lng": round(float(lng), 8),
            "quality": quality,
            "role": "arterial" if road else "quiet_residential",
            "meas_day": dy, "meas_eve": ev, "meas_night": ni,
            "meas_lden": round(lden(dy, ev, ni), 1),
            "metric": "LAeq", "source": NCX_SOURCE, "note": note,
        })
        print(f"  {street_addr} {suburb}: ({lat:.6f},{lng:.6f}) [{quality}] "
              f"Lden {rows[-1]['meas_lden']}")
    return rows


def iot_rows(csv_path, tz, t_col, dev_col, db_col, loc_col, state, source,
             default_role):
    """Aggregate an Opendatasoft IoT export to per-sensor Lden with floor stats."""
    import duckdb
    if not os.path.exists(csv_path) or os.path.getsize(csv_path) < 100:
        print(f"  !! missing {csv_path} - skipped", file=sys.stderr)
        return []
    con = duckdb.connect()
    con.execute("SET TimeZone='UTC'")
    agg = con.execute(f"""
        with t as (
          select {dev_col} dev, {db_col} db,
                 {loc_col} loc,
                 extract(hour from ({t_col}::timestamptz at time zone '{tz}')) h
          from read_csv_auto('{csv_path}', ignore_errors=true)
          where {db_col} > 0 and {db_col} between 30 and 130
        ),
        per as (
          select dev,
            case when h>=7 and h<19 then 'day' when h>=19 and h<23 then 'eve'
                 else 'night' end period,
            10*log10(avg(pow(10, db/10.0))) laeq, count(*) n
          from t group by dev, period
        ),
        floor_stats as (
          select dev, min(db) mn,
                 quantile_cont(case when h>=23 or h<7 then db end, 0.25) night_p25,
                 any_value(loc) loc, count(*) n_all
          from t group by dev
        )
        select p.dev, f.loc, f.mn, f.night_p25, f.n_all,
               max(case when period='day' then laeq end) laeq_day,
               max(case when period='eve' then laeq end) laeq_eve,
               max(case when period='night' then laeq end) laeq_night
        from per p join floor_stats f using (dev)
        group by p.dev, f.loc, f.mn, f.night_p25, f.n_all
    """).fetchall()
    rows = []
    for dev, loc, mn, night_p25, n_all, day, eve, night in agg:
        if None in (day, eve, night, night_p25) or not loc:
            continue
        try:
            lat, lng = (float(x) for x in str(loc).split(","))
        except ValueError:
            continue
        if not (-44 < lat < -10):
            continue
        ml = lden(day, eve, night)
        if n_all < MIN_SAMPLES:
            print(f"  -- junk-excluded: {dev} (n={n_all})")
            continue
        if night_p25 - mn <= FLOOR_NIGHT_P25_DB:
            print(f"  -- floor-excluded: {dev} (min={mn:.0f} "
                  f"night_p25={night_p25:.0f})")
            continue
        role = "loud_roadside" if ml >= LOUD_ROADSIDE_LDEN else default_role
        rows.append({
            "state": state, "site": dev, "address": "",
            "lat": round(lat, 6), "lng": round(lng, 6),
            "quality": "sensor", "role": role,
            "meas_day": round(day, 1), "meas_eve": round(eve, 1),
            "meas_night": round(night, 1), "meas_lden": round(ml, 1),
            "metric": "IoT~LAeq", "source": source,
            "note": f"n={n_all} min={mn:.0f} night_p25={night_p25:.0f}",
        })
        print(f"  {dev}: Lden {ml:.1f} [{role}] n={n_all}")
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lakemac", default="/tmp/lakemac.csv")
    args = ap.parse_args()

    print("== NorthConnex (geocoding via daleads G-NAF, suburb-filtered) ==")
    rows = northconnex_rows()

    print("== Lake Macquarie EMS ==")
    rows += iot_rows(args.lakemac, "Australia/Sydney", "metadata_time",
                     "device_name", "payload_fields_avg_soundpressure",
                     "location", "NSW",
                     "Lake Macquarie EMS (data.lakemac.com.au)", "iot_quiet")

    print("== Ballarat noise observations ==")
    rows += iot_rows(BALLARAT, "Australia/Melbourne", "date_time",
                     "location_description", "sound_pressure_level_average",
                     "point", "VIC",
                     "City of Ballarat noise-observations", "parkland")

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    cols = ["state", "site", "address", "lat", "lng", "quality", "role",
            "meas_day", "meas_eve", "meas_night", "meas_lden", "metric",
            "source", "note"]
    with open(OUT, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    print(f"\nwrote {len(rows)} rows -> {OUT}")
    from collections import Counter
    print("roles:", dict(Counter(r["role"] for r in rows)))


if __name__ == "__main__":
    main()
