#!/bin/bash
# Start or resume the 12-month aircraft-activity run on a Colab VM, driven from a Mac with the `colab` CLI.
#
#   colab_launch.sh start  SESSION FIRST_UTC LAST_UTC     # fresh run
#   colab_launch.sh resume SESSION FIRST_UTC LAST_UTC     # new VM, continue from what the Drive relay already holds
#
# env: AA_PY        python interpreter on the Mac (default python3)
#      AA_DRIVE_DIR  local Google Drive folder used by year_relay.sh (default: first
#                    ~/Library/CloudStorage/GoogleDrive-*/My Drive/_limon-ops/aircraft-activity)
#      AA_DAY_PLAN   day_plan.csv from make_day_plan.py (default: $AA_DRIVE_DIR/day_plan.csv)
#      AA_TMP        local scratch directory for the resume bundle (default: $TMPDIR)
#      AA_TAG        shard tag for multi-VM runs (package names daily_<tag>_..., PROGRESS_<tag>.md)
#      AA_TYPECAT    seed type->category table (typecat.json from an earlier run) for every shard
#      AA_ASSEMBLE   1 on the one VM that assembles grids, 0 on the other shards (default 1)
# Every colab call gets </dev/null and returns at once: the work runs under nohup on the VM, so no long exec
# stays attached (a long attached exec spins the local CPU and a killed one gets the VM reclaimed).
set -euo pipefail
MODE=${1:?start|resume}; S=${2:?session}; FIRST=${3:?first utc day}; LAST=${4:?last utc day}
HERE="$(cd "$(dirname "$0")" && pwd)"
DRIVE=${AA_DRIVE_DIR:-"$(ls -d "$HOME"/Library/CloudStorage/GoogleDrive-*/"My Drive" 2>/dev/null | head -1)/_limon-ops/aircraft-activity"}
PLAN=${AA_DAY_PLAN:-"$DRIVE/day_plan.csv"}; TMP=${AA_TMP:-${TMPDIR:-/tmp}}
q() { grep -v "new version\|colab update\|silence" || true; }

colab sessions </dev/null 2>&1 | grep -q "^\[$S\]" || colab new -s "$S" --high-mem </dev/null 2>&1 | q
X0=$(mktemp "${TMPDIR:-/tmp}/aa_exec.XXXXXX"); echo "import subprocess; subprocess.run(\"apt-get install -y -q aria2 >/dev/null 2>&1\", shell=True)" > "$X0"; colab exec -s "$S" -f "$X0" --timeout 300 </dev/null 2>&1 | q; rm -f "$X0"
for f in nat_common.py year_pipeline.py year_rollup.py year_assemble.py year_pack.py; do
  colab upload -s "$S" "$HERE/$f" "/content/$f" </dev/null 2>&1 | q
done
X=$(mktemp "${TMPDIR:-/tmp}/aa_exec.XXXXXX")
cat > "$X" <<'EOF'
import os; [os.makedirs(p, exist_ok=True) for p in ('/content/year/day', '/content/year/month', '/content/aa_out/pkgs')]
EOF
colab exec -s "$S" -f "$X" </dev/null 2>&1 | q; rm -f "$X"
colab upload -s "$S" "$PLAN" /content/year/day_plan.csv </dev/null 2>&1 | q
# shards start from the same type->category table so the jet/light split does not depend on which VM ran a day
[ -n "${AA_TYPECAT:-}" ] && colab upload -s "$S" "$AA_TYPECAT" /content/year/typecat.json </dev/null 2>&1 | q

if [ "$MODE" = resume ]; then
  # one tar with: rolled months, daily aggregates not yet rolled, the done ledger and the type->category table
  B="$TMP/aa_resume_$$.tar"
  ${AA_PY:-python3} - "$DRIVE" "$B" <<'PY'
import sys, os, glob, tarfile
d, out = sys.argv[1], sys.argv[2]
with tarfile.open(out, 'w') as t:
    for p in glob.glob(f'{d}/monthly/month/*'):          # rolled local months -> /content/year/month/<YYYY-MM>
        t.add(p, arcname=os.path.relpath(p, f'{d}/monthly'))
    if os.path.isdir(f'{d}/daily/day'):                  # daily aggregates of months not rolled yet -> /content/year/day
        t.add(f'{d}/daily/day', arcname='day')
    if os.path.exists(f'{d}/daily/typecat.json'): t.add(f'{d}/daily/typecat.json', arcname='typecat.json')
    # every VM ships its own done ledger; merge them (an OK record wins over a failed one for the same day)
    import json, io
    best = {}
    for f in glob.glob(f'{d}/done*.jsonl') + glob.glob(f'{d}/daily/done*.jsonl'):
        for line in open(f):
            r = json.loads(line)
            if r['date'] not in best or (r['status'] == 'OK' and best[r['date']]['status'] != 'OK'): best[r['date']] = r
    data = ''.join(json.dumps(best[k]) + '\n' for k in sorted(best)).encode()
    ti = tarfile.TarInfo('done.jsonl'); ti.size = len(data); t.addfile(ti, io.BytesIO(data))
PY
  colab upload -s "$S" "$B" /content/aa_resume.tar </dev/null 2>&1 | q
  rm -f "$B"
  X=$(mktemp "${TMPDIR:-/tmp}/aa_exec.XXXXXX")
cat > "$X" <<'EOF'
import tarfile, os, glob, json
with tarfile.open('/content/aa_resume.tar') as t: t.extractall('/content/year')
os.remove('/content/aa_resume.tar')
# days that are already safe on Drive (daily files present, or inside a rolled month) count as packed
days = {os.path.basename(p)[:-5] for p in glob.glob('/content/year/day/*.done')}
for m in glob.glob('/content/year/month/*/meta.json'):
    days.update(d for d in json.load(open(m))['utc_days'])
    for d in json.load(open(m))['utc_days']:
        open(f'/content/year/day/{d}.done', 'w').write('ROLLED')   # skip re-processing
open('/content/aa_out/packed_days.txt', 'w').write('\n'.join(sorted(days)))
for p in glob.glob('/content/year/month/*/'):
    open(f'/content/aa_out/pkgs/month_{os.path.basename(p.rstrip("/"))}.tar', 'w').close()  # already relayed
print('resumed: days', len(days), 'months', len(glob.glob('/content/year/month/*/DONE')))
EOF
colab exec -s "$S" -f "$X" </dev/null 2>&1 | q; rm -f "$X"
fi

X=$(mktemp "${TMPDIR:-/tmp}/aa_exec.XXXXXX")
cat > "$X" <<EOF
import subprocess
for role in ('fetch', 'process'):
    subprocess.Popen(f"cd /content && nohup python year_pipeline.py {role} $FIRST $LAST >> /content/year/{role}.log 2>&1 &", shell=True)
subprocess.Popen("cd /content && FIRST_UTC=$FIRST LAST_UTC=$LAST AA_TAG=${AA_TAG:-} AA_ASSEMBLE=${AA_ASSEMBLE:-1} nohup python year_pack.py >> /content/year/pack.log 2>&1 &", shell=True)
print('started fetch/process/pack')
EOF
colab exec -s "$S" -f "$X" </dev/null 2>&1 | q; rm -f "$X"
echo "now run: year_relay.sh $S 600   (Mac side, low-frequency poll)"
