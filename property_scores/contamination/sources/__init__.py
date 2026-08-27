"""Contamination data source adapters (first batch, 2026-08-27).

This package is the *data access layer only*: each module knows how to query
one upstream register and hand back plain dicts. Nothing here touches the
score, the bands or the response contract; scoring integration is done
separately in ``property_scores.contamination.score``.

Every adapter follows the same fail-closed discipline inherited from
``score.py`` (2026-08-10 fail-closed audit):

* ``None``  = the query FAILED (network error, non-2xx, an HTTP 200 carrying
  an upstream error body, or a payload whose structure we do not recognise).
* ``[]``    = the query SUCCEEDED and the register holds nothing nearby.

The two must never be conflated. Collapsing an outage into an empty list is
exactly how a dropped connection once turned a contaminated site into
"Very Clean" and cached the lie for an hour.

Licensing note (see limon-ops docs/contamination-data-sources-tracker.md):
all four sources are CC BY 4.0 or equivalent and require attribution on every
delivery surface that exposes the records. NSW's licence version is still
being confirmed for paid API redistribution.
"""

from property_scores.contamination.sources import (  # noqa: F401
    ga_waste,
    nsw_groundwater,
    nsw_sites,
    qld_ea,
    sa_gpa,
    sa_licensed,
    vic_wfs,
)

__all__ = [
    "nsw_sites", "nsw_groundwater", "vic_wfs", "sa_gpa", "sa_licensed",
    "qld_ea", "ga_waste",
]
