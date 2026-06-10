"""Validate the noise model against council IoT noise-sensor networks.

For each fixed sensor: aggregate its 15-min measured dB samples into day/evening/
night energy-mean levels (in the sensor's local timezone), combine to Lden, then
compare to the model's Lden at the sensor coordinate.

Sources (council Opendatasoft, measured dB + coords, ~15-min cadence):
  - Lake Macquarie EMS (NSW): metadata_time(UTC), device_name, avg_soundpressure(dBA), location
  - City of Melbourne microclimate (VIC): received_at(UTC), device_id, sensorlocation, latlong, noise

Caveats (printed): weighting undocumented (treat as ~LAeq), low-cost IoT (+/-2-3 dB),
total ambient (road+tram+people), rooftop sensors excluded for road validation.
"""
import csv
import math
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import duckdb
from property_scores.noise.score import noise_score

# City of Melbourne rooftop / non-street devices to exclude for road-noise validation
MELB_ROOFTOP = {"ICTMicroclimate-02", "ICTMicroclimate-03", "ICTMicroclimate-09"}


def lden(day, eve, night):
    return 10 * math.log10((12 * 10 ** (day / 10) + 4 * 10 ** ((eve + 5) / 10)
                            + 8 * 10 ** ((night + 10) / 10)) / 24)


def aggregate(csv_path, tz, t_col, dev_col, db_col, loc_col, loc_kind):
    """Return {device: {lat,lng,day,eve,night,n}} energy-mean per period."""
    con = duckdb.connect()
    con.execute("SET TimeZone='UTC'")
    if loc_kind == "latlong_str":  # "lat, lon"
        lat_expr = f"CAST(split_part({loc_col}, ',', 1) AS DOUBLE)"
        lng_expr = f"CAST(split_part({loc_col}, ',', 2) AS DOUBLE)"
    else:
        lat_expr = lng_expr = None
    rows = con.execute(f"""
        with t as (
          select {dev_col} dev, {db_col} db,
                 {lat_expr} lat, {lng_expr} lng,
                 extract(hour from ({t_col}::timestamptz at time zone '{tz}')) h
          from read_csv_auto('{csv_path}', ignore_errors=true)
          where {db_col} > 0 and {db_col} between 30 and 130
        )
        select dev, any_value(lat) lat, any_value(lng) lng,
          case when h>=7 and h<19 then 'day' when h>=19 and h<23 then 'eve' else 'night' end period,
          10*log10(avg(pow(10, db/10.0))) laeq, count(*) n
        from t group by dev, period
    """).fetchall()
    sens = {}
    for dev, lat, lng, period, laeq, n in rows:
        d = sens.setdefault(dev, {"lat": lat, "lng": lng, "n": 0})
        d[period] = laeq
        d["n"] += n
    return sens


def run(label, csv_path, tz, t_col, dev_col, db_col, loc_col, loc_kind, exclude=None):
    if not os.path.exists(csv_path) or os.path.getsize(csv_path) < 100:
        print(f"\n[{label}] CSV missing/empty ({csv_path}) - skip")
        return []
    exclude = exclude or set()
    sens = aggregate(csv_path, tz, t_col, dev_col, db_col, loc_col, loc_kind)
    print(f"\n=== {label} ({len(sens)} sensors, tz {tz}) ===")
    print(f"{'sensor':32} {'meas Lden':>9} {'model':>6} {'resid':>6}  coord")
    out = []
    for dev, d in sorted(sens.items()):
        if dev in exclude or any(p not in d for p in ("day", "eve", "night")):
            continue
        if d["lat"] is None or not (-44 < d["lat"] < -10):
            continue
        ml = lden(d["day"], d["eve"], d["night"])
        ns = noise_score(d["lat"], d["lng"])
        mod = ns.get("lden_db")
        if mod is None:
            continue
        res = mod - ml
        out.append({"dev": dev, "meas": ml, "model": mod, "res": res, "n": d["n"]})
        print(f"{dev[:32]:32} {ml:>9.1f} {mod:>6.1f} {res:>+6.1f}  ({d['lat']:.4f},{d['lng']:.4f}) n={d['n']}")
    if out:
        import statistics
        biases = [o["res"] for o in out]
        print(f"  --> {label}: n={len(out)}  mean residual {statistics.mean(biases):+.2f}  "
              f"MAE {statistics.mean(abs(b) for b in biases):.2f}")
    return out


def main():
    all_out = []
    all_out += run("Lake Macquarie EMS (NSW)", "/tmp/lakemac.csv", "Australia/Sydney",
                   "metadata_time", "device_name", "payload_fields_avg_soundpressure",
                   "location", "latlong_str")
    all_out += run("City of Melbourne (VIC)", "/tmp/melb_micro.csv", "Australia/Melbourne",
                   "received_at", "device_id", "noise", "latlong", "latlong_str",
                   exclude=MELB_ROOFTOP)
    if all_out:
        import statistics
        b = [o["res"] for o in all_out]
        print(f"\n=== COMBINED IoT clean validation: n={len(all_out)}  "
              f"mean residual {statistics.mean(b):+.2f}  MAE {statistics.mean(abs(x) for x in b):.2f} ===")
        print("(caveat: IoT sensors, weighting undocumented ~LAeq, total ambient, +/-2-3 dB)")


if __name__ == "__main__":
    main()
