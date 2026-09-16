# -*- coding: utf-8 -*-
"""
ゴルフ天気アプリ用：天気予報の的中記録スクリプト
GitHub Actions から毎日実行され、前日までの
「各モデルの予報（1〜3日前に出したもの）」と「アメダスの実測」を比べて
data/records.json に追記します。標準ライブラリだけで動きます。
"""
import json, math, os, sys, time, urllib.request
from datetime import datetime, timedelta, timezone

JST = timezone(timedelta(hours=9))
COURSES = [
    {"id": "sobu", "name": "総武カントリークラブ 北コース", "lat": 35.7856, "lon": 140.1785},
    {"id": "koshigaya", "name": "KOSHIGAYA GOLF CLUB", "lat": 35.8895, "lon": 139.8859},
]
MODELS = ["jma_seamless", "ecmwf_ifs", "gfs_seamless", "icon_seamless"]
LEADS = [1, 2, 3]
HOURS = range(7, 18)          # 7〜17時（ゴルフの時間帯）
FC_RAIN = 0.3                  # 予報で「雨あり」とみなす 1時間の雨量(mm)
OBS_RAIN = 0.5                 # 実測で「雨あり」とみなす 1時間の雨量(mm)
BACK_DAYS = 8                  # さかのぼって埋める日数（アメダスの公開は約10日分）
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "records.json")


def get_json(url, tries=3):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "golf-weather-recorder"})
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:  # noqa
            last = e
            time.sleep(3 * (i + 1))
    raise last


def nearest_station(table, lat, lon):
    best, bd = None, 1e9
    for code, s in table.items():
        el = s.get("elems", "")
        if len(el) < 2 or el[0] != "1" or el[1] != "1":
            continue
        la = s["lat"][0] + s["lat"][1] / 60
        lo = s["lon"][0] + s["lon"][1] / 60
        d = math.hypot(la - lat, (lo - lon) * math.cos(math.radians(lat)))
        if d < bd:
            bd, best = d, {"code": code, "name": s.get("kjName", code), "km": round(d * 111, 1)}
    return best


def amedas_day(code, day):
    """day: 'YYYY-MM-DD' -> {hour: {"t":..,"p":..}}"""
    key = day.replace("-", "")
    out = {}
    for hh in ["06", "09", "12", "15"]:
        try:
            j = get_json(f"https://www.jma.go.jp/bosai/amedas/data/point/{code}/{key}_{hh}.json")
        except Exception:
            continue
        for t, e in j.items():
            if t[10:14] != "0000":
                continue
            h = int(t[8:10])
            tv = e.get("temp", [None])[0] if e.get("temp") else None
            pv = e.get("precipitation1h", [None])[0] if e.get("precipitation1h") else None
            out[h] = {"t": tv, "p": pv}
    return out


def previous_runs(course, model):
    vars_ = []
    for L in LEADS:
        vars_ += [f"temperature_2m_previous_day{L}", f"precipitation_previous_day{L}"]
    url = ("https://previous-runs-api.open-meteo.com/v1/forecast"
           f"?latitude={course['lat']}&longitude={course['lon']}"
           f"&hourly={','.join(vars_)}&timezone=Asia%2FTokyo&past_days={BACK_DAYS + 2}&forecast_days=1"
           f"&models={model}")
    return get_json(url)["hourly"]


def score_day(day, obs, hourly):
    idx = {t: i for i, t in enumerate(hourly["time"])}
    res = {}
    for L in LEADS:
        tk, pk = f"temperature_2m_previous_day{L}", f"precipitation_previous_day{L}"
        s = {"n": 0, "hit": 0, "rain_obs": 0, "rain_caught": 0, "fc_rain": 0, "false_alarm": 0,
             "t_n": 0, "t_err": 0.0, "p_err": 0.0, "fc_total": 0.0}
        for h in HOURS:
            i = idx.get(f"{day}T{h:02d}:00")
            o = obs.get(h)
            if i is None or o is None:
                continue
            ft = hourly.get(tk, [None] * (i + 1))[i]
            fp = hourly.get(pk, [None] * (i + 1))[i]
            if o["t"] is not None and ft is not None:
                s["t_n"] += 1
                s["t_err"] += abs(ft - o["t"])
            if o["p"] is not None and fp is not None:
                s["n"] += 1
                orain, frain = o["p"] >= OBS_RAIN, fp >= FC_RAIN
                s["hit"] += int(orain == frain)
                s["rain_obs"] += int(orain)
                s["rain_caught"] += int(orain and frain)
                s["fc_rain"] += int(frain)
                s["false_alarm"] += int(frain and not orain)
                s["p_err"] += abs(fp - o["p"])
                s["fc_total"] += fp
        if s["n"] or s["t_n"]:
            s["t_err"] = round(s["t_err"], 2)
            s["p_err"] = round(s["p_err"], 2)
            s["fc_total"] = round(s["fc_total"], 1)
            res[str(L)] = s
    return res


def main():
    if os.path.exists(OUT):
        with open(OUT, encoding="utf-8") as f:
            db = json.load(f)
    else:
        db = {"version": 1, "courses": {}}
    db.setdefault("courses", {})
    db["settings"] = {"hours": [HOURS.start, HOURS.stop - 1], "fc_rain_mm": FC_RAIN,
                      "obs_rain_mm": OBS_RAIN, "models": MODELS, "leads": LEADS}

    today = datetime.now(JST).date()
    days = [(today - timedelta(days=k)).isoformat() for k in range(BACK_DAYS, 0, -1)]
    table = get_json("https://www.jma.go.jp/bosai/amedas/const/amedastable.json")
    added = 0

    for c in COURSES:
        cdb = db["courses"].setdefault(c["id"], {"days": {}})
        cdb["name"] = c["name"]
        st = nearest_station(table, c["lat"], c["lon"])
        cdb["station"] = st
        todo = [d for d in days if not (d in cdb["days"] and cdb["days"][d].get("complete"))]
        if not todo:
            continue
        runs = {}
        for m in MODELS:
            try:
                runs[m] = previous_runs(c, m)
            except Exception as e:
                print(f"[warn] {c['id']} {m}: {e}", file=sys.stderr)
        for d in todo:
            obs = amedas_day(st["code"], d)
            got = [h for h in HOURS if h in obs and obs[h]["p"] is not None]
            if len(got) < 8:
                print(f"[skip] {c['id']} {d}: 実測が不足 ({len(got)}時間)")
                continue
            rain_vals = [obs[h]["p"] for h in got]
            temps = [obs[h]["t"] for h in HOURS if h in obs and obs[h]["t"] is not None]
            rec = {
                "obs": {"hours": len(got), "rain_mm": round(sum(rain_vals), 1),
                        "rain_hours": sum(1 for v in rain_vals if v >= OBS_RAIN),
                        "tmin": min(temps) if temps else None, "tmax": max(temps) if temps else None},
                "models": {},
            }
            for m, H in runs.items():
                sc = score_day(d, obs, H)
                if sc:
                    rec["models"][m] = sc
            rec["complete"] = len(rec["models"]) == len(MODELS) and len(got) >= len(HOURS)
            cdb["days"][d] = rec
            added += 1
            print(f"[ok] {c['id']} {d}: 実測雨量 {rec['obs']['rain_mm']}mm, モデル {len(rec['models'])}")

    db["updated"] = datetime.now(JST).strftime("%Y-%m-%d %H:%M")
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    print(f"done: {added} 日分を記録/更新")


if __name__ == "__main__":
    main()
