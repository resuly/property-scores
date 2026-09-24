"""SA Property & Planning Atlas (SAPPA) MapServer, one place for its version.

PlanSA publishes the Planning & Design Code overlays as a version-pinned
service, ``SAPPA/PropertyPlanningAtlasV{N}``, and deletes old versions without
a redirect. V18 started answering 404 "Service ... not found" on 2026-09-24
while the folder listed V19 and V20. Bump ``SAPPA_ATLAS_VERSION`` here and in
``data/truth_anchors/canaries.json`` together (a test checks they agree).

How the version was chosen (2026-09-24): the SAPPA front end bundle
(sappa.plan.sa.gov.au/dist/js/all-*.min.js) sets ``DataDynamicUrl`` to V19.
V19 and V20 have the same 282 layers with the same ids and names, and every
layer id used by the flood and bushfire scorers kept its V18 id. Two names
changed: 141 "Hazards (Flooding)" is now "Hazards (Flooding High)" and 372
"Hazards (Flooding - General)" is now "Hazards (Flooding General)". Before
the next bump, re-check ids by NAME, not by number: ids can be renumbered
between versions.

The host sits behind a CloudFront WAF that requires the SAPPA Referer.
"""

SAPPA_ATLAS_VERSION = 19

SAPPA_BASE = (
    "https://lsa2.geohub.sa.gov.au/arcgis/rest/services"
    f"/SAPPA/PropertyPlanningAtlasV{SAPPA_ATLAS_VERSION}/MapServer"
)

SAPPA_HEADERS = {"Referer": "https://sappa.plan.sa.gov.au/"}
