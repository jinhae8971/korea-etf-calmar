#!/usr/bin/env python3
"""피지컬 AI·로봇 사이클 트래커 파이프라인.

주간 실행. 마일스톤은 분기 단위로 바뀌므로 주간이면 충분하다.
텔레그램 발송은 하지 않는다 — latest.json 의 message 를 릴레이가 읽어 보낸다.
"""

from __future__ import annotations

import html
import json
import os
import re
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from core import milestones as ms  # noqa: E402
from core import narrative, prices, realization  # noqa: E402

KST = timezone(timedelta(hours=9))
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
BADGE = {"PASS": "✅", "FAIL": "❌", "UNKNOWN": "⚪"}


def basket_excess(series_map: dict, tickers: list[str], bench: str) -> dict[str, float]:
    """분기별 12개월 초과수익 (종목 수익률 중앙값 기준)."""
    quarters = sorted({q[:6] for t in tickers if series_map.get(t) for q in []} ) or []
    # 일별 종가를 분기말로 축약
    def to_q(series: dict[str, float]) -> dict[str, float]:
        out: dict[str, float] = {}
        for day, close in sorted(series.items()):
            y, m = int(day[:4]), int(day[5:7])
            out[f"{y}Q{(m - 1) // 3 + 1}"] = close
        return out

    legs = {t: to_q(series_map[t]) for t in tickers if series_map.get(t)}
    bq = to_q(series_map.get(bench) or {})
    if not legs or not bq:
        return {}
    allq = sorted(set().union(*[set(v) for v in legs.values()]) & set(bq))
    out: dict[str, float] = {}
    for q in allq:
        prev = ms.qshift(q, -4)
        if prev not in bq or not bq[prev]:
            continue
        rets = [v[q] / v[prev] - 1 for v in legs.values()
                if v.get(q) and v.get(prev) and v[prev] > 0]
        if len(rets) >= 2:
            out[q] = statistics.median(rets) - (bq[q] / bq[prev] - 1)
    return out


def rolling_corr(a: dict[str, float], b: dict[str, float], window: int = 12) -> float | None:
    common = sorted(set(a) & set(b))[-window:]
    if len(common) < 8:
        return None
    xs = [a[q] for q in common]
    ys = [b[q] for q in common]
    mx, my = statistics.mean(xs), statistics.mean(ys)
    sx, sy = statistics.pstdev(xs), statistics.pstdev(ys)
    if sx == 0 or sy == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (len(common) * sx * sy)


def render(report: dict, dashboard: str = "") -> str:
    summary = report["summary"]
    lines = [
        f"<b>🤖 피지컬 AI·로봇 사이클 — {report['date']}</b>",
        "",
        f"<b>현재 국면: {summary['stage']}/{summary['total']}단계 통과</b>",
    ]
    if summary.get("next"):
        lines.append(f"다음 관문: {html.escape(summary['next'])}")
    lines.append("")

    for m in report["milestones"]:
        cur = m["current"]
        shown = f"{cur:+.0%}" if isinstance(cur, float) and abs(cur) < 5 else (
            f"{cur}" if cur is not None else "–")
        ref = f" · AI반도체 {m['reference']}" if m.get("reference") else ""
        lines.append(f"{BADGE.get(m['status'], '⚪')} {html.escape(m['label'])}  "
                     f"{shown} / 기준 {m['target']}{ref}")
        if m.get("note"):
            lines.append(f"     <i>{html.escape(str(m['note']))}</i>")

    alert = report.get("alert")
    if alert:
        mark = "⚠️" if alert["level"] == "WARN" else "ℹ️"
        lines += ["", f"{mark} {html.escape(alert['text'])}"]

    dec = report.get("decoupling")
    if dec and dec.get("corr") is not None:
        state = "독립 사이클 조짐" if dec["decoupled"] else "기성 자동화와 동행"
        lines += ["", f"<b>디커플링</b> 상관 {dec['corr']:+.2f} — {state}"]

    status = report.get("data_status", {})
    degraded = [k for k, v in status.items() if v.get("mode") not in ("OK", None)]
    if degraded:
        lines += ["", f"⚠️ 데이터 상태: {', '.join(degraded)}"]

    lines += ["", "<i>이 지표는 예측이 아니라 관측입니다. 단계 통과 여부만 보고합니다.</i>",
              "<i>투자 판단의 참고 정보이며, 매매 권유가 아닙니다.</i>"]
    if dashboard:
        lines.append(f"📎 {dashboard}")
    return "\n".join(lines)


def main() -> int:
    target = (sys.argv[1] if len(sys.argv) > 1 else "").strip() or os.environ.get("TARGET_DATE", "").strip()
    if target and not DATE_RE.match(target):
        print(f"::error::date 형식 오류: {target}", file=sys.stderr)
        return 1
    today = target or datetime.now(KST).strftime("%Y-%m-%d")

    with open(ROOT / "config" / "cycle.yaml", encoding="utf-8") as handle:
        cfg = yaml.safe_load(handle)

    data_dir = Path(os.environ.get("DATA_DIR") or ROOT / "docs" / "data")
    data_dir.mkdir(parents=True, exist_ok=True)

    # 가격
    pure = cfg["baskets"]["pure"]["tickers"]
    legacy = cfg["baskets"]["legacy"]["tickers"]
    aisemi = cfg["baskets"]["aisemi"]["tickers"]
    bench = cfg["meta"]["benchmark"]
    series, pstatus = prices.collect(
        sorted(set(pure + legacy + aisemi + [bench])), str(ROOT / "data" / "prices.json"), 900
    )

    ex_pure = basket_excess(series, pure, bench)
    ex_legacy = basket_excess(series, legacy, bench)
    ex_ai = basket_excess(series, aisemi, bench)

    # 서사
    cache, nstatus = narrative.collect(cfg["terms"]["robot"], str(ROOT / "data" / "narrative.json"))
    robot_narr = narrative.combine(cache, cfg["terms"]["robot"], "hits")

    # 실현
    real, rstatus = realization.collect(cfg["baskets"]["pure"]["ciks"],
                                        str(ROOT / "data" / "realization.json"))

    result = ms.evaluate(cfg, robot_narr, real, ex_pure)
    summary = ms.stage_summary(result)

    hist_path = data_dir / "history.json"
    history = []
    if hist_path.exists():
        try:
            history = json.loads(hist_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            history = []

    corr = rolling_corr(ex_pure, ex_legacy)
    limit = cfg["alerts"]["decoupling_corr"]
    report = {
        "date": today,
        "generated_at": datetime.now(KST).isoformat(timespec="seconds"),
        "summary": summary,
        "milestones": result,
        "alert": ms.gap_alert(result, history, cfg),
        "decoupling": {"corr": None if corr is None else round(corr, 3),
                       "decoupled": corr is not None and corr < limit,
                       "threshold": limit},
        "excess": {"pure": ex_pure, "legacy": ex_legacy, "aisemi": ex_ai},
        "narrative": robot_narr,
        "data_status": {"prices": pstatus, "narrative": nstatus, "realization": rstatus},
    }
    report["message"] = render(report, os.environ.get("DASHBOARD_URL", ""))

    record = {"date": today, "stage": summary["stage"],
              "corr": report["decoupling"]["corr"],
              "status": {m["id"]: m["status"] for m in result}}
    history = [h for h in history if h.get("date") != today] + [record]
    history = sorted(history, key=lambda h: h["date"])[-260:]
    hist_path.write_text(json.dumps(history, ensure_ascii=False), encoding="utf-8")
    (data_dir / "latest.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1, default=str), encoding="utf-8")

    print(f"[stage] {summary['stage']}/{summary['total']} (불명 {summary['unknown']})")
    for m in result:
        print(f"  {m['status']:8} {m['label']} cur={m['current']} tgt={m['target']}")
    print(f"[decoupling] corr={report['decoupling']['corr']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
