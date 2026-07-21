#!/usr/bin/env bash
# reg-09: build the Central Coast 1% AEP depth COG from the CC-BY open study zip.
# Reproducible; no account. Requires GDAL. On mac unset the EclipseSUMO PROJ pollution.
set -euo pipefail
unset PROJ_LIB PROJ_DATA GDAL_DATA || true
OUT="${1:-cc_q100y_depth_4326.tif}"
URL="https://flooddata.ses.nsw.gov.au/dataset/northern-lakes-floodplain-risk-management-study-and-plan-processed-hydraulic-results-public/resource/75760bca-1236-4e08-9d72-4f59c1a6777a/download"
tmp=$(mktemp -d)
curl -sL -o "$tmp/cc.zip" "$URL"
unzip -o "$tmp/cc.zip" "Depth/Q100y_*Depth.tif" -d "$tmp" >/dev/null
gdalbuildvrt -srcnodata -9999.99 -vrtnodata -9999.99 "$tmp/q.vrt" "$tmp"/Depth/Q100y_*Depth.tif >/dev/null
# source tiles carry a non-standard ENGCRS wrapper -> force -s_srs EPSG:28356 (GDA94/MGA56)
gdalwarp -s_srs EPSG:28356 -t_srs EPSG:4326 -r bilinear -dstnodata -9999.99 \
  -co TILED=YES -co COMPRESS=DEFLATE -overwrite "$tmp/q.vrt" "$OUT"
rm -rf "$tmp"
echo "built $OUT"
