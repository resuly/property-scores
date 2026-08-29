"""验证 heat-island 本地化无损(P2-1)。

对 data/truth_anchors/heat_island_anchors.csv 的真值锚点:
1. 跑本地版 heat_island_score, 自动判 expected gate(score<=/>=X, label, source)
2. 与远程 _modis_lst_remote 对比 point LST(证明本地 mosaic 数值无损)
3. 测冷启动延迟(应从 ~18s 降到亚秒)

用法:
  python scripts/validate_heat_local.py            # 含远程对比(慢, 每点 ~18s)
  python scripts/validate_heat_local.py --no-remote # 只跑本地(快)
"""
import argparse
import csv
import re
import time

from property_scores.heat_island import score as H

ANCHORS = "data/truth_anchors/heat_island_anchors.csv"


def check_gate(expected: str, res: dict) -> tuple[str, str]:
    """按 expected 文本判 gate。返回 (PASS/FAIL/MANUAL, detail)。"""
    score = res.get("score")
    label = res.get("label", "")
    src = res.get("source", "")
    exp = expected.lower()
    checks, ok = [], True
    m = re.search(r"score\s*<=\s*(\d+)", exp)
    if m:
        lim = int(m.group(1)); c = score is not None and score <= lim
        checks.append(f"<={lim}:{'Y' if c else 'N'}({score})"); ok &= c
    m = re.search(r"score\s*>=\s*(\d+)", exp)
    if m:
        lim = int(m.group(1)); c = score is not None and score >= lim
        checks.append(f">={lim}:{'Y' if c else 'N'}({score})"); ok &= c
    m = re.search(r"label (very cool|moderate heat|extreme heat|hot|cool)", exp)
    if m:
        want = m.group(1); c = want in label.lower()
        checks.append(f"label~{want}:{'Y' if c else 'N'}({label})"); ok &= c
    if ("returns null" in exp or "water pixel" in exp
            or "data unavailable" in exp):
        # The commercial Open-Meteo/ERA5 fallback was removed 2026-08-02.
        # A true MODIS/water gap must now fail closed rather than silently move
        # to a differently scaled, non-commercial upstream.
        c = score is None and label.lower() == "data unavailable"
        checks.append(
            f"data-unavailable:{'Y' if c else 'N'}({score}/{label})")
        ok &= c
    if not checks:
        return "MANUAL", expected[:48]
    return ("PASS" if ok else "FAIL"), " ".join(checks)


def run(with_remote: bool = True) -> int:
    rows = list(csv.DictReader(open(ANCHORS)))
    npass = nfail = nman = 0
    maxd = 0.0
    print(f"\n{'why':26s} {'scr':>4s} {'label':13s} {'src':5s} "
          f"{'lLST':>5s} {'rLST':>5s} {'dLST':>5s} {'ms':>6s}  verdict / gate")
    print("-" * 120)
    for r in rows:
        lat, lng = float(r["lat"]), float(r["lng"])
        H._cache.clear()  # 冷测每点
        t = time.time(); res = H.heat_island_score(lat, lng); ms = (time.time() - t) * 1000
        lLST = res.get("modis_lst_c")
        rLST = d = None
        if with_remote:
            rem = H._modis_lst_remote(lat, lng)
            rLST = rem.get("point_lst_c") if rem else None
            if lLST is not None and rLST is not None:
                d = round(float(lLST) - float(rLST), 1)
                maxd = max(maxd, abs(d))
        verdict, detail = check_gate(r["expected"], res)
        npass += verdict == "PASS"; nfail += verdict == "FAIL"; nman += verdict == "MANUAL"
        print(f"{r['why'][:26]:26s} {str(res.get('score')):>4s} {res.get('label', ''):13s} "
              f"{res.get('source', ''):5s} {str(lLST):>5s} {str(rLST):>5s} "
              f"{str(d) if d is not None else '':>5s} {ms:6.0f}  {verdict} {detail}")
    print("-" * 120)
    print(f"PASS {npass} · FAIL {nfail} · MANUAL {nman}  (共 {len(rows)} 锚点)")
    if with_remote:
        print(f"本地 vs 远程 point LST 最大绝对差: {maxd:.1f}°C")
    return nfail


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-remote", action="store_true", help="跳过远程对比(快)")
    a = ap.parse_args()
    raise SystemExit(1 if run(with_remote=not a.no_remote) else 0)
