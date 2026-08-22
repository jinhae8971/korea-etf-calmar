# -*- coding: utf-8 -*-
"""
라이브 추적 (forward tracking)
=============================
백테스트는 과거 캔들로 '차트축'만 검증할 수 있다. 시총·유동성·홀더 같은 스냅샷 지표는
과거값이 존재하지 않기 때문이다. 그래서 **실제로 승격된 후보의 이후 성과**를 배포 시점부터
기록해 둔다. 이것만이 라이브 점수(수급축 포함) 전체를 검증하는 유일한 증거다.

규칙
  - 승격(상위10 · 2일 유지)된 종목만 기록한다. 사후에 고르지 않는다.
  - 성과는 항상 **같은 날 유니버스 중앙값 대비 초과**로 본다. 알파 유니버스 자체가
    시간이 갈수록 빠지는 시장이라, 절대수익만 보면 판단을 그르친다.
  - 표본이 20건 미만이면 수치를 내지 않고 '표본 부족'이라고 쓴다.
"""

import os

HORIZONS = [3, 7, 14]
MIN_SAMPLE = 20
KEEP_ENTRIES = 400


def _median(xs):
    s = sorted(xs)
    n = len(s)
    if not n:
        return None
    m = n // 2
    return s[m] if n % 2 else (s[m - 1] + s[m]) / 2.0


def _price_map(hist, date):
    for h in hist:
        if h["date"] == date:
            return {k: v.get("p", 0.0) for k, v in h.get("tokens", {}).items()}
    return {}


def universe_return(hist, d0, d1):
    """d0 → d1 유니버스 중앙 수익률. 두 날짜에 모두 존재하는 종목만 쓴다."""
    p0, p1 = _price_map(hist, d0), _price_map(hist, d1)
    rets = []
    for aid, a in p0.items():
        b = p1.get(aid)
        if a and b:
            rets.append(b / a - 1.0)
    return (_median(rets), len(rets)) if rets else (None, 0)


def _days_between(hist, d0, d1):
    dates = [h["date"] for h in hist]
    if d0 not in dates or d1 not in dates:
        return None
    return dates.index(d1) - dates.index(d0)


def update(track, promoted_rows, hist, today):
    """오늘 승격분을 기록하고, 만기가 된 기존 기록의 실현 성과를 확정한다."""
    track.setdefault("entries", [])
    track.setdefault("results", {str(h): [] for h in HORIZONS})

    known = {(e["d"], e["aid"]) for e in track["entries"]}
    for r in promoted_rows:
        key = (today, r["alpha_id"])
        if key in known:
            continue
        track["entries"].append({
            "d": today, "aid": r["alpha_id"], "sym": r["symbol"],
            "p0": r["s"]["price"], "score": round(r["score"], 3),
            "rank": r["rank"], "streak": r["streak"], "done": [],
        })

    px_now = _price_map(hist, today)
    for e in track["entries"]:
        age = _days_between(hist, e["d"], today)
        if age is None:
            continue
        for h in HORIZONS:
            if h in e["done"] or age < h:
                continue
            p1 = px_now.get(e["aid"])
            if not p1 or not e.get("p0"):
                continue
            uni, n_uni = universe_return(hist, e["d"], today)
            if uni is None:
                continue
            ret = p1 / e["p0"] - 1.0
            track["results"][str(h)].append({
                "d": e["d"], "sym": e["sym"], "age": age,
                "ret": round(ret, 4), "uni": round(uni, 4),
                "excess": round(ret - uni, 4), "n_uni": n_uni,
            })
            e["done"].append(h)

    track["entries"] = track["entries"][-KEEP_ENTRIES:]
    for h in HORIZONS:
        track["results"][str(h)] = track["results"][str(h)][-KEEP_ENTRIES:]
    return track


def summarize(track):
    out = {"open_entries": len(track.get("entries", [])), "horizons": {}}
    for h in HORIZONS:
        rs = track.get("results", {}).get(str(h), [])
        if len(rs) < MIN_SAMPLE:
            out["horizons"][str(h)] = {"n": len(rs), "status": "표본 부족",
                                       "need": MIN_SAMPLE - len(rs)}
            continue
        ex = [r["excess"] for r in rs]
        ab = [r["ret"] for r in rs]
        out["horizons"][str(h)] = {
            "n": len(rs), "status": "집계",
            "excess_median": _median(ex),
            "abs_median": _median(ab),
            "win_rate": sum(1 for v in ex if v > 0) / len(ex),
        }
    return out


def load(path):
    import json
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:                     # noqa: BLE001
        return {"entries": [], "results": {str(h): [] for h in HORIZONS}}
