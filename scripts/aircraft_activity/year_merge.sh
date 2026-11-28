#!/bin/bash
# After all shards finished: give the main VM every rolled month and every runway-usage file, then assemble all regions there.
#   year_merge.sh MAIN_SESSION "REGION ..."        env: AA_DRIVE_DIR, AA_TMP, AA_PY (as in year_relay.sh)
# The relay then brings back grids_ALL_<first>_<last>.tar.
# AA_MERGE_MONTHS="2026-05 2026-06 ..." limits the upload to months rolled on the other VMs.
set -euo pipefail
S=${1:?main session}; REGIONS=${2:-"MEL SYD BNE OOL MCY PER ADL CBR HBA LST DRW CNS TSV NTL"}
DRIVE=${AA_DRIVE_DIR:-"$(ls -d "$HOME"/Library/CloudStorage/GoogleDrive-*/"My Drive" 2>/dev/null | head -1)/_limon-ops/aircraft-activity"}
TMP=${AA_TMP:-${TMPDIR:-/tmp}}; B="$TMP/aa_merge_$$.tar"
${AA_PY:-python3} - "$DRIVE" "$B" "${AA_MERGE_MONTHS:-}" <<'PY'
import sys, os, glob, tarfile
d, out, only = sys.argv[1], sys.argv[2], sys.argv[3].split()
with tarfile.open(out, 'w') as t:
    for p in glob.glob(f'{d}/monthly/month/*'):
        if only and os.path.basename(p) not in only: continue   # months the main VM rolled itself are already there
        t.add(p, arcname=os.path.relpath(p, f'{d}/monthly'))
    for f in glob.glob(f'{d}/daily/day/*_rwy.csv'):
        t.add(f, arcname=os.path.join('day', os.path.basename(f)))
print('bundle', os.path.getsize(out) // 1_000_000, 'MB')
PY
colab upload -s "$S" "$B" /content/aa_merge.tar </dev/null >/dev/null 2>&1; rm -f "$B"
X=$(mktemp "${TMPDIR:-/tmp}/aa_exec.XXXXXX")
cat > "$X" <<EOF
import tarfile, subprocess, glob, os
with tarfile.open('/content/aa_merge.tar') as t:
    # keep files the main VM already has (its own months/days); add the shards' ones
    for m in t.getmembers():
        if not os.path.exists(os.path.join('/content/year', m.name)): t.extract(m, '/content/year')
os.remove('/content/aa_merge.tar')
for m in glob.glob('/content/year/month/*/'):
    open(os.path.join(m, 'DONE'), 'a').close()
subprocess.Popen("cd /content && nohup bash -c 'python year_assemble.py $REGIONS > /content/year/merge.log 2>&1; cd /content/year && M=\$(ls month | sort | head -1)_\$(ls month | sort | tail -1) && tar -cf /content/aa_out/pkgs/grids_ALL_\$M.tar out && echo MERGED >> /content/year/merge.log' &", shell=True)
print('merge started; months:', sorted(os.listdir('/content/year/month')))
EOF
colab exec -s "$S" -f "$X" --timeout 120 </dev/null 2>&1 | grep -v "new version\|colab update\|silence" || true
rm -f "$X"
