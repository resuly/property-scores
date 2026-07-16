#!/bin/bash
# Final verification per handoff §3 + manifest backfill.
set -e
cd /work
echo "=== 1. rebuild VRT (7 tiles) + seed manifest ==="
python3 scripts/bake_lidar_cog.py --vrt-only
python3 scripts/bake_lidar_cog.py --seed-manifest
echo
echo "=== 2. 7 COGs present ==="
ls -lh data/global/lidar/*_5m.tif
echo
echo "=== 3. VRT SourceFilenames (must be bare relative names) ==="
grep -o '<SourceFilename[^>]*>[^<]*' data/global/lidar/au_lidar_5m.vrt
echo
echo "=== 4. cross-state samples via VRT (dm; /10 = m) ==="
echo "expected: Sydney ~209 / Adelaide ~449 / Perth ~128 / Darwin ~253"
for p in "151.2093 -33.8688 Sydney" "138.6007 -34.9285 Adelaide" "115.8605 -31.9505 Perth" "130.8456 -12.4634 Darwin"; do
  set -- $p
  printf '%-9s %s %s -> ' "$3" "$1" "$2"
  echo "$1 $2" | gdallocationinfo -valonly -geoloc data/global/lidar/au_lidar_5m.vrt
done
echo
echo "=== 5. manifest ==="
cat data/global/lidar/manifest.json
echo
echo "=== 6. COG structure spot-check (z54) ==="
gdalinfo data/global/lidar/nationalz54ag_5m.tif | grep -E "Driver|Size is|Type=Int16|Overviews|NoData" | head -6
echo FINAL_VERIFY_DONE
