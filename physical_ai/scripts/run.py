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



# ---------------------------------------------- 가시성 규격 v2 (2026-09-07)
# 글자 수보다 "한눈에 읽히는가"가 기준. 상한은 안전장치이지 목표가 아니다.
#   · 한 줄 폭 LINE_COLS(반각) 이하 — 넘치면 모바일에서 접히고 들여쓰기가
#     사라져 항목 경계가 무너진다. 접느니 줄을 나눈다.
#   · 모든 증감에 색 점을 붙인다(전 트랙 동일 임계).
DIGEST_CAP = 900
LINE_COLS = 40
VIS_FROZEN_AT = "2026-09-07"
_TAGRE = re.compile(r"<[^>]+>")


def vis_width(s):
    """한글·이모지는 2칸으로 세는 표시 폭."""
    return sum(2 if ord(c) > 0x2000 else 1 for c in _TAGRE.sub("", s or ""))


def dot(v, unit="%"):
    """증감 색 점. pp 는 %와 겨루도록 3배 가중(압축 규격과 동일 기준)."""
    if v is None:
        return "\u26aa"
    x = v * 3.0 if unit == "pp" else v
    if x >= 20:
        return "\U0001F7E9"
    if x >= 3:
        return "\U0001F7E2"
    if x > -3:
        return "\u26aa"
    if x > -20:
        return "\U0001F534"
    return "\U0001F7E5"


def sig(v, unit="%", digits=0):
    """색 점 + 부호 있는 값. 예: 🟢+8% / 🔴-12%"""
    if v is None:
        return "\u26aa–"
    return "%s%+.*f%s" % (dot(v, unit), digits, v, "pp" if unit == "pp" else "%")


def rank_arrow(d):
    if not d:
        return ""
    return " \u25b2%d" % d if d > 0 else " \u25bc%d" % abs(d)


def wrap_items(label, items, cols=LINE_COLS, sep=" \u00b7 "):
    """라벨 + 항목들을 폭 상한에 맞춰 여러 줄로. 이어지는 줄은 공백 들여쓰기."""
    out, cur = [], "<b>%s</b> " % label
    pad = " " * (len(label) + 1)
    for it in items:
        cand = cur + (sep if cur.strip() != ("<b>%s</b>" % label) and not cur.endswith(" ") else "") + it
        if vis_width(cand) > cols and vis_width(cur) > vis_width("<b>%s</b> " % label):
            out.append(cur.rstrip())
            cur = pad + it
        else:
            cur = cand if cur.endswith(" ") else cur + sep + it
    if cur.strip():
        out.append(cur.rstrip())
    return out


def clip(s, budget=LINE_COLS):
    """표시 폭 기준으로 자른다. 글자 수로 자르면 한글에서 여전히 넘친다."""
    limit = budget
    out, w = [], 0
    for ch in str(s):
        cw = 2 if ord(ch) > 0x2000 else 1
        if w + cw > limit - 2:   # 말줄임표(…)도 폭 2
            out.append("\u2026")
            break
        out.append(ch)
        w += cw
    return "".join(out)


def cap_lines(lines, tail, cap=DIGEST_CAP):
    budget = cap - sum(len(_TAGRE.sub("", t)) + 1 for t in tail)
    out, used = [], 0
    for ln in lines:
        n = len(_TAGRE.sub("", ln)) + 1
        if used + n > budget:
            out.append("\u2026")
            break
        out.append(ln)
        used += n
    return out + tail


def plain_len(s):
    return len(_TAGRE.sub("", s or ""))


def render_digest(report: dict, dashboard: str = "") -> str:
    """가시성 규격 v2 — 마일스톤은 통과/미통과로 묶고, 증감은 색 점으로."""
    su = report["summary"]
    bar = lambda a: "●" * a["passed"] + "○" * (a["total"] - a["passed"])
    L = [f"🤖 <b>피지컬 AI·로봇 사이클</b> · {report['date'][5:]}",
         "서사 %s · 실현 %s · 가격 %s" % (bar(su["narrative"]), bar(su["realization"]),
                                          bar(su["price"]))]
    if report.get("regime_text"):
        for chunk in _wrap_text(str(report["regime_text"]), LINE_COLS):
            L.append("<i>%s</i>" % html.escape(chunk))

    ms_ = report.get("milestones") or []
    if ms_:
        L.append("")
        L.append("<b>마일스톤</b>")
        icon = {"PASS": "✅", "FAIL": "❌"}
        for m in ms_:
            mark = icon.get(m["status"], "⚪")
            name = m["label"].split(" ", 1)[-1]
            cur, tgt = m.get("current"), m.get("target")
            val = ""
            if cur is not None and tgt is not None:
                # 지표마다 단위가 달라(배수·%p) 임의로 붙이면 틀린다 — 원값 그대로.
                val = " <i>%g / 기준 %g</i>" % (float(cur), float(tgt))
            L.append("%s %s%s" % (mark, html.escape(clip(name, 18)), val))

    dc = report.get("decoupling") or {}
    if dc.get("corr") is not None:
        L.append("")
        L.append("디커플링 상관 %s%+.2f%s" % (
            dot(dc["corr"] * 100), dc["corr"],
            " <i>· 독립 사이클</i>" if dc.get("decoupled") else ""))

    al = report.get("alert") or {}
    if al.get("text"):
        head = "ℹ️ "
        L.append(head + "<i>%s</i>" % html.escape(
            clip(al["text"], LINE_COLS - vis_width(head))))

    wl = (report.get("watchlist") or {}).get("stocks") or []
    if wl:
        L.append("")
        L.append("<b>흐름 적합도</b>")
        for r in wl[:5]:
            L.append("%s %s <i>%.0f점</i>" % (
                dot((r.get("score") or 0) - 50), html.escape(r.get("ticker", "?")),
                r.get("score") or 0))

    tail = [""]
    if dashboard:
        tail.append(f'📊 <a href="{dashboard}">전체 대시보드</a>')
    tail.append("<i>관측 서술 · 매매 신호 아님</i>")
    return "\n".join(cap_lines(L, tail))


def _wrap_text(text, cols):
    """긴 문장을 폭 상한에 맞춰 단어 단위로 접는다."""
    out, cur = [], ""
    for word in str(text).split(" "):
        cand = (cur + " " + word).strip()
        if vis_width(cand) > cols and cur:
            out.append(cur)
            cur = word
        else:
            cur = cand
    if cur:
        out.append(cur)
    return out


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
    _dash = os.environ.get("DASHBOARD_URL", "")
    report["message_full"] = render(report, _dash)
    report["message"] = render_digest(report, _dash)

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
