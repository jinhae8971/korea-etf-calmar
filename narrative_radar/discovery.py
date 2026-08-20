"""
Discovery — 고정 유니버스 **밖**까지 훑어 '예치금은 늘었는데 가격이 안 따라온' 종목을 찾는다.

왜 별도 모듈인가:
  universe.json은 내러티브 해석을 담는 고정 목록이라, 거기 없는 종목은 원리적으로 안 잡힌다.
  Monad가 v1에서 누락된 것이 정확히 그 실패였다. 이 모듈은 반대로 **매핑 없이** DefiLlama 전체에서
  gecko_id가 붙은 체인·프로토콜을 긁어와 기계적으로 걸러낸다.

무엇을 찾나 (LAGGING = Monad형):
  · TVL이 의미 있게 늘었고
  · 가격은 그만큼 못 따라왔고 (괴리 ≤ -15%p)
  · MC/TVL이 낮아 예치금 대비 시가총액이 싸 보이고
  · 이미 크게 오르지 않았다

무엇을 조심하나 (전부 코드로 걸러낸다):
  · TVL이 하루짜리 점프로 만들어졌으면 제외 — 고래 1명·인센티브 개시일 수 있다
  · 유통량이 적은 신규 토큰은 MC/TVL이 착시를 준다 → FDV/TVL을 함께 보고, 둘 다 표기
  · 거래대금이 없으면 '싸다'가 의미 없다 → 최소 거래대금 필터
  · 하루 반짝 뜬 후보는 알리지 않는다 → 연속 등장 일수를 세어 유지된 것만 승격

이 모듈도 예측기가 아니다. "이런 상태인 종목이 있다"는 관측이며 수익을 주장하지 않는다.
"""
from __future__ import annotations

import json
import os
import time
import urllib.parse
from datetime import datetime, timedelta, timezone

import tvl_divergence as tv

KST = timezone(timedelta(hours=9))
LLAMA = tv.LLAMA

# 스캔 범위
CHAIN_MIN_TVL = 20_000_000
PROTO_MIN_TVL = 50_000_000
MAX_CHAINS = 60           # 체인당 시계열 1회 호출이므로 상한을 둔다

# LAGGING(예치금 선행) 판정
MIN_TVL_GROWTH_30D = 20.0     # TVL 30일 증가율
MIN_TVL_GROWTH_7D = 10.0      # 30일이 없을 때 대체
MAX_DIVERGENCE = -15.0        # 가격 − TVL (%p). 음수일수록 가격이 뒤처짐
MAX_MC_TVL = 1.5
MAX_PRICE_RUN_30D = 50.0      # 이미 이만큼 올랐으면 '지연'이 아니다
MIN_VOLUME_USD = 1_000_000
MAX_SINGLE_DAY_SHARE = 0.60   # 30일 증가분의 60% 이상이 하루에 발생 → 제외

# HOT(과열) 판정 — 반대 방향, 참고용
HOT_MIN_DIVERGENCE = 30.0
HOT_MIN_TVL_DROP = -15.0

PROMOTE_AFTER_DAYS = 2        # 연속 등장 며칠부터 알릴지


def _series(name: str):
    d = tv._get(f"{LLAMA}/v2/historicalChainTvl/{urllib.parse.quote(name)}", tries=2)
    return d if isinstance(d, list) and d else None


def _single_day_share(series: list, days: int) -> float:
    """days 구간 증가분 중 최대 1일 증가가 차지하는 비중. 1.0에 가까울수록 한 방에 만들어진 TVL."""
    if not series or len(series) < 3:
        return 0.0
    win = series[-(days + 1):]
    total = (win[-1].get("tvl") or 0) - (win[0].get("tvl") or 0)
    if total <= 0:
        return 0.0
    biggest = 0.0
    for a, b in zip(win, win[1:]):
        biggest = max(biggest, (b.get("tvl") or 0) - (a.get("tvl") or 0))
    return biggest / total


def collect_candidates(protocols: list | None = None) -> dict:
    """gecko_id -> {tvl, t7, t30, source, spike_share}"""
    out = {}

    chains = tv._get(f"{LLAMA}/v2/chains", tries=3)
    if isinstance(chains, list):
        pool = [c for c in chains
                if (c.get("tvl") or 0) >= CHAIN_MIN_TVL and c.get("gecko_id")]
        pool.sort(key=lambda c: -(c.get("tvl") or 0))
        pool = pool[:MAX_CHAINS]
        print(f"[discovery] 체인 후보 {len(pool)}개 시계열 조회")
        for c in pool:
            s = _series(c["name"])
            if not s:
                continue
            tvl_now, t7 = tv._series_change(s, 7)
            _, t30 = tv._series_change(s, 30)
            if not tvl_now:
                continue
            out[c["gecko_id"]] = {
                "tvl": tvl_now, "t7": t7, "t30": t30,
                "source": f"chain:{c['name']}",
                "spike_share": _single_day_share(s, 30),
            }
            time.sleep(0.6)
    else:
        print("[discovery] 체인 목록 실패")

    if protocols is None:
        protocols = tv._get(f"{LLAMA}/protocols", tries=3)
    if isinstance(protocols, list):
        n = 0
        for p in protocols:
            g = p.get("gecko_id")
            if not g or g in out:
                continue
            if (p.get("tvl") or 0) < PROTO_MIN_TVL or p.get("change_7d") is None:
                continue
            out[g] = {"tvl": p["tvl"], "t7": p.get("change_7d"), "t30": None,
                      "source": f"protocol:{p.get('slug')}", "spike_share": 0.0}
            n += 1
        print(f"[discovery] 프로토콜 후보 {n}개")
    else:
        print("[discovery] 프로토콜 목록 실패")

    print(f"[discovery] 총 후보 {len(out)}개")
    return out


def fetch_prices(ids: list, fetch_markets) -> dict:
    """CoinGecko 배치 조회. 250개씩 끊는다 (무료 티어 호출을 아끼기 위해)."""
    rows = {}
    for i in range(0, len(ids), 250):
        chunk = ids[i:i + 250]
        got = fetch_markets(chunk)
        rows.update(got)
        if i + 250 < len(ids):
            time.sleep(20)
    return rows


def screen(cand: dict, mkt: dict, history_tvl: dict | None = None) -> dict:
    """LAGGING / HOT 후보를 가려낸다. 탈락 사유도 집계해 스크리너가 왜 조용한지 알 수 있게 한다."""
    lagging, hot = [], []
    reasons = {}

    def rej(k):
        reasons[k] = reasons.get(k, 0) + 1

    for gid, rec in cand.items():
        m = mkt.get(gid)
        if not m:
            rej("시세없음")
            continue
        mcap = m.get("market_cap") or 0
        fdv = m.get("fully_diluted_valuation") or 0
        vol = m.get("total_volume") or 0
        r7 = m.get("price_change_percentage_7d_in_currency")
        r30 = m.get("price_change_percentage_30d_in_currency")
        tvl = rec["tvl"]

        # 자체 누적 이력으로 30일 TVL 보완 (프로토콜은 API가 30일을 안 준다)
        t30 = rec.get("t30")
        if t30 is None and history_tvl and gid in history_tvl:
            t30 = tv.pct(tvl, history_tvl[gid])

        if vol < MIN_VOLUME_USD:
            rej("거래대금 미달")
            continue
        if not mcap:
            rej("시총없음")
            continue

        mc_tvl = mcap / tvl if tvl else None
        fdv_tvl = (fdv / tvl) if (fdv and tvl) else None

        # ── LAGGING (Monad형) ──
        horizon, tchg, pchg = None, None, None
        if t30 is not None and r30 is not None:
            horizon, tchg, pchg = "30d", t30, r30
        elif rec.get("t7") is not None and r7 is not None:
            horizon, tchg, pchg = "7d", rec["t7"], r7

        if horizon:
            div = pchg - tchg
            need = MIN_TVL_GROWTH_30D if horizon == "30d" else MIN_TVL_GROWTH_7D
            if tchg >= need and div <= MAX_DIVERGENCE:
                if pchg > MAX_PRICE_RUN_30D:
                    rej("이미 급등")
                elif mc_tvl is not None and mc_tvl > MAX_MC_TVL:
                    rej("MC/TVL 과다")
                elif rec.get("spike_share", 0) > MAX_SINGLE_DAY_SHARE:
                    rej("하루짜리 TVL 점프")
                else:
                    lagging.append({
                        "id": gid, "symbol": (m.get("symbol") or "").upper(),
                        "name": m.get("name"), "horizon": horizon,
                        "div": round(div, 1), "tvl_chg": round(tchg, 1),
                        "price_chg": round(pchg, 1), "tvl": tvl, "mcap": mcap,
                        "mc_tvl": round(mc_tvl, 2) if mc_tvl else None,
                        "fdv_tvl": round(fdv_tvl, 2) if fdv_tvl else None,
                        "vol": vol, "source": rec["source"],
                        "spike_share": round(rec.get("spike_share", 0), 2),
                    })
                    continue

        # ── HOT (반대 방향, 리스크 참고) ──
        if rec.get("t7") is not None and r7 is not None:
            d7 = r7 - rec["t7"]
            if d7 >= HOT_MIN_DIVERGENCE and rec["t7"] <= HOT_MIN_TVL_DROP:
                hot.append({
                    "id": gid, "symbol": (m.get("symbol") or "").upper(),
                    "div": round(d7, 1), "tvl_chg": round(rec["t7"], 1),
                    "price_chg": round(r7, 1), "tvl": tvl,
                    "mc_tvl": round(mc_tvl, 2) if mc_tvl else None,
                    "source": rec["source"],
                })

    # 괴리가 클수록, 그리고 MC/TVL이 낮을수록 위로
    lagging.sort(key=lambda x: (x["div"], x["mc_tvl"] if x["mc_tvl"] is not None else 9))
    hot.sort(key=lambda x: -x["div"])
    return {"lagging": lagging[:12], "hot": hot[:6], "rejected": reasons,
            "scanned": len(cand)}


def track(result: dict, state: dict, today: str) -> dict:
    """연속 등장 일수를 센다. 하루 반짝은 알리지 않고, 사라지면 이탈로 기록한다."""
    prev = state.get("lagging") or {}
    cur = {}
    for x in result["lagging"]:
        old = prev.get(x["id"])
        if old and old.get("last_seen") != today:
            x["streak"] = old.get("streak", 1) + 1
            x["first_seen"] = old.get("first_seen", today)
        elif old:
            x["streak"] = old.get("streak", 1)
            x["first_seen"] = old.get("first_seen", today)
        else:
            x["streak"] = 1
            x["first_seen"] = today
        x["last_seen"] = today
        x["status"] = "신규" if x["streak"] == 1 else f"{x['streak']}일 유지"
        cur[x["id"]] = {"streak": x["streak"], "first_seen": x["first_seen"],
                        "last_seen": today, "symbol": x["symbol"]}

    dropped = [v["symbol"] for k, v in prev.items() if k not in cur]
    result["dropped"] = dropped[:6]
    # 하루 반짝은 조용히 관찰만, 유지된 것만 알림 대상으로 승격
    result["alert"] = [x for x in result["lagging"] if x["streak"] >= PROMOTE_AFTER_DAYS]
    return {"lagging": cur}


def render(result: dict) -> list:
    """텔레그램 본문 조각."""
    esc = lambda s: str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    L = ["\n<b>■ 괴리 발굴</b> <i>(전체 시장 스캔 · 예치금은 느는데 가격이 안 따라온 종목)</i>"]
    lag = result.get("lagging") or []
    if not lag:
        rj = result.get("rejected") or {}
        top = ", ".join(f"{k} {v}" for k, v in sorted(rj.items(), key=lambda x: -x[1])[:3])
        L.append(f"· 조건 충족 종목 없음 (스캔 {result.get('scanned', 0)}개"
                 + (f" · 주요 탈락: {top}" if top else "") + ")")
        return L
    for x in lag[:6]:
        tag = "🆕" if x.get("streak", 1) == 1 else "📌"
        L.append(f"{tag} <b>{esc(x['symbol'])}</b> <code>{x['div']:+.0f}%p</code> "
                 f"({esc(x['horizon'])}) · {esc(x.get('status', ''))}")
        L.append(f"    TVL {x['tvl_chg']:+.0f}% vs 가격 {x['price_chg']:+.0f}% "
                 f"· ${x['tvl'] / 1e6:,.0f}M")
        ratios = f"MC/TVL {x['mc_tvl']}" if x["mc_tvl"] is not None else ""
        if x.get("fdv_tvl"):
            ratios += f" · FDV/TVL {x['fdv_tvl']}"
        if ratios:
            L.append(f"    {ratios}")
    if result.get("dropped"):
        L.append(f"· 이탈: {esc(', '.join(result['dropped']))}")
    hot = result.get("hot") or []
    if hot:
        L.append("\n<b>■ 과열 주의</b> <i>(예치금은 빠지는데 가격만 오른 종목)</i>")
        for x in hot[:3]:
            L.append(f"🔺 <b>{esc(x['symbol'])}</b> <code>{x['div']:+.0f}%p</code> "
                     f"· TVL {x['tvl_chg']:+.0f}% vs 가격 {x['price_chg']:+.0f}%")
    return L


def events(result: dict) -> list:
    ev = []
    for x in (result.get("alert") or [])[:3]:
        ev.append({
            "kind": "DISCOVERY_LAGGING", "level": "high",
            "text": f"{x['symbol']} 예치금 선행 {x['streak']}일 유지 — "
                    f"TVL {x['tvl_chg']:+.0f}% vs 가격 {x['price_chg']:+.0f}% "
                    f"(MC/TVL {x['mc_tvl']}, {x['source']})",
        })
    return ev
