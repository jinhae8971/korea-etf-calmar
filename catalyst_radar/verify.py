"""사전등록 검증 — 탐지 시점을 박아두고 전방 성과를 스스로 채점한다.

설계 의도
  [[consensus-gap]] 에서 선행신호 가설 4개가 연속 기각된 뒤 남은 규칙은 하나다:
  "같은 데이터에 변형을 계속 시도하면 p-해킹이다. 표본 외 사전 등록으로만 진행하라."
  그래서 이 모듈은 임계값을 조정하지 않는다. 탐지 시점의 가격을 박아두고,
  지평(T+7 / T+30)에 도달한 것만 사후에 채점해 쌓는다.

구현상의 핵심
  과거 시세 API 를 쓰지 않는다. 매 실행의 현재가만으로 충분하다 —
  탐지 시점 가격(px0·btc0)을 registry 에 저장해 두었으므로, 나이가 지평을 넘긴
  이벤트에 대해 그 시점의 현재가로 한 번만 확정 기록하면 된다.
  이 구조는 look-ahead 를 원리적으로 차단한다(미래 데이터를 조회할 방법이 없다).
"""
import statistics

HORIZONS = (7, 30)
MIN_SAMPLE = 8          # 이보다 적으면 수치를 제시하지 않고 '표본 부족'으로 표기
BENCH = "BTC"


def _age_days(detected_at, today):
    import datetime as dt
    try:
        d0 = dt.date.fromisoformat(str(detected_at)[:10])
    except (ValueError, TypeError):
        return None
    return (today - d0).days


def accrue(events, price_by_symbol, today):
    """지평에 도달한 이벤트의 초과수익을 1회만 확정 기록한다.

    초과수익 = (종목 수익률) − (BTC 수익률). 절대수익만 보면 시장 전체의
    상승·하락이 신호의 성과로 오인된다(alpha-radar 에서 겪은 RELATIVE_ONLY 판정).
    """
    btc_now = price_by_symbol.get(BENCH)
    updated = 0
    for ev in events:
        reg = ev.get("registry") or {}
        px0, btc0 = reg.get("price_at_detect"), reg.get("btc_at_detect")
        if not px0 or not btc0:
            continue
        age = _age_days(reg.get("detected_at"), today)
        if age is None:
            continue
        px_now = price_by_symbol.get(ev["symbol"])
        if not px_now or not btc_now:
            continue
        for h in HORIZONS:
            field = "t%d" % h
            if field in reg or age < h:
                continue
            reg[field] = round(((px_now / px0) - (btc_now / btc0)) * 100, 2)
            reg["%s_at" % field] = today.isoformat()
            updated += 1
        ev["registry"] = reg
    return updated


def summarize(events):
    """임팩트·출처별 전방 초과수익 분포. 평균이 아니라 중앙값을 쓴다.

    동일가중 바스켓의 평균은 소수 극단치가 지배한다 — consensus-gap 1차 검정에서
    +76% 같은 값이 나온 원인이 정확히 이것이었다.
    """
    buckets = {}
    for ev in events:
        reg = ev.get("registry") or {}
        for h in HORIZONS:
            v = reg.get("t%d" % h)
            if v is None:
                continue
            for key in ("impact:%s" % ev.get("impact"), "source:%s" % ev.get("source"), "ALL"):
                buckets.setdefault((key, h), []).append(v)

    out = []
    for (key, h), vals in sorted(buckets.items()):
        row = {"bucket": key, "horizon": h, "n": len(vals)}
        if len(vals) >= MIN_SAMPLE:
            row["median"] = round(statistics.median(vals), 2)
            row["hit_rate"] = round(sum(1 for v in vals if v > 0) / len(vals) * 100, 1)
            if len(vals) >= 2:
                row["iqr"] = [round(x, 2) for x in _quartiles(vals)]
            row["status"] = "OK"
        else:
            row["status"] = "표본 부족"
        out.append(row)
    return out


def _quartiles(vals):
    s = sorted(vals)
    n = len(s)
    return (s[max(0, n // 4 - (n % 4 == 0))], s[min(n - 1, (3 * n) // 4)])


def render_line(summary):
    """브리프 1줄. 표본이 찰 때까지 수치를 주장하지 않는다."""
    all30 = next((r for r in summary if r["bucket"] == "ALL" and r["horizon"] == 30), None)
    if not all30:
        return "검증: 사전등록 누적 중 (아직 지평 도달 이벤트 없음)"
    if all30["status"] != "OK":
        return "검증: 사전등록 %d건 누적 — 표본 부족(기준 %d건), 수치 판단 보류" % (
            all30["n"], MIN_SAMPLE)
    return "검증: T+30 초과수익 중앙값 %+.1f%%p · 적중 %.0f%% (n=%d, BTC 대비)" % (
        all30["median"], all30["hit_rate"], all30["n"])
