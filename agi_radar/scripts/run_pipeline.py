#!/usr/bin/env python3
"""agi-thesis-radar 메인 파이프라인.

L1 논지그래프 → L2 증거수집 → L4 리스크 → 규칙판정 → L3 토론 → L5 산출
텔레그램 발송은 하지 않는다. latest.json 의 message 필드를 릴레이 레포가 읽어 보낸다.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from core import funding as funding_mod  # noqa: E402
from core import history, metrics, prices, render, verdict_rules, weekly  # noqa: E402
from orchestrator.debate import run_debate  # noqa: E402

KST = timezone(timedelta(hours=9))
MODEL = os.environ.get("DEBATE_MODEL", "claude-sonnet-4-6")
MODES = ("daily", "weekly", "watchlist", "auto")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def load_config() -> dict:
    with open(ROOT / "config" / "thesis_graph.yaml", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def all_symbols(cfg: dict) -> list[str]:
    symbols: list[str] = []
    for node in cfg["nodes"]:
        symbols += node["tickers"]
    symbols += cfg.get("benchmarks", [])
    seen, out = set(), []
    for symbol in symbols:
        if symbol not in seen:
            seen.add(symbol)
            out.append(symbol)
    return out


def build_report(cfg: dict, series: dict, status: dict, target_date: str) -> dict:
    benchmark = cfg["meta"]["benchmark"]
    thresholds = cfg["thresholds"]

    dates = metrics.common_dates(series, [benchmark] + [
        t for n in cfg["nodes"] for t in n["tickers"] if series.get(t)
    ][:1] or [benchmark])
    # 벤치마크 기준 거래일 축을 쓰되, 종목별 결측은 basket_returns 가 흡수한다
    dates = sorted(series.get(benchmark, {}))
    if len(dates) < 80:
        raise SystemExit("벤치마크 시계열이 부족합니다 (최소 80거래일 필요)")

    long_tickers = [t for n in cfg["nodes"] if n["role"] == "long" for t in n["tickers"]]
    short_tickers = [t for n in cfg["nodes"] if n["role"] == "short" for t in n["tickers"]]

    long_rets = metrics.basket_returns(series, long_tickers, dates)
    short_rets = metrics.basket_returns(series, short_tickers, dates)
    bench_rets = metrics.to_returns(series.get(benchmark, {}), dates)

    hedge = metrics.hedge_efficacy(long_rets, short_rets, 20, thresholds["hedge"])
    intra = metrics.avg_pairwise_corr(series, long_tickers, dates, 20)
    crowding = metrics.crowding_index(long_rets, bench_rets, intra, thresholds["crowding"])
    nodes = metrics.node_strength(series, cfg["nodes"], dates, benchmark)
    breadth_info = metrics.breadth(nodes)
    shift = metrics.bottleneck_shift(nodes)

    fund = funding_mod.collect(
        cfg["fred_series"], str(ROOT / "data" / "funding_cache.json"), thresholds["funding"]
    )

    rule_verdict = verdict_rules.evaluate(
        nodes, breadth_info, hedge, crowding, fund, thresholds
    )

    evidence = {
        "nodes": nodes, "hedge": hedge, "crowding": crowding,
        "funding": fund, "breadth": breadth_info, "data_status": status,
    }
    verdict, debate = run_debate(
        evidence, rule_verdict, os.environ.get("ANTHROPIC_API_KEY"), MODEL
    )

    return {
        "date": target_date,
        "generated_at": datetime.now(KST).isoformat(timespec="seconds"),
        "as_of_close": dates[-1],
        "nodes": nodes,
        "breadth": breadth_info,
        "bottleneck_shift": shift,
        "hedge": hedge,
        "crowding": crowding,
        "funding": fund,
        "rule_verdict": rule_verdict,
        "verdict": verdict,
        "debate": debate,
        "data_status": status,
    }


def write_outputs(report: dict, mode: str, cfg: dict) -> None:
    dashboard = os.environ.get("DASHBOARD_URL", "")
    # 서브디렉터리 배포 시 Pages 경로가 레포 루트 쪽에 있으므로 분리 가능하게 둔다
    docs = Path(os.environ["DATA_DIR"]) if os.environ.get("DATA_DIR") else ROOT / "docs" / "data"
    docs.mkdir(parents=True, exist_ok=True)
    latest = docs / "latest.json"

    previous = None
    if latest.exists():
        try:
            previous = json.loads(latest.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            previous = None

    records = history.append(str(docs / "history.json"), report)

    if mode == "weekly":
        review = weekly.weekly_review(records, report)
        report["weekly_review"] = review
        report["message"] = render.render_weekly(report, review, dashboard)
        (docs / "weekend.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=1, default=str), encoding="utf-8"
        )
        print(f"[weekly] {review.get('span') or review.get('reason')}")
        return

    if mode == "watchlist":
        watch = weekly.watchlist(report, cfg["thresholds"])
        report["watchlist"] = watch
        report["message"] = render.render_watchlist(report, watch, dashboard)
        (docs / "weekend.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=1, default=str), encoding="utf-8"
        )
        print(f"[watchlist] 발동 임박 {watch['armed_count']}개")
        return

    report["message"] = render.render_brief(report, dashboard)

    # 세션 단위 스냅샷 — 같은 종가일에 대해서는 파일을 다시 쓰지 않는다.
    # 장중 재실행 시 시세가 미세하게 움직여 매번 새 커밋이 쌓이는 것을 막는다.
    # 단, 직전 스냅샷이 DEGRADED 였고 이번에 정상 수집됐다면 갱신한다.
    if previous and not os.environ.get("FORCE_REFRESH"):
        same_session = (
            previous.get("as_of_close") == report.get("as_of_close")
            and previous.get("date") == report.get("date")
        )
        prev_mode = (previous.get("data_status") or {}).get("mode")
        improved = prev_mode not in ("OK", None) and report["data_status"]["mode"] == "OK"
        if same_session and not improved:
            print(f"[idempotent] 동일 세션({report['as_of_close']}) 스냅샷 존재 — 쓰기 생략")
            return

    latest.write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str), encoding="utf-8")

    archive = ROOT / "data" / "history"
    archive.mkdir(parents=True, exist_ok=True)
    (archive / f"{report['date']}.json").write_text(
        json.dumps(report, ensure_ascii=False, separators=(",", ":"), default=str), encoding="utf-8"
    )


def resolve_mode() -> str:
    """BRIEF_MODE=auto 면 KST 요일로 결정한다 (토=주간리뷰, 일=워치리스트)."""
    mode = (os.environ.get("BRIEF_MODE") or "auto").strip().lower()
    if mode not in MODES:
        print(f"::warning::알 수 없는 BRIEF_MODE '{mode}' — auto 로 처리")
        mode = "auto"
    if mode != "auto":
        return mode
    weekday = datetime.now(KST).weekday()  # 월=0 … 토=5, 일=6
    return {5: "weekly", 6: "watchlist"}.get(weekday, "daily")


def main() -> int:
    target = (sys.argv[1] if len(sys.argv) > 1 else "").strip() or os.environ.get("TARGET_DATE", "").strip()
    if target and not DATE_RE.match(target):
        print(f"::error::date 입력 형식 오류 (YYYY-MM-DD): {target}", file=sys.stderr)
        return 1
    target_date = target or datetime.now(KST).strftime("%Y-%m-%d")

    cfg = load_config()
    series, status = prices.collect(
        all_symbols(cfg), str(ROOT / "data" / "prices.json"), cfg["meta"]["lookback_days"]
    )
    print(f"[prices] {status}")
    if status["mode"] == "FAILED":
        print("::error::모든 가격 소스 실패 + 캐시 없음", file=sys.stderr)
        return 1

    mode = resolve_mode()
    print(f"[mode] {mode}")
    report = build_report(cfg, series, status, target_date)
    report["brief_mode"] = mode
    write_outputs(report, mode, cfg)
    print(f"[verdict] {report['verdict']['final_state']} "
          f"(규칙 {report['rule_verdict']['state']}, 점수 {report['rule_verdict']['score']})")
    print(f"[hedge] {report['hedge']['status']} ratio={report['hedge']['spread_vol_ratio']}")
    print(f"[crowding] {report['crowding']['score']} {report['crowding']['level']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
