#!/bin/bash
# Mac-side relay: pull finished packages from the Colab VM into the local Google Drive folder (Drive client uploads them).
# usage: year_relay.sh "SESSION[ SESSION...]" [INTERVAL_SECONDS]   (one low-frequency loop for all VMs)      env: AA_DRIVE_DIR (default: first ~/Library/CloudStorage/GoogleDrive-*/My Drive/_limon-ops/aircraft-activity)
#        AA_TMP (scratch dir for downloads), AA_MIN_FREE_GB (default 8), AA_PY (python interpreter, default python3)
SESSIONS=${1:?sessions}; INTERVAL=${2:-600}
DRIVE=${AA_DRIVE_DIR:-"$(ls -d "$HOME"/Library/CloudStorage/GoogleDrive-*/"My Drive" 2>/dev/null | head -1)/_limon-ops/aircraft-activity"}
TMP=${AA_TMP:-${TMPDIR:-/tmp}/aa_relay}; MINFREE=${AA_MIN_FREE_GB:-8}
mkdir -p "$DRIVE"/{daily,monthly,grids-12m,results-year} "$TMP"
LEDGER="$DRIVE/.relay_done"; touch "$LEDGER"
log() { echo "$(date '+%F %T') $*"; }
while true; do
  FREE=$(df -g /System/Volumes/Data | awk 'NR==2{print $4}')
  if [ "$FREE" -lt "$MINFREE" ]; then log "STOP: free disk ${FREE}GB < ${MINFREE}GB"; exit 3; fi
  ALLDONE=1
  for S in $SESSIONS; do
  grep -qx "$S" "$TMP/finished_sessions" 2>/dev/null && continue
  ALLDONE=0
  LIST=$(colab ls -s "$S" /content/aa_out/pkgs </dev/null 2>&1)
  if echo "$LIST" | grep -q "not found"; then log "SESSION GONE: $S"; continue; fi
  for f in $(echo "$LIST" | grep -E '^(daily|month|grids)_.*\.tar$'); do
    grep -qx "$f" "$LEDGER" && continue
    rm -f "$TMP/$f"
    colab download -s "$S" "/content/aa_out/pkgs/$f" "$TMP/$f" </dev/null >/dev/null 2>&1
    if ! tar -tf "$TMP/$f" >/dev/null 2>&1; then log "bad download $f, retry next round"; continue; fi
    case "$f" in
      daily_*) tar -xf "$TMP/$f" -C "$DRIVE/daily" ;;
      month_*) tar -xf "$TMP/$f" -C "$DRIVE/monthly" ;;
      grids_*) mkdir -p "$DRIVE/grids-12m/raw" && tar -xf "$TMP/$f" -C "$DRIVE/grids-12m/raw" ;;
    esac
    rm -f "$TMP/$f"; echo "$f" >> "$LEDGER"; log "relayed $f"
    # once a local month is rolled up, drop the daily files whose two local dates are both inside rolled months
    if [[ "$f" == month_* ]]; then
      ${AA_PY:-python3} - "$DRIVE" <<'PY'
import sys, os, glob, datetime as dt
d = sys.argv[1]; rolled = {os.path.basename(p) for p in glob.glob(f'{d}/monthly/month/*') if os.path.exists(f'{p}/DONE')}
for f in glob.glob(f'{d}/daily/day/*'):
    if f.endswith('_rwy.csv'): continue   # tiny; kept for runway-usage tables after a resume
    u = dt.datetime.strptime(os.path.basename(f)[:10], '%Y.%m.%d').date()
    if u.strftime('%Y-%m') in rolled and (u + dt.timedelta(1)).strftime('%Y-%m') in rolled:
        os.remove(f)
PY
    fi
  done
  TOP=$(colab ls -s "$S" /content/aa_out </dev/null 2>&1)
  for pm in $(echo "$TOP" | grep -E '^PROGRESS.*\.md$'); do colab download -s "$S" "/content/aa_out/$pm" "$DRIVE/$pm" </dev/null >/dev/null 2>&1; done
  colab download -s "$S" /content/aa_out/done.jsonl "$DRIVE/done_$S.jsonl" </dev/null >/dev/null 2>&1
  if echo "$TOP" | grep -qx FINISHED; then log "FINISHED: $S"; echo "$S" >> "$TMP/finished_sessions"; fi
  done
  if [ "$ALLDONE" = 1 ]; then log "ALL SESSIONS FINISHED"; exit 0; fi
  sleep "$INTERVAL"
done
