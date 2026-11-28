"""Parse Airservices 'Movements at Australian Airports' monthly PDFs (converted with `pdftotext -layout`) into one CSV.

usage: parse_airservices.py TXT_DIR OUT_CSV   (TXT_DIR holds airport-movements-YYYY-MM.txt)
Columns are assigned by character position against a row that has all seven numbers, and every row is checked
against its printed total.
"""
import re, sys, csv, os
CODES = 'YSSY YSBK YSCN YBBN YBAF YBCG YBSU YPPH YPJT YPAD YPPF YSCB YMHB YCBG YMLT YPDN YBCS YBTL YWLM YMML YMEN YMMB YMAV'.split()
COLS = ['gt136', 't7_136', 'lt7', 'heli', 'unknown', 'military', 'total']
out = []
import glob
for f in sorted(glob.glob(os.path.join(sys.argv[1], 'airport-movements-20??-??.txt'))):
    tag = f[-11:-4]
    lines = open(f).read().splitlines()
    # column right edges from the YSSY row (all 7 present except maybe 0)
    ends = None
    for l in lines:
        m = re.search(r'\b(Y[A-Z]{3})\b', l)
        if not m: continue
        nums = list(re.finditer(r'[\d,]+', l[m.end():]))
        if len(nums) == 7:
            ends = [n.end() + m.end() for n in nums]; break
    for l in lines:
        m = re.search(r'\b(Y[A-Z]{3})\b', l)
        if not m or m.group(1) not in CODES: continue
        rec = {'month': tag, 'code': m.group(1)}
        for n in re.finditer(r'[\d,]+', l[m.end():]):
            e = n.end() + m.end()
            j = min(range(7), key=lambda k: abs(ends[k] - e))
            rec[COLS[j]] = int(n.group().replace(',', ''))
        out.append(rec)
w = csv.DictWriter(open(sys.argv[2], 'w'), fieldnames=['month', 'code'] + COLS); w.writeheader(); w.writerows(out)
for r in out:
    s = sum(r.get(c, 0) for c in COLS[:-1]); print(r['month'], r['code'], {c: r.get(c) for c in COLS}, 'sumOK' if s == r.get('total') else f'SUM MISMATCH {s}')
