"""마일스톤 판정 — 서사/실현/가격 3축으로 분리해 보고한다.

[2026-08-16 구조 교정]
  ①~⑥을 하나의 사다리로 늘어놓았던 최초 설계는 틀렸다. "산업 확산이
  켜졌는데 매출 가속은 꺼짐"이 모순처럼 보였으나, 둘은 애초에 다른 축이다.
  ①② 는 서사(말), ③④⑤ 는 실현(돈), ⑥ 은 가격이다.
  축 간 격차 자체가 메타버스형 서사 선행을 판별하는 핵심 지표다.

[실증 근거]
  메타버스 실현 축 통과 이력: 1,1,1,0,0,0,1,1,0,0,0,0  (한 번도 정착 못함)
  AI 반도체:                 …,0,1,1,0,1,1,1,1,1,1     (2023Q4부터 정착)
  둘 다 개별 분기로는 +90%, +39% 를 찍었다. 가른 것은 크기가 아니라 지속성이다.
"""

from __future__ import annotations

import statistics

PASS, FAIL, UNKNOWN = "PASS", "FAIL", "UNKNOWN"


def qidx(q: str) -> int:
    return int(q[:4]) * 4 + int(q[5]) - 1


def qshift(q: str, n: int) -> str:
    i = qidx(q) + n
    return f"{i // 4}Q{i % 4 + 1}"


def yoy(series: dict[str, float]) -> dict[str, float]:
    out = {}
    for q, v in series.items():
        prev = series.get(qshift(q, -4))
        if prev and prev > 0 and v is not None:
            out[q] = v / prev - 1.0
    return out


def zlast(series: dict[str, float]):
    if len(series) < 8:
        return None, None, None
    quarters = sorted(series)
    vals = [series[q] for q in quarters]
    mu, sd = statistics.mean(vals), statistics.pstdev(vals)
    return ((vals[-1] - mu) / sd if sd else 0.0), vals[-1], quarters[-1]


def median_yoy(per_ticker: dict, concept: str, min_names: int = 2) -> dict[str, float]:
    buckets: dict[str, list[float]] = {}
    for data in per_ticker.values():
        for q, g in yoy((data or {}).get(concept) or {}).items():
            buckets.setdefault(q, []).append(g)
    return {q: statistics.median(v) for q, v in buckets.items() if len(v) >= min_names}


def _r(spec, status, current, target, note=None, ref=None):
    return {"id": spec["id"], "axis": spec["axis"], "label": spec["label"],
            "rule": spec["rule"], "status": status, "current": current,
            "target": target, "note": note, "reference": ref}


def _ratio(series: dict, key: str, quarters: list[str]):
    """4분기 창 대비 2년 전 4분기 창의 배수. 10-K 계절성을 4분기 합으로 흡수한다."""
    if len(quarters) < 12:
        return None
    if key == "hits":
        recent = sum(series[q][key] for q in quarters[-4:])
        base = sum(series[q][key] for q in quarters[-12:-8])
    else:
        recent = statistics.mean(series[q][key] for q in quarters[-4:])
        base = statistics.mean(series[q][key] for q in quarters[-12:-8])
    return recent / base if base else None


# --------------------------------------------------------------------------- #
def evaluate(cfg, narrative, realization, price_excess) -> list[dict]:
    specs = {m["id"]: m for m in cfg["milestones"]}
    ref = cfg.get("reference_timeline", {})
    quarters = sorted(narrative)
    out: list[dict] = []

    # 서사 축 ---------------------------------------------------------------
    spec = specs["narrative"]
    ratio = _ratio(narrative, "hits", quarters)
    out.append(_r(spec, UNKNOWN if ratio is None else
                  (PASS if ratio >= spec["threshold"] else FAIL),
                  None if ratio is None else round(ratio, 2), spec["threshold"],
                  "2년 전 대비 언급 배수" if ratio is not None else "이력 12분기 미만",
                  ref.get("narrative")))

    spec = specs["diffusion"]
    dratio = _ratio(narrative, "sic_n", quarters)
    out.append(_r(spec, UNKNOWN if dratio is None else
                  (PASS if dratio >= spec["threshold"] else FAIL),
                  None if dratio is None else round(dratio, 2), spec["threshold"],
                  "SIC 다양성 배수 (버킷 상한 30)" if dratio is not None else "이력 부족",
                  ref.get("diffusion")))

    # 실현 축 ---------------------------------------------------------------
    spec = specs["revenue"]
    rev = median_yoy(realization, "revenue")
    window, need = spec.get("window", 4), spec.get("persistence", 3)
    rev_pass = False
    if len(rev) < window:
        out.append(_r(spec, UNKNOWN, None, spec["abs_threshold"],
                      f"분기 이력 {len(rev)}개 (최소 {window})", ref.get("revenue")))
    else:
        ordered = sorted(rev)
        recent = [rev[q] for q in ordered[-window:]]
        hits = sum(1 for v in recent if v >= spec["abs_threshold"])
        rev_pass = hits >= need
        lq = ordered[-1]
        out.append(_r(spec, PASS if rev_pass else FAIL, round(rev[lq], 3),
                      spec["abs_threshold"],
                      f"최근 {window}분기 중 {hits}회 통과 (기준 {need}회) · "
                      f"{lq} {rev[lq]:+.0%}", ref.get("revenue")))

    spec = specs["bottleneck"]
    inv = median_yoy(realization, "inventory")
    common = sorted(set(inv) & set(rev))
    if not common:
        out.append(_r(spec, UNKNOWN, None, spec["gap_threshold"],
                      "재고·매출 공통 분기 없음", ref.get("bottleneck")))
    else:
        q = common[-1]
        gap = inv[q] - rev[q]
        # 재고가 매출보다 빨리 느는 것은 병목일 수도 적체(수요 부진)일 수도 있다.
        # 실제로 RR 은 재고 +135% / 매출 +1% 였다 — 안 팔린 것이다.
        if not rev_pass:
            out.append(_r(spec, UNKNOWN, round(gap, 3), spec["gap_threshold"],
                          f"{q} 재고YoY-매출YoY (매출 가속 미정착 — 병목·적체 구분 불가)",
                          ref.get("bottleneck")))
        else:
            out.append(_r(spec, PASS if gap >= spec["gap_threshold"] else FAIL,
                          round(gap, 3), spec["gap_threshold"],
                          f"{q} 재고YoY-매출YoY", ref.get("bottleneck")))

    spec = specs["margin"]
    margins: dict[str, list[float]] = {}
    for data in realization.values():
        rv = (data or {}).get("revenue") or {}
        oi = (data or {}).get("operating_income") or {}
        for q in set(rv) & set(oi):
            if rv[q]:
                margins.setdefault(q, []).append(oi[q] / rv[q])
    med = {q: statistics.median(v) for q, v in margins.items() if len(v) >= 2}
    delta = None
    if med:
        lq = sorted(med)[-1]
        prev = med.get(qshift(lq, -4))
        if prev is not None:
            delta = med[lq] - prev
    out.append(_r(spec, UNKNOWN if delta is None else
                  (PASS if delta >= spec["threshold"] else FAIL),
                  None if delta is None else round(delta, 3), spec["threshold"],
                  "YoY 마진 변화" if delta is not None else "4분기 비교 불가",
                  ref.get("margin")))

    # 가격 축 ---------------------------------------------------------------
    spec = specs["rerating"]
    if not price_excess:
        out.append(_r(spec, UNKNOWN, None, spec["threshold"], "가격 데이터 부족",
                      ref.get("rerating")))
    else:
        lq = sorted(price_excess)[-1]
        out.append(_r(spec, PASS if price_excess[lq] >= spec["threshold"] else FAIL,
                      round(price_excess[lq], 3), spec["threshold"],
                      f"{lq} 12개월 초과수익", ref.get("rerating")))

    order = [m["id"] for m in cfg["milestones"]]
    return sorted(out, key=lambda m: order.index(m["id"]))


def axis_summary(milestones: list[dict], cfg: dict) -> dict:
    """축별 진행도 + 축 간 격차. 격차가 이 시스템의 핵심 판정값이다."""
    axes = cfg.get("axes", {})
    out: dict = {}
    for key, meta in axes.items():
        group = [m for m in milestones if m["axis"] == key]
        out[key] = {
            "label": meta["label"],
            "passed": sum(1 for m in group if m["status"] == PASS),
            "total": meta["total"],
            "unknown": sum(1 for m in group if m["status"] == UNKNOWN),
        }
    narr, real = out.get("narrative", {}), out.get("realization", {})
    n_pct = narr.get("passed", 0) / max(1, narr.get("total", 1))
    r_pct = real.get("passed", 0) / max(1, real.get("total", 1))
    out["gap"] = round(n_pct - r_pct, 3)
    if out["gap"] >= 0.6:
        out["regime"] = "NARRATIVE_LED"
    elif out["gap"] <= -0.3:
        out["regime"] = "REALIZATION_LED"
    else:
        out["regime"] = "BALANCED"
    return out


REGIME_TEXT = {
    "NARRATIVE_LED": "서사가 실현보다 크게 앞섬 — 메타버스형(말만 확산) 패턴과 형태가 같은 구간",
    "REALIZATION_LED": "실현이 서사보다 앞섬 — 실적이 나오는데 아직 덜 알려진 구간",
    "BALANCED": "서사와 실현이 나란히 진행 중",
}


def gap_alert(summary: dict, history: list[dict], cfg: dict) -> dict | None:
    limit = cfg.get("alerts", {}).get("watch_gap_quarters", 6)
    if summary.get("regime") != "NARRATIVE_LED":
        return None
    streak = 0
    for record in reversed(history):
        if record.get("regime") == "NARRATIVE_LED":
            streak += 1
        else:
            break
    level = "WARN" if streak >= limit else "INFO"
    text = (f"서사 선행 {streak}회 연속 관측 (경고 기준 {limit}회). "
            "메타버스는 이 상태를 12분기 유지한 뒤 실현으로 넘어가지 못했습니다."
            if level == "WARN" else
            f"서사 선행 상태 {streak}회 연속 (경고 기준 {limit}회)")
    return {"level": level, "streak": streak, "limit": limit, "text": text}
