"""Validate the LIVE model's quiet-end against NorthConnex measured LAeq.

NorthConnex Operational Noise Assessment (Wilkinson Murray 13245-O Ver J),
Table 7-1 (logger->address) + Table 7-2 (measured LAeq Day/Eve/Night), Class-1
loggers, NW Sydney residential receivers (Wahroonga/Pennant Hills/Thornleigh =
Simon Kean's region). LAeq -> Lden is a clean conversion (unlike RBL/L90), so we
compare measured Lden directly to the model at each geocoded address.
"""
import json
import math
import urllib.parse
import urllib.request

# (address for geocoding, suburb, LAeq day, eve, night) from Table 7-1 + 7-2.
# Roadside arterial loggers (Pennant Hills Rd) flagged road=True for context.
ROWS = [
    ("45 Bareena Avenue Wahroonga NSW", 61, 60, 56, False),
    ("4 Douglas Avenue Wahroonga NSW", 60, 59, 57, False),
    ("118A Coonanbarra Road Wahroonga NSW", 57, 54, 52, False),
    ("18 Woniora Avenue Wahroonga NSW", 62, 58, 54, False),
    ("12 Trelawney Street Thornleigh NSW", 45, 43, 38, False),
    ("6 Trelawney Street Thornleigh NSW", 59, 56, 52, False),
    ("6 Duffy Avenue Thornleigh NSW", 60, 57, 55, False),
    ("1A Killaloe Avenue Pennant Hills NSW", 54, 51, 50, False),
    ("18 Wilson Road Pennant Hills NSW", 53, 50, 47, False),
    ("35 Coral Tree Drive Carlingford NSW", 55, 53, 50, False),
    ("223 Pennant Hills Road Carlingford NSW", 57, 57, 51, True),
    ("440 Pennant Hills Road Pennant Hills NSW", 66, 65, 64, True),
    ("606 Pennant Hills Road Beecroft NSW", 52, 51, 44, True),
]


def meas_lden(day, eve, night):
    return 10 * math.log10((12 * 10 ** (day / 10) + 4 * 10 ** ((eve + 5) / 10)
                            + 8 * 10 ** ((night + 10) / 10)) / 24)


def geocode(addr):
    url = "https://daleads.com.au/api/address-autocomplete?q=" + urllib.parse.quote(addr)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        d = json.load(urllib.request.urlopen(req, timeout=20))
        items = d if isinstance(d, list) else d.get("results") or d.get("suggestions") or d.get("data") or []
        if not items:
            return None, None
        it = items[0]
        lat = it.get("lat") or it.get("latitude") or (it.get("center") or [None, None])[1]
        lng = it.get("lng") or it.get("lon") or it.get("longitude") or (it.get("center") or [None, None])[0]
        return (float(lat), float(lng)) if lat and lng else (None, None)
    except Exception as e:
        return None, None


def model_lden(lat, lng):
    url = f"https://daleads.com.au/api/property-scores/scores/noise?lat={lat}&lng={lng}&detail=true"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        d = json.load(urllib.request.urlopen(req, timeout=30))
        return d.get("lden_db"), d.get("dominant_source"), d.get("low_confidence")
    except Exception:
        return None, None, None


def main():
    print(f"{'address':40} {'meas':>5} {'model':>6} {'resid':>6} {'dom':>14}")
    res_side, res_road = [], []
    for addr, dy, ev, ni, road in ROWS:
        ml = meas_lden(dy, ev, ni)
        lat, lng = geocode(addr)
        if lat is None:
            print(f"{addr[:40]:40} {ml:>5.1f}   geocode-fail")
            continue
        mod, dom, lc = model_lden(lat, lng)
        if mod is None:
            print(f"{addr[:40]:40} {ml:>5.1f}   model-fail ({lat:.4f},{lng:.4f})")
            continue
        r = mod - ml
        tag = " [road]" if road else ""
        (res_road if road else res_side).append(r)
        print(f"{addr[:40]:40} {ml:>5.1f} {mod:>6.1f} {r:>+6.1f} {str(dom)[:14]:>14}{tag}")
    import statistics
    if res_side:
        print(f"\nRESIDENTIAL side-street (n={len(res_side)}): mean resid {statistics.mean(res_side):+.2f}  MAE {statistics.mean(abs(x) for x in res_side):.2f}")
    if res_road:
        print(f"Pennant Hills Rd arterial (n={len(res_road)}): mean resid {statistics.mean(res_road):+.2f}  MAE {statistics.mean(abs(x) for x in res_road):.2f}")


if __name__ == "__main__":
    main()
