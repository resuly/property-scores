#!/usr/bin/env bash
# Quarterly cron body: HEAD-check the GA 5m LiDAR source zips against the baked
# manifest (bake_lidar_cog.py --check). The national mosaic has been static
# since 2015, so this normally no-ops. Exits non-zero on CHANGED zones or HEAD
# failures so cron_with_alert.sh raises a Telegram alert -> re-bake on the PC
# per docs/lidar-build-pc-handoff.md section 7.
set -uo pipefail
cd "$(dirname "$0")/.."
out=$(.venv/bin/python scripts/bake_lidar_cog.py --check 2>&1)
echo "$out"
echo "$out" | grep -q "CHANGED" && exit 1
echo "$out" | grep -q "HEAD failed" && exit 1
exit 0
