#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
protocol.py — 로빈후드 체인 **플랫폼/프로토콜 토큰** 트랙

밈 트랙과 무엇이 다른가
  밈 트랙은 "지금 자금이 어디에 반응하는가"를 시총 순위로 본다. 근거가 가격뿐이다.
  이 트랙은 **프로토콜이 실제로 벌어들인 수수료·매출**을 분모로 놓고 밸류에이션을 본다.
  같은 체인이지만 측정 대상이 다르므로 순위를 섞지 않고 별도 트랙으로 유지한다.

유니버스 구성 (고정 목록이 아니다)
  DefiLlama `/overview/fees/Robinhood Chain` — 이 체인에서 수수료가 발생한 전 프로토콜.
  실측 2026-08-30 기준 138개. 여기서 규칙으로 3분류한다.

    NATIVE   chains == ['Robinhood Chain'] 이고 토큰 심볼이 있는 것
             → 배수 랭킹 대상. 매출이 이 체인에 귀속되고 토큰도 이 체인에 있다.
    EXTERNAL chains 가 여러 개 (Uniswap V3/V4, Morpho, Virtuals 등)
             → 수수료는 크지만 토큰 가치가 이 체인에 귀속되지 않는다. 참고 표기만.
             실측: Uniswap V4가 30일 수수료 1위($31M)지만 UNI는 로빈후드 체인 플레이가 아니다.
    TOKENLESS 네이티브인데 심볼이 '-' 또는 없음 (Arcus Perps 등)
             → 에어드랍 관찰 대상. 밸류에이션 자체가 성립하지 않으므로 순위에 넣지 않는다.

  이 분류는 하드코딩 차단목록이 아니라 API가 주는 chains/symbol 필드로 판정한다.
  신규 프로토콜이 생기면 다음 실행에 자동 편입된다.

핵심 지표
  PF = FDV / (30일 매출 × 365/30)
       "이 프로토콜을 통째로 사면 현재 매출 속도로 몇 년 만에 회수되는가".
       업계에서 FDV/revenue 배수라 부르는 값과 같은 정의다.
  매출(revenue)은 `dataType=dailyRevenue`, 수수료(fees)는 기본값으로 각각 받는다.
  둘은 다르다 — 수수료는 사용자가 낸 총액, 매출은 프로토콜에 귀속된 몫이다.
  매출이 집계되지 않는 프로토콜은 수수료로 대체하고 basis=FEES로 표기한다(비교 시 주의).

무엇을 잡으려는 것인가
  배수가 싼 종목을 찾는 것이 1차 목적이 아니다. **분자(가격)와 분모(매출)가
  서로 다른 방향으로 움직이는 순간**을 잡는 것이다.
    · REV_COLLAPSE — 매출이 먼저 무너지는데 가격은 아직 안 빠진 구간.
      실측 사례: NOXA는 7/11~13 사이 런치패드로서 붕괴했다. 수수료 급감은 하루 안에 보인다.
    · REV_SURGE   — 매출이 먼저 뛰는데 가격은 아직 반응 안 한 구간.
    · SHARE_SHIFT — 런치패드 카테고리 내 점유율 이동. 개별 매출보다 선행하는 경우가 있다.

한계 (대시보드에도 그대로 노출한다)
  · PF는 **후행 지표**다. 이 체인의 런치패드 매출은 밈 발행 활동에 거의 100% 연동되므로
    체인 활동이 식으면 분모가 같이 무너진다. 배수가 싸 보이는 것이 안전을 뜻하지 않는다.
  · 30일 매출을 그대로 연환산한다. 가동 2개월짜리 체인에서 이 연환산은 낙관 편향이 있다.
  · 시총 소스가 갈릴 수 있다. GeckoTerminal이 풀 색인을 놓치는 종목이 실측된다
    (ARROW: GT 유동성 $3.5 vs DexScreener $10.5M) → 두 소스를 병행하고 괴리를 표기한다.
"""

import json
import time
import urllib.request

LLAMA = "https://api.llama.fi"
GT = "https://api.geckoterminal.com/api/v2"
DS = "https://api.dexscreener.com"
CHAIN = "Robinhood%20Chain"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# 카테고리 표기 (없으면 원문 그대로)
CAT_KO = {
    "Launchpad": "런치패드", "Dexs": "DEX", "Derivatives": "파생",
    "Lending": "렌딩", "Indexes": "인덱스", "Telegram Bot": "봇",
    "AI Agents": "AI 에이전트", "Liquidity Manager": "유동성 관리",
    "NFT Marketplace": "NFT", "Gamified Mining": "게임형 채굴",
    "DEX Aggregator": "애그리게이터", "Chain": "체인", "Yield": "이자",
    "Risk Curators": "리스크 큐레이터", "Liquidity Automation": "유동성 자동화",
}


def _fnum(v, default=0.0):
    try:
        f = float(v)
        return default if f != f else f
    except (TypeError, ValueError):
        return default


def _get(url, timeout=30, tries=3, sleep=(3, 8, 18), log=print):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            last = exc
            if i < tries - 1:
                time.sleep(sleep[min(i, len(sleep) - 1)])
    raise RuntimeError("%s 조회 실패: %s" % (url.split("?")[0], last))


# ------------------------------------------------------------------ 수집

def fetch_overview(kind="fees", log=print):
    """kind: 'fees' | 'dexs'. 체인 전체 프로토콜 집계."""
    return _get("%s/overview/%s/%s?excludeTotalDataChartBreakdown=true" % (LLAMA, kind, CHAIN), log=log)


def fetch_summary(slug, data_type=None, log=print):
    url = "%s/summary/fees/%s" % (LLAMA, slug)
    if data_type:
        url += "?dataType=%s" % data_type
    return _get(url, timeout=25, tries=2, log=log)


def token_address(summary):
    """
    summary.address 는 'robinhood:0x...' 형식. 체인 접두사가 없거나 다른 체인이면
    그 토큰은 이 체인의 자산이 아니다 — 실측: lighter-robinhood-perps → ethereum:0x...,
    sushi-launchpad → 접두사 없는 0x6b35...(이더리움 SUSHI).
    """
    raw = (summary or {}).get("address") or ""
    if ":" not in raw:
        return None
    net, addr = raw.split(":", 1)
    if net.strip().lower() != "robinhood":
        return None
    addr = addr.strip().lower()
    return addr if addr.startswith("0x") and len(addr) == 42 else None


def rh_share(summary):
    """
    글로벌 수수료 중 로빈후드 체인이 차지하는 비중.

    `chains` 필드로 단일체인 여부를 판정하면 안 된다 — 실측에서 NOXA Fun의 chains는
    ['Stable','Monad','MegaETH','Merlin','Intuition','Robinhood Chain']이지만
    나머지 체인 수수료는 전부 0이었다. 목록에 있다는 것과 거기서 번다는 것은 다르다.
    """
    tot = _fnum(summary.get("total30d"))
    cb = summary.get("chainBreakdown") or {}
    rh = cb.get("Robinhood Chain")
    rh30 = _fnum(rh.get("total30d")) if isinstance(rh, dict) else 0.0
    if tot <= 0:
        return None
    return round(rh30 / tot * 100.0, 1)


def _norm(s):
    """프로토콜 이름을 심볼 대조용으로 정규화."""
    s = (s or "").lower()
    for junk in (" v1", " v2", " v3", "-v1", "-v2", "-v3", ".fun", " fun", "-fun",
                 " sh", ".sh", " protocol", " finance", " labs", "."):
        s = s.replace(junk, "")
    return "".join(ch for ch in s if ch.isalnum())


def infer_token(proto, rows_by_symbol):
    """
    DefiLlama에 토큰이 매핑되지 않은 프로토콜을 밈 트랙 유니버스와 대조해 연결한다.

    왜 필요한가 — DefiLlama의 symbol 필드는 불완전하다. 실측에서 NOXA Fun과
    StonkBrokers는 둘 다 symbol='-'로 나오지만 두 토큰 모두 이 체인에 실재한다
    (STONKBROKER는 밈 트랙 시총 6위, $40.7M).

    다만 잘못 연결하면 없는 밸류에이션을 만들어내므로 **후보가 정확히 하나일 때만**
    연결하고, 출처를 link_src='inferred'로 남겨 대시보드에 표시한다.
    """
    keys = {_norm(proto.get("name")), _norm(proto.get("displayName")), _norm(proto.get("slug"))}
    keys = {k for k in keys if len(k) >= 3}
    hits = {}
    for sym, row in rows_by_symbol.items():
        n = _norm(sym)
        for k in keys:
            # 단수/복수 흔들림만 허용하고 부분일치는 허용하지 않는다
            if n == k or n == k + "s" or k == n + "s":
                hits[row["address"]] = row
    if len(hits) == 1:
        return list(hits.values())[0]
    return None


# ------------------------------------------------------------------ 시총 해결

def _gt_token(addr, timeout=20):
    d = _get("%s/networks/robinhood/tokens/%s" % (GT, addr), timeout=timeout, tries=2)
    at = ((d or {}).get("data") or {}).get("attributes") or {}
    return {
        "fdv": _fnum(at.get("fdv_usd"), 0.0),
        "mcap": _fnum(at.get("market_cap_usd"), 0.0),
        "liq": _fnum(at.get("total_reserve_in_usd"), 0.0),
        "v24": _fnum((at.get("volume_usd") or {}).get("h24"), 0.0),
        "price": _fnum(at.get("price_usd"), 0.0),
        "symbol": at.get("symbol"),
    }


def _ds_token(addr, timeout=15):
    pairs = _get("%s/token-pairs/v1/robinhood/%s" % (DS, addr), timeout=timeout, tries=2) or []
    mine = [p for p in pairs
            if p.get("chainId") == "robinhood"
            and (p.get("baseToken") or {}).get("address", "").lower() == addr.lower()]
    if not mine:
        return None
    fdvs = [_fnum(p.get("fdv")) for p in mine if _fnum(p.get("fdv")) > 0]
    mcs = [_fnum(p.get("marketCap")) for p in mine if _fnum(p.get("marketCap")) > 0]
    return {
        "fdv": max(fdvs) if fdvs else 0.0,
        "mcap": max(mcs) if mcs else 0.0,
        "liq": sum(_fnum((p.get("liquidity") or {}).get("usd")) for p in mine),
        "v24": sum(_fnum((p.get("volume") or {}).get("h24")) for p in mine),
        "price": max((_fnum(p.get("priceUsd")) for p in mine), default=0.0),
        "symbol": (mine[0].get("baseToken") or {}).get("symbol"),
    }


def resolve_market(addr, rows_by_addr, gt_sleep=2.2, log=print):
    """
    시총·유동성 해결 순서
      1) 밈 트랙이 이미 수집한 row (같은 GeckoTerminal 스냅샷 — 추가 호출 0)
      2) GeckoTerminal 단일 토큰 조회
      3) DexScreener 폴백

    GT가 풀 색인을 놓치면 유동성이 사실상 0으로 나온다(ARROW 실측 $3.5).
    그래서 GT 유동성이 $1K 미만인데 DS가 실질 유동성을 보고하면 DS 값을 채택하고
    출처를 함께 기록한다. FDV가 두 소스에서 8% 넘게 갈리면 SRC_GAP으로 표기한다.
    """
    hit = rows_by_addr.get(addr)
    if hit:
        return {"fdv": _fnum(hit.get("fdv")), "mcap": _fnum(hit.get("mcap")),
                "liq": _fnum(hit.get("liq")), "v24": _fnum(hit.get("v24")),
                "price": _fnum(hit.get("price")), "symbol": hit.get("symbol"),
                "src": "GT(meme-track)", "gap_pct": None}

    gt = ds = None
    try:
        gt = _gt_token(addr)
    except Exception as exc:
        log("[protocol] GT %s 실패: %s" % (addr[:10], exc))
    time.sleep(gt_sleep)

    need_ds = (gt is None) or (gt.get("liq", 0) < 1000) or (gt.get("fdv", 0) <= 0)
    if need_ds:
        try:
            ds = _ds_token(addr)
        except Exception as exc:
            log("[protocol] DS %s 실패: %s" % (addr[:10], exc))
        time.sleep(0.8)

    if gt is None and ds is None:
        return None
    if gt is None:
        ds["src"], ds["gap_pct"] = "DS", None
        return ds
    if ds is None:
        gt["src"], gt["gap_pct"] = "GT", None
        return gt

    gap = None
    if gt.get("fdv", 0) > 0 and ds.get("fdv", 0) > 0:
        gap = round((ds["fdv"] - gt["fdv"]) / gt["fdv"] * 100.0, 2)
    out = {
        "fdv": gt["fdv"] or ds["fdv"],
        "mcap": gt["mcap"] or ds["mcap"],
        "liq": max(gt["liq"], ds["liq"]),
        "v24": max(gt["v24"], ds["v24"]),
        "price": gt["price"] or ds["price"],
        "symbol": gt.get("symbol") or ds.get("symbol"),
        "src": "GT+DS" if gt["liq"] >= ds["liq"] else "GT+DS(liq=DS)",
        "gap_pct": gap,
    }
    return out


# ------------------------------------------------------------------ 지표

def annualize(v30):
    return _fnum(v30) * 365.0 / 30.0


def compute_metrics(item):
    """PF/PS·매출 모멘텀 계산. 분모가 0이면 배수를 만들지 않는다(무한대 금지)."""
    rev30 = _fnum(item.get("rev30"))
    fee30 = _fnum(item.get("fee30"))
    basis = "REV" if rev30 > 0 else "FEES"
    base30 = rev30 if rev30 > 0 else fee30
    item["basis"] = basis
    item["base_ann"] = annualize(base30)
    fdv = _fnum(item.get("fdv"))
    item["pf"] = round(fdv / item["base_ann"], 2) if item["base_ann"] > 0 and fdv > 0 else None
    fee_ann = annualize(fee30)
    item["ps"] = round(fdv / fee_ann, 2) if fee_ann > 0 and fdv > 0 else None

    # 매출 모멘텀 — 7일 런레이트를 30일 런레이트와 비교
    d7 = _fnum(item.get("fee7")) / 7.0
    d30 = fee30 / 30.0
    item["momentum_pct"] = round((d7 - d30) / d30 * 100.0, 1) if d30 > 0 else None
    # 직전 24시간이 7일 평균 대비 어디에 있는가 (붕괴/급증 조기 신호)
    item["burst_pct"] = round((_fnum(item.get("fee24")) - d7) / d7 * 100.0, 1) if d7 > 0 else None
    return item


def category_shares(protocols, key="total24h"):
    """카테고리별 점유율(%) — 런치패드 전쟁 국면 판정용."""
    tot = {}
    for p in protocols:
        cat = p.get("category") or "기타"
        tot[cat] = tot.get(cat, 0.0) + _fnum(p.get(key))
    grand = sum(tot.values()) or 1.0
    return {c: round(v / grand * 100.0, 2) for c, v in sorted(tot.items(), key=lambda x: -x[1])}


def intra_category_shares(items, category, key="fee24"):
    """한 카테고리 안에서 프로토콜별 점유율(%)."""
    pool = [i for i in items if i.get("category") == category]
    grand = sum(_fnum(i.get(key)) for i in pool)
    if grand <= 0:
        return {}
    return {i["slug"]: round(_fnum(i.get(key)) / grand * 100.0, 2) for i in pool}


# ------------------------------------------------------------------ 본체

def build(rows, cfg, log=print, deadline_sec=300.0):
    """
    반환: native(랭킹 대상) / external(참고) / tokenless(에어드랍 관찰) / shares / summary
    실패해도 예외를 밖으로 던지지 않는다 — 밈 트랙 본체가 인질이 되면 안 된다.

    수집 순서
      1) /overview/fees/Robinhood Chain — 이 체인에서 발생한 수수료 (체인 스코프)
      2) 상위 N개에 대해 /summary/fees/{slug} — **글로벌** 수수료 + chainBreakdown
      3) 같은 slug에 dataType=dailyRevenue — 글로벌 매출

    배수는 (2)(3)의 글로벌 값으로 계산한다. FDV가 전 체인의 토큰 가치를 반영하므로
    분모도 전 체인 매출이어야 앞뒤가 맞는다. (1)은 체인 내 점유율·활동 측정에만 쓴다.
    """
    started = time.time()
    top_n = int(cfg.get("protocol_top_n", 26))
    min_fee30 = _fnum(cfg.get("protocol_min_fee30_usd", 20000.0))
    min_liq = _fnum(cfg.get("protocol_min_liq_usd", 50000.0))
    micro_fdv = _fnum(cfg.get("protocol_micro_fdv_usd", 2000000.0))
    rh_pure = _fnum(cfg.get("protocol_rh_share_min_pct", 90.0))

    ov = fetch_overview("fees", log=log)
    all_protos = ov.get("protocols") or []
    skip_cats = set(cfg.get("protocol_skip_categories") or ["Chain", "Foundation"])
    protos = [p for p in all_protos
              if _fnum(p.get("total30d")) >= min_fee30 and p.get("category") not in skip_cats]
    protos.sort(key=lambda p: -_fnum(p.get("total30d")))
    log("[protocol] 수수료 발생 프로토콜 %d개 (30일 $%.0f 이상 %d개)"
        % (len(all_protos), min_fee30, len(protos)))

    rows = rows or []
    rows_by_addr = {(r.get("address") or "").lower(): r for r in rows}
    rows_by_symbol = {}
    for r in rows:
        rows_by_symbol.setdefault((r.get("symbol") or "").upper(), r)

    native, external, tokenless = [], [], []

    for proto in protos[:top_n]:
        if time.time() - started > deadline_sec:
            log("[protocol] 시간 상한 도달 — 이후 프로토콜 생략")
            break
        slug = proto.get("slug")
        item = {
            "slug": slug, "slugs": [slug],
            "name": proto.get("displayName") or proto.get("name"),
            "category": proto.get("category"), "chains": proto.get("chains") or [],
            # 체인 스코프 (점유율·버스트용)
            "fee24": _fnum(proto.get("total24h")), "fee7": _fnum(proto.get("total7d")),
            "fee30_chain": _fnum(proto.get("total30d")),
            "fee_change_1d": proto.get("change_1d"), "fee_change_7d": proto.get("change_7d"),
        }

        try:
            sfee = fetch_summary(slug, None, log=log)
        except Exception as exc:
            log("[protocol] %s 수수료 요약 실패: %s" % (slug, exc))
            sfee = {}
        time.sleep(0.3)
        try:
            srev = fetch_summary(slug, "dailyRevenue", log=log)
        except Exception:
            srev = {}
        time.sleep(0.3)

        item["fee30"] = _fnum(sfee.get("total30d")) or item["fee30_chain"]
        item["rev24"] = _fnum(srev.get("total24h"))
        item["rev7"] = _fnum(srev.get("total7d"))
        item["rev30"] = _fnum(srev.get("total30d"))
        item["rh_share_pct"] = rh_share(sfee)
        item["url"] = sfee.get("url")

        addr = token_address(sfee) or token_address(srev)
        if addr:
            item["address"] = addr
            item["symbol"] = (sfee.get("symbol") or srev.get("symbol") or "").strip()
            item["link_src"] = "llama"
            native.append(item)
            continue

        raw_addr = (sfee.get("address") or "")
        if raw_addr and not raw_addr.startswith("robinhood:"):
            # 토큰이 다른 체인에 있다 — 이 체인 매출로 그 토큰을 평가할 수 없다
            item["kind"] = "EXTERNAL"
            item["symbol"] = (sfee.get("symbol") or "").strip()
            net = raw_addr.split(":")[0] if ":" in raw_addr else "체인 미표기(이더리움 기본)"
            item["ext_reason"] = "토큰이 타 체인 자산 (%s)" % net
            external.append(item)
            continue

        hit = infer_token(proto, rows_by_symbol)
        if hit:
            item["address"] = (hit.get("address") or "").lower()
            item["symbol"] = hit.get("symbol")
            item["link_src"] = "inferred"
            native.append(item)
        else:
            item["symbol"] = ""
            rh = item.get("rh_share_pct")
            if rh is not None and rh < rh_pure:
                # 다체인 서비스가 이 체인에서도 벌 뿐이다. 에어드랍 기대 대상이 아니다.
                item["kind"] = "EXTERNAL"
                item["ext_reason"] = "다체인 서비스 · 토큰 미매핑 (이 체인 비중 %.0f%%)" % rh
                external.append(item)
            else:
                item["kind"] = "TOKENLESS"
                tokenless.append(item)

    # ---- 같은 토큰을 쓰는 프로토콜 병합 (Pons V1 + V2 → PONS 하나) ----
    merged = {}
    for it in native:
        key = it["address"]
        if key not in merged:
            merged[key] = it
            continue
        m = merged[key]
        m["slugs"] = sorted(set(m["slugs"] + it["slugs"]))
        for k in ("fee24", "fee7", "fee30", "fee30_chain", "rev24", "rev7", "rev30"):
            m[k] = _fnum(m.get(k)) + _fnum(it.get(k))
        if _fnum(it.get("fee30")) > _fnum(m.get("fee30")) / 2:
            m["name"] = m["name"].split(" V")[0]
        m["link_src"] = m.get("link_src") or it.get("link_src")
    native = list(merged.values())
    if len(merged) < len(rows_by_addr) + len(merged):
        log("[protocol] 토큰 기준 병합 후 네이티브 %d종" % len(native))

    # ---- 시총 해결 ----
    for it in native:
        if time.time() - started > deadline_sec + 90:
            log("[protocol] 시총 해결 시간 상한 — 이후 생략")
            break
        mk = resolve_market(it["address"], rows_by_addr, log=log)
        if mk:
            it.update({"fdv": mk["fdv"], "mcap": mk["mcap"], "liq": mk["liq"],
                       "tok_v24": mk["v24"], "price": mk["price"],
                       "mkt_src": mk["src"], "src_gap_pct": mk["gap_pct"]})
            if not it.get("symbol") and mk.get("symbol"):
                it["symbol"] = mk["symbol"]
        compute_metrics(it)

    # ---- 플래그 ----
    pfs = sorted(i["pf"] for i in native if i.get("pf"))
    # 표본이 4~5종뿐인 구간이라 하위 1/3 컷은 사실상 발동하지 않는다 → 중앙값을 쓴다
    cheap_cut = pfs[len(pfs) // 2] if pfs else None
    for it in native:
        flags = []
        if it.get("basis") == "FEES":
            flags.append(("FEES_BASIS", "매출 미집계 — 수수료 기준 배수(다른 종목과 직접 비교 주의)"))
        if not _fnum(it.get("fdv")):
            flags.append(("NO_MCAP", "토큰 시총을 두 소스 모두에서 해결하지 못함"))
        if it.get("link_src") == "inferred":
            flags.append(("LINK_INFERRED",
                          "DefiLlama에 토큰 매핑이 없어 심볼 대조로 연결함 — 주소를 직접 확인하세요"))
        gap = it.get("src_gap_pct")
        if gap is not None and abs(gap) >= _fnum(cfg.get("protocol_src_tolerance_pct", 8.0)):
            flags.append(("SRC_GAP", "시총 소스 괴리 %+.1f%%" % gap))
        if _fnum(it.get("liq")) < min_liq:
            flags.append(("LIQ_THIN", "유동성 $%s — 배수가 싸도 실제로 못 산다" % _h(it.get("liq"))))
        if 0 < _fnum(it.get("fdv")) < micro_fdv:
            flags.append(("MICRO", "FDV $%s — 배수보다 규모를 먼저 보세요" % _h(it.get("fdv"))))
        rh = it.get("rh_share_pct")
        if rh is not None and rh < rh_pure:
            flags.append(("MULTICHAIN",
                          "글로벌 수수료 중 이 체인 비중 %.0f%% — 배수는 전 체인 기준입니다" % rh))
        mom = it.get("momentum_pct")
        if mom is not None and mom <= -50:
            flags.append(("REV_FADING", "7일 매출속도가 30일 대비 %.0f%%" % mom))
            if cheap_cut is not None and it.get("pf") and it["pf"] <= cheap_cut:
                flags.append(("VALUE_TRAP",
                              "배수는 싸지만 매출이 무너지는 중 — 분모가 곧 따라 내려옵니다"))
        it["flags"] = [{"code": c, "detail": d} for c, d in flags]

    # ---- 랭킹 ----
    rankable = [i for i in native
                if i.get("pf") and i["pf"] > 0 and _fnum(i.get("liq")) >= min_liq]
    rankable.sort(key=lambda i: i["pf"])
    for n, it in enumerate(rankable, 1):
        it["value_rank"] = n

    native.sort(key=lambda i: -_fnum(i.get("fee30")))
    external.sort(key=lambda i: -_fnum(i.get("fee30_chain")))
    tokenless.sort(key=lambda i: -_fnum(i.get("fee30_chain")))

    lp_share = intra_category_shares(native + external + tokenless, "Launchpad", "fee24")

    out = {
        "as_of_epoch": int(time.time()),
        "native": native,
        "external": external[: int(cfg.get("protocol_external_n", 8))],
        "tokenless": tokenless[: int(cfg.get("protocol_tokenless_n", 8))],
        "rankable": [i["slug"] for i in rankable],
        "shares": {
            "by_category_24h": category_shares(all_protos, "total24h"),
            "by_category_30d": category_shares(all_protos, "total30d"),
            "launchpad_24h": lp_share,
        },
        "summary": {
            "chain_fee_24h": _fnum(ov.get("total24h")),
            "chain_fee_30d": _fnum(ov.get("total30d")),
            "scanned": min(len(protos), top_n),
            "native_n": len(native), "external_n": len(external),
            "tokenless_n": len(tokenless), "rankable_n": len(rankable),
            "inferred_n": sum(1 for i in native if i.get("link_src") == "inferred"),
            "elapsed_sec": round(time.time() - started, 1),
        },
    }
    log("[protocol] 네이티브 %d(추정연결 %d) · 외부 %d · 토큰없음 %d · 배수산출 %d (%.1fs)"
        % (len(native), out["summary"]["inferred_n"], len(external),
           len(tokenless), len(rankable), out["summary"]["elapsed_sec"]))
    return out


# ------------------------------------------------------------------ 변화 탐지

def snapshot(payload, ts):
    """이력 스냅샷 — 파일이 무한히 커지지 않도록 필요한 값만 남긴다."""
    return {
        "ts": ts,
        "pf": {i["slug"]: i["pf"] for i in payload["native"] if i.get("pf")},
        "fee24": {i["slug"]: i.get("fee24") for i in payload["native"]},
        "rev24": {i["slug"]: i.get("rev24") for i in payload["native"]},
        "fdv": {i["slug"]: i.get("fdv") for i in payload["native"] if i.get("fdv")},
        "lp_share": payload["shares"].get("launchpad_24h", {}),
    }


def _ref(history, hours, now_epoch):
    """now-hours 에 가장 가까운 과거 스냅샷. 없으면 None."""
    target = now_epoch - hours * 3600
    best, gap = None, None
    for snap in history:
        ts = snap.get("epoch")
        if ts is None or ts > now_epoch:
            continue
        g = abs(ts - target)
        if gap is None or g < gap:
            best, gap = snap, g
    if best is None or gap is None or gap > hours * 3600 * 0.6:
        return None
    return best


def detect(payload, history, cfg, now_epoch):
    """
    변화 탐지. 배수 순위가 아니라 **분자와 분모의 엇갈림**을 본다.
    표본이 없으면 조용히 아무것도 내지 않는다 — 없는 신호를 만들지 않는다.
    """
    events = []
    by_slug = {i["slug"]: i for i in payload["native"]}
    burst_up = _fnum(cfg.get("protocol_burst_up_pct", 120.0))
    burst_dn = _fnum(cfg.get("protocol_burst_dn_pct", -60.0))
    pf_move = _fnum(cfg.get("protocol_pf_move_pct", 35.0))
    share_move = _fnum(cfg.get("protocol_share_move_pp", 8.0))

    # 1) 매출 급변 — 이력 없이도 API의 7일 평균 대비로 판정 가능
    for it in payload["native"]:
        b = it.get("burst_pct")
        if b is None or _fnum(it.get("fee30")) <= 0:
            continue
        sym = it.get("symbol") or it["slug"]
        if b >= burst_up:
            events.append({
                "code": "REV_SURGE", "slug": it["slug"], "symbol": sym, "window": "24h",
                "detail": "24h 수수료가 7일 평균 대비 %+.0f%% ($%s) — 플랫폼 활동 급증" % (b, _h(it["fee24"])),
                "severity": 8.0 + min(b / 100.0, 4.0),
            })
        elif b <= burst_dn:
            sev = 11.0 if b <= -85 else 8.5
            events.append({
                "code": "REV_COLLAPSE", "slug": it["slug"], "symbol": sym, "window": "24h",
                "detail": "24h 수수료가 7일 평균 대비 %.0f%% ($%s) — 가격보다 먼저 무너지는 구간" % (b, _h(it["fee24"])),
                "severity": sev,
            })
        if _fnum(it.get("fee24")) <= 0 < _fnum(it.get("fee7")):
            events.append({
                "code": "REV_ZERO", "slug": it["slug"], "symbol": sym, "window": "24h",
                "detail": "24시간 수수료 0 — 직전 7일에는 $%s. 서비스 중단 가능성" % _h(it["fee7"]),
                "severity": 12.0,
            })

    # 2) 배수 급변 (이력 필요)
    ref = _ref(history, 24, now_epoch)
    if ref:
        for slug, prev_pf in (ref.get("pf") or {}).items():
            cur = by_slug.get(slug)
            if not cur or not cur.get("pf") or not prev_pf:
                continue
            d = (cur["pf"] - prev_pf) / prev_pf * 100.0
            if abs(d) < pf_move:
                continue
            sym = cur.get("symbol") or slug
            code = "PF_CHEAP" if d < 0 else "PF_RERATE"
            events.append({
                "code": code, "slug": slug, "symbol": sym, "window": "24h",
                "detail": "매출배수 %.1f→%.1f배 (%+.0f%%)" % (prev_pf, cur["pf"], d),
                "severity": 6.5 + min(abs(d) / 40.0, 3.0),
            })

        # 3) 런치패드 점유율 이동
        prev_share = ref.get("lp_share") or {}
        cur_share = payload["shares"].get("launchpad_24h") or {}
        for slug, cs in cur_share.items():
            ps = prev_share.get(slug)
            if ps is None:
                continue
            dpp = cs - ps
            if abs(dpp) < share_move:
                continue
            it = by_slug.get(slug) or {}
            events.append({
                "code": "SHARE_SHIFT", "slug": slug,
                "symbol": it.get("symbol") or slug, "window": "24h",
                "detail": "런치패드 점유율 %.1f%%→%.1f%% (%+.1f%%p)" % (ps, cs, dpp),
                "severity": 7.0 + min(abs(dpp) / 10.0, 3.0),
            })

        # 4) 신규 진입
        seen = set(ref.get("fee24") or {})
        for it in payload["native"]:
            if it["slug"] not in seen and _fnum(it.get("fee30")) > 0:
                events.append({
                    "code": "NEW_PROTOCOL", "slug": it["slug"],
                    "symbol": it.get("symbol") or it["slug"], "window": "24h",
                    "detail": "%s 카테고리 신규 진입 · 30일 수수료 $%s"
                              % (CAT_KO.get(it.get("category"), it.get("category") or "-"), _h(it.get("fee30"))),
                    "severity": 6.0,
                })

    events.sort(key=lambda e: -e["severity"])
    return events


# ------------------------------------------------------------------ 렌더

def _h(v):
    v = _fnum(v)
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(v) >= div:
            return "%.2f%s" % (v / div, unit)
    return "%.0f" % v


def _esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def render_telegram(payload, events, cfg):
    """밈 브리프 뒤에 붙는 섹션. 길이가 한도를 먹지 않도록 압축한다."""
    if not payload or not payload.get("native"):
        return []
    top_n = int(cfg.get("protocol_top_n_telegram", 6))
    rank = [i for i in payload["native"] if i.get("value_rank")]
    rank.sort(key=lambda i: i["value_rank"])
    s = payload["summary"]

    lines = ["🏭 <b>플랫폼·프로토콜 트랙</b>",
             "체인 24h 수수료 $%s · 네이티브 %d종 · 배수산출 %d종"
             % (_h(s["chain_fee_24h"]), s["native_n"], s["rankable_n"])]

    if rank:
        lines.append("")
        lines.append("<b>매출배수 저평가 순 (FDV ÷ 연환산매출)</b>")
        for it in rank[:top_n]:
            mom = ("%+.0f%%" % it["momentum_pct"]) if it.get("momentum_pct") is not None else "–"
            mark = "*" if it.get("basis") == "FEES" else ""
            lines.append("%d. <b>%s</b> %.1f배%s · FDV $%s · 30d매출 $%s · 7d모멘텀 %s" % (
                it["value_rank"], _esc(it.get("symbol") or it["slug"]), it["pf"], mark,
                _h(it.get("fdv")), _h(it.get("rev30") or it.get("fee30")), mom))
        if any(i.get("basis") == "FEES" for i in rank[:top_n]):
            lines.append("<i>*매출 미집계 — 수수료 기준. 다른 종목과 직접 비교하지 마세요.</i>")

    big = [e for e in events if e["code"] in ("REV_ZERO", "REV_COLLAPSE", "REV_SURGE", "SHARE_SHIFT")][:5]
    if big:
        lines.append("")
        lines.append("📉 <b>매출·점유율 변화</b>")
        icon = {"REV_SURGE": "🔺", "REV_COLLAPSE": "🔻", "REV_ZERO": "⛔", "SHARE_SHIFT": "🔀"}
        for e in big:
            lines.append("%s %s — %s" % (icon.get(e["code"], "·"), _esc(e["symbol"]), _esc(e["detail"])))

    pfev = [e for e in events if e["code"] in ("PF_CHEAP", "PF_RERATE")][:3]
    if pfev:
        lines.append("")
        lines.append("💱 <b>배수 급변</b>")
        for e in pfev:
            lines.append("· %s — %s" % (_esc(e["symbol"]), _esc(e["detail"])))

    lp = payload["shares"].get("launchpad_24h") or {}
    if lp:
        top = sorted(lp.items(), key=lambda x: -x[1])[:4]
        lines.append("")
        lines.append("🏁 <b>런치패드 점유율(24h)</b> " +
                     " · ".join("%s %.0f%%" % (_esc(k), v) for k, v in top))

    tl = payload.get("tokenless") or []
    if tl:
        lines.append("")
        lines.append("🎁 <b>토큰 없는 네이티브</b> " +
                     ", ".join(_esc(i["name"]) for i in tl[:5]))
        lines.append("<i>매출은 나오는데 토큰이 없는 프로토콜입니다. 밸류에이션 대상이 아니라 관찰 대상입니다.</i>")

    lines.append("")
    lines.append("<i>배수는 후행 지표입니다. 이 체인의 런치패드 매출은 밈 발행 활동에 연동돼 "
                 "체인이 식으면 분모가 함께 무너집니다 — 싸 보이는 것이 안전을 뜻하지 않습니다.</i>")
    return lines


def render_html(payload, events, cfg):
    if not payload or not payload.get("native"):
        return "<div class='empty'>프로토콜 수수료 데이터를 불러오지 못했습니다.</div>"

    def flags_html(it):
        if not it.get("flags"):
            return "<span class='ok'>–</span>"
        return " ".join("<span class='flag f-%s' title=\"%s\">%s</span>"
                        % (f["code"], f["detail"].replace('"', "'"), f["code"]) for f in it["flags"])

    rank = sorted([i for i in payload["native"] if i.get("value_rank")], key=lambda i: i["value_rank"])
    trs = []
    for it in rank:
        mom = it.get("momentum_pct")
        mom_html = ("<span class='%s'>%+.0f%%</span>" % ("up" if mom >= 0 else "down", mom)) \
            if mom is not None else "<span class='dim'>–</span>"
        brs = it.get("burst_pct")
        brs_html = ("<span class='%s'>%+.0f%%</span>" % ("up" if brs >= 0 else "down", brs)) \
            if brs is not None else "<span class='dim'>–</span>"
        trs.append(
            "<tr><td class='rk'>%d</td>"
            "<td><b>%s</b><div class='nm'>%s · %s</div></td>"
            "<td class='num'><b>%.1f</b><div class='nm'>%s</div></td>"
            "<td class='num'>$%s</td><td class='num'>$%s</td><td class='num'>$%s</td>"
            "<td class='num'>%s</td><td class='num'>%s</td>"
            "<td class='num'>$%s</td><td>%s</td></tr>" % (
                it["value_rank"], _esc(it.get("symbol") or it["slug"]),
                _esc((it.get("name") or "")[:26]), _esc(CAT_KO.get(it.get("category"), it.get("category") or "-")),
                it["pf"], "매출기준" if it["basis"] == "REV" else "수수료기준",
                _h(it.get("fdv")), _h(it.get("rev30") or 0), _h(it.get("fee30")),
                mom_html, brs_html, _h(it.get("liq")), flags_html(it)))

    unranked = [i for i in payload["native"] if not i.get("value_rank")]
    un_html = ""
    if unranked:
        un_html = "<div class='note' style='margin-top:8px'>배수 미산출 %d종 — %s</div>" % (
            len(unranked), _esc(", ".join(
                "%s(%s)" % (i.get("symbol") or i["slug"],
                            (i["flags"][0]["code"] if i.get("flags") else "매출 0"))
                for i in unranked[:10])))

    ev_html = "".join(
        "<li><span class='evi'>%s</span> <b>%s</b> <span class='evc'>%s·%s</span> %s</li>" % (
            {"REV_SURGE": "🔺", "REV_COLLAPSE": "🔻", "REV_ZERO": "⛔", "SHARE_SHIFT": "🔀",
             "PF_CHEAP": "💚", "PF_RERATE": "🔶", "NEW_PROTOCOL": "🆕"}.get(e["code"], "·"),
            _esc(e["symbol"]), e["code"], e["window"], _esc(e["detail"]))
        for e in events[:16]) or "<li class='dim'>임계 초과 변화 없음</li>"

    lp = payload["shares"].get("launchpad_24h") or {}
    bars = ""
    for slug, pct in sorted(lp.items(), key=lambda x: -x[1])[:8]:
        bars += ("<div class='bar'><span class='bl'>%s</span>"
                 "<span class='bt'><i style='width:%.1f%%'></i></span>"
                 "<span class='bv'>%.1f%%</span></div>") % (_esc(slug), min(pct, 100.0), pct)
    bars = bars or "<div class='empty'>런치패드 점유율 산출 불가</div>"

    ext = payload.get("external") or []
    ext_html = "".join(
        "<tr><td>%s</td><td class='nm'>%s</td><td class='num'>$%s</td><td class='num'>$%s</td></tr>"
        % (_esc(i["name"]), _esc(CAT_KO.get(i.get("category"), i.get("category") or "-")),
           _h(i.get("fee24")), _h(i.get("fee30"))) for i in ext)
    tl = payload.get("tokenless") or []
    tl_html = "".join(
        "<tr><td>%s</td><td class='nm'>%s</td><td class='num'>$%s</td><td class='num'>$%s</td></tr>"
        % (_esc(i["name"]), _esc(CAT_KO.get(i.get("category"), i.get("category") or "-")),
           _h(i.get("fee24")), _h(i.get("fee30"))) for i in tl)

    s = payload["summary"]
    return """
<div class="kpis">
<div class="kpi"><b>$%s</b><span>체인 24h 수수료</span></div>
<div class="kpi"><b>$%s</b><span>체인 30일 수수료</span></div>
<div class="kpi"><b>%d</b><span>네이티브 프로토콜</span></div>
<div class="kpi"><b>%d</b><span>배수 산출</span></div>
<div class="kpi"><b>%d</b><span>토큰 없음(관찰)</span></div>
</div>
<h2 style="margin-top:12px">매출배수 저평가 순</h2>
<div class="tw"><table>
<tr><th>#</th><th>토큰 / 프로토콜</th><th>배수</th><th>FDV</th><th>30d 매출</th><th>30d 수수료</th>
<th>7d 모멘텀</th><th>24h 버스트</th><th>유동성</th><th>플래그</th></tr>
%s</table></div>%s
<h2 style="margin-top:16px">매출·점유율 변화</h2><ul>%s</ul>
<h2 style="margin-top:16px">런치패드 24h 수수료 점유율</h2>%s
<div class="note" style="margin-top:8px">런치패드는 이 체인에서 가장 경쟁이 격한 카테고리입니다.
NOXA가 7월 11~13일 사이 붕괴했을 때처럼, 개별 토큰 가격보다 점유율이 먼저 움직이는 경우가 있습니다.</div>
<h2 style="margin-top:16px">참고 — 다체인 프로토콜 (배수 랭킹 제외)</h2>
<div class="tw"><table><tr><th>프로토콜</th><th>분류</th><th>24h 수수료</th><th>30d 수수료</th></tr>%s</table></div>
<div class="note" style="margin-top:8px">이 체인에서 수수료를 벌지만 토큰 가치가 이 체인에 귀속되지 않습니다.
Uniswap V4가 30일 수수료 1위여도 UNI는 로빈후드 체인 플레이가 아니므로 배수 랭킹에 넣지 않습니다.</div>
<h2 style="margin-top:16px">토큰 없는 네이티브 (에어드랍 관찰)</h2>
<div class="tw"><table><tr><th>프로토콜</th><th>분류</th><th>24h 수수료</th><th>30d 수수료</th></tr>%s</table></div>
<div class="note" style="margin-top:10px">
· <b>배수 정의</b> — FDV ÷ (30일 매출 × 365/30). 매출이 집계되지 않는 프로토콜은 수수료로 대체하며
<code>FEES_BASIS</code>를 답니다. 두 기준은 서로 직접 비교하면 안 됩니다(수수료는 사용자가 낸 총액, 매출은 프로토콜 귀속분).<br>
· <b>배수는 후행 지표입니다.</b> 이 체인의 런치패드 매출은 밈 발행 활동에 거의 연동돼, 체인이 식으면 분모가 함께 무너집니다.
싸 보이는 것이 안전을 뜻하지 않습니다.<br>
· <b>30일 연환산의 편향</b> — 메인넷 가동 2개월짜리 체인입니다. 최근 30일이 예외적으로 뜨거웠다면 연환산은 낙관 쪽으로 틀립니다.<br>
· <b>편입 규칙</b> — DefiLlama 기준 30일 수수료 $%s 이상 + 단일체인(Robinhood Chain) + 토큰 심볼 보유.
고정 목록이 아니므로 신규 프로토콜은 다음 실행에 자동 편입됩니다.<br>
· <b>유동성 필터</b> — 유동성 $%s 미만은 배수가 싸도 랭킹에서 제외합니다. 실제로 체결되지 않는 가격이기 때문입니다.<br>
· <b>시총 소스</b> — GeckoTerminal 우선, 풀 색인 누락 시 DexScreener로 보강합니다
(ARROW 실측: GT 유동성 $3.5 vs DS $10.5M). 두 소스 FDV가 8%% 넘게 갈리면 <code>SRC_GAP</code>을 답니다.<br>
· 데이터: DefiLlama fees/revenue + GeckoTerminal + DexScreener. 프로토콜 트랙이 실패해도 밈 트랙은 정상 발송됩니다.
</div>""" % (_h(s["chain_fee_24h"]), _h(s["chain_fee_30d"]), s["native_n"], s["rankable_n"],
             s["tokenless_n"], "".join(trs) or "<tr><td colspan='10' class='dim'>산출 가능한 종목 없음</td></tr>",
             un_html, ev_html, bars,
             ext_html or "<tr><td colspan='4' class='dim'>–</td></tr>",
             tl_html or "<tr><td colspan='4' class='dim'>–</td></tr>",
             _h(cfg.get("protocol_min_fee30_usd", 20000)),
             _h(cfg.get("protocol_min_liq_usd", 50000)))
