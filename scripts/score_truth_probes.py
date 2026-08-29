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
while a regression pings Telegram the day it lands. The cost of that, found
2026-08-21: a failure that never recovers also never speaks again. The QLD
bushfire canary broke 2026-07-23, alerted once, and sat red for 30 runs
without a word -- a whole state's upstream was gone for a month and the only
reason anyone noticed was an unrelated dig through the log. So a still-red
check is re-reported every STALE_RED_DAYS (7), which is loud enough to
surface a month-long outage and quiet enough that the dozen known model gaps
cost one line each per week.

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
# How long a check may stay red before it is reported again. Days, not runs,
# so a few missed crons cannot silence it.
STALE_RED_DAYS = float(os.environ.get("TRUTH_PROBE_STALE_RED_DAYS", "7"))
# Where the cron wrapper puts this run's output, quoted in the truncation
# notice. Absolute on purpose: cron does `cd /var/www/property-scores`, which
# HAS a logs/ directory that does not contain this file, so a relative path
# sends the reader somewhere real and empty -- reading as "the log is gone".
LOG_PATH = os.environ.get("TRUTH_PROBE_LOG_PATH",
                          "/var/www/daleads.com.au/logs/truth_probes.log")


def _write_state(failing, since, reminded, ts):
    """Atomic, because this file now carries the staleness clocks too.

    A torn write used to cost one day of "new failure" noise; it would now
    also reset every since/reminded, i.e. re-arm the month-long silence this
    whole mechanism exists to prevent. The read path swallows every
    exception, so a truncated file fails silently rather than loudly.
    """
    # pid in the name: cron_with_alert.sh takes no flock, so two overlapping
    # runs would otherwise write the same tmp and one could rename a file the
    # other was mid-write -- and the read path swallows the resulting garbage,
    # zeroing every clock silently.
    tmp = STATE_FILE.with_suffix(f"{STATE_FILE.suffix}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps({"failing": sorted(failing), "since": since,
                               "reminded": reminded, "ts": ts}))
    os.replace(tmp, STATE_FILE)


def _get(url: str, timeout: int = 120, headers: dict | None = None,
         attempts: int = 2, retry_wait: float = 5.0) -> dict | None:
    """One retry before calling an endpoint unreachable.

    A single timeout is not evidence of a dead endpoint and it costs a false
    "new failure", which is the one thing this sentinel alerts on. Measured
    2026-08-21: "endpoint unreachable" has fired on 11 separate days since
    2026-06-13. The anchor behind it that morning (bushfire
    -37.7416,145.2269) and the two Brisbane flood points that hit it the day
    before all answered in under 0.2s when re-probed by hand the same day. A
    genuinely dead endpoint fails both attempts and still reports.
    """
    h = {"User-Agent": "truth-probe"}
    if headers:
        h.update(headers)
    for attempt in range(1, attempts + 1):
        try:
            req = urllib.request.Request(url, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except Exception:
            if attempt < attempts:
                time.sleep(retry_wait)
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
      <field>=<value>  e.g. epa_status=not_integrated
      label <text>  (substring, case-insensitive, also matches 'label X/Y')
      <category><=Nm  e.g. pool<=200m (walkability category distance)
      official_*_prone / *_mapped  -> zones/hits list must be non-empty
      'must NOT render/show' free text -> MANUAL (UI assertions)
    """
    e = expected.strip()
    score = payload.get("score")

    # Contract/status anchors are for deliberate coverage gates. They remain
    # live canaries: when a pending source is integrated and the status moves,
    # the old expectation turns red instead of being silently deleted.
    m = re.fullmatch(r"([a-z_][a-z0-9_]*)\s*=\s*([A-Za-z0-9_.-]+)", e, re.I)
    if m:
        field, want = m.group(1), m.group(2)
        got = payload.get(field)
        # Walkability contract fields live under category_scores, while
        # status fields such as epa_status are top-level.  Resolve both rather
        # than treating `supermarket_barrier=false` as a missing top-level key.
        if field not in payload:
            for suffix in ("water_barrier", "distance_m", "barrier", "count", "decay"):
                marker = "_" + suffix
                if field.endswith(marker):
                    category = field[:-len(marker)]
                    got = ((payload.get("category_scores") or {}).get(category) or {}).get(suffix)
                    break
        if want.lower() in ("true", "false"):
            ok = isinstance(got, bool) and got is (want.lower() == "true")
        else:
            ok = str(got).lower() == want.lower()
        return ("PASS" if ok else "FAIL",
                f"{field}={got!r} expected {want!r}")

    m = re.match(r"score\s*(<=|<|>=|>)\s*(\d+)", e)
    if m:
        op, n = m.group(1), int(m.group(2))
        if not isinstance(score, (int, float)):
            # an anchor that asks about the score and gets no score is a dead
            # signal, not a MANUAL: MANUAL never reaches new_failures
            return ("FAIL", f"no score in payload (expected {op}{n}, "
                            f"label={payload.get('label')!r})")
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
        # A known-positive anchor that comes back without a score is a dead
        # signal, not a case for a human to look at later: MANUAL never reaches
        # new_failures, so this is exactly the silent-death this file exists to
        # catch. Contamination can now legitimately return score None when both
        # its signal layers are down (2026-08-10 fail-closed change), which made
        # that gap reachable in production.
        return ("FAIL", f"no score in payload (label={payload.get('label')!r})")

    m = re.search(r"label\s+(?:>=\s*)?([A-Za-z][A-Za-z /]+)", e)
    if m and payload.get("label"):
        want = [w.strip().lower() for w in m.group(1).split("/")]
        got = str(payload["label"]).lower()
        ok = any(w in got for w in want)
        return ("PASS" if ok else "FAIL", f"label='{payload['label']}' expected ~{want}")

    return ("MANUAL", e[:70])


def evaluate_margin(payload: dict, reference: dict, min_margin: float) -> tuple[str, str]:
    """Require this score to stay at least ``min_margin`` above a control."""
    score = payload.get("score")
    ref_score = reference.get("score")
    if not isinstance(score, (int, float)) or not isinstance(ref_score, (int, float)):
        return "FAIL", f"comparison unavailable: score={score!r}, reference={ref_score!r}"
    actual = score - ref_score
    return ("PASS" if actual >= min_margin else "FAIL",
            f"score={score} reference={ref_score} margin={actual} expected >={min_margin:g}")


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
            result_key = row.get("id") or f"{row['lat']},{row['lng']}"
            url = f"{base}/scores/{ep}?lat={row['lat']}&lng={row['lng']}"
            payload = _get(url)
            time.sleep(SLEEP_S)
            if payload is None:
                results.append({"domain": domain, "key": result_key,
                                "status": "FAIL", "note": "endpoint unreachable",
                                "expected": row["expected"][:60],
                                "reminder_days": row.get("reminder_days"),
                                "blocker": row.get("blocker")})
                continue
            status, note = evaluate(row["expected"], _flatten(payload))
            if status == "PASS" and row.get("ref_lat") and row.get("ref_lng"):
                ref_url = (f"{base}/scores/{ep}?lat={row['ref_lat']}"
                           f"&lng={row['ref_lng']}")
                ref_payload = _get(ref_url)
                time.sleep(SLEEP_S)
                if ref_payload is None:
                    status, margin_note = "FAIL", "comparison endpoint unreachable"
                else:
                    try:
                        min_margin = float(row.get("min_margin") or 0)
                    except ValueError:
                        status, margin_note = "FAIL", "invalid comparison min_margin"
                    else:
                        status, margin_note = evaluate_margin(
                            _flatten(payload), _flatten(ref_payload), min_margin)
                note = f"{note}; {margin_note}"
            results.append({"domain": domain, "key": result_key,
                            "status": status, "note": note,
                            "expected": row["expected"][:60],
                            "reminder_days": row.get("reminder_days"),
                            "blocker": row.get("blocker")})
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


def reminder_due_seconds(result: dict) -> float:
    """Per-anchor reminder cadence; known external blockers may be quieter."""
    try:
        days = float(result.get("reminder_days") or STALE_RED_DAYS)
    except (TypeError, ValueError):
        days = float(STALE_RED_DAYS)
    return max(days, 1.0) * 86400 - 1800


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

    prev, since, reminded = set(), {}, {}
    if STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text())
            prev = set(state.get("failing", []))
            since = dict(state.get("since", {}))
            reminded = dict(state.get("reminded", {}))
        except Exception:
            pass
    now = time.time()
    now_failing = {f"{r['domain']}|{r['key']}" for r in fails}
    # A --domain run probes one domain, so everything else is simply unknown
    # this run, not recovered. Overwriting the whole file with that partial
    # view already invented "new" failures on the next full run; now it would
    # also reset their staleness clocks to zero, which is exactly the silence
    # this reminder exists to break. Scoped runs keep the other domains' rows.
    # Note untouched keys stay OUT of now_failing: they were not probed, so
    # they must not be judged stale or reported on this run, only carried.
    # run_canaries ignores --domain and always runs, so canary rows are
    # measured on every run and are never untouched -- carrying them would
    # keep a canary that just recovered marked as failing forever.
    untouched = ({k for k in prev
                  if not k.startswith(f"{args.domain}|")
                  and not k.startswith("canary|")}
                 if args.domain else set())
    new_failures = now_failing - prev
    recovered = prev - now_failing - untouched

    # A key already red when this bookkeeping shipped has no true start date.
    # Stamping it now understates its age -- the alternative is inventing one,
    # and a week's delay on the first reminder is the cheaper error.
    keep = now_failing | untouched
    since = {k: since.get(k, now) for k in keep}
    reminded = {k: v for k, v in reminded.items() if k in keep}
    fail_by_key = {f"{r['domain']}|{r['key']}": r for r in fails}
    stale = sorted(
        k for k in now_failing - new_failures
        if now - since[k] >= reminder_due_seconds(fail_by_key[k])
        and now - reminded.get(k, since[k]) >= reminder_due_seconds(fail_by_key[k]))

    def _send(title, level, keys):
        """True only if Telegram actually took the message.

        send_alert RETURNS False on a delivery failure and raises nothing, so
        a try/except here would only ever catch an ImportError. Ignoring the
        return value is how a 502 or an expired token turns into a week of
        silence -- the very failure this reminder exists to end.
        """
        by_key = fail_by_key
        shown = [k for k in keys if k in by_key]
        lines = [
            f"• {by_key[k]['domain']} {by_key[k]['key']}: {by_key[k]['note']}"
            + (f"；阻塞: {by_key[k]['blocker']}" if by_key[k].get("blocker") else "")
            + (f" (已红 {int((now - since[k]) / 86400)} 天)" if k in stale else "")
            for k in shown[:12]]
        # Never let the cap read as "that was all of them": there are already
        # 13 persistently failing checks, so the stale digest hits this on its
        # very first send.
        if len(shown) > 12:
            lines.append(f"…还有 {len(shown) - 12} 项未列出(消息长度上限), "
                         f"完整清单见 {LOG_PATH}")
        try:
            sys.path.insert(0, str(ROOT / "scripts"))
            from alert_telegram import send_alert
            return bool(send_alert(project="da-leads", level=level,
                                   title=title, message="\n".join(lines)))
        except Exception as e:
            print(f"(alert failed: {e})", file=sys.stderr)
            return False

    # sorted, because `keys` is a set and the [:12] cap would otherwise show a
    # hash-ordered slice: a lost state file makes all 13 "new" at once, and
    # which one gets dropped would change with PYTHONHASHSEED.
    delivered_new = bool(new_failures) and not args.no_alert and _send(
        f"真值哨兵: {len(new_failures)} 项新失败", "error", sorted(new_failures))
    # warn, not error: nothing changed today, this is the reminder that
    # something has been broken for a week or more and nobody acted.
    # A dry run spends nothing either -- --no-alert sends no message, so
    # stamping these would buy another week of silence for a failure nobody
    # was told about.
    delivered_stale = bool(stale) and not args.no_alert and _send(
        f"真值哨兵: {len(stale)} 项持续失败未处理", "warn", stale)

    # --no-alert is a debugging switch and reads only. It defaults to the same
    # ~/.score_truth_probes_state.json the cron writes, and an operator SSHes
    # in as the same `ubuntu` user cron runs as, so the `--domain X --no-alert`
    # example in this file's own header used to mutate production state: one
    # debug run would silently downgrade a real regression from today's error
    # alert to a warn digest a week later. Nothing is gained by writing --
    # nobody was told anything this run.
    if args.no_alert:
        print("(--no-alert: state left untouched)", file=sys.stderr)
    else:
        # An undelivered NEW failure is held out of the state entirely, so the
        # next run sees it as new again and retries at error level. Waiting for
        # the 7-day digest would leave the most urgent path on the weakest
        # guarantee -- the same mistake the stale reminder exists to fix.
        persist = now_failing | untouched
        if new_failures and not delivered_new:
            persist -= new_failures
        _write_state(persist, {k: v for k, v in since.items() if k in persist},
                     {**reminded,
                      **({k: now for k in stale} if delivered_stale else {})},
                     now)

    for k in stale:
        print(f"still failing after {int((now - since[k]) / 86400)}d: {k}"
              + ("" if delivered_stale else " (提醒未送达, 下次运行重试)"))
    if recovered:
        print(f"recovered since last run: {len(recovered)}")

    sys.exit(1 if new_failures else 0)


if __name__ == "__main__":
    main()
