"""Choose one adsb.lol archive per UTC day (prod-0 unless staging-0 is >10% larger) and write day_plan.csv.

usage: make_day_plan.py FIRST_UTC LAST_UTC OUT_CSV      dates as YYYY.MM.DD

Lists the releases of adsblol/globe_history_<year> through the public GitHub API (no token needed;
set GITHUB_TOKEN to raise the rate limit). A day with neither release is written as MISSING.
"""
import csv, datetime as dt, json, os, re, sys, urllib.error, urllib.request

def releases(year):
    out, page = [], 1
    while True:
        req = urllib.request.Request(f'https://api.github.com/repos/adsblol/globe_history_{year}/releases?per_page=100&page={page}',
                                     headers={'Accept': 'application/vnd.github+json', 'User-Agent': 'aircraft-activity-pipeline'})
        if os.environ.get('GITHUB_TOKEN'):
            req.add_header('Authorization', 'Bearer ' + os.environ['GITHUB_TOKEN'])
        try:
            batch = json.load(urllib.request.urlopen(req, timeout=60))
        except urllib.error.HTTPError as e:
            if e.code == 404: return out          # next year's repository may not exist yet
            raise
        if not batch:
            return out
        for r in batch:
            m = re.match(r'v(\d{4}\.\d{2}\.\d{2})-planes-readsb-(\w+-\d)', r['tag_name'])
            if m:
                out.append((m.group(1), m.group(2), sum(a['size'] for a in r['assets']), f'globe_history_{year}'))
        page += 1

def main():
    a, b, out = sys.argv[1:4]
    d0 = dt.datetime.strptime(a, '%Y.%m.%d').date(); d1 = dt.datetime.strptime(b, '%Y.%m.%d').date()
    rel = {}
    for y in range(d0.year, d1.year + 2):
        for d, kind, size, repo in releases(y):   # late-December archives can sit in the next year's repository
            if size > rel.get(d, {}).get(kind, (0, ''))[0]:
                rel.setdefault(d, {})[kind] = (size, repo)
    with open(out, 'w', newline='') as f:
        w = csv.writer(f); w.writerow(['date', 'kind', 'bytes', 'repo'])
        for i in range((d1 - d0).days + 1):
            d = (d0 + dt.timedelta(i)).strftime('%Y.%m.%d'); r = rel.get(d, {})
            (prod, prepo), (stg, srepo) = r.get('prod-0', (0, '')), r.get('staging-0', (0, ''))
            if not prod and not stg:
                w.writerow([d, 'MISSING', 0, ''])
            elif prod >= 0.9 * max(prod, stg):
                w.writerow([d, 'prod-0', prod, prepo])
            else:
                w.writerow([d, 'staging-0', stg, srepo])

if __name__ == '__main__':
    main()
