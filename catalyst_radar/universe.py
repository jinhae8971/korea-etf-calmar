"""유니버스 산출 — 시총 100위 ∩ (DefiLlama 체인 | 프로토콜), 기계적 판정.

설계 원칙
  · 수동 종목 목록 금지. 성과를 보고 편입/제외하면 사후선택 편향이 된다.
  · 매칭 실패는 침묵하지 않는다 — unmatched 를 항상 산출물에 남긴다.
    (Monad 누락·SOL 누락의 공통 원인이 "목록에 없으면 원리적으로 안 잡힘"이었다)
"""
import collections
import re

from net import cached_json, get_json

INCLUSION_RULE = {
    "frozen_at": "2026-09-07",
    "rule": [
        "① CoinGecko 시가총액 상위 100위",
        "② DefiLlama 에 체인 또는 프로토콜로 등재 (parentProtocol 자식 승계 포함)",
        "③ 자산형 토큰 제외: 스테이블·랩드·상품(금)·거래소 토큰",
        "④ ②의 매칭은 gecko_id 1순위, 정규화 심볼·명칭 2순위",
    ],
    "note": "성과 기반 편입/제외 금지. 변경 시 frozen_at 갱신 필수.",
}

# 자산형 토큰 — 카탈리스트(거버넌스·업그레이드·공급규칙)가 원리적으로 발생하지 않는 부류.
# 카테고리 배제는 최소로 좁힌다: RWA·Bridge·Basis Trading 은 거버넌스가 살아있어 남긴다.
EXCLUDE_CATEGORY = {"Stablecoin", "CEX", "Wrapped", "Treasury Manager"}
EXCLUDE_SYMBOL = {
    "USDT", "USDC", "DAI", "USDE", "PYUSD", "RLUSD", "USDY", "USDS", "USD1", "USDG",
    "USDD", "USDGO", "USDF", "BFUSD", "EURSAFO", "USTB", "JAAA", "GHO", "EUTBL",
    "JTRSY", "BUIDL", "USYC", "STABLE", "M", "U", "FIGR_HELOC", "BCAP",
    "XAUT", "PAXG",                                     # 상품(금)
    "OKB", "GT", "WBT", "KCS", "LEO", "BGB", "HTX", "NEXO", "CRO",  # 거래소
}
_STABLE_RE = re.compile(r"^(USD|EUR|GBP)|USD$|^W?ST?ETH$")


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _is_asset_token(symbol, category):
    s = symbol.upper()
    if s in EXCLUDE_SYMBOL:
        return "자산형 심볼"
    if _STABLE_RE.search(s):
        return "스테이블 추정 심볼"
    if category in EXCLUDE_CATEGORY:
        return "category=%s" % category
    return None


def _index_protocols(protocols):
    """gecko_id 직접 매칭 + parentProtocol 자식→부모 승계 + 정규화 명칭 색인."""
    by_gecko, by_name, tvl = {}, {}, collections.defaultdict(float)
    for p in protocols:
        g = p.get("gecko_id")
        if g:
            by_gecko.setdefault(g, p)
            tvl[g] += float(p.get("tvl") or 0)
        for key in (p.get("name"), p.get("slug"), p.get("symbol")):
            n = _norm(key)
            if n and n not in by_name:
                by_name[n] = p

    kids = collections.defaultdict(list)
    for p in protocols:
        parent = p.get("parentProtocol")
        if parent:
            kids[parent.split("#")[-1]].append(p)

    inherited = 0
    for slug, group in kids.items():
        if slug in by_gecko:
            continue
        gid = next((k.get("gecko_id") for k in group if k.get("gecko_id")), None) or slug
        if gid in by_gecko:
            continue
        merged = {
            "name": slug,
            "category": group[0].get("category"),
            "tvl": sum(float(k.get("tvl") or 0) for k in group),
            "via": "parentProtocol",
        }
        by_gecko[gid] = merged
        by_name.setdefault(_norm(slug), merged)
        tvl[gid] = merged["tvl"]
        inherited += 1
    return by_gecko, by_name, tvl, inherited


def build_universe(top_n=100):
    """(rows, meta) 반환. rows 는 감시 대상, meta 에 제외·미매칭 감사 정보."""
    markets = cached_json(
        "https://api.coingecko.com/api/v3/coins/markets"
        "?vs_currency=usd&order=market_cap_desc&per_page=%d&page=1" % top_n, ttl=21600
    )
    chains_raw = cached_json("https://api.llama.fi/v2/chains", ttl=3600)
    protocols = cached_json("https://api.llama.fi/protocols", ttl=3600)

    chains_by_gecko = {c["gecko_id"]: c for c in chains_raw if c.get("gecko_id")}
    chains_by_name = {_norm(c.get("name")): c for c in chains_raw}
    p_gecko, p_name, p_tvl, inherited = _index_protocols(protocols)

    rows, excluded, unmatched = [], [], []
    for rank, c in enumerate(markets, 1):
        cid, sym = c["id"], (c.get("symbol") or "").upper()
        name_n = _norm(c.get("name"))

        kind = item = match = None
        if cid in chains_by_gecko:
            kind, item, match = "chain", chains_by_gecko[cid], "gecko_id"
        elif cid in p_gecko:
            kind, item, match = "protocol", p_gecko[cid], "gecko_id"
        elif name_n in chains_by_name:
            kind, item, match = "chain", chains_by_name[name_n], "name"
        elif name_n in p_name:
            kind, item, match = "protocol", p_name[name_n], "name"

        category = "L1/L2" if kind == "chain" else (item or {}).get("category")
        reason = _is_asset_token(sym, category)
        if reason:
            excluded.append({"symbol": sym, "rank": rank, "reason": reason})
            continue
        if kind is None:
            unmatched.append({"symbol": sym, "rank": rank, "gecko_id": cid,
                              "name": c.get("name"), "mcap": c.get("market_cap") or 0})
            continue

        rows.append({
            "symbol": sym,
            "gecko_id": cid,
            "name": c.get("name"),
            "kind": kind,
            "category": category,
            "match": match,
            "rank": rank,
            "mcap": c.get("market_cap") or 0,
            "price": c.get("current_price") or 0,
            "chg7d": c.get("price_change_percentage_7d_in_currency"),
            "tvl": round(p_tvl.get(cid, 0.0) or float((item or {}).get("tvl") or 0)),
        })

    meta = {
        "inclusion_rule": INCLUSION_RULE,
        "scanned": len(markets),
        "parent_inherited": inherited,
        "excluded": excluded,
        "unmatched": sorted(unmatched, key=lambda x: -x["mcap"]),
        "counts": {
            "total": len(rows),
            "chain": sum(1 for r in rows if r["kind"] == "chain"),
            "protocol": sum(1 for r in rows if r["kind"] == "protocol"),
        },
    }
    return rows, meta
