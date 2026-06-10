#!/usr/bin/env python3
"""Daily truth-anchor sentinel: probe scores against known ground truth.

The 2026-06-11 audit found whole states silently dead (SA ArcGIS deleted
upstream, TAS wired to one council's layer) because nothing ever asserted a
KNOWN POSITIVE. This runner closes that class:

1. Endpoint canaries (canaries.json): known-positive official-endpoint checks
   (a Stirling SA point must hit a bushfire zone, the TAS statewide layer must
   return polygons, fire history max year must reach Black Summer...). Each
   carries active:true/false so not-yet-fixed gaps don't alarm daily.
2. Truth anchors (data/truth_anchors/*.csv, lat,lng,expected,why): runs the
   live score API per domain and evaluates the machine-parseable expectations
   (score<N, label X, foo<=Nm, *_mapped / official_* flags). Unparseable rows
   are listed as MANUAL, never silently dropped.

Only NEW failures (vs the state file) alert, so a long-known gap doesn't spam
while a regression pings Telegram the day it lands.

Usage:
  python scripts/score_truth_probes.py                  # full run, alert on new failures
  python scripts/score_truth_probes.py --base http://127.0.0.1:8099
  python scripts/score_truth_probes.py --domain bushfire --no-alert
Cron: daily on Oracle (see limon-ops/bin/install_daleads_cron.sh).
"""
import argparse
import csv
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ANCHOR_DIR = ROOT / "data" / "truth_anchors"
CANARIES = ROOT / "data" / "truth_anchors" / "canaries.json"
STATE_FILE = Path(os.environ.get("TRUTH_PROBE_STATE",
                                 str(Path.home() / ".score_truth_probes_state.json")))

DOMAIN_ENDPOINT = {
    "walkability": "walkability", "flood": "flood", "bushfire": "bushfire",
    "heat_island": "heat-island", "view_quality": "view-quality",
    "solar": "solar", "contamination": "contamination",
}
SLEEP_S = float(os.environ.get("TRUTH_PROBE_SLEEP", "2.5"))


def _get(url: str, timeout: int = 120, headers: dict | None = None) -> dict | None:
    try:
        h = {"User-Agent": "truth-probe"}
        if headers:
            h.update(headers)
        req = urllib.request.Request(url, headers=h)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except Exception:
        return None


def _flatten(d: dict) -> dict:
    """The noise endpoint nests the payload under 'score' when detail mode."""
    if isinstance(d.get("score"), dict):
        return d["score"]
    return d


def evaluate(expected: str, payload: dict) -> tuple[str, str]:
    """Evaluate one expectation against a score payload.

    Returns (status, note): status PASS / FAIL / MANUAL.
    Parseable forms (first match wins):
      score<N, score<=N, score>=N, score>N
      label <text>  (substring, case-insensitive, also matches 'label X/Y')
      <category><=Nm  e.g. pool<=200m (walkability category distance)
      official_*_prone / *_mapped  -> zones/hits list must be non-empty
      'must NOT render/show' free text -> MANUAL (UI assertions)
    """
    e = expected.strip()
    score = payload.get("score")

    m = re.match(r"score\s*(<=|<|>=|>)\s*(\d+)", e)
    if m and isinstance(score, (int, float)):
        op, n = m.group(1), int(m.group(2))
        ok = {"<": score < n, "<=": score <= n, ">": score > n, ">=": score >= n}[op]
        return ("PASS" if ok else "FAIL", f"score={score} expected {op}{n}")

    m = re.match(r"([a-z_]+)\s*<=\s*(\d+)\s*m", e)
    if m:
        cat, dist = m.group(1), int(m.group(2))
        cs = (payload.get("category_scores") or {}).get(cat) or {}
        got = cs.get("distance_m")
        if got is None:
            return ("FAIL", f"{cat} not found (expected <= {dist}m)")
        return ("PASS" if got <= dist else "FAIL", f"{cat}={got}m expected <={dist}m")

    if re.match(r"official_.*prone|.*_mapped$", e):
        zones = (payload.get("bushfire_zones") or payload.get("flood_zones")
                 or payload.get("zones") or [])
        return ("PASS" if zones else "FAIL", f"zones={zones or '[]'}")

    if e.startswith("flag_high_risk"):
        if isinstance(score, (int, float)):
            return ("PASS" if score <= 40 else "FAIL",
                    f"score={score} expected <=40 (high risk site)")
        return ("MANUAL", "no score in payload")

    m = re.search(r"label\s+(?:>=\s*)?([A-Za-z][A-Za-z /]+)", e)
    if m and payload.get("label"):
        want = [w.strip().lower() for w in m.group(1).split("/")]
        got = str(payload["label"]).lower()
        ok = any(w in got for w in want)
        return ("PASS" if ok else "FAIL", f"label='{payload['label']}' expected ~{want}")

    return ("MANUAL", e[:70])


def run_anchors(base: str, only_domain: str | None) -> list[dict]:
    results = []
    for f in sorted(ANCHOR_DIR.glob("*_anchors.csv")):
        domain = f.stem.replace("_anchors", "")
        if only_domain and domain != only_domain:
            continue
        ep = DOMAIN_ENDPOINT.get(domain)
        if ep is None:
            continue  # ui_claims / paid_reports / suburb_data need their own surfaces
        for row in csv.DictReader(open(f)):
            url = f"{base}/scores/{ep}?lat={row['lat']}&lng={row['lng']}"
            payload = _get(url)
            time.sleep(SLEEP_S)
            if payload is None:
                results.append({"domain": domain, "key": f"{row['lat']},{row['lng']}",
                                "status": "FAIL", "note": "endpoint unreachable",
                                "expected": row["expected"][:60]})
                continue
            status, note = evaluate(row["expected"], _flatten(payload))
            results.append({"domain": domain, "key": f"{row['lat']},{row['lng']}",
                            "status": status, "note": note,
                            "expected": row["expected"][:60]})
    return results


def run_canaries() -> list[dict]:
    if not CANARIES.exists():
        return []
    out = []
    for c in json.loads(CANARIES.read_text()):
        if not c.get("active", True):
            continue
        url = c["url"]
        data = _get(url, timeout=30, headers=c.get("headers"))
        time.sleep(1.0)
        ok, note = False, "no response"
        if data is not None:
            if "error" in data:
                note = f"error body: {str(data.get('error'))[:60]}"
            else:
                expr = c.get("assert", "count>0")
                count = data.get("count")
                if count is None and isinstance(data.get("features"), list):
                    count = len(data["features"])
                m = re.match(r"count\s*(>=|>)\s*(\d+)", expr)
                if m and count is not None:
                    ok = count > int(m.group(2)) if m.group(1) == ">" else count >= int(m.group(2))
                    note = f"count={count} expected {expr}"
                elif expr.startswith("max:"):
                    field, minv = expr[4:].split(">=")
                    vals = [ft["attributes"].get(field.strip())
                            for ft in data.get("features", [])]
                    vals = [v for v in vals if isinstance(v, (int, float))]
                    ok = bool(vals) and max(vals) >= int(minv)
                    note = f"max({field.strip()})={max(vals) if vals else None} expected >={minv}"
                else:
                    ok, note = True, "responded"
        out.append({"domain": "canary", "key": c["name"],
                    "status": "PASS" if ok else "FAIL", "note": note,
                    "expected": c.get("assert", "")})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=os.environ.get("SCORES_BASE", "http://127.0.0.1:8099"))
    ap.add_argument("--domain", default=None)
    ap.add_argument("--no-alert", action="store_true")
    args = ap.parse_args()

    results = run_canaries() + run_anchors(args.base, args.domain)

    fails = [r for r in results if r["status"] == "FAIL"]
    manual = [r for r in results if r["status"] == "MANUAL"]
    passes = [r for r in results if r["status"] == "PASS"]
    for r in results:
        print(f"[{r['status']:6}] {r['domain']:14} {r['key']:28} {r['note']}")
    print(f"\n{len(passes)} pass, {len(fails)} fail, {len(manual)} manual "
          f"of {len(results)}")

    prev = set()
    if STATE_FILE.exists():
        try:
            prev = set(json.loads(STATE_FILE.read_text()).get("failing", []))
        except Exception:
            pass
    now_failing = {f"{r['domain']}|{r['key']}" for r in fails}
    new_failures = now_failing - prev
    recovered = prev - now_failing
    STATE_FILE.write_text(json.dumps({"failing": sorted(now_failing),
                                      "ts": time.time()}))

    if new_failures and not args.no_alert:
        lines = [r for r in fails if f"{r['domain']}|{r['key']}" in new_failures]
        body = "\n".join(f"• {r['domain']} {r['key']}: {r['note']}" for r in lines[:12])
        try:
            sys.path.insert(0, str(ROOT / "scripts"))
            from alert_telegram import send_alert
            send_alert(project="da-leads", level="error",
                       title=f"真值哨兵: {len(new_failures)} 项新失败",
                       message=body)
        except Exception as e:
            print(f"(alert failed: {e})", file=sys.stderr)
    if recovered:
        print(f"recovered since last run: {len(recovered)}")

    sys.exit(1 if new_failures else 0)


if __name__ == "__main__":
    main()
