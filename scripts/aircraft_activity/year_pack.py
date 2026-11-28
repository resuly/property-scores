# Colab VM background loop: package finished work into small tarballs under /content/aa_out/pkgs for the Mac-side relay.
#   python year_pack.py LAST_UTC_DATE
# - daily_<first>_<last>.tar : per-UTC-day aggregates (every PACK_DAYS finished days)
# - month_<YYYY-MM>.tar      : local-month rollup (after year_rollup.py rolls it)
# - grids_<tag>.tar          : (partial-)year grids + quarterly grids + tables for the regions in ASSEMBLE_FIRST, then all
# - /content/aa_out/PROGRESS.md updated every loop
import os, glob, json, time, subprocess, datetime as dt, tarfile
Y = '/content/year'; O = '/content/aa_out'; P = f'{O}/pkgs'; os.makedirs(P, exist_ok=True)
PACK_DAYS = int(os.environ.get('PACK_DAYS', '5'))
PRIORITY = ['MEL', 'SYD']
ALL = ['MEL', 'SYD', 'BNE', 'OOL', 'MCY', 'PER', 'ADL', 'CBR', 'HBA', 'LST', 'DRW', 'CNS', 'TSV', 'NTL']
LAST = os.environ.get('LAST_UTC', '2026.11.26')
TAG = os.environ.get('AA_TAG', '')            # machine/shard tag, keeps package names unique across VMs
ASSEMBLE = os.environ.get('AA_ASSEMBLE', '1') == '1'   # only one VM assembles grids
T_ = (TAG + '_') if TAG else ''
T_START = time.time()

def done_days():
    # every finished attempt (OK or not) is shipped, so a later OK re-run of a failed day is shipped again
    return sorted(os.path.basename(p)[:-5] for p in glob.glob(f'{Y}/day/*.done'))

def _mt(d):
    f = f'{Y}/day/{d}.done'
    return int(os.path.getmtime(f)) if os.path.exists(f) else 0

def packed_state():
    """{UTC day: mtime of its .done marker when it was shipped}; a re-run day (new marker) is shipped again"""
    f = f'{O}/packed_state.json'
    if os.path.exists(f):
        return json.load(open(f))
    st = {}
    old = f'{O}/packed_days.txt'
    if os.path.exists(old):
        st = {d: _mt(d) for d in open(old).read().split()}
    else:
        for p in glob.glob(f'{P}/daily_*.tar'):
            with tarfile.open(p) as t:
                st.update({os.path.basename(n)[:10]: _mt(os.path.basename(n)[:10]) for n in t.getnames() if n.endswith('.done')})
    json.dump(st, open(f, 'w')); return st

def mark_packed(days):
    st = packed_state(); st.update({d: _mt(d) for d in days}); json.dump(st, open(f'{O}/packed_state.json', 'w'))

def tar(path, files, arcbase):
    tmp = path + '.part'
    with tarfile.open(tmp, 'w') as t:
        for f in files: t.add(f, arcname=os.path.relpath(f, arcbase))
    os.replace(tmp, path)

def progress(final=False):
    rows = [json.loads(l) for l in open(f'{Y}/done.jsonl')] if os.path.exists(f'{Y}/done.jsonl') else []
    ok = [r for r in rows if r['status'] == 'OK']; bad = [r for r in rows if r['status'] != 'OK']
    secs = [r.get('total_s', 0) for r in ok[-20:]]
    per = (sum(secs) / len(secs)) if secs else 0
    f0 = dt.datetime.strptime(os.environ.get('FIRST_UTC', '2025.11.26'), '%Y.%m.%d'); f1 = dt.datetime.strptime(LAST, '%Y.%m.%d')
    total = (f1 - f0).days + 1; left = max(total - len(rows), 0)
    miss = json.load(open(f'{Y}/missing.json')) if os.path.exists(f'{Y}/missing.json') else {}
    months = sorted(os.path.basename(os.path.dirname(p)) for p in glob.glob(f'{Y}/month/*/DONE'))
    txt = [f'# Aircraft activity 12-month run (14 regions)', '',
           f'- Updated (UTC): {dt.datetime.utcnow():%Y-%m-%d %H:%M}',
           f'- Machine/shard: {TAG or "main"}, range up to {LAST}',
           f'- UTC archive days processed on this VM: {len(rows)} (OK {len(ok)}, not OK {len(bad)})',
           f'- Last processed UTC day: {rows[-1]["date"] if rows else "-"}',
           f'- Recent processing time per day: {per:.0f} s (download overlapped); estimated total for {total} days: {total * max(per, 60) / 3600:.1f} h; remaining: {left * max(per, 60) / 3600:.1f} h',
           f'- Run started on this VM (UTC): {dt.datetime.utcfromtimestamp(T_START):%Y-%m-%d %H:%M}',
           f'- Local months rolled up: {", ".join(months) if months else "none yet"}',
           f'- Bad UTC archive days: {json.dumps(miss.get("bad_utc", {}))}',
           f'- Local days excluded (missing_days): {sorted(miss.get("missing_local", {}).keys())}',
           f'- Not-OK processing records: {[(r["date"], r["status"]) for r in bad]}', '']
    open(f'{O}/PROGRESS{("_" + TAG) if TAG else ""}.md', 'w').write('\n'.join(txt))

assembled = set()
while True:
    dd = done_days(); pk = packed_state()
    new = [d for d in dd if pk.get(d) != _mt(d)]
    # package in runs of PACK_DAYS (or whatever is left at the end of the range)
    while len(new) >= PACK_DAYS or (new and LAST in dd):
        grp = new[:PACK_DAYS]; new = new[PACK_DAYS:]
        files = [f for d in grp for f in glob.glob(f'{Y}/day/{d}_*') + [f'{Y}/day/{d}.done']] + [f for f in (f'{Y}/typecat.json', f'{Y}/done.jsonl') if os.path.exists(f)]
        tar(f'{P}/daily_{T_}{grp[0]}_{grp[-1]}.tar', files, Y); mark_packed(grp)
        subprocess.run(['cp', f'{Y}/done.jsonl', f'{O}/done.jsonl'])
    # roll months, package new month rollups
    subprocess.run(['python', '/content/year_rollup.py'], capture_output=True)
    newmonths = []
    for mdir in sorted(glob.glob(f'{Y}/month/*/DONE')):
        tag = os.path.basename(os.path.dirname(mdir))
        ver = int(os.path.getmtime(mdir))
        name = f'{P}/month_{T_}{tag}_v{ver}.tar'
        if not os.path.exists(name) and not (os.path.exists(f'{P}/month_{tag}.tar') and os.path.getmtime(f'{P}/month_{tag}.tar') >= ver):
            tar(name, glob.glob(f'{Y}/month/{tag}/*'), Y); newmonths.append(tag)
    if newmonths and ASSEMBLE:
        # priority regions first, as partial-year grids
        subprocess.run(['python', '/content/year_assemble.py'] + PRIORITY, capture_output=True)
        months = sorted(os.path.basename(os.path.dirname(p)) for p in glob.glob(f'{Y}/month/*/DONE'))
        tagm = f'{months[0]}_{months[-1]}'
        tar(f'{P}/grids_{"-".join(PRIORITY)}_{tagm}_v{int(time.time())}.tar', glob.glob(f'{Y}/out/*'), Y)
    progress()
    if LAST in dd and not new:
        subprocess.run(['python', '/content/year_rollup.py'], capture_output=True)
        if 'ALL' not in assembled and ASSEMBLE:
            subprocess.run(['python', '/content/year_assemble.py'] + ALL, capture_output=True)
            months = sorted(os.path.basename(os.path.dirname(p)) for p in glob.glob(f'{Y}/month/*/DONE'))
            tar(f'{P}/grids_ALL_{months[0]}_{months[-1]}.tar', glob.glob(f'{Y}/out/*'), Y)
            assembled.add('ALL'); progress()
            open(f'{O}/FINISHED', 'w').write('ok')
        if not ASSEMBLE:
            progress(); open(f'{O}/FINISHED', 'w').write('ok')
    time.sleep(60)
