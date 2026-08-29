# Landscape Openness

The legacy module and response key remain `view_quality` for compatibility.
The product name and labels are Landscape Openness.

## Product boundary

This is location-level visual-amenity context. It does not raytrace a view from
a window and does not model observer storey, window/balcony orientation,
building occlusion along a target sightline, or future development. A high
score cannot guarantee an ocean, city, landmark or green view.

## Six factors

1. Ocean/coast proximity, weight 3.0.
2. Inland-water proximity, weight 1.5.
3. Relative elevation advantage, weight 2.5.
4. Tree canopy and green-destination context, weight 2.0.
5. Nearby building openness, weight 2.0.
6. Eight-direction bare-earth terrain horizon, nominal weight 2.5. At least
   six directions must have DEM coverage; otherwise the factor is omitted.
   With six or seven directions its effective weight is scaled by directional
   coverage instead of treating a narrow clear arc as a complete horizon.

Terrain now uses the shared local elevation path: 5 m LiDAR where available,
then the 30 m bare-earth DEM. Direction samples use equal ground distances in
every bearing. The former degree offsets made east/west samples too short at
Australian latitudes and diagonal samples too long.

## Coverage semantics

No ocean or inland-water feature inside the documented search radius is a
checked zero contribution, not missing data. A source outage or absent local
artifact remains missing.

Missing factors still use compatible adaptive weighting, but they can no longer
silently present as a complete score:

- `missing_factors` names absent factors;
- `partial_factors` names incomplete directional factors;
- `factor_weight_completeness` reports the active weight share; and
- `degraded=true` plus the caveat states that the remaining factors were
  reweighted.

## Output contract

- `product=landscape_openness`
- `legacy_score_key=view_quality`
- `assessment_level=location_context`
- Landscape Openness labels rather than guaranteed-view labels
- explicit `line_of_sight` false flags
- per-factor evidence and response-level source/rights rows

## Future evidence gate

A true Viewshed or View Intelligence product requires at least observer height,
orientation, 3D terrain plus building occlusion, visible-target classification
and labelled real-property validation. It is not a rename of this score and
should be built only after a named buyer demonstrates a repeated sightline job.
