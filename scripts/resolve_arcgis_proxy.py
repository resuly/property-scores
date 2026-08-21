#!/usr/bin/env python3
"""Find the live replacement for a rotated ArcGIS proxy item.

The QLD bushfire layer went dead on 2026-07-23 with
`{"error": {"code": 500, "details": ["Error generating token"]}}` served over
HTTP 200. It was not withdrawn: the publisher had rebuilt the item, and the
same service was live under a new id two weeks later. Finding that cost an
afternoon and one wrong conclusion written into a code comment ("no public
replacement exists"), because the obvious searches -- the publisher's own
host, the state open-data portal -- do not surface it. One ArcGIS Online
item search scoped to the owner does.

This tool has exactly two ways to be harmful, and both are worse than the
outage it diagnoses:

  A. Saying "no replacement exists" when one does. That sentence is what
     went into the code comment last time. So every path that can produce
     it must first prove it actually looked everywhere -- hence paging, and
     hence the refusal to filter by title (a rebuild may be renamed).
  B. Recommending the wrong layer. Both the dead and live QLD items are
     still listed, same owner, same title, so names prove nothing; a
     publisher can leave a broken NEWER item up, so recency proves nothing;
     a rebuilt-but-empty layer answers cleanly and would score every QLD
     property "not bushfire prone", which is silently wrong rather than
     visibly broken.

So liveness is decided by probing, an unreachable probe is "unknown" and
never "dead", and an empty layer is reported but never offered. It is
read-only and never rewrites config: a proxy id is a load-bearing constant
and swapping it stays a reviewed code change -- both the ACT and QLD
rotations needed a human to compare coverage and fields first.

Usage:
  python scripts/resolve_arcgis_proxy.py <dead-item-id-or-proxy-url>
  python scripts/resolve_arcgis_proxy.py 8ac1ba8eccee472fbd0e7a57bf3ad320
Exit: 0 resolved or still-live, 1 nothing offered, 2 could not tell.
"""
from __future__ import annotations

import json
import re
import sys
import urllib.parse
import urllib.request

AGO = "https://www.arcgis.com/sharing/rest"
TIMEOUT = 20
PAGE = 100                      # AGO's per-request maximum
MAX_PAGES = 100                 # AGO itself refuses start+num > 10000, so
                                # this only has to not be tighter than that
UA = {"User-Agent": "limon-ops-proxy-resolver"}

# Hosts that answer only with a Referer. Without this an SA layer reads as
# dead: measured 2026-08-21, the SAPPA MapServer returns HTTP 403 bare and
# {"count": 378} with the header. Mirrors SA_HEADERS in bushfire/score.py.
HOST_HEADERS = {"geohub.sa.gov.au": {"Referer": "https://sappa.plan.sa.gov.au/"}}

# 32 hex, not part of a longer hex run: a 40-char sha or a 36-char id earlier
# in the string would otherwise win and be silently truncated to 32.
_ID_RE = re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{32}(?![0-9a-fA-F])")

LIVE, DEAD, UNKNOWN = "live", "dead", "unknown"


def _get(url: str, headers: dict | None = None, timeout: int = TIMEOUT,
         attempts: int = 2) -> dict | list | None:
    """None means "could not tell", never "does not exist"."""
    h = {**UA, **(headers or {})}
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except Exception:
            if i == attempts - 1:
                return None
    return None


def _headers_for(url: str) -> dict | None:
    # netloc, not a substring test: "https://evil/geohub.sa.gov.au/" would
    # otherwise be handed the Referer.
    netloc = urllib.parse.urlsplit(url).netloc.lower()
    for host, hdrs in HOST_HEADERS.items():
        if netloc == host or netloc.endswith("." + host):
            return hdrs
    return None


def item_id_from(target: str) -> str | None:
    """Prefer the id in a /servers/<id>/ or /items/<id> segment."""
    for marker in ("/servers/", "/items/"):
        if marker in target:
            seg = target.split(marker, 1)[1].split("/", 1)[0].split("?", 1)[0]
            if _ID_RE.fullmatch(seg):
                return seg.lower()
    m = _ID_RE.search(target)
    return m.group(0).lower() if m else None


def describe(item_id: str) -> dict | None:
    d = _get(f"{AGO}/content/items/{item_id}?f=json")
    return d if isinstance(d, dict) else None


def _layer_query_url(service_url: str) -> tuple[str | None, bool]:
    """(url, guessed). `guessed` marks a /0 we picked without evidence.

    Never blindly appends /0: measured on this owner, 5 of 49 Feature
    Service items already carry a layer index (".../MapServer/6"), and
    ".../MapServer/6/0/query" answers `error 400 Invalid or missing input
    parameters` -- indistinguishable from a dead layer if we did not check.
    """
    base = service_url.rstrip("/")
    if re.search(r"/\d+$", base):
        return base, False
    meta = _get(f"{base}?f=json", headers=_headers_for(base))
    if isinstance(meta, dict) and "error" in meta:
        # The service itself is answering with an error, which is the very
        # symptom this tool diagnoses -- the dead QLD proxy returns
        # "Error generating token" on its root too. The second element says
        # "the /0 here is a GUESS", so probe can tell a real death from our
        # having picked the wrong layer number. Hand back the /0 form so
        # probe() reads that error and rules DEAD. Returning None here (an
        # earlier over-correction) made the tool report "cannot tell" for
        # the exact incident it exists for.
        return f"{base}/0", True
    if not isinstance(meta, dict):
        return None, False                # unreachable: cannot tell
    if meta.get("type") in ("Feature Layer", "Table"):
        return base, False                # the url already IS a layer
    layers = meta.get("layers")
    if not isinstance(layers, list) or not layers:
        return None, False                # e.g. a GeocodeServer: no layers
    # NOT layers[0]. The SAPPA MapServer's first layer is a Group Layer
    # ("Survey Marks"), and querying a group answers `error 400 Invalid or
    # missing input parameters` -- which would read as a dead service on the
    # very host HOST_HEADERS exists for. Take the first real, queryable one.
    for lyr in layers:
        if not isinstance(lyr, dict) or lyr.get("id") is None:
            continue
        if lyr.get("subLayerIds"):
            continue                      # a group, not queryable
        if lyr.get("type") in ("Group Layer", "Raster Layer"):
            continue
        return f"{base}/{lyr['id']}", False
    return None, False


def probe(service_url: str) -> tuple[str, str, int | None]:
    """(verdict, detail, feature_count). verdict is live / dead / unknown."""
    q_base, guessed = _layer_query_url(service_url)
    if q_base is None:
        return UNKNOWN, "no queryable layer could be identified", None
    data = _get(f"{q_base}/query?where=1%3D1&returnCountOnly=true&f=json",
                headers=_headers_for(q_base))
    if data is None:
        return UNKNOWN, "unreachable (network, WAF or timeout)", None
    if not isinstance(data, dict):
        return UNKNOWN, f"unexpected body type {type(data).__name__}", None
    if "error" in data:
        err = data["error"] if isinstance(data["error"], dict) else {}
        detail = "; ".join(err.get("details") or []) or err.get("message", "")
        if guessed and err.get("code") == 400:
            # We invented the /0. A 400 "Invalid or missing input parameters"
            # is evidence we guessed the wrong layer number, not that the
            # service is dead -- which is the very confusion the non-guessing
            # paths exist to avoid.
            return UNKNOWN, "guessed layer 0 and it rejected the query", None
        return DEAD, f"error {err.get('code')}: {detail}"[:90], None
    count = data.get("count")
    if not isinstance(count, int):
        # A real layer ALWAYS answers returnCountOnly with an int. Anything
        # else is some other kind of service echoing its own document at us:
        # a GeocodeServer ignores the appended /0/query entirely and returns
        # its metadata, which has no "error" key, so treating "not an error"
        # as live once offered "Queensland Locator View" as a replacement for
        # a bushfire layer.
        return UNKNOWN, "no integer count (not a queryable layer)", None
    return LIVE, f"count={count}", count


def candidates(owner: str) -> tuple[list[dict], bool]:
    """(items, complete). Every type, paged.

    Not filtered by title, because a rebuild may be renamed and an empty
    result reads as "no replacement exists" -- failure mode A. Not filtered
    by type either, for the same reason: this owner publishes 174 items
    across 14 types, and a layer republished as a Map Service instead of a
    Feature Service would vanish from a type-filtered search.
    """
    if not owner:
        return [], False
    out, start, total = [], 1, None
    for _ in range(MAX_PAGES):
        q = urllib.parse.quote(f"owner:{owner}")
        page = _get(f"{AGO}/search?q={q}&f=json&num={PAGE}&start={start}"
                    f"&sortField=created&sortOrder=desc")
        if not isinstance(page, dict) or "results" not in page:
            return out, False              # partial: never claim completeness
        out.extend(page["results"])
        if isinstance(page.get("total"), int):
            total = page["total"]
        nxt = page.get("nextStart", -1)
        if not isinstance(nxt, int) or nxt <= 0:
            # Completeness is cross-checked against `total` rather than
            # trusted from nextStart alone. AGO does send nextStart on
            # normal pages, but it returns an ERROR body once
            # start+num > 10000, and a missing or non-int field would
            # otherwise default to "-1" and be read as "that was all of
            # them" -- failure mode A, silently.
            return out, (total is None or len(out) >= total)
        start = nxt
    return out, False


def resolve(target: str) -> dict:
    item_id = item_id_from(target)
    if not item_id:
        return {"ok": False, "reason": f"no 32-hex item id in {target!r}"}
    dead = describe(item_id)
    if dead is None or "error" in dead:
        return {"ok": False, "reason": f"item {item_id} not readable on AGO"}

    dead_url = dead.get("url") or ""
    verdict, why, count = (probe(dead_url) if dead_url
                           else (UNKNOWN, "item carries no service url", None))
    result = {
        "ok": True,
        "queried": {"id": item_id, "title": dead.get("title"),
                    "owner": dead.get("owner"), "type": dead.get("type"),
                    "url": dead_url, "verdict": verdict, "probe": why,
                    "count": count},
        "search_complete": None,
        "replacements": [],
        "empty_candidates": [],
    }
    # Only a real error body justifies looking for a replacement. An
    # unreachable probe means a WAF, a rate limit or one timeout -- offering
    # replacements there is how a healthy layer gets swapped out.
    if verdict != DEAD:
        return result

    found, complete = candidates(dead.get("owner", ""))
    result["search_complete"] = complete
    for c in found:
        if c.get("id") == item_id or not c.get("url"):
            continue
        v, p, n = probe(c["url"])
        if v != LIVE:
            continue
        row = {"id": c["id"], "title": c.get("title"), "url": c["url"],
               "type": c.get("type"), "created": c.get("created"), "probe": p,
               "count": n, "same_title": c.get("title") == dead.get("title")}
        # An empty layer answers cleanly and would score every QLD property
        # "not bushfire prone" -- silently wrong, worse than the outage.
        (result["empty_candidates"] if n == 0
         else result["replacements"]).append(row)
    return result


def _rank(reps: list[dict]) -> list[dict]:
    return sorted(reps, key=lambda r: (not r["same_title"], -(r["created"] or 0)))


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__.strip().split("Usage:")[1], file=sys.stderr)
        return 2
    out = resolve(argv[1])
    print(json.dumps(out, indent=2, ensure_ascii=False))
    if not out["ok"]:
        print(f"\n{out['reason']}", file=sys.stderr)
        return 2
    q = out["queried"]
    if q["verdict"] == LIVE:
        if q["count"] == 0:
            # The empty-layer rule was applied to candidates but not to the
            # incumbent -- and the incumbent is the one actually scoring
            # production. An empty bushfire layer answers cleanly and marks
            # every QLD property "not in zone", which is the silent wrong
            # answer this file's own header calls worse than the outage.
            print(f"\n⚠ {q['id']} answers, but it is EMPTY (count=0). It is "
                  f"not 'fine' -- an empty layer scores every point as not in "
                  f"zone, silently. Check the publisher before trusting it.",
                  file=sys.stderr)
            return 1
        print(f"\n{q['id']} is still live ({q['probe']}); nothing to replace.",
              file=sys.stderr)
        return 0
    if q["verdict"] == UNKNOWN:
        print(f"\nCannot tell whether {q['id']} is dead ({q['probe']}). No "
              f"candidates offered -- an unreachable probe is not evidence of "
              f"a dead layer, and swapping a healthy one is worse than the "
              f"outage.", file=sys.stderr)
        return 2
    reps, empties = _rank(out["replacements"]), out["empty_candidates"]
    if empties:
        print(f"\n{len(empties)} live but EMPTY candidate(s), not offered "
              f"(an empty layer scores every point as 'not in zone'):",
              file=sys.stderr)
        for e in empties[:5]:
            print(f"  {e['id']}  {e['title']}", file=sys.stderr)
    if not reps:
        if out["search_complete"]:
            print(f"\nNo live non-empty candidate among all of "
                  f"{q['owner']}'s items. Still check the publisher's own "
                  f"site before concluding the layer is gone.", file=sys.stderr)
            return 1
        if not q["owner"]:
            print(f"\n{q['id']} has no owner on AGO, so there is nothing to "
                  f"search. This is not 'no replacement exists'.",
                  file=sys.stderr)
            return 2
        print(f"\nSearch was TRUNCATED -- this is not 'no replacement "
              f"exists', it is 'we did not finish looking'. Re-run or widen "
              f"before drawing any conclusion.", file=sys.stderr)
        return 2
    if not out["search_complete"]:
        print(f"\n⚠ search truncated; candidates below are what we saw, not "
              f"all there is.", file=sys.stderr)
    print(f"\n{len(reps)} live candidate(s), same-title first:", file=sys.stderr)
    for r in reps:
        print(f"  {r['id']}  {r['title']}  ({r['probe']}, {r['type']})",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
