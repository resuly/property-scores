"""Mapbox 速度->交通流量特征 (噪声模型破平台用的非冗余真实交通信号)。

地理特征/AADT 估计都从路类派生 (冗余); Mapbox 观测的实际速度反映真实车流量,
是唯一非冗余的交通信号。复用 TrafficTwin 的成熟公式 (BPR/Greenshields/HCM 加权)。

Directions API v5 driving-traffic 取 observed speed + congestion + maxspeed:
- observed speed 覆盖最广 (主信号)
- congestion / congestion_numeric 部分路段为 unknown/None, 缺失时用 -1 哨兵 + 兜底
- 免费额度 10万请求/月; 磁盘缓存避免重复请求; rate limit 300/min

Token: MAPBOX_ACCESSTOKEN (环境变量 或 TrafficTwin/.env)。
"""
import hashlib
import json
import os
import statistics as st
import time
import urllib.error
import urllib.request

BASE = "https://api.mapbox.com/directions/v5/mapbox/driving-traffic/"

# road_class -> (alpha, beta, capacity_per_lane, lanes)  (来自 TrafficTwin)
ROAD_PARAMS = {
    "motorway": (0.15, 4.0, 2000, 3), "trunk": (0.15, 4.0, 1800, 2),
    "primary": (0.25, 4.0, 1600, 2), "secondary": (0.35, 4.0, 1400, 1),
    "tertiary": (0.45, 4.0, 1200, 1), "residential": (0.55, 4.0, 800, 1),
    "unclassified": (0.5, 4.0, 900, 1), "service": (0.6, 4.0, 600, 1),
    "living_street": (0.7, 4.0, 300, 1),
}
DEFAULT_PARAMS = ROAD_PARAMS["secondary"]


def _token(tok=None):
    if tok:
        return tok
    t = os.environ.get("MAPBOX_ACCESSTOKEN")
    if t:
        return t
    envp = os.path.expanduser("~/Documents/GitHub/TrafficTwin/.env")
    if os.path.exists(envp):
        for line in open(envp):
            if line.startswith("MAPBOX_ACCESSTOKEN"):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def speed_to_flow(obs_kmh, maxspeed_kmh, road_class):
    """observed speed -> vehicles/hour (BPR 0.5 + Greenshields 0.3 + HCM 0.2)。"""
    alpha, beta, cap_lane, lanes = ROAD_PARAMS.get(road_class, DEFAULT_PARAMS)
    cap = cap_lane * lanes
    ff = 0.9 * maxspeed_kmh
    if ff <= 0 or obs_kmh <= 0:
        return 0.0
    obs = min(obs_kmh, ff)
    # BPR (backward inference of V/C)
    ttr = ff / max(obs, 1.0)
    bpr = min(((ttr - 1.0) / alpha) ** (1.0 / beta), 0.95) * cap if ttr > 1.0 else 0.0
    # Greenshields
    jam = 190 * lanes
    gs = max(0.0, obs * (jam * (1.0 - obs / ff))) if obs < ff else 0.0
    # HCM (LOS flow ratio)
    sr = obs / ff
    ratio = (0.35 if sr >= .9 else .55 if sr >= .8 else .75 if sr >= .7
             else .85 if sr >= .6 else .95 if sr >= .5 else 1.0)
    hcm = cap * ratio
    return min(0.5 * bpr + 0.3 * gs + 0.2 * hcm, cap * 0.95)


def _cache_path(cache_dir, key):
    return os.path.join(cache_dir, key[:2], key + ".json")


def directions(od, token=None, depart_at=None, cache_dir="data/mapbox_cache",
               delay=0.21, retries=3):
    """od = [(lng,lat),(lng,lat)] 一段路的 OD。返回聚合 dict 或 None。

    {speed_kmh(median), congestion_num(mean有值), cong_cov, maxspeed_kmh(median), n_seg}
    磁盘缓存 (含 depart_at)。401/403/连续失败返回 None。
    """
    token = _token(token)
    if not token:
        return None
    key = hashlib.md5(json.dumps([od, depart_at], sort_keys=True).encode()).hexdigest()
    cp = _cache_path(cache_dir, key)
    if os.path.exists(cp):
        try:
            return json.load(open(cp))
        except Exception:
            pass
    coords = ";".join(f"{lng:.6f},{lat:.6f}" for lng, lat in od)
    url = (BASE + coords + "?annotations=speed,congestion,congestion_numeric,maxspeed"
           "&overview=full&geometries=geojson&access_token=" + token)
    if depart_at:
        url += "&depart_at=" + depart_at
    out = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "noise-poc"})
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.load(r)
            leg = data["routes"][0]["legs"][0]
            ann = leg.get("annotation", {}) or {}
            sp = [x for x in (ann.get("speed") or []) if x is not None]
            cn_raw = ann.get("congestion_numeric") or []
            cn = [x for x in cn_raw if x is not None]
            mxs = [m.get("speed") for m in (ann.get("maxspeed") or [])
                   if isinstance(m, dict) and m.get("speed")]
            out = {
                "speed_kmh": (st.median(sp) * 3.6) if sp else None,
                "congestion_num": (sum(cn) / len(cn)) if cn else None,
                "cong_cov": (len(cn) / len(cn_raw)) if cn_raw else 0.0,
                "maxspeed_kmh": (st.median(mxs)) if mxs else None,
                # duration/distance 是 depart_at 下唯一对时段敏感的字段 (speed annotation 不敏感)
                "duration_s": leg.get("duration"),
                "distance_m": leg.get("distance"),
                "n_seg": len(sp),
            }
            break
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries - 1:
                time.sleep(2.0)
                continue
            return None
        except Exception:
            if attempt < retries - 1:
                time.sleep(1.0)
                continue
            return None
    if out is not None:
        os.makedirs(os.path.dirname(cp), exist_ok=True)
        json.dump(out, open(cp, "w"))
        time.sleep(delay)  # rate limit 300/min
    return out


def point_features(od, road_class, fallback_maxspeed, token=None, depart_at=None,
                   cache_dir="data/mapbox_cache"):
    """噪声特征: mapbox_speed_ratio, mapbox_congestion, mapbox_flow_vph, mapbox_ok。

    od = 最近主干道的一段 [(lng,lat),(lng,lat)] (调用方从 roads 表取)。
    缺 Mapbox 数据时用 fallback maxspeed + 中等假设兜底, mapbox_ok=0。
    """
    d = directions(od, token=token, depart_at=depart_at, cache_dir=cache_dir)
    maxspeed = (d and d.get("maxspeed_kmh")) or fallback_maxspeed or 50.0
    if d and d.get("speed_kmh"):
        obs = d["speed_kmh"]
        ratio = min(obs / (0.9 * maxspeed), 1.2)
        cong = d.get("congestion_num")
        return {
            "mapbox_speed_ratio": ratio,
            "mapbox_congestion": cong if cong is not None else -1.0,
            "mapbox_flow_vph": speed_to_flow(obs, maxspeed, road_class),
            "mapbox_ok": 1.0,
        }
    # 兜底: 无观测速度 -> 中等流量假设
    return {
        "mapbox_speed_ratio": 1.0,
        "mapbox_congestion": -1.0,
        "mapbox_flow_vph": speed_to_flow(0.7 * maxspeed, maxspeed, road_class),
        "mapbox_ok": 0.0,
    }


FEATURE_KEYS = ["mapbox_speed_ratio", "mapbox_congestion", "mapbox_flow_vph", "mapbox_ok"]

# --- 时段拥堵特征 (用 depart_at 取典型工作日高峰 vs 深夜) ---
# 实测: depart_at 下 duration 随时段大幅变, speed annotation 几乎不变。
# 所以真实交通负荷信号 = duration 推导的拥堵幅度, 不是 speed annotation。
DIURNAL_KEYS = ["mb_diurnal_slowdown", "mb_peak_congestion", "mb_peak_speed_ratio", "mb_diurnal_ok"]


def diurnal_features(od, road_class, fallback_maxspeed, peak_utc, night_utc,
                     token=None, cache_dir="data/mapbox_cache"):
    """典型工作日高峰 vs 深夜的拥堵特征 (该路真实交通负荷的代理, 超出路类)。

    - mb_diurnal_slowdown = peak_duration / night_duration (>1 = 高峰更堵 = 车流量大)
    - mb_peak_congestion  = 高峰典型 congestion_numeric (-1 缺失)
    - mb_peak_speed_ratio = 高峰有效车速(distance/duration) / free-flow
    peak_utc / night_utc = 该城高峰/深夜对应的 UTC ISO8601 Z 字符串。
    """
    out = {"mb_diurnal_slowdown": 1.0, "mb_peak_congestion": -1.0,
           "mb_peak_speed_ratio": 1.0, "mb_diurnal_ok": 0.0}
    if od is None:
        return out
    dp = directions(od, token=token, depart_at=peak_utc, cache_dir=cache_dir)
    dn = directions(od, token=token, depart_at=night_utc, cache_dir=cache_dir)
    maxspeed = (dp and dp.get("maxspeed_kmh")) or fallback_maxspeed or 50.0
    if dp and dp.get("duration_s") and dp.get("distance_m"):
        peak_eff = (dp["distance_m"] / dp["duration_s"]) * 3.6
        out["mb_peak_speed_ratio"] = min(peak_eff / (0.9 * maxspeed), 1.2)
        out["mb_peak_congestion"] = dp["congestion_num"] if dp.get("congestion_num") is not None else -1.0
        out["mb_diurnal_ok"] = 1.0
        if dn and dn.get("duration_s"):
            out["mb_diurnal_slowdown"] = dp["duration_s"] / max(dn["duration_s"], 1.0)
    return out


if __name__ == "__main__":
    # 自检: speed_to_flow 逻辑 + 一个真实 Directions 调用
    print("speed_to_flow tests:")
    for cls in ["motorway", "secondary", "residential"]:
        for obs, mx in [(90, 100), (40, 60), (15, 50)]:
            print(f"  {cls:12s} obs={obs} max={mx} -> {speed_to_flow(obs, mx, cls):.0f} vph")
    print("\nlive directions test (Melbourne):")
    d = directions([(144.9876, -37.8074), (144.9881, -37.8020)])
    print(" ", d)
    print("point_features:", point_features(
        [(144.9876, -37.8074), (144.9881, -37.8020)], "secondary", 50.0))
