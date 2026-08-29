# Third-party notices

## Geoscience Australia Bushfire Attack Level Toolbox

`property_scores/bal_prescreen/tables.py` adapts numeric lookup data from the
Geoscience Australia Bushfire Attack Level Toolbox at commit
`18c6cff4b37544805e78cf00ec376dbca2ff8cd0`:

https://github.com/GeoscienceAustralia/BAL

Copyright © 2016 Commonwealth of Australia (Geoscience Australia).
Licensed under the Apache License, Version 2.0:

https://github.com/GeoscienceAustralia/BAL/blob/18c6cff4b37544805e78cf00ec376dbca2ff8cd0/LICENSE

DA Leads' preliminary screening implementation is modified from the source
shape and combines the lookup data with separate terrain, land-cover and
official-overlay inputs. Geoscience Australia does not endorse DA Leads or the
resulting product. This notice does not represent that the product conforms to
the current edition of AS 3959.

## ACT Register of contaminated sites and ACTGOV Block

Contamination Screening uses the ACT Government Register of contaminated sites
dataset (`ecgf-jdca`) and the ACTGOV Block FeatureServer to attach register rows
to the district, division, section and block containing the query point.

- https://www.data.act.gov.au/Environment/Register-of-contaminated-sites/ecgf-jdca
- https://www.arcgis.com/home/item.html?id=802b1fe9b1bc480ba41d6a653ec40b62

Copyright © Australian Capital Territory. Both sources are licensed under
Creative Commons Attribution 4.0 International:

https://creativecommons.org/licenses/by/4.0/

The register supplies no geometry. DA Leads performs the block join and does
not imply endorsement by the ACT Government.
