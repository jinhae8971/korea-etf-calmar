#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
backtest.py — "순위 급등이 이후 수익률과 관계가 있는가"를 측정한다.

이 시스템은 관측기로 설계됐다. 그렇다면 관측이 매매 신호로 오독되지 않도록
**실제로 예측력이 있는지 없는지를 직접 재서 대시보드에 적어야 한다.**
결과가 "무관"이나 "역상관"으로 나와도 그대로 표기한다 — 그게 이 측정의 목적이다.

방법
  각 스냅샷 t에서 24시간 전 대비 순위 상승이 임계 이상인 종목을 픽으로 잡고,
  t 이후 forward_hours 뒤의 시총 변화율을 구한다.
  같은 구간 유니버스 **중앙값** 변화율을 벤치마크로 빼서 초과수익을 낸다(평균은 극단치가 지배).
  픽이 없거나 표본이 부족하면 판정을 유보한다 — 억지로 결론 내지 않는다.
"""

import json
import math
from datetime import datetime


def _parse(ts):
    try:
        return datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None


def _median(vals):
    if not vals:
        return None
    s = sorted(vals)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def _at(history, target, tol_hours):
    """target 시각에 가장 가까운 스냅샷(허용 오차 내)."""
    best, gap = None, None
    for snap in history:
        t = _parse(snap.get("ts"))
        if t is None:
            continue
        d = abs((t - target).total_seconds())
        if d <= tol_hours * 3600 and (gap is None or d < gap):
            best, gap = snap, d
    return best


def run(history, rank_threshold=5, lookback_hours=24, forward_hours=24, tol_hours=3.0, min_picks=10):
    from datetime import timedelta

    picks, bench_rows = [], []
    for snap in history:
        t = _parse(snap.get("ts"))
        if t is None:
            continue
        past = _at(history, t - timedelta(hours=lookback_hours), tol_hours)
        fwd = _at(history, t + timedelta(hours=forward_hours), tol_hours)
        if not past or not fwd or fwd is snap:
            continue

        now_mc = snap.get("mcap") or {}
        fwd_mc = fwd.get("mcap") or {}
        universe = []
        for addr, mc in now_mc.items():
            f = fwd_mc.get(addr)
            if mc and f:
                universe.append((addr, (f - mc) / mc * 100.0))
        if len(universe) < 8:
            continue
        bench = _median([r for _, r in universe])
        bench_rows.append(bench)
        ret_by_addr = dict(universe)

        for addr, rank in (snap.get("rank") or {}).items():
            old = (past.get("rank") or {}).get(addr)
            if not old or addr not in ret_by_addr:
                continue
            if (old - rank) >= rank_threshold:
                picks.append({
                    "ts": snap["ts"], "addr": addr,
                    "symbol": (snap.get("symbol") or {}).get(addr, "?"),
                    "d_rank": old - rank,
                    "ret": ret_by_addr[addr],
                    "excess": ret_by_addr[addr] - bench,
                })

    result = {
        "rank_threshold": rank_threshold,
        "lookback_hours": lookback_hours,
        "forward_hours": forward_hours,
        "n_picks": len(picks),
        "snapshots_used": len(bench_rows),
    }

    if len(picks) < min_picks:
        result["verdict"] = "INSUFFICIENT"
        result["note"] = ("표본 %d건(최소 %d건) — 판정 유보. 스냅샷이 더 쌓여야 한다."
                          % (len(picks), min_picks))
        return result

    excess = [p["excess"] for p in picks]
    raw = [p["ret"] for p in picks]
    mean_ex = sum(excess) / len(excess)
    var = sum((x - mean_ex) ** 2 for x in excess) / max(1, len(excess) - 1)
    se = math.sqrt(var / len(excess))
    lo, hi = mean_ex - 1.96 * se, mean_ex + 1.96 * se
    win = sum(1 for x in excess if x > 0) / len(excess) * 100.0

    if lo > 0:
        verdict = "POSITIVE"
        note = "순위 급등 이후 초과수익이 통계적으로 0보다 크다. 단, 관측 구간이 짧다."
    elif hi < 0:
        verdict = "NEGATIVE"
        note = "순위 급등 이후 초과수익이 0보다 작다 — 이미 오른 뒤에 잡히는 후행 지표다. 매수 신호로 쓰면 안 된다."
    else:
        verdict = "NO_EDGE"
        note = "초과수익 신뢰구간이 0을 포함한다 — 예측력이 확인되지 않는다. 관측 지표로만 쓸 것."

    result.update({
        "median_excess_pct": round(_median(excess), 2),
        "mean_excess_pct": round(mean_ex, 2),
        "ci95_low": round(lo, 2), "ci95_high": round(hi, 2),
        "median_raw_pct": round(_median(raw), 2),
        "win_rate_pct": round(win, 1),
        "verdict": verdict, "note": note,
    })
    return result


def render_line(result):
    result = result or {}
    if result.get("verdict") in (None, "INSUFFICIENT"):
        return "검증: 표본 %d건 — 판정 유보" % result.get("n_picks", 0)
    return ("검증: %s · 픽 %d건 · %dh 초과수익 중앙 %+.1f%%p (95%% %+.1f~%+.1f) · 승률 %.0f%%" % (
        result["verdict"], result["n_picks"], result["forward_hours"],
        result["median_excess_pct"], result["ci95_low"], result["ci95_high"],
        result["win_rate_pct"]))


if __name__ == "__main__":
    import sys
    with open(sys.argv[1], encoding="utf-8") as fh:
        hist = json.load(fh)
    print(json.dumps(run(hist), ensure_ascii=False, indent=1))
