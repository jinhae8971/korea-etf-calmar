"""L2/L4 지표 엔진.

전부 순수 함수다 — 네트워크 접근이 없으므로 단위테스트로 완전히 고정된다.
핵심은 세 가지:
  1) hedge_efficacy : 롱/숏 페어가 리스크를 줄이고 있는가, 늘리고 있는가
  2) crowding       : 이 논지에 시장이 얼마나 한쪽으로 몰려 있는가
  3) node_strength  : 병목이 사슬의 어디로 이동했는가
"""

from __future__ import annotations

import math

Series = dict[str, float]


# --------------------------------------------------------------------------- #
# 기초 통계
# --------------------------------------------------------------------------- #
def mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def stdev(xs: list[float]) -> float:
    if len(xs) < 2:
        return 0.0
    mu = mean(xs)
    return math.sqrt(sum((x - mu) ** 2 for x in xs) / (len(xs) - 1))


def correlation(xs: list[float], ys: list[float]) -> float:
    n = min(len(xs), len(ys))
    if n < 3:
        return 0.0
    xs, ys = xs[-n:], ys[-n:]
    sx, sy = stdev(xs), stdev(ys)
    if sx == 0 or sy == 0:
        return 0.0
    mx, my = mean(xs), mean(ys)
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (n - 1)
    return max(-1.0, min(1.0, cov / (sx * sy)))


def beta(asset: list[float], bench: list[float]) -> float:
    n = min(len(asset), len(bench))
    if n < 3:
        return 0.0
    asset, bench = asset[-n:], bench[-n:]
    var = stdev(bench) ** 2
    if var == 0:
        return 0.0
    ma, mb = mean(asset), mean(bench)
    cov = sum((a - ma) * (b - mb) for a, b in zip(asset, bench)) / (n - 1)
    return cov / var


def zscore(value: float, history: list[float]) -> float:
    sd = stdev(history)
    if sd == 0:
        return 0.0
    return (value - mean(history)) / sd


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


# --------------------------------------------------------------------------- #
# 정렬 / 수익률
# --------------------------------------------------------------------------- #
def common_dates(series_map: dict[str, Series], symbols: list[str]) -> list[str]:
    sets = [set(series_map[s]) for s in symbols if series_map.get(s)]
    if not sets:
        return []
    return sorted(set.intersection(*sets))


def to_returns(series: Series, dates: list[str]) -> list[float]:
    out: list[float] = []
    for prev, curr in zip(dates, dates[1:]):
        p0, p1 = series.get(prev), series.get(curr)
        if not p0 or not p1 or p0 <= 0:
            out.append(0.0)
        else:
            out.append(p1 / p0 - 1.0)
    return out


def basket_returns(
    series_map: dict[str, Series], tickers: list[str], dates: list[str]
) -> list[float]:
    """동일가중 바스켓 일간수익률. 데이터 없는 종목은 자동 제외."""
    valid = [t for t in tickers if series_map.get(t)]
    if not valid:
        return [0.0] * max(0, len(dates) - 1)
    legs = [to_returns(series_map[t], dates) for t in valid]
    return [mean([leg[i] for leg in legs]) for i in range(len(legs[0]))]


def cumulative(rets: list[float]) -> float:
    total = 1.0
    for r in rets:
        total *= 1.0 + r
    return total - 1.0


# --------------------------------------------------------------------------- #
# L4-1 헤지 유효성 — SA 가 죽은 지점
# --------------------------------------------------------------------------- #
def hedge_efficacy(
    long_rets: list[float], short_rets: list[float], window: int = 20, thresholds: dict | None = None
) -> dict:
    """롱/숏 페어의 헤지가 실제로 작동 중인지 계량화.

    [실증 재보정 2026-08] 최초 설계는 spread_vol_ratio >= 1.0 을 붕괴로 봤으나,
    ratio = sqrt(1 + (sd_S/sd_L)^2 - 2*rho*sd_S/sd_L) 이므로 rho 가 0 근처면
    ratio 는 구조적으로 항상 1을 넘는다. 실제 데이터에서 전 구간 BROKEN 이 떠
    신호가 무효했다. 절대 임계값을 폐기하고 아래 셋으로 교체한다:

      1) both_legs_lose : 롱이 빠지는 동안 숏 다리가 오르는가 (SA 를 죽인 그 패턴)
      2) corr20         : 롱-숏 상관. 깊은 음수 = 이중 노출 구조
      3) ratio_z        : 변동성비를 자기 1년 이력 대비 z 로 상대화

    BROKEN 은 1)과 2)가 동시에 성립할 때만 부여한다. 단독 조건은 WATCH 까지만.
    base_rate 로 이 경보가 지난 1년 중 몇 %의 날에 켜졌는지도 함께 보고한다.
    """
    th = thresholds or {}
    corr_watch = th.get("corr20_watch", -0.20)
    corr_broken = th.get("corr20_broken", -0.40)
    ratio_z_watch = th.get("ratio_z_watch", 1.0)

    n = min(len(long_rets), len(short_rets))
    if n < window + 1:
        return {"status": "NO_DATA", "corr20": None, "spread_vol_ratio": None,
                "spread_return": None, "history": [], "base_rate": None}

    def snapshot(end: int) -> dict:
        lw, sw = long_rets[end - window:end], short_rets[end - window:end]
        sd_long = stdev(lw)
        return {
            "corr": correlation(lw, sw),
            "ratio": (stdev([a - b for a, b in zip(lw, sw)]) / sd_long) if sd_long > 0 else None,
            "long_ret": cumulative(lw),
            "short_ret": cumulative(sw),
        }

    history = [dict(snapshot(end), i=end) for end in range(window, n + 1)]
    current = history[-1]
    ratio_hist = [h["ratio"] for h in history[-252:] if h["ratio"] is not None]
    ratio_z = zscore(current["ratio"], ratio_hist) if current["ratio"] is not None else 0.0

    def classify(snap: dict, rz: float) -> str:
        if snap["ratio"] is None:
            return "NO_DATA"
        both_lose = snap["long_ret"] < 0 and snap["short_ret"] > 0
        if both_lose and snap["corr"] <= corr_broken:
            return "BROKEN"
        if both_lose or snap["corr"] <= corr_watch or rz >= ratio_z_watch:
            return "WATCH"
        return "OK"

    status = classify(current, ratio_z)

    # 베이스레이트 — 경보가 얼마나 흔한지 알려야 신호로 읽을 수 있다
    recent = history[-252:]
    broken_days = sum(1 for h in recent if classify(h, 0.0) == "BROKEN")
    base_rate = round(broken_days / len(recent), 3) if recent else None

    return {
        "status": status,
        "corr20": round(current["corr"], 4),
        "spread_vol_ratio": round(current["ratio"], 4) if current["ratio"] is not None else None,
        "ratio_z": round(ratio_z, 2),
        "both_legs_lose": bool(current["long_ret"] < 0 and current["short_ret"] > 0),
        "spread_return": round(current["long_ret"] - current["short_ret"], 4),
        "long_return_20d": round(current["long_ret"], 4),
        "short_return_20d": round(current["short_ret"], 4),
        "base_rate_broken_1y": base_rate,
        "history": [
            {"i": h["i"], "corr": round(h["corr"], 4),
             "ratio": round(h["ratio"], 4) if h["ratio"] is not None else None}
            for h in history[-120:]
        ],
    }


# --------------------------------------------------------------------------- #
# L4-2 혼잡도
# --------------------------------------------------------------------------- #
def avg_pairwise_corr(
    series_map: dict[str, Series], tickers: list[str], dates: list[str], window: int = 20
) -> float:
    valid = [t for t in tickers if series_map.get(t)]
    if len(valid) < 2:
        return 0.0
    legs = [to_returns(series_map[t], dates)[-window:] for t in valid]
    pairs = [
        correlation(legs[i], legs[j])
        for i in range(len(legs))
        for j in range(i + 1, len(legs))
    ]
    return mean(pairs)


def drawdown_from_peak(rets: list[float], lookback: int = 252) -> float:
    window = rets[-lookback:]
    equity, peak, level = [], 1.0, 1.0
    for r in window:
        level *= 1.0 + r
        peak = max(peak, level)
        equity.append(level)
    if not equity or peak == 0:
        return 0.0
    return equity[-1] / peak - 1.0


def crowding_index(
    long_rets: list[float],
    bench_rets: list[float],
    intra_corr: float,
    thresholds: dict | None = None,
) -> dict:
    """가격 기반 혼잡도 대리지표 (0~100).

    13F 중복도/숏이자 같은 포지셔닝 원천 데이터는 무료 경로가 없어
    아래 네 축의 가격 대리지표로 합성한다. 단정이 아니라 정황 지표다.
    """
    th = thresholds or {}
    if len(long_rets) < 70:
        return {"score": None, "level": "NO_DATA", "components": {}}

    mom60 = cumulative(long_rets[-60:])
    mom_hist = [cumulative(long_rets[i - 60:i]) for i in range(60, len(long_rets))]
    mom_z = zscore(mom60, mom_hist) if len(mom_hist) >= 20 else 0.0

    b = beta(long_rets[-60:], bench_rets[-60:])
    dd = drawdown_from_peak(long_rets)

    components = {
        # 한 방향으로 같이 움직일수록 = 한 팩터 트레이드가 됐다는 뜻
        "intra_correlation": round(clamp((intra_corr - 0.3) / 0.5 * 100), 1),
        # 모멘텀이 자기 이력 대비 얼마나 늘어나 있는가
        "momentum_extension": round(clamp((mom_z + 1.0) / 3.0 * 100), 1),
        # 시장 베타 상승 = 레버리지/집중의 가격 흔적
        "beta_to_market": round(clamp((b - 0.8) / 1.4 * 100), 1),
        # 고점 대비 낙폭이 크면 이미 청산이 진행 중 (혼잡 해소 국면)
        "unwind_progress": round(clamp((1 + dd / 0.5) * 100), 1),
    }
    score = _crowd_score(components)

    # [실증 재보정 2026-08] 고정 임계값(65/80)은 ALERT 가 한 번도 켜지지 않았다.
    # 점수의 절대 수준은 바스켓 구성에 따라 달라지므로, 자기 1년 이력의
    # 분위수로 등급을 매긴다 → 경보 빈도가 구조적으로 고정된다.
    hist_scores = _score_history(long_rets, bench_rets, intra_corr)
    watch_p = th.get("watch_percentile", 0.70)
    alert_p = th.get("alert_percentile", 0.90)
    watch = percentile(hist_scores, watch_p) if hist_scores else 65.0
    alert = percentile(hist_scores, alert_p) if hist_scores else 80.0
    level = "ALERT" if score >= alert else "WATCH" if score >= watch else "NORMAL"
    return {
        "score": round(score, 1),
        "level": level,
        "components": components,
        "cutoffs": {"watch": round(watch, 1), "alert": round(alert, 1),
                    "basis": f"자기 1년 이력 {int(watch_p * 100)}/{int(alert_p * 100)} 분위수"},
        "raw": {"mom60": round(mom60, 4), "mom_z": round(mom_z, 2),
                "beta60": round(b, 3), "drawdown_1y": round(dd, 4),
                "intra_corr20": round(intra_corr, 4)},
    }


def _crowd_score(components: dict) -> float:
    return (
        components["intra_correlation"] * 0.35
        + components["momentum_extension"] * 0.30
        + components["beta_to_market"] * 0.20
        + components["unwind_progress"] * 0.15
    )


def percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return ordered[idx]


def _score_history(long_rets: list[float], bench_rets: list[float], intra_corr: float) -> list[float]:
    """과거 각 시점의 혼잡도 점수를 재계산해 분위수 기준을 만든다.

    intra_correlation 은 시점별 재계산 비용이 커서 현재값으로 고정한다
    (등급 기준에 상수로 들어가므로 순위 비교에는 영향이 없다).
    """
    out: list[float] = []
    fixed_intra = round(clamp((intra_corr - 0.3) / 0.5 * 100), 1)
    end = len(long_rets)
    for i in range(130, end + 1):
        window = long_rets[:i]
        mom60 = cumulative(window[-60:])
        mom_hist = [cumulative(window[j - 60:j]) for j in range(60, len(window))]
        if len(mom_hist) < 20:
            continue
        out.append(
            _crowd_score(
                {
                    "intra_correlation": fixed_intra,
                    "momentum_extension": round(clamp((zscore(mom60, mom_hist) + 1.0) / 3.0 * 100), 1),
                    "beta_to_market": round(clamp((beta(window[-60:], bench_rets[:i][-60:]) - 0.8) / 1.4 * 100), 1),
                    "unwind_progress": round(clamp((1 + drawdown_from_peak(window) / 0.5) * 100), 1),
                }
            )
        )
    return out


# --------------------------------------------------------------------------- #
# L1/L2 노드 강도 + 병목 이동
# --------------------------------------------------------------------------- #
def node_strength(
    series_map: dict[str, Series], nodes: list[dict], dates: list[str], benchmark: str
) -> list[dict]:
    bench = to_returns(series_map.get(benchmark, {}), dates)
    out: list[dict] = []
    for node in nodes:
        rets = basket_returns(series_map, node["tickers"], dates)
        if len(rets) < 65 or len(bench) < 65:
            continue
        rs20 = cumulative(rets[-20:]) - cumulative(bench[-20:])
        rs60 = cumulative(rets[-60:]) - cumulative(bench[-60:])
        prev20 = cumulative(rets[-40:-20]) - cumulative(bench[-40:-20])
        out.append(
            {
                "id": node["id"],
                "label": node["label"],
                "role": node["role"],
                "stage": node["stage"],
                "coverage": sum(1 for t in node["tickers"] if series_map.get(t)),
                "universe": len(node["tickers"]),
                "rs20": round(rs20, 4),
                "rs60": round(rs60, 4),
                "rs20_prev": round(prev20, 4),
                "ret20": round(cumulative(rets[-20:]), 4),
                "ret60": round(cumulative(rets[-60:]), 4),
            }
        )
    ranked = sorted(out, key=lambda n: n["rs20"], reverse=True)
    prev_ranked = sorted(out, key=lambda n: n["rs20_prev"], reverse=True)
    prev_rank = {n["id"]: i + 1 for i, n in enumerate(prev_ranked)}
    for i, node in enumerate(ranked):
        node["rank"] = i + 1
        node["rank_prev"] = prev_rank[node["id"]]
        node["rank_delta"] = prev_rank[node["id"]] - (i + 1)
    # 롱 노드끼리의 순위를 따로 매긴다 — 브리프에서 번호가 건너뛰어 보이지 않도록
    for j, node in enumerate([n for n in ranked if n["role"] == "long"]):
        node["long_rank"] = j + 1
    return ranked


def breadth(nodes: list[dict]) -> dict:
    longs = [n for n in nodes if n["role"] == "long"]
    if not longs:
        return {"ratio": None, "leading": 0, "total": 0}
    leading = sum(1 for n in longs if n["rs60"] > 0)
    return {"ratio": round(leading / len(longs), 3), "leading": leading, "total": len(longs)}


def bottleneck_shift(nodes: list[dict]) -> dict | None:
    """병목 이동 = 롱 노드 1위가 바뀌었는가."""
    longs = [n for n in nodes if n["role"] == "long"]
    if not longs:
        return None
    top = longs[0]
    prev_top = min(longs, key=lambda n: n["rank_prev"])
    if top["id"] == prev_top["id"]:
        return {"shifted": False, "current": top["label"], "previous": prev_top["label"]}
    return {"shifted": True, "current": top["label"], "previous": prev_top["label"]}
