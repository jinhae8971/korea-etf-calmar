"""
TVL 괴리 모듈 — 가격 추세와 예치금(TVL) 추세의 이탈을 측정한다.

한계를 먼저 적는다:
  · TVL은 "펀더멘털"이 아니다. 예치금일 뿐이고, 인센티브 파밍으로 부풀 수 있으며
    같은 자산이 여러 프로토콜에 중복 계상되기도 한다.
  · 프라이버시 코인·DePIN·오라클처럼 TVL 개념 자체가 무의미한 카테고리가 있다.
    이들은 universe.json에서 llama 필드를 비워 두고, 여기서도 계산하지 않는다.
  · 따라서 괴리는 "틀렸다"는 판정이 아니라 "가격과 예치금이 다른 말을 하고 있다"는 관측이다.

데이터:
  · 체인   → /v2/historicalChainTvl/{chain}  (일별 전체 시계열, 경량)
  · 프로토콜 → /protocols 1회 (change_7d 제공) + 자체 누적 이력으로 30일 산출
"""
from __future__ import annotations

import json
import random
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
LLAMA = "https://api.llama.fi"
UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Safari/605.1.15",
]

# 노이즈 필터 — 소형 풀은 %변화가 쉽게 폭발한다
MIN_TVL_USD = 20_000_000
TH_DIVERGENCE = 25.0   # |가격변화 − TVL변화| %p


def _get(url: str, tries: int = 4):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": random.choice(UA_POOL), "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=90) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            last = e
            w = [5, 12, 28, 55][min(i, 3)] + random.uniform(0, 3)
            print(f"[llama] {type(e).__name__}: {e} — {w:.0f}s 후 재시도 ({i + 1}/{tries})")
            time.sleep(w)
    print(f"[llama] 포기: {url} ({last})")
    return None


def pct(now, then):
    if now is None or then is None or then <= 0:
        return None
    return (now / then - 1.0) * 100.0


def _series_change(series: list, days: int):
    """[{date:unix, tvl:float}] 에서 days일 전 대비 변화율."""
    if not series:
        return None, None
    now = series[-1].get("tvl")
    target = series[-1].get("date", 0) - days * 86400
    prev = None
    for pt in reversed(series):
        if pt.get("date", 0) <= target:
            prev = pt.get("tvl")
            break
    return now, pct(now, prev)


def collect(universe: dict) -> dict:
    """coin_id -> {tvl, t7, t30, source} (실패한 항목은 생략)."""
    chains, protos = {}, {}
    for nar in universe["narratives"].values():
        for m in nar["members"]:
            ll = m.get("llama")
            if not ll:
                continue
            (chains if ll["kind"] == "chain" else protos)[m["id"]] = ll["key"]

    out = {}

    # 체인 — 시계열을 통째로 받으므로 7일·30일 즉시 산출
    for cid, name in chains.items():
        d = _get(f"{LLAMA}/v2/historicalChainTvl/{urllib.parse.quote(name)}")
        if not isinstance(d, list) or not d:
            continue
        tvl, t7 = _series_change(d, 7)
        _, t30 = _series_change(d, 30)
        if tvl:
            out[cid] = {"tvl": tvl, "t7": t7, "t30": t30, "source": f"chain:{name}"}
        time.sleep(1.2)

    # 프로토콜 — 목록 1회 호출. DefiLlama는 상위 브랜드를 parentProtocol로 쪼개 두므로
    # 직접 슬러그가 없으면 자식들을 TVL 가중으로 합산한다.
    if protos:
        allp = _get(f"{LLAMA}/protocols", tries=3)
        if isinstance(allp, list):
            byslug = {x.get("slug"): x for x in allp if x.get("slug")}
            children = {}
            for x in allp:
                pp = x.get("parentProtocol")
                if pp:
                    children.setdefault(pp.split("#", 1)[-1], []).append(x)
            for cid, slug in protos.items():
                rec = byslug.get(slug)
                if rec and rec.get("tvl"):
                    out[cid] = {"tvl": rec["tvl"], "t7": rec.get("change_7d"), "t30": None,
                                "source": f"protocol:{slug}"}
                    continue
                kids = [k for k in children.get(slug, []) if (k.get("tvl") or 0) > 0]
                if not kids:
                    # 최종 폴백 — /tvl/{slug}는 상위 브랜드도 받아준다 (현재값만, 변화율 없음).
                    # 30일 변화는 자체 누적 이력으로 나중에 채워진다.
                    v = _get(f"{LLAMA}/tvl/{urllib.parse.quote(slug)}", tries=2)
                    if isinstance(v, (int, float)) and v > 0:
                        out[cid] = {"tvl": float(v), "t7": None, "t30": None,
                                    "source": f"protocol:{slug}(now)"}
                        print(f"[llama] {slug}: /tvl 폴백 사용 (변화율 없음)")
                    else:
                        print(f"[llama] 미매칭 슬러그: {slug} ({cid})")
                    time.sleep(1.0)
                    continue
                tot = sum(k["tvl"] for k in kids)
                w = [(k["tvl"], k.get("change_7d")) for k in kids if k.get("change_7d") is not None]
                t7 = (sum(t * c for t, c in w) / sum(t for t, _ in w)) if w else None
                out[cid] = {"tvl": tot, "t7": t7, "t30": None,
                            "source": f"protocol:{slug}(+{len(kids)})"}
        else:
            print("[llama] /protocols 실패 — 프로토콜 TVL 생략")

    print(f"[llama] TVL 수집 {len(out)}종목 (체인 {len(chains)} / 프로토콜 {len(protos)})")
    return out


def backfill_30d(tvl_map: dict, history: list) -> None:
    """t30이 없는 항목을 자체 누적 이력으로 채운다 (약 30일 운용 후부터 유효)."""
    if not history:
        return
    target = (datetime.now(KST) - timedelta(days=30)).strftime("%Y-%m-%d")
    snap = None
    for h in history:
        if h.get("as_of", "") <= target:
            snap = h
        else:
            break
    if not snap:
        return
    old = snap.get("tvl") or {}
    for cid, rec in tvl_map.items():
        if rec.get("t30") is None and cid in old:
            rec["t30"] = pct(rec["tvl"], old[cid])
            rec["t30_src"] = "self-history"


def attach(rows: list, tvl_map: dict) -> None:
    """종목 row에 TVL 지표와 괴리를 붙인다."""
    for r in rows:
        rec = tvl_map.get(r["id"])
        if not rec:
            r["tvl"] = None
            r["div7"] = r["div30"] = r["mc_tvl"] = None
            continue
        r["tvl"] = rec["tvl"]
        r["tvl_source"] = rec.get("source")
        r["t7"] = rec.get("t7")
        r["t30"] = rec.get("t30")
        # 괴리 = 가격 변화 − 예치금 변화 (%p)
        r["div7"] = (r["r7"] - rec["t7"]) if (r["r7"] is not None and rec.get("t7") is not None) else None
        r["div30"] = (r["r30"] - rec["t30"]) if (r["r30"] is not None and rec.get("t30") is not None) else None
        r["mc_tvl"] = (r["mcap"] / rec["tvl"]) if (r["mcap"] and rec["tvl"]) else None


def rank_divergence(rows: list) -> list:
    """괴리 크기순. 부호를 유지해 양방향을 함께 보여준다."""
    cand = []
    for r in rows:
        if not r.get("tvl") or r["tvl"] < MIN_TVL_USD:
            continue
        d = r.get("div30") if r.get("div30") is not None else r.get("div7")
        if d is None:
            continue
        cand.append({
            "symbol": r["symbol"], "narrative_name": r.get("narrative_name", ""),
            "div": round(d, 1),
            "horizon": "30d" if r.get("div30") is not None else "7d",
            "price": round(r["r30"] if r.get("div30") is not None else r["r7"], 1),
            "tvl_chg": round((r["t30"] if r.get("div30") is not None else r["t7"]), 1),
            "tvl_usd": r["tvl"],
            "mc_tvl": round(r["mc_tvl"], 2) if r.get("mc_tvl") else None,
            "direction": "가격 선행" if d > 0 else "가격 지연",
            "source": r.get("tvl_source", ""),
        })
    cand.sort(key=lambda x: -abs(x["div"]))
    return cand


def events(div_rows: list) -> list:
    ev = []
    for d in div_rows:
        if abs(d["div"]) < TH_DIVERGENCE:
            continue
        icon = "가격이 예치금보다 앞섬" if d["div"] > 0 else "예치금이 늘었는데 가격이 안 따라옴"
        ev.append({
            "kind": "TVL_DIVERGENCE",
            "level": "watch",
            "text": f"{d['symbol']} TVL 괴리 {d['div']:+.0f}%p ({d['horizon']}) — {icon} "
                    f"[가격 {d['price']:+.0f}% vs TVL {d['tvl_chg']:+.0f}%]",
        })
    return ev[:4]
