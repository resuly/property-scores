"""Harvest MEASURED road-traffic-noise logger points from NSW road-project EIS/REF PDFs.

NSW has no open road-noise dataset, but major-road EIS/REF noise reports contain
unattended noise-logger measurements (Day LAeq,15hr / Night LAeq,9hr) at street
addresses. This extracts them into a CSV that can be geocoded (G-NAF) and used as
the first MEASURED NSW road-noise validation/calibration set.

Per-report layout varies, but the recurring shape is:
  - a logger->address table  (cell "NL12" | "23 President Avenue, Kogarah")
  - a measured "Ambient road traffic noise" table (Day LAeq,15hr / Night LAeq,9hr)
joined by logger id (NL\\d+, NM\\d+, P\\d+, L\\d+).

Usage: python scripts/eis_noise_harvest.py <report_label> <pdf_path> [...] > out.csv
"""
import re
import sys

import fitz  # pymupdf

LOGGER_RE = re.compile(r"^(NL|NM|NV|RM|P|L|R)\s?\d{1,3}[A-Za-z]?$", re.I)
STREET = (r"(Road|Street|Avenue|Highway|Crescent|Drive|Place|Lane|Parade|Way|Close|"
          r"Court|Boulevard|Terrace|Circuit|Grove|Esplanade|Rd|St|Ave|Hwy|Pde|Cres)")
ADDR_RE = re.compile(r"\d+[A-Za-z]?(?:[/-]\d+\w*)?\s+[A-Z][\w' ]*?\b" + STREET + r"\b", re.I)


def _clean(c):
    return (c or "").replace("\n", " ").strip()


def _dbnums(cells):
    """dB-range integers/floats (40-85) in row order."""
    out = []
    for c in cells:
        for tok in re.findall(r"\b(\d{2}(?:\.\d)?)\b", c):
            v = float(tok)
            if 38 <= v <= 86:
                out.append(v)
    return out


def harvest(pdf_path):
    doc = fitz.open(pdf_path)
    addr = {}          # logger_id -> full address cell
    measured = {}      # logger_id -> (day, night)
    for pg in range(doc.page_count):
        page = doc[pg]
        for tab in page.find_tables().tables:
            rows = [[_clean(c) for c in r] for r in tab.extract()]
            header = " ".join(" ".join(r) for r in rows[:5]).lower()
            # The authoritative measured table is the "ambient road traffic noise"
            # one with Day LAeq,15hr + Night LAeq,9hr. Exclude attended-survey
            # snapshot tables (Date/Time/Comments) which hold one-off spot readings.
            is_snapshot = ("date" in header and "time" in header)
            is_meas = ("road traffic noise" in header and "aeq" in header
                       and "15" in header and "9" in header and not is_snapshot)
            for r in rows:
                if not r:
                    continue
                lid = None
                for c in r[:2]:
                    cc = c.replace(" ", "")
                    if LOGGER_RE.match(cc):
                        lid = cc.upper()
                        break
                if not lid:
                    continue
                # address: use the FULL cell that contains a street address (keeps suburb)
                if lid not in addr:
                    for c in r:
                        if ADDR_RE.search(c):
                            addr[lid] = c.strip().rstrip(",")
                            break
                # measured day/night: first two dB-range numbers; require night<=day+1 (sanity)
                if is_meas and lid not in measured:
                    nums = _dbnums(r)
                    if len(nums) >= 2 and nums[1] <= nums[0] + 1:
                        measured[lid] = (nums[0], nums[1])
    rows_out = []
    for lid, (d, n) in measured.items():
        if lid in addr:
            rows_out.append((lid, addr[lid], d, n))
    return rows_out


# --- WA (Main Roads WA / Lloyd George Acoustics) format ------------------------
# WA road-project noise reports do NOT use NSW-style NL/P logger ids joined across
# two tables. Two recurring WA shapes:
#  (1) Self-contained measured table: col0 = "<n>. <address>, <suburb>", followed
#      by LA10,18hour | LAeq,24hour | LAeq(Day) | LAeq(Night) (Lloyd George style).
#      Header reads "Average Weekday Noise Level". Day/Night are the last two
#      Aeq columns. -> address + day + night all in one table, no join needed.
#  (2) "Site A/B/C/D" measured table ("Site A RBL" rows, LAeq,16-hr(day) /
#      LAeq,8-hr(night)) that must be joined to a setup table mapping
#      "Site A" -> "78 Holland Street, Fremantle" (High St Fremantle style).
WA_NUM_ADDR = re.compile(r"^\s*\d{1,2}\.\s*(.+\S)")           # "2. 199 Kelvin Road, Maddington"
WA_SITE = re.compile(r"^(Site\s+[A-Z])\b", re.I)


def _wa_nums(cells):
    out = []
    for c in cells:
        for tok in re.findall(r"\b(\d{2,3}(?:\.\d)?)\b", c or ""):
            v = float(tok)
            if 30 <= v <= 95:
                out.append(v)
    return out


def harvest_wa(pdf_path):
    """Return (key, address, day, night) for WA-format measured noise tables."""
    doc = fitz.open(pdf_path)
    site_addr = {}     # "Site A" -> address (setup table)
    rows_out = []
    seen = set()
    for pg in range(doc.page_count):
        for tab in doc[pg].find_tables().tables:
            rows = [[_clean(c) for c in r] for r in tab.extract()]
            flat = " ".join(" ".join(r) for r in rows).lower()
            # setup table: "Site A | 78 Holland Street, Fremantle ..."
            if "site id" in flat or ("logger set up" in flat and "easting" in flat):
                for r in rows:
                    joined = " ".join(c for c in r if c)
                    m = WA_SITE.match(joined)
                    if m and ADDR_RE.search(joined):
                        am = ADDR_RE.search(joined)
                        tail = joined[am.start():]
                        # WA setup cell = "<address> Noise logger located ...".
                        # Keep only the address: cut at the description / coords.
                        tail = re.split(r"\bNoise logger\b|\bRepresentative\b|\s\d{6}\b",
                                        tail, maxsplit=1)[0]
                        site_addr.setdefault(m.group(1).title(),
                                             tail.strip().rstrip(",").strip())
            # measured table type (2): "Site A RBL" rows with day/night dB
            is_site_meas = "aeq" in flat and ("(day)" in flat or "16-hr" in flat) and "site" in flat
            # measured table type (1): self-contained numbered-address rows
            is_num_meas = ("average weekday noise level" in flat
                           or ("aeq (day)" in flat and "aeq (night)" in flat))
            for r in rows:
                joined = " ".join(c for c in r if c).strip()
                # type (1): "<n>. <address>" + 3-4 dB numbers, last two = day/night
                m = WA_NUM_ADDR.match(r[0] if r else "")
                if is_num_meas and m and ADDR_RE.search(joined):
                    nums = _wa_nums(r[1:])
                    # columns: LA10,18 | LAeq,24 | LAeq(Day) | LAeq(Night)
                    # or       LA10,18 | LAeq(Day) | LAeq(Night)
                    if len(nums) >= 3:
                        day, night = nums[-2], nums[-1]
                        if night <= day + 1:
                            key = (m.group(1).rstrip("*").strip())
                            if key not in seen:
                                seen.add(key)
                                rows_out.append((key, key, day, night))
                # type (2): "Site A RBL ..." -> join address later
                sm = WA_SITE.match(joined)
                if is_site_meas and sm:
                    nums = _wa_nums(r)
                    # LA10,18 | LAeq,24 | LAeq,16(day) | LAeq,8(night)
                    if len(nums) >= 4 and nums[3] <= nums[2] + 1:
                        site = sm.group(1).title()
                        if site not in seen:
                            seen.add(site)
                            rows_out.append((site, None, nums[2], nums[3]))
    # resolve Site-X addresses from setup table
    resolved = []
    for key, addr, d, n in rows_out:
        if addr is None:
            addr = site_addr.get(key, key)
        resolved.append((key, addr, d, n))
    return resolved


# --- VIC (MRPV / VicRoads / DTP / LXRP) format --------------------------------
# VIC road-project EES/REF noise reports do NOT join two tables on a logger id.
# Two recurring VIC shapes, both self-contained (ref + address + levels in one row):
#  (1) North East Link / VicRoads style "existing noise levels" table:
#        Ref | Address | LA10(18hour) | LAeq(8 hour) | LA90 Day | LA90 Evening | LA90 Night
#      Ref = N01..N50 / NS01.. (residences / schools). Day metric = LA10(18hour)
#      (6am-midnight); Night metric = LAeq(8 hour) (10pm-6am). The two LA10/LAeq
#      columns are the FIRST two numeric columns after the address; the trailing
#      three columns are LA90 background (must NOT be read as day/night).
#      Bad rows hold text ("noise affected data" / "failed logger" / "Noise data
#      levels affected by external noise") instead of numbers -> skip.
#  (2) LXRP (level-crossing) style: a "Location | Monitoring Address" setup table
#      joined by an integer location no. to a "No. | Location | Day Leq 16 hr |
#      Night Leq 8 hr" measured table. Join key is the integer 1..N.
VIC_REF = re.compile(r"^(N|NS|NC|NV|L)\s?\d{1,3}[A-Z]?$", re.I)
VIC_BADTEXT = ("noise affected", "failed logger", "data levels affected",
               "affected by external", "no data", "invalid", "n/a")


def _vic_first_nums(cells):
    """Numbers in dB range, de-duping consecutive identical values (cell-merge
    artifacts duplicate the same number, e.g. ['63','63',...])."""
    out = []
    for c in cells:
        for tok in re.findall(r"\b(\d{2,3}(?:\.\d)?)\b", c or ""):
            v = float(tok)
            if 30 <= v <= 90:
                if out and out[-1] == v:
                    continue
                out.append(v)
    return out


def harvest_vic(pdf_path):
    """Return (ref, address, day, night) for VIC-format measured noise tables."""
    doc = fitz.open(pdf_path)
    rows_out = []
    seen = set()
    # type (2) join state (LXRP): location-no -> address, location-no -> (day,night)
    loc_addr, loc_meas = {}, {}
    for pg in range(doc.page_count):
        for tab in doc[pg].find_tables().tables:
            rows = [[_clean(c) for c in r] for r in tab.extract()]
            flat = " ".join(" ".join(r) for r in rows).lower()
            # ---- type (1): self-contained Ref/Address/LA10(18hour)/LAeq table ----
            is_t1 = ("la10" in flat and "address" in flat
                     and ("18hour" in flat.replace(" ", "") or "18 hour" in flat))
            if is_t1:
                for r in rows:
                    ne = [c for c in r if c]
                    if not ne:
                        continue
                    ref = None
                    for c in ne[:2]:
                        cc = c.replace(" ", "")
                        if VIC_REF.match(cc):
                            ref = cc.upper()
                            break
                    if not ref or ref in seen:
                        continue
                    joined = " ".join(ne).lower()
                    if any(b in joined for b in VIC_BADTEXT):
                        continue
                    # locate ref + address cells, then read dB numbers only from
                    # the cells AFTER the address (else a slash-address like
                    # "3/69 Sweyn Street" leaks "69" as a dB value).
                    ref_idx = next(i for i, c in enumerate(ne)
                                   if c.replace(" ", "").upper() == ref)
                    addr_idx = next((i for i, c in enumerate(ne)
                                     if i > ref_idx and ADDR_RE.search(c)), None)
                    if addr_idx is None:
                        # schools have no street number; address is cell after ref
                        addr_idx = ref_idx + 1 if ref_idx + 1 < len(ne) else None
                    address = ne[addr_idx] if addr_idx is not None else None
                    nums = _vic_first_nums(ne[addr_idx + 1:]) if addr_idx is not None else []
                    # first two dB numbers = LA10(18hr) day, LAeq(8hr) night
                    if address and len(nums) >= 2 and nums[1] <= nums[0] + 1:
                        seen.add(ref)
                        rows_out.append((ref, address.rstrip(", "), nums[0], nums[1]))
                continue
            # ---- type (2a): LXRP setup table (Location -> Monitoring Address) ----
            if "monitoring address" in flat or ("location" in flat and "address" in flat
                                                and "leq" not in flat):
                for r in rows:
                    ne = [c for c in r if c]
                    if len(ne) >= 2 and re.fullmatch(r"\d{1,2}", ne[0]):
                        am = next((c for c in ne[1:] if ADDR_RE.search(c) or "," in c), None)
                        if am:
                            loc_addr[int(ne[0])] = am.rstrip(", ")
            # ---- type (2b): LXRP measured table (No. | Loc | Day Leq | Night Leq)-
            if "leq" in flat and ("16 hr" in flat or "16hr" in flat) and (
                    "8 hr" in flat or "8hr" in flat):
                for r in rows:
                    ne = [c for c in r if c]
                    if len(ne) >= 2 and re.fullmatch(r"\d{1,2}", ne[0]):
                        nums = _vic_first_nums(ne[1:])
                        if len(nums) >= 2 and nums[1] <= nums[0] + 1:
                            loc_meas[int(ne[0])] = (nums[0], nums[1])
    # join LXRP type (2)
    for loc, (d, n) in loc_meas.items():
        addr = loc_addr.get(loc)
        if addr:
            rows_out.append((f"L{loc}", addr, d, n))
    return rows_out


def main():
    args = sys.argv[1:]
    wa = "--wa" in args
    vic = "--vic" in args
    args = [a for a in args if a not in ("--wa", "--vic")]
    print("report,logger,address,meas_day,meas_night")
    fn = harvest_wa if wa else harvest_vic if vic else harvest
    for i in range(0, len(args), 2):
        label, path = args[i], args[i + 1]
        for lid, address, d, n in fn(path):
            addr_csv = '"' + str(address).replace('"', "") + '"'
            print(f"{label},{lid},{addr_csv},{d},{n}", flush=True)


if __name__ == "__main__":
    main()
