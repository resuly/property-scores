# Solar Resource

## Product boundary

`solar_score` is a regional, open-horizon resource screen. It is not a
rooftop-design model.

- GHI, DNI and GTI: approximately 250 m.
- PVOUT and air temperature: approximately 1 km.
- Optimum tilt: approximately 4 km.
- No roof planes, usable-area calculation, tree/building shading, obstructions,
  existing-system detection, tariff, self-consumption or battery dispatch.

The API retains the legacy `estimated_annual_kwh` field only when a caller
supplies `roof_area_m2`. The result is also returned as
`generation_scenario.status=gross_open_horizon_scenario`, with the area labelled
as an unvalidated panel-area proxy and every unmodelled input listed. The batch
`/scores` path no longer feeds an Overture building footprint into that
calculation. It returns the footprint separately as building context because a
ground footprint is not usable roof area.

## Source and rights

Source: Global Solar Atlas 2.0, developed and operated by Solargis on behalf of
the World Bank Group with ESMAP funding.

- Licence: CC BY 4.0.
- Terms: <https://globalsolaratlas.info/support/terms-of-use>.
- Required attribution is returned in every successful and unavailable
  response.
- Field-level source update, version and period metadata from the live response
  are passed through in `source_metadata.vintage`.

The location score is derived only from PVOUT using the current 750 to 2000
kWh/kWp anchors. A scenario orientation changes generation, not the regional
resource score.

## Output contract

The compatible legacy measures remain, with these truth fields added:

- `product=solar_resource`
- `assessment_level=regional_resource`
- `spatial_resolution_m` per returned measure
- `open_horizon=true`
- `roof_model` explicit false flags
- `generation_scenario` or null
- `source`, `licence`, `attribution`, `source_metadata`

## Validation and readiness

Current tests pin score anchors, per-field resolution, provenance, invalid
inputs, and the rule that orientation cannot move the resource score. A
standalone Rooftop Solar product remains a different project and requires roof
segmentation, usable area, 3D shading and installer validation before it can be
named or sold as such.
