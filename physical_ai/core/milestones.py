"""마일스톤 판정 — 사전 등록된 6단계를 통과했는지만 보고한다.

설계 원칙(2026-08-16 검증에서 도출):
  - 시차(N분기 뒤)를 계산해 보고하지 않는다. 순수 피지컬AI 바스켓의
    상장 이력이 짧아 교차상관이 진동하며 허위해를 낸다.
  - 각 단계는 통과/미통과 이진 판정 + 현재값 + 임계값까지의 거리를 함께 낸다.
  - 판정 불가(데이터 부족)는 '미통과'가 아니라 별도 상태로 표기한다.
    없는 걸 없다고 말하는 것과, 모르는 걸 없다고 말하는 것은 다르다.
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


def zlast(series: dict[str, float]) -> tuple[float | None, float | None, str | None]:
    if len(series) < 8:
        return None, None, None
    quarters = sorted(series)
    vals = [series[q] for q in quarters]
    mu, sd = statistics.mean(vals), statistics.pstdev(vals)
    z = (vals[-1] - mu) / sd if sd else 0.0
    return z, vals[-1], quarters[-1]


def median_yoy(per_ticker: dict[str, dict[str, dict[str, float]]], concept: str,
               min_names: int = 2) -> dict[str, float]:
    buckets: dict[str, list[float]] = {}
    for data in per_ticker.values():
        series = (data or {}).get(concept) or {}
        for q, g in yoy(series).items():
            buckets.setdefault(q, []).append(g)
    return {q: statistics.median(v) for q, v in buckets.items() if len(v) >= min_names}


def _result(mid, label, rule, status, current, target, note=None, ref=None):
    return {"id": mid, "label": label, "rule": rule, "status": status,
            "current": current, "target": target, "note": note, "reference": ref}


# --------------------------------------------------------------------------- #
def evaluate(cfg: dict, narrative: dict, realization: dict,
             price_excess: dict) -> list[dict]:
    """narrative: {quarter: {hits, sic_n}} (로봇 용어 합산)
    realization: {ticker: {concept: {quarter: value}}}
    price_excess: {quarter: 순수 바스켓 12M 초과수익}
    """
    specs = {m["id"]: m for m in cfg["milestones"]}
    ref = cfg.get("reference_timeline", {})
    out: list[dict] = []

    quarters = sorted(narrative)

    # ① 서사 점화 — 4분기 합이 2년 전 대비 몇 배인가
    spec = specs["narrative"]
    ratio = None
    if len(quarters) >= 12:
        recent = sum(narrative[q]["hits"] for q in quarters[-4:])
        base = sum(narrative[q]["hits"] for q in quarters[-12:-8])
        ratio = recent / base if base else None
    if ratio is None:
        out.append(_result("narrative", spec["label"], spec["rule"], UNKNOWN, None,
                           spec["threshold"], "이력 12분기 미만", ref.get("narrative")))
    else:
        out.append(_result("narrative", spec["label"], spec["rule"],
                           PASS if ratio >= spec["threshold"] else FAIL,
                           round(ratio, 2), spec["threshold"], "2년 전 대비 배수",
                           ref.get("narrative")))

    # ② 선도기업 매출 가속 — 이 산업의 진짜 분기점
    spec = specs["revenue"]
    rev = median_yoy(realization, "revenue")
    z, last, lq = zlast(rev)
    if z is None:
        out.append(_result("revenue", spec["label"], spec["rule"], UNKNOWN, None,
                           spec["abs_threshold"], "분기 이력 8개 미만", ref.get("revenue")))
    else:
        ok = z >= spec["z_threshold"] and last >= spec["abs_threshold"]
        out.append(_result("revenue", spec["label"], spec["rule"], PASS if ok else FAIL,
                           round(last, 3), spec["abs_threshold"],
                           f"{lq} 매출YoY 중앙값 (z={z:+.2f})", ref.get("revenue")))

    # ③ 공급 병목 — 재고가 매출보다 앞서 쌓이는가
    spec = specs["bottleneck"]
    inv = median_yoy(realization, "inventory")
    common = sorted(set(inv) & set(rev))
    if not common:
        out.append(_result("bottleneck", spec["label"], spec["rule"], UNKNOWN, None,
                           spec["gap_threshold"], "재고·매출 공통 분기 없음", ref.get("bottleneck")))
    else:
        q = common[-1]
        gap = inv[q] - rev[q]
        # [2026-08-16 설계 교정] 재고가 매출보다 빠르게 느는 것은 '공급 병목'일 수도
        # '재고 적체'(수요 부진)일 수도 있어 그 자체로는 구분되지 않는다.
        # 실제로 RR 은 재고 +135% / 매출 +1% 였다 — 병목이 아니라 안 팔린 것이다.
        # 따라서 ② 매출 가속이 먼저 통과한 뒤에만 병목으로 판정한다(순차 게이트).
        revenue_passed = out[-1]["status"] == PASS
        if not revenue_passed:
            out.append(_result("bottleneck", spec["label"], spec["rule"], UNKNOWN,
                               round(gap, 3), spec["gap_threshold"],
                               f"{q} 재고YoY-매출YoY (②미통과 — 병목·적체 구분 불가)",
                               ref.get("bottleneck")))
        else:
            out.append(_result("bottleneck", spec["label"], spec["rule"],
                               PASS if gap >= spec["gap_threshold"] else FAIL,
                               round(gap, 3), spec["gap_threshold"],
                               f"{q} 재고YoY-매출YoY", ref.get("bottleneck")))

    # ④ 산업 전반 확산 — SIC 다양성
    spec = specs["diffusion"]
    dratio = None
    if len(quarters) >= 12:
        recent = statistics.mean(narrative[q]["sic_n"] for q in quarters[-4:])
        base = statistics.mean(narrative[q]["sic_n"] for q in quarters[-12:-8])
        dratio = recent / base if base else None
    if dratio is None:
        out.append(_result("diffusion", spec["label"], spec["rule"], UNKNOWN, None,
                           spec["threshold"], "이력 부족", ref.get("diffusion")))
    else:
        out.append(_result("diffusion", spec["label"], spec["rule"],
                           PASS if dratio >= spec["threshold"] else FAIL,
                           round(dratio, 2), spec["threshold"],
                           "SIC 다양성 배수 (버킷 상한 30)", ref.get("diffusion")))

    # ⑤ 이익률 확장
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
    if delta is None:
        out.append(_result("margin", spec["label"], spec["rule"], UNKNOWN, None,
                           spec["threshold"], "영업이익률 4분기 비교 불가", ref.get("margin")))
    else:
        out.append(_result("margin", spec["label"], spec["rule"],
                           PASS if delta >= spec["threshold"] else FAIL,
                           round(delta, 3), spec["threshold"], "YoY 마진 변화", ref.get("margin")))

    # ⑥ 가격 재평가
    spec = specs["rerating"]
    if not price_excess:
        out.append(_result("rerating", spec["label"], spec["rule"], UNKNOWN, None,
                           spec["threshold"], "가격 데이터 부족", ref.get("rerating")))
    else:
        lq = sorted(price_excess)[-1]
        val = price_excess[lq]
        out.append(_result("rerating", spec["label"], spec["rule"],
                           PASS if val >= spec["threshold"] else FAIL,
                           round(val, 3), spec["threshold"],
                           f"{lq} 12개월 초과수익", ref.get("rerating")))

    return out


def stage_summary(milestones: list[dict]) -> dict:
    """가장 앞선 연속 통과 단계 = 현재 국면."""
    passed = 0
    for m in milestones:
        if m["status"] == PASS:
            passed += 1
        else:
            break
    unknown = sum(1 for m in milestones if m["status"] == UNKNOWN)
    return {"stage": passed, "total": len(milestones), "unknown": unknown,
            "next": milestones[passed]["label"] if passed < len(milestones) else None}


def gap_alert(milestones: list[dict], history: list[dict], cfg: dict) -> dict | None:
    """①만 켜진 채로 오래 지속되면 서사 선행 경고 (메타버스 패턴)."""
    limit = cfg.get("alerts", {}).get("watch_gap_quarters", 6)
    by_id = {m["id"]: m for m in milestones}
    if by_id.get("narrative", {}).get("status") != PASS:
        return None
    if by_id.get("revenue", {}).get("status") == PASS:
        return None
    streak = 0
    for record in reversed(history):
        if record.get("stage") == 1:
            streak += 1
        else:
            break
    if streak < limit:
        return {"level": "INFO", "streak": streak, "limit": limit,
                "text": f"서사만 점화된 상태 {streak}분기째 (경고 기준 {limit}분기)"}
    return {"level": "WARN", "streak": streak, "limit": limit,
            "text": f"서사 점화 후 {streak}분기째 매출 가속이 오지 않음 — "
                    "실현 없는 서사 확산 패턴을 경계할 구간"}
