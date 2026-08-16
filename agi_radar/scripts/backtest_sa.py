#!/usr/bin/env python3
"""SA 붕괴 구간 사후검증.

2026년 7월 말 Situational Awareness 강제청산 이전에
① 헤지 유효성 경보와 ② 혼잡도 경보가 며칠 먼저 켜졌는지 확인한다.
경보가 사후에만 켜졌다면 지표 설계가 틀린 것이므로 임계값을 재보정해야 한다.

사용: python scripts/backtest_sa.py [시작일 YYYY-MM-DD] [종료일 YYYY-MM-DD]
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from core import metrics, prices  # noqa: E402

EVENT = "2026-07-30"  # CNBC 보도: 공개주식 전량 매각


def main() -> int:
    start = sys.argv[1] if len(sys.argv) > 1 else "2026-05-01"
    end = sys.argv[2] if len(sys.argv) > 2 else "2026-08-15"

    with open(ROOT / "config" / "thesis_graph.yaml", encoding="utf-8") as handle:
        cfg = yaml.safe_load(handle)

    symbols = [t for n in cfg["nodes"] for t in n["tickers"]] + cfg["benchmarks"]
    series, status = prices.collect(symbols, str(ROOT / "data" / "prices.json"),
                                    cfg["meta"]["lookback_days"])
    print(f"[data] {status}\n")

    benchmark = cfg["meta"]["benchmark"]
    dates = sorted(series.get(benchmark, {}))
    long_t = [t for n in cfg["nodes"] if n["role"] == "long" for t in n["tickers"]]
    short_t = [t for n in cfg["nodes"] if n["role"] == "short" for t in n["tickers"]]

    th = cfg["thresholds"]
    first_hedge = first_broken = first_crowd = first_alert = None
    rows = []

    for i, day in enumerate(dates):
        if day < start or day > end or i < 130:
            continue
        window = dates[: i + 1]
        long_r = metrics.basket_returns(series, long_t, window)
        short_r = metrics.basket_returns(series, short_t, window)
        bench_r = metrics.to_returns(series[benchmark], window)
        hedge = metrics.hedge_efficacy(long_r, short_r, 20, th["hedge"])
        intra = metrics.avg_pairwise_corr(series, long_t, window, 20)
        crowd = metrics.crowding_index(long_r, bench_r, intra, th["crowding"])

        if first_hedge is None and hedge["status"] in ("WATCH", "BROKEN"):
            first_hedge = (day, hedge["status"])
        if first_broken is None and hedge["status"] == "BROKEN":
            first_broken = (day, "BROKEN")
        if first_crowd is None and crowd["level"] in ("WATCH", "ALERT"):
            first_crowd = (day, crowd["level"])
        if first_alert is None and crowd["level"] == "ALERT":
            first_alert = (day, "ALERT")

        rows.append((day, hedge["status"], hedge["spread_vol_ratio"], hedge["corr20"],
                     crowd["score"], crowd["level"]))

    print(f"{'날짜':<12}{'헤지':<9}{'변동성비':>9}{'상관':>8}{'혼잡도':>8}  등급")
    for day, hstat, ratio, corr, cscore, clevel in rows[::3]:
        print(f"{day:<12}{hstat:<9}{ratio if ratio is not None else '-':>9}"
              f"{corr if corr is not None else '-':>8}{cscore if cscore is not None else '-':>8}  {clevel}")

    print(f"\n=== 검증 결과 (기준 사건 {EVENT}) ===")
    broken_days = sum(1 for r in rows if r[1] == "BROKEN")
    print(f"발동 빈도: 기간 {len(rows)}거래일 중 BROKEN {broken_days}일 "
          f"({broken_days / len(rows):.0%}) — 상시 발동이면 신호로서 무효")
    for label, hit in (("헤지 WATCH(1차)", first_hedge), ("헤지 BROKEN(확정)", first_broken),
                       ("혼잡도 WATCH", first_crowd), ("혼잡도 ALERT", first_alert)):
        if not hit:
            print(f"{label}: 기간 내 미발동 — 임계값 재보정 필요")
            continue
        day, level = hit
        lead = _business_days(dates, day, EVENT)
        verdict = f"사건 {lead}거래일 전 선행" if lead > 0 else f"사건 {abs(lead)}거래일 후 (후행)"
        print(f"{label}: {day} [{level}] → {verdict}")
    return 0


def _business_days(dates: list[str], start: str, end: str) -> int:
    try:
        return dates.index(end) - dates.index(start)
    except ValueError:
        window = [d for d in dates if start <= d <= end]
        return max(0, len(window) - 1)


if __name__ == "__main__":
    raise SystemExit(main())
