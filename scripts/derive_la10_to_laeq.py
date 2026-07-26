#!/usr/bin/env python3
"""Measure the LA10(18h) -> LAeq offset instead of assuming it.

The measured gate converted LA10 rows to LAeq by subtracting a flat 3 dB, "the
same offset the old script used". That is a free-flowing-traffic rule of thumb,
and 71 of the 79 Victorian rows in the corpus depend on it, so if it is wrong it
manufactures a Victorian over-read that is not in the model.

Two Victorian reports already on disk publish LA10 AND LAeq measured by the same
logger at the same site, so the offset can be read off rather than assumed:

  * West Gate Tunnel EES Technical Report H appendix -- 34 "Noise Monitoring
    Data Sheets" giving L10,18hr and Leq,15hr (7-22h) side by side, with GPS,
    distance to road and facade flag. Freeway corridor, 188k vehicles/day.
  * Mordialloc Freeway appendix E -- per-logger daily tables giving LA10,18h and
    LAeq,16h (6-22h). Quiet suburban, set back.

The two periods are not identical (18h vs 15-16h), so this is an offset between
the quantities as each report publishes them, which is exactly the quantity the
gate needs: the corpus stores what the reports print.

    .venv/bin/python scripts/derive_la10_to_laeq.py
"""
from __future__ import annotations

import re
import statistics
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WGT = ROOT / "data/eis_noise/vic_wgt_techH_app.pdf"
MORD = ROOT / "data/eis_noise/vic_mordialloc_appE.pdf"


def page_text(pdf: Path, page: int) -> str:
    return subprocess.run(
        ["pdftotext", "-layout", "-f", str(page), "-l", str(page), str(pdf), "-"],
        capture_output=True, text=True, timeout=60).stdout


def wgt_pairs() -> list[tuple[str, float, float]]:
    """(site, L10_18h, LAeq_15h) from the monitoring data sheets."""
    out = []
    for p in range(40, 112):
        t = page_text(WGT, p)
        if "Noise Monitoring Data Sheet" not in t:
            continue
        site = re.search(r"Noise Monitoring Data Sheet\s+(.+?)\s*$", t, re.M)
        l10 = re.search(r"L10,18hr, Arith Av, 6-24h\s+(\d+)\s", t)
        leq = re.search(r"Leq,15hr, Log Av, 7-22h\s+(\d+)\s", t)
        if l10 and leq:
            out.append(((site.group(1).strip() if site else f"p{p}")[:34],
                        float(l10.group(1)), float(leq.group(1))))
    return out


def mordialloc_pairs() -> list[tuple[str, float, float]]:
    """(site, LA10_18h, LAeq_16h) per logging DAY, from the summary sheets.

    Daily rather than per-site: the sheets tabulate each day across the row, and
    a day is the unit at which both quantities are published together.
    """
    out = []
    for p in range(60, 130):
        t = page_text(MORD, p)
        if "Noise Logger Summary" not in t.replace(" ", " ") and \
           "No ise Lo g g er Su m m ar y" not in t:
            continue
        site = re.search(r"Logger Location\s+(.+?)\s*$", t, re.M)
        name = (site.group(1).strip() if site else f"p{p}")[:34]
        l10 = re.search(r"LA10,18h\s+([\d.\s]+)$", t, re.M)
        leq = re.search(r"LAeq,16h\s+([\d.\s]+)$", t, re.M)
        if not (l10 and leq):
            continue
        a = [float(x) for x in l10.group(1).split()]
        b = [float(x) for x in leq.group(1).split()]
        for i in range(min(len(a), len(b))):
            out.append((name, a[i], b[i]))
    return out


def report(label: str, pairs: list[tuple[str, float, float]]) -> list[float]:
    d = [l10 - leq for _, l10, leq in pairs]
    if not d:
        print(f"{label}: no pairs extracted")
        return d
    print(f"\n{label}  n={len(d)}")
    print(f"  mean   {statistics.mean(d):+.2f} dB")
    print(f"  median {statistics.median(d):+.2f} dB")
    if len(d) > 1:
        print(f"  sd     {statistics.stdev(d):.2f}")
    print(f"  range  {min(d):+.1f} .. {max(d):+.1f}")
    return d


def main() -> int:
    if subprocess.run(["which", "pdftotext"], capture_output=True).returncode:
        print("pdftotext not found (poppler)", file=sys.stderr)
        return 2
    w = report("West Gate Tunnel (freeway, facade, per site)", wgt_pairs())
    m = report("Mordialloc Freeway (suburban, per logging day)", mordialloc_pairs())
    pooled = w + m
    if not pooled:
        return 1
    print(f"\nPOOLED n={len(pooled)}  mean {statistics.mean(pooled):+.2f} dB  "
          f"median {statistics.median(pooled):+.2f} dB")
    print(f"\nThe gate currently subtracts 3.0 dB from every LA10 row.")
    print(f"Measured offset is {statistics.mean(pooled):+.2f}, so the corpus "
          f"understates those measurements by about "
          f"{3.0 - statistics.mean(pooled):.1f} dB, which shows up as model "
          f"over-read that is not in the model.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
