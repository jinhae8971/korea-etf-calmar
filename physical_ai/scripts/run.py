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
sys.path.insert(0, str(ROOT / "scripts"))
from build_watchlist import build as build_watchlist  # noqa: E402

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
    bar = lambda a: "●" * a["passed"] + "○" * (a["total"] - a["passed"])
    lines = [
        f"<b>🤖 피지컬 AI·로봇 사이클 — {report['date']}</b>",
        "",
        f"<b>서사</b> {bar(summary['narrative'])} {summary['narrative']['passed']}/{summary['narrative']['total']}   "
        f"<b>실현</b> {bar(summary['realization'])} {summary['realization']['passed']}/{summary['realization']['total']}   "
        f"<b>가격</b> {bar(summary['price'])} {summary['price']['passed']}/{summary['price']['total']}",
        f"축 간 격차 {summary['gap']:+.2f} — {html.escape(report.get('regime_text',''))}",
        "",
    ]

    # 배수형(서사 축)과 비율형(실현·가격 축)은 표기 단위가 다르다
    RATIO_IDS = {"narrative", "diffusion"}
    for m in report["milestones"]:
        cur = m["current"]
        if cur is None:
            shown = "–"
        elif m["id"] in RATIO_IDS:
            shown = f"{cur:.2f}배"
        elif isinstance(cur, float) and abs(cur) < 5:
            shown = f"{cur:+.0%}"
        else:
            shown = str(cur)
        ref = f" · AI반도체 {m['reference']}" if m.get("reference") else ""
        tgt = (f"{m['target']:.2f}배" if m["id"] in RATIO_IDS
               else (f"{m['target']:+.0%}" if isinstance(m["target"], float) and abs(m["target"]) < 5
                     else m["target"]))
        lines.append(f"{BADGE.get(m['status'], '⚪')} {html.escape(m['label'])}  "
                     f"{shown} / 기준 {tgt}{ref}")
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

    wl = report.get("watchlist")
    if wl and wl.get("stocks"):
        lines += ["", "<b>📋 흐름 적합도 상위 10 (미국 상장)</b>"]
        for r in wl["stocks"]:
            tag = ""
            d = r.get("detail", {})
            if d.get("independence", 0) >= 0.6:
                tag = " · 순수형"
            elif d.get("independence", 1) < 0.35:
                tag = " · 반도체 동행형"
            lines.append(f"  {r['rank']:>2}. {r['ticker']:<5} {r['score']:.0f}점{tag}")
        etfs = wl.get("etfs") or []
        if etfs:
            best = etfs[0]
            exp = best.get("exposure")
            if exp is not None and exp < 0:
                lines += ["", "<b>ETF</b> — 후보 전부 순수 노출이 음수입니다. "
                              "로봇 ETF 들이 순수 피지컬AI보다 기성 자동화·반도체에 더 붙어 있습니다.",
                          f"  (최상위 {best['ticker']} 순수노출 {exp:+.2f})"]
            else:
                lines += ["", "<b>ETF</b> " + " / ".join(
                    f"{e['ticker']} {e['score']:.0f}" for e in etfs[:3])]

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
    with open(ROOT / "config" / "watchlist.yaml", encoding="utf-8") as handle:
        wl_cfg = yaml.safe_load(handle)
    universe = sorted(set(pure + legacy + aisemi + [bench]
                          + list(wl_cfg["stocks"]) + wl_cfg["etfs"]))
    series, pstatus = prices.collect(universe, str(ROOT / "data" / "prices.json"), 900)

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
    summary = ms.axis_summary(result, cfg)

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
        "regime_text": ms.REGIME_TEXT.get(summary.get("regime"), ""),
        "milestones": result,
        "alert": ms.gap_alert(summary, history, cfg),
        "decoupling": {"corr": None if corr is None else round(corr, 3),
                       "decoupled": corr is not None and corr < limit,
                       "threshold": limit},
        "excess": {"pure": ex_pure, "legacy": ex_legacy, "aisemi": ex_ai},
        "narrative": robot_narr,
        "data_status": {"prices": pstatus, "narrative": nstatus, "realization": rstatus},
    }
    try:
        report["watchlist"] = build_watchlist(cfg, series)
        report["data_status"].update(report["watchlist"].pop("status", {}))
    except Exception as exc:  # noqa: BLE001
        print(f"::warning::워치리스트 산출 실패: {type(exc).__name__}: {exc}")
        report["watchlist"] = None
    report["message"] = render(report, os.environ.get("DASHBOARD_URL", ""))

    record = {"date": today, "regime": summary.get("regime"), "gap": summary.get("gap"),
              "narrative": summary["narrative"]["passed"],
              "realization": summary["realization"]["passed"],
              "corr": report["decoupling"]["corr"],
              "status": {m["id"]: m["status"] for m in result}}
    history = [h for h in history if h.get("date") != today] + [record]
    history = sorted(history, key=lambda h: h["date"])[-260:]
    hist_path.write_text(json.dumps(history, ensure_ascii=False), encoding="utf-8")
    (data_dir / "latest.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1, default=str), encoding="utf-8")

    print(f"[axis] 서사 {summary['narrative']['passed']}/{summary['narrative']['total']} · "
          f"실현 {summary['realization']['passed']}/{summary['realization']['total']} · "
          f"가격 {summary['price']['passed']}/{summary['price']['total']} "
          f"| 격차 {summary['gap']:+.2f} → {summary['regime']}")
    for m in result:
        print(f"  {m['status']:8} {m['label']} cur={m['current']} tgt={m['target']}")
    print(f"[decoupling] corr={report['decoupling']['corr']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
