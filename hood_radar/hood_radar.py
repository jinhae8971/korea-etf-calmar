#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HOOD RADAR — Robinhood Chain memecoin market-cap ranking & rank-change detector.

설계 원칙
  1) 관측기이지 예측기가 아니다. 순위·변동은 "지금 자금이 어디에 반응하는가"의 서술이며
     미래 수익률을 주장하지 않는다.
  2) 수집 실패 시 "변화 없음"을 보내지 않는다 → "판정 불가" 명시 후 exit(1).
  3) 유니버스는 고정 목록이 아니라 규칙(유동성·거래대금·시총 임계)으로 자동 구성한다.
     신규 상장 밈코인이 임계를 넘으면 다음 실행에서 자동 편입된다.
  4) 토큰의 정체성은 심볼이 아니라 컨트랙트 주소다(카피캣 대량 발생 체인).

외부 의존성 0 (stdlib only).
"""

import json
import os
import random
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backfill          # noqa: E402
import backtest          # noqa: E402
import crosscheck        # noqa: E402
import security          # noqa: E402

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE, "data")
API = "https://api.geckoterminal.com/api/v2"
KST = timezone(timedelta(hours=9))

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36",
]
BACKOFF = [8, 20, 45, 90, 150]


# ---------------------------------------------------------------- utilities
def now_kst():
    return datetime.now(timezone.utc).astimezone(KST)


def fnum(v, default=0.0):
    """None/빈문자열/음수쓰레기에 강한 float 변환."""
    try:
        if v is None:
            return default
        f = float(v)
        if f != f or f in (float("inf"), float("-inf")):
            return default
        return f
    except (TypeError, ValueError):
        return default


def load_cfg():
    with open(os.path.join(BASE, "config.json"), "r", encoding="utf-8") as fh:
        return json.load(fh)


def read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def write_json_if_changed(path, obj, ignore_keys=()):
    """내용이 실질적으로 같으면 쓰지 않는다(멱등 — 빈 커밋 방지)."""
    def strip(o):
        if isinstance(o, dict):
            return {k: strip(v) for k, v in o.items() if k not in ignore_keys}
        if isinstance(o, list):
            return [strip(x) for x in o]
        return o

    old = read_json(path, None)
    if old is not None and strip(old) == strip(obj):
        return False
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=1)
    return True


# ---------------------------------------------------------------- fetching
def http_get_json(url, tries=len(BACKOFF)):
    last = None
    for i in range(tries):
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": random.choice(USER_AGENTS),
                "Accept": "application/json;version=20230302",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=40) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:  # pragma: no cover - network
            last = exc
            wait = BACKOFF[min(i, len(BACKOFF) - 1)]
            ra = exc.headers.get("Retry-After") if exc.headers else None
            if ra:
                try:
                    wait = max(wait, int(float(ra)))
                except ValueError:
                    pass
            if exc.code not in (429, 500, 502, 503, 504):
                raise
            print("[fetch] HTTP %s — %ss 후 재시도 (%d/%d)" % (exc.code, wait, i + 1, tries))
            time.sleep(wait + random.uniform(0, 3))
        except Exception as exc:  # pragma: no cover - network
            last = exc
            wait = BACKOFF[min(i, len(BACKOFF) - 1)]
            print("[fetch] %s — %ss 후 재시도 (%d/%d)" % (exc, wait, i + 1, tries))
            time.sleep(wait + random.uniform(0, 3))
    raise RuntimeError("fetch 실패: %s (%s)" % (url, last))


def fetch_pools(cfg):
    """상위 pool_pages 페이지의 풀 + 토큰 메타를 수집."""
    pools, tokens = [], {}
    for page in range(1, int(cfg["pool_pages"]) + 1):
        url = "%s/networks/%s/pools?page=%d&sort=h24_volume_usd_desc&include=base_token,quote_token" % (
            API, cfg["network"], page,
        )
        payload = http_get_json(url)
        rows = payload.get("data") or []
        if not rows:
            break
        pools.extend(rows)
        for tok in payload.get("included") or []:
            tokens[tok["id"]] = tok.get("attributes") or {}
        if page < int(cfg["pool_pages"]):
            time.sleep(float(cfg["page_sleep_sec"]))
    return pools, tokens


# ---------------------------------------------------------------- classify
def classify(meta, cfg):
    """INFRA(스테이블·랩드) / RWA(토큰화 주식·ETF) / MEME(체인 네이티브 커뮤니티 토큰)."""
    sym = (meta.get("symbol") or "").upper()
    name = (meta.get("name") or "").lower()
    cg = (meta.get("coingecko_coin_id") or "").lower()

    if sym in [s.upper() for s in cfg["infra_symbols"]]:
        return "INFRA"
    for marker in cfg["rwa_name_markers"]:
        if marker in name:
            return "RWA"
    for suf in cfg["rwa_cg_suffixes"]:
        if cg.endswith(suf):
            return "RWA"
    return "MEME"


def pick_mcap(mc, fdv, cfg):
    """
    브릿지 토큰은 market_cap_usd가 전 체인 합산이라 이 체인 순위를 왜곡한다.
    mc > fdv * ratio 이면 교차체인 시총으로 보고 FDV를 쓴다.
    반환: (사용시총, 기준문자열, bridged여부)
    """
    ratio = float(cfg["bridged_mc_fdv_ratio"])
    if mc > 0 and fdv > 0:
        if mc > fdv * ratio:
            return fdv, "FDV", True
        return mc, "MC", False
    if mc > 0:
        return mc, "MC", False
    if fdv > 0:
        return fdv, "FDV", False
    return 0.0, "NONE", False


def build_universe(pools, tokens, cfg):
    """풀 단위 데이터를 base token(컨트랙트 주소) 단위로 집계."""
    agg = {}
    for pool in pools:
        attrs = pool.get("attributes") or {}
        rel = (pool.get("relationships") or {}).get("base_token") or {}
        tid = ((rel.get("data") or {}).get("id")) or ""
        if not tid:
            continue
        meta = tokens.get(tid, {})
        addr = (meta.get("address") or tid.split("_")[-1]).lower()

        vol = attrs.get("volume_usd") or {}
        chg = attrs.get("price_change_percentage") or {}
        txn = (attrs.get("transactions") or {}).get("h24") or {}
        reserve = fnum(attrs.get("reserve_in_usd"))
        neg = reserve < 0
        reserve = max(reserve, 0.0)

        ent = agg.get(addr)
        if ent is None:
            ent = agg[addr] = {
                "address": addr,
                "symbol": (meta.get("symbol") or "?").strip(),
                "name": (meta.get("name") or "").strip(),
                "cg_id": meta.get("coingecko_coin_id"),
                "class": classify(meta, cfg),
                "mc_raw": 0.0,
                "fdv": 0.0,
                "price": 0.0,
                "liq": 0.0,
                "v24": 0.0,
                "v6": 0.0,
                "chg24": None,
                "chg6": None,
                "buys24": 0,
                "sells24": 0,
                "pools": 0,
                "top_pool_liq": 0.0,
                "top_pool_address": None,
                "oldest_pool": None,
                "data_warn": False,
            }
        ent["pools"] += 1
        ent["liq"] += reserve
        ent["v24"] += fnum(vol.get("h24"))
        ent["v6"] += fnum(vol.get("h6"))
        ent["buys24"] += int(fnum(txn.get("buys")))
        ent["sells24"] += int(fnum(txn.get("sells")))
        ent["mc_raw"] = max(ent["mc_raw"], fnum(attrs.get("market_cap_usd")))
        ent["fdv"] = max(ent["fdv"], fnum(attrs.get("fdv_usd")))
        if neg:
            ent["data_warn"] = True
        # 가격·변동률은 유동성이 가장 깊은 풀 기준(얕은 풀의 노이즈 배제)
        if reserve >= ent["top_pool_liq"]:
            ent["top_pool_liq"] = reserve
            ent["top_pool_address"] = attrs.get("address")
            ent["price"] = fnum(attrs.get("base_token_price_usd"))
            ent["chg24"] = fnum(chg.get("h24"), None) if chg.get("h24") is not None else None
            ent["chg6"] = fnum(chg.get("h6"), None) if chg.get("h6") is not None else None
        created = attrs.get("pool_created_at")
        if created and (ent["oldest_pool"] is None or created < ent["oldest_pool"]):
            ent["oldest_pool"] = created

    for ent in agg.values():
        mcap, basis, bridged = pick_mcap(ent["mc_raw"], ent["fdv"], cfg)
        ent["mcap"] = mcap
        ent["mcap_basis"] = basis
        ent["bridged"] = bridged
        ent["turnover"] = (ent["v24"] / mcap) if mcap > 0 else 0.0
        ent["mc_liq"] = (mcap / ent["liq"]) if ent["liq"] > 0 else None
    return agg


def fetch_tokens_multi(addresses, cfg, log=print):
    """거래대금 상위 풀 밖으로 밀려난 추적 대상을 주소로 직접 조회한다(최대 30개/콜)."""
    out = {}
    addrs = list(addresses)
    for i in range(0, len(addrs), 30):
        chunk = addrs[i:i + 30]
        url = "%s/networks/%s/tokens/multi/%s" % (API, cfg["network"], ",".join(chunk))
        try:
            payload = http_get_json(url, tries=2)
        except Exception as exc:
            log("[sticky] 직접 조회 실패: %s" % exc)
            continue
        for tok in payload.get("data") or []:
            attrs = tok.get("attributes") or {}
            addr = (attrs.get("address") or "").lower()
            if addr:
                out[addr] = attrs
        time.sleep(float(cfg["page_sleep_sec"]))
    return out


def apply_sticky(universe, tracked, cfg, log=print):
    """
    유니버스는 '24h 거래대금 상위 200풀'로 구성되므로, 시총이 큰데 거래가 식은 토큰은
    통째로 사라져 DROPPED_OUT 오탐이 난다. 과거에 편입됐던 주소는 직접 조회로 되살린다.
    """
    missing = [a for a in tracked if a not in universe][: cfg["sticky_max"]]
    if not missing:
        return universe, 0
    fetched = fetch_tokens_multi(missing, cfg, log)
    added = 0
    for addr, attrs in fetched.items():
        mcap, basis, bridged = pick_mcap(fnum(attrs.get("market_cap_usd")),
                                         fnum(attrs.get("fdv_usd")), cfg)
        v24 = fnum((attrs.get("volume_usd") or {}).get("h24"))
        liq = fnum(attrs.get("total_reserve_in_usd"))
        universe[addr] = {
            "address": addr, "symbol": (attrs.get("symbol") or "?").strip(),
            "name": (attrs.get("name") or "").strip(),
            "cg_id": attrs.get("coingecko_coin_id"),
            "class": classify(attrs, cfg),
            "mc_raw": fnum(attrs.get("market_cap_usd")), "fdv": fnum(attrs.get("fdv_usd")),
            "price": fnum(attrs.get("price_usd")), "liq": liq, "v24": v24, "v6": 0.0,
            "chg24": None, "chg6": None, "buys24": 0, "sells24": 0, "pools": 0,
            "top_pool_liq": 0.0, "top_pool_address": None, "oldest_pool": None,
            "data_warn": False, "mcap": mcap, "mcap_basis": basis, "bridged": bridged,
            "turnover": (v24 / mcap) if mcap > 0 else 0.0,
            "mc_liq": (mcap / liq) if liq > 0 else None,
            "revived": True,
        }
        added += 1
    log("[sticky] 상위 풀 밖 추적 대상 %d건 중 %d건 복원" % (len(missing), added))
    return universe, added


def gate(universe, cfg):
    """밈코인 랭킹 대상만 통과시킨다."""
    out = []
    dropped = {"infra": 0, "rwa": 0, "liq": 0, "vol": 0, "mcap": 0}
    for ent in universe.values():
        if ent["class"] == "INFRA":
            dropped["infra"] += 1
            continue
        if ent["class"] == "RWA":
            dropped["rwa"] += 1
            continue
        if ent["liq"] < cfg["min_liquidity_usd"] and not ent.get("revived"):
            dropped["liq"] += 1
            continue
        if ent["v24"] < cfg["min_volume24_usd"]:
            dropped["vol"] += 1
            continue
        if ent["mcap"] < cfg["min_mcap_usd"]:
            dropped["mcap"] += 1
            continue
        out.append(ent)
    out.sort(key=lambda e: -e["mcap"])
    for idx, ent in enumerate(out, 1):
        ent["rank"] = idx
    return out, dropped


# ---------------------------------------------------------------- risk flags
def tag_risks(rows, cfg, prev_snapshot):
    sym_count = {}
    for row in rows:
        key = row["symbol"].upper()
        sym_count[key] = sym_count.get(key, 0) + 1

    prev_liq = (prev_snapshot or {}).get("liq", {})
    now = now_kst()
    for row in rows:
        flags = []
        if row["mc_liq"] is not None and row["mc_liq"] >= cfg["risk_mc_liq_ratio"]:
            flags.append({"code": "LIQ_THIN", "detail": "시총/유동성 %.0f배" % row["mc_liq"]})
        if sym_count.get(row["symbol"].upper(), 0) > 1:
            flags.append({"code": "COPYCAT", "detail": "동일 티커 컨트랙트 %d개" % sym_count[row["symbol"].upper()]})
        if row["pools"] == 1 and row["liq"] < cfg["min_liquidity_usd"] * 8:
            flags.append({"code": "SINGLE_POOL", "detail": "단일 풀 · 유동성 $%s" % human(row["liq"])})
        if row["data_warn"]:
            flags.append({"code": "DATA_WARN", "detail": "음수 유동성 보고 — 지표 신뢰도 낮음"})
        if row["bridged"]:
            flags.append({"code": "BRIDGED_MC", "detail": "교차체인 시총 감지 → FDV로 대체"})
        if row["oldest_pool"]:
            try:
                created = datetime.strptime(row["oldest_pool"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                age_h = (now - created).total_seconds() / 3600.0
                row["age_hours"] = round(age_h, 1)
                if age_h < cfg["risk_young_hours"]:
                    flags.append({"code": "YOUNG", "detail": "풀 생성 %.0f시간 경과" % age_h})
            except ValueError:
                row["age_hours"] = None
        else:
            row["age_hours"] = None
        old_liq = fnum(prev_liq.get(row["address"]))
        if old_liq > 0:
            drain = (row["liq"] - old_liq) / old_liq * 100.0
            row["liq_chg6"] = round(drain, 1)
            if drain <= cfg["risk_liq_drain_pct"]:
                flags.append({"code": "LIQ_DRAIN", "detail": "6시간 유동성 %.0f%%" % drain})
        else:
            row["liq_chg6"] = None
        row["flags"] = flags
    return rows


def attach_security(rows, cache, cfg):
    """GoPlus 요약을 행에 붙이고 보안 플래그를 병합한다."""
    for row in rows:
        summary = cache.get(row["address"])
        row["security"] = summary
        row["flags"].extend(security.flags_for(summary, cfg))
    return rows


def attach_promotion(rows, boosts):
    for row in rows:
        kinds = boosts.get(row["address"])
        row["promoted"] = kinds or None
        if kinds:
            row["flags"].append({
                "code": "PROMOTED",
                "detail": "DexScreener 유료 프로모션 등재(%s) — 품질 보증 아님" % ",".join(kinds),
            })
    return rows


def attach_crosscheck(rows, cross, tolerance):
    for row in rows:
        info = cross.get(row["address"])
        row["crosscheck"] = info
        if info and abs(info["gap_pct"]) >= tolerance:
            row["flags"].append({
                "code": "SRC_DIVERGENCE",
                "detail": "2차 소스 시총 괴리 %+.1f%%" % info["gap_pct"],
            })
    return rows


# ---------------------------------------------------------------- change detection
def snapshot_of(rows, ts, chain_v24=None):
    return {
        "ts": ts,
        "rank": {r["address"]: r["rank"] for r in rows},
        "mcap": {r["address"]: round(r["mcap"], 2) for r in rows},
        "liq": {r["address"]: round(r["liq"], 2) for r in rows},
        "symbol": {r["address"]: r["symbol"] for r in rows},
        # 거래대금 추세용 — 체인 전체(스캔 풀 합계)와 추적 종목 합계를 같이 남긴다.
        "chain_v24": round(chain_v24, 2) if chain_v24 else None,
        "tracked_v24": round(sum(r.get("v24") or 0.0 for r in rows), 2),
    }


def pick_reference(history, hours, now_ts):
    """now보다 최소 `hours` 이전 스냅샷 중 가장 가까운 것."""
    target = now_ts - timedelta(hours=hours)
    best, best_gap = None, None
    for snap in history:
        try:
            ts = datetime.fromisoformat(snap["ts"])
        except (KeyError, ValueError):
            continue
        if ts > target:
            continue
        gap = (target - ts).total_seconds()
        if best_gap is None or gap < best_gap:
            best, best_gap = snap, gap
    return best


def paired_ranks(rows, ref):
    """
    과거 스냅샷과 현재를 **같은 모집단**에서 비교한다.

    유니버스 크기는 매 실행 달라지고(신규 편입·이탈), 소급 백필 스냅샷은 20종 안팎이다.
    모집단이 다른 순위를 그대로 빼면 "13위 → 23위"처럼 실재하지 않는 하락이 만들어진다.
    따라서 양쪽에 모두 존재하는 주소만 남겨 각각 다시 순위를 매긴 뒤 비교한다.

    반환: {address: (과거순위, 현재순위)} — 교집합이 5종 미만이면 빈 dict(비교 포기).
    """
    if not ref:
        return {}
    ref_rank = ref.get("rank") or {}
    common = [r for r in rows if r["address"] in ref_rank]
    if len(common) < 5:
        return {}
    cur_sorted = sorted(common, key=lambda r: -r["mcap"])
    cur = {r["address"]: i + 1 for i, r in enumerate(cur_sorted)}
    past_sorted = sorted((r["address"] for r in common), key=lambda a: ref_rank[a])
    past = {a: i + 1 for i, a in enumerate(past_sorted)}
    return {a: (past[a], cur[a]) for a in cur}


def detect_changes(rows, history, cfg, now_ts):
    ref6 = pick_reference(history, 5, now_ts)      # 6시간 주기 → 5시간 이상 경과분
    ref24 = pick_reference(history, 22, now_ts)    # 하루 전
    prev = history[-1] if history else None
    events = []

    pair6 = paired_ranks(rows, ref6)
    pair24 = paired_ranks(rows, ref24)

    def mcap_of(snap, addr):
        if not snap:
            return None
        v = (snap.get("mcap") or {}).get(addr)
        return v if v else None

    # 신규 진입 판정은 **실측 스냅샷**만 근거로 한다.
    # 백필은 상위 소수 종목만 담고 있어, 거기 없다는 사실이 "신규"를 뜻하지 않는다.
    live_hist = [s for s in history if s.get("source") != "ohlcv_backfill"]
    known_before = set()
    for snap in live_hist[-8:]:
        known_before |= set((snap.get("rank") or {}).keys())

    for row in rows:
        addr = row["address"]
        p6 = pair6.get(addr)
        p24 = pair24.get(addr)
        row["rank_6h_ago"] = p6[0] if p6 else None
        row["rank_24h_ago"] = p24[0] if p24 else None
        row["d_rank_6h"] = (p6[0] - p6[1]) if p6 else None   # 양수 = 순위 상승
        row["d_rank_24h"] = (p24[0] - p24[1]) if p24 else None
        m24 = mcap_of(ref24, addr)
        row["d_mcap_24h_pct"] = round((row["mcap"] - m24) / m24 * 100.0, 1) if m24 else None

        if row["d_rank_24h"] is not None and abs(row["d_rank_24h"]) >= cfg["rank_move_24h_threshold"]:
            events.append({
                "code": "RANK_SURGE" if row["d_rank_24h"] > 0 else "RANK_DROP",
                "window": "24h",
                "symbol": label(row), "address": addr,
                "detail": "%d위 → %d위 (%+d계단, 공통 %d종 기준)" % (
                    p24[0], p24[1], row["d_rank_24h"], len(pair24)),
                "mcap": row["mcap"], "severity": abs(row["d_rank_24h"]),
            })
        elif row["d_rank_6h"] is not None and abs(row["d_rank_6h"]) >= cfg["rank_move_6h_threshold"]:
            events.append({
                "code": "RANK_SURGE" if row["d_rank_6h"] > 0 else "RANK_DROP",
                "window": "6h",
                "symbol": label(row), "address": addr,
                "detail": "%d위 → %d위 (%+d계단, 6시간, 공통 %d종 기준)" % (
                    p6[0], p6[1], row["d_rank_6h"], len(pair6)),
                "mcap": row["mcap"], "severity": abs(row["d_rank_6h"]),
            })

        if row["d_mcap_24h_pct"] is not None and abs(row["d_mcap_24h_pct"]) >= cfg["mcap_move_24h_pct"]:
            events.append({
                "code": "MCAP_SURGE" if row["d_mcap_24h_pct"] > 0 else "MCAP_COLLAPSE",
                "window": "24h",
                "symbol": label(row), "address": addr,
                "detail": "시총 %+.0f%% ($%s)" % (row["d_mcap_24h_pct"], human(row["mcap"])),
                "mcap": row["mcap"], "severity": abs(row["d_mcap_24h_pct"]) / 10.0,
            })

        if live_hist and addr not in known_before and row["rank"] <= cfg["new_entry_rank"]:
            events.append({
                "code": "NEW_ENTRY", "window": "6h",
                "symbol": label(row), "address": addr,
                "detail": "신규 진입 %d위 · 시총 $%s" % (row["rank"], human(row["mcap"])),
                "mcap": row["mcap"], "severity": max(1.0, (cfg["new_entry_rank"] - row["rank"]) / 2.0),
            })

        for flag in row["flags"]:
            if flag["code"] == "LIQ_DRAIN":
                events.append({
                    "code": "LIQ_DRAIN", "window": "6h",
                    "symbol": label(row), "address": addr,
                    "detail": flag["detail"], "mcap": row["mcap"], "severity": 8.0,
                })

    # 순위권에서 사라진 토큰 — 실측 직전 스냅샷 기준으로만 판정
    prev_live = live_hist[-1] if live_hist else None
    if prev_live:
        current = {r["address"] for r in rows}
        for addr, rank in (prev_live.get("rank") or {}).items():
            if addr not in current and rank <= cfg["new_entry_rank"]:
                events.append({
                    "code": "DROPPED_OUT", "window": "6h",
                    "symbol": (prev_live.get("symbol") or {}).get(addr, "?"), "address": addr,
                    "detail": "%d위에서 이탈 — 유동성/거래대금 임계 미달" % rank,
                    "mcap": 0.0, "severity": max(1.0, (cfg["new_entry_rank"] - rank) / 2.0),
                })

    events.sort(key=lambda e: -e["severity"])
    return events


# ---------------------------------------------------------------- rendering
def human(v):
    v = fnum(v)
    if v >= 1e9:
        return "%.2fB" % (v / 1e9)
    if v >= 1e6:
        return "%.1fM" % (v / 1e6)
    if v >= 1e3:
        return "%.1fK" % (v / 1e3)
    return "%.0f" % v


ARROW = {"RANK_SURGE": "🔺", "RANK_DROP": "🔻", "NEW_ENTRY": "🆕",
         "DROPPED_OUT": "⤵️", "MCAP_SURGE": "📈", "MCAP_COLLAPSE": "📉",
         "LIQ_DRAIN": "🩸"}


def has_flag(row, code):
    return any(f["code"] == code for f in row.get("flags", []))


def label(row):
    """동일 티커 카피캣이 존재하면 주소 앞자리를 붙여 구분한다."""
    if has_flag(row, "COPYCAT"):
        return "%s·%s" % (row["symbol"], row["address"][2:6])
    return row["symbol"]


def chg_str(row):
    """
    상장 24시간 미만 풀의 h24 변동률은 0에 가까운 기준가 대비라 수백만 %로 튄다.
    그대로 쓰면 지표가 아니라 노이즈이므로 신규는 '신규'로, 나머지는 ±999%로 클램프한다.
    """
    chg = row.get("chg24")
    if chg is None:
        return None, "–"
    if has_flag(row, "YOUNG"):
        return chg, "신규"
    if chg > 999:
        return chg, "+999%↑"
    if chg < -99.9:
        return chg, "-99%↓"
    return chg, "%+.0f%%" % chg


def render_telegram(payload, cfg, dash_url):
    rows = payload["rows"]
    ev = payload["events"]
    lines = []
    lines.append("🏹 <b>로빈후드 체인 밈코인 레이더</b>")
    lines.append("%s KST · 6시간 주기 · %s" % (payload["as_of_kst"], payload["data_status"]))
    lines.append("")

    lines.append("<b>시총 TOP %d</b>" % min(cfg["top_n_telegram"], len(rows)))
    for row in rows[: cfg["top_n_telegram"]]:
        if row["d_rank_24h"]:
            mark = "▲%d" % row["d_rank_24h"] if row["d_rank_24h"] > 0 else "▼%d" % abs(row["d_rank_24h"])
        else:
            mark = "–"
        _, chg = chg_str(row)
        lines.append("%2d. <b>%s</b> $%s %s <code>%s</code>" % (
            row["rank"], esc(label(row)), human(row["mcap"]), chg, mark))
    lines.append("")

    big = [e for e in ev if e["code"] in ("RANK_SURGE", "RANK_DROP", "NEW_ENTRY", "DROPPED_OUT")][:6]
    if big:
        lines.append("🚨 <b>순위 급변</b>")
        for e in big:
            lines.append("%s %s — %s" % (ARROW.get(e["code"], "·"), esc(e["symbol"]), esc(e["detail"])))
    else:
        lines.append("🚨 <b>순위 급변</b>: 임계(6h %d계단 / 24h %d계단) 초과 없음" % (
            cfg["rank_move_6h_threshold"], cfg["rank_move_24h_threshold"]))
    lines.append("")

    mc_ev = [e for e in ev if e["code"] in ("MCAP_SURGE", "MCAP_COLLAPSE")][:4]
    if mc_ev:
        lines.append("💥 <b>시총 급변(24h)</b>")
        for e in mc_ev:
            lines.append("%s %s — %s" % (ARROW.get(e["code"], "·"), esc(e["symbol"]), esc(e["detail"])))
        lines.append("")

    risky = []
    for row in rows[: cfg["top_n_dashboard"]]:
        codes = [f for f in row["flags"] if f["code"] in ("LIQ_DRAIN", "LIQ_THIN", "COPYCAT", "DATA_WARN")]
        if codes:
            risky.append("· %s — %s" % (esc(label(row)), esc("; ".join(f["detail"] for f in codes[:2]))))
    if risky:
        lines.append("⚠️ <b>리스크 플래그</b>")
        lines.extend(risky[:5])
        lines.append("")

    sec_ev = [e for e in ev if e["code"] == "SECURITY"][:5]
    if sec_ev:
        lines.append("🔐 <b>컨트랙트 경보</b>")
        for e in sec_ev:
            lines.append("· %s — %s" % (esc(e["symbol"]), esc(e["detail"])))
        lines.append("")

    unver = [r for r in rows[:20] if has_flag(r, "UNVERIFIED")]
    if unver:
        lines.append("🕳 <b>보안 미색인</b> %s" % esc(", ".join(label(r) for r in unver[:6])))
        lines.append("<i>스캐너에 잡히지 않는 신생·소형 토큰 — 안전이 아니라 검증 불가입니다.</i>")
        lines.append("")

    bt = payload.get("backtest") or {}
    if bt:
        lines.append("🧪 " + esc(backtest.render_line(bt)))
        if bt.get("verdict") in ("NEGATIVE", "NO_EDGE"):
            lines.append("<i>%s</i>" % esc(bt.get("note", "")))
        lines.append("")

    meta = payload["meta"]
    cc = meta.get("crosscheck") or {}
    lines.append("📊 추적 %d종 · 24h DEX 거래대금 $%s · 풀 %d개 스캔" % (
        len(rows), human(meta["chain_volume_24h"]), meta["pools_scanned"]))
    lines.append("🔎 2차 소스 대조 %s(%d종, 최대 괴리 %.1f%%) · 보안 캐시 %d종" % (
        cc.get("status", "-"), cc.get("checked", 0), cc.get("worst_gap_pct", 0.0),
        meta.get("security_cached", 0)))
    lines.append('<a href="%s">대시보드 열기</a>' % dash_url)
    lines.append("")
    lines.append("<i>관측 시스템입니다. 순위·변동은 자금 반응의 서술이며 수익을 보장하지 않습니다. "
                 "이 체인은 허니팟·카피캣이 다수 보고된 구간이므로 컨트랙트 주소를 반드시 직접 확인하세요.</i>")
    msg = "\n".join(lines)
    if len(msg) > 3900:  # 텔레그램 4096자 한도 — 태그가 끊기지 않도록 줄 단위로 자른다
        keep, total = [], 0
        for ln in lines:
            if total + len(ln) > 3700:
                break
            keep.append(ln); total += len(ln) + 1
        keep.append("… (이벤트가 많아 일부 생략 — 대시보드에서 전체 확인)")
        msg = "\n".join(keep)
    return msg


def render_dashboard(payload, cfg, out_path, history=None):
    rows = payload["rows"][: cfg["top_n_dashboard"]]
    ev = payload["events"]

    def flag_html(row):
        if not row["flags"]:
            return '<span class="ok">–</span>'
        return " ".join('<span class="flag f-%s" title="%s">%s</span>' % (
            f["code"], f["detail"].replace('"', "'"), f["code"]) for f in row["flags"])

    def darrow(d):
        if d is None:
            return '<span class="dim">new</span>'
        if d > 0:
            return '<span class="up">▲%d</span>' % d
        if d < 0:
            return '<span class="down">▼%d</span>' % abs(d)
        return '<span class="dim">–</span>'

    def sec_html(row):
        sec = row.get("security")
        if not sec:
            return '<span class="dim">–</span>'
        if not sec.get("indexed"):
            return '<span class="flag f-UNVERIFIED">미색인</span>'
        bits = []
        hp = str(sec.get("honeypot"))
        bits.append('<span class="ok2">허니팟 아님</span>' if hp == "0"
                    else ('<span class="bad">허니팟</span>' if hp == "1"
                          else '<span class="warn">판정불가</span>'))
        bits.append('<span class="ok2">오너 소각</span>' if sec.get("owner_renounced")
                    else '<span class="warn">오너 활성</span>')
        if str(sec.get("mintable")) == "1":
            bits.append('<span class="bad">발행가능</span>')
        if sec.get("holder_count"):
            bits.append('<span class="dim">홀더 %s</span>' % f"{sec['holder_count']:,}")
        if sec.get("top10_pct") is not None:
            bits.append('<span class="dim">상위10 %.1f%%</span>' % sec["top10_pct"])
        return "<div class='secbits'>" + " · ".join(bits) + "</div>"

    trs = []
    for row in rows:
        raw, txt = chg_str(row)
        if raw is None:
            chg_html = '<span class="dim">–</span>'
        elif txt == "신규":
            chg_html = '<span class="dim">신규</span>'
        else:
            chg_html = '<span class="%s">%s</span>' % ("up" if raw >= 0 else "down", txt)
        trs.append(
            "<tr><td class='rk'>%d</td>"
            "<td><b>%s</b><div class='nm'>%s</div>"
            "<div class='addr'>%s</div></td>"
            "<td class='num'>$%s<div class='nm'>%s</div></td>"
            "<td class='num'>%s</td><td class='num'>%s</td><td class='num'>%s</td>"
            "<td class='num'>$%s</td><td class='num'>$%s</td><td>%s</td><td>%s</td></tr>" % (
                row["rank"], esc(label(row)), esc(row["name"][:28]), row["address"][:10] + "…",
                human(row["mcap"]), row["mcap_basis"],
                darrow(row["d_rank_6h"]), darrow(row["d_rank_24h"]), chg_html,
                human(row["v24"]), human(row["liq"]), flag_html(row), sec_html(row),
            )
        )

    ev_html = "".join(
        "<li><span class='evi'>%s</span> <b>%s</b> <span class='evc'>%s·%s</span> %s</li>" % (
            ARROW.get(e["code"], "·"), esc(e["symbol"]), e["code"], e["window"], esc(e["detail"]))
        for e in ev[:20]
    ) or "<li class='dim'>임계 초과 이벤트 없음</li>"

    hist = payload.get("spark", {})
    spark_html = ""
    for sym, series in list(hist.items())[:8]:
        spark_html += "<div class='sp'><span>%s</span>%s</div>" % (esc(sym), sparkline(series))

    bt = payload.get("backtest") or {}
    vcolor = {"POSITIVE": "var(--up)", "NEGATIVE": "var(--down)",
              "NO_EDGE": "#d29922", "INSUFFICIENT": "var(--dim)"}.get(bt.get("verdict"), "var(--dim)")
    verdict_html = ("<div class='verdict' style='color:%s'>%s</div><div class='note'>%s</div>" % (
        vcolor, esc(backtest.render_line(bt)), esc(bt.get("note", "표본이 쌓이면 갱신됩니다."))))
    cc = (payload["meta"].get("crosscheck") or {})
    cc_html = ("<div class='note'>2차 소스(DexScreener) 대조 — 판정 <b>%s</b> · 대조 %d종 · "
               "중앙 괴리 %.2f%% · 최대 괴리 %.2f%%. 8%% 초과 시 해당 종목에 SRC_DIVERGENCE를 답니다.</div>" % (
                   esc(cc.get("status", "-")), cc.get("checked", 0),
                   cc.get("median_gap_pct", 0.0), cc.get("worst_gap_pct", 0.0)))

    html = DASH_TMPL.format(
        volchart=volume_chart(history or []),
        volshare=volume_share_chart(payload["rows"]),
        verdict=verdict_html,
        crosscheck=cc_html,
        promoted=payload["meta"].get("promoted", 0),
        as_of=esc(payload["as_of_kst"]),
        status=esc(payload["data_status"]),
        n=len(payload["rows"]),
        vol=human(payload["meta"]["chain_volume_24h"]),
        pools=payload["meta"]["pools_scanned"],
        rows="".join(trs),
        events=ev_html,
        sparks=spark_html,
        top_n=cfg["top_n_dashboard"],
        th6=cfg["rank_move_6h_threshold"],
        th24=cfg["rank_move_24h_threshold"],
    )
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(html)


def volume_chart(history, w=680, h=190):
    """
    24h DEX 거래대금 추세 — 실측 스냅샷만 사용한다.
    소급 백필 스냅샷에는 거래대금이 없으므로 섞지 않는다(있는 척하지 않는다).
    """
    pts = []
    for snap in history:
        if snap.get("source") == "ohlcv_backfill":
            continue
        cv = snap.get("chain_v24")
        if not cv:
            continue
        try:
            t = datetime.fromisoformat(snap["ts"])
        except (KeyError, ValueError):
            continue
        pts.append((t, float(cv), float(snap.get("tracked_v24") or 0.0)))
    pts.sort(key=lambda x: x[0])

    if len(pts) < 2:
        n = len(pts)
        cur = ("현재 $%s" % human(pts[0][1])) if n else "관측치 없음"
        return ("<div class='empty'>관측 스냅샷 %d개 — 추세선은 6시간마다 한 점씩 채워집니다. "
                "(%s)</div>" % (n, cur))

    pad_l, pad_r, pad_t, pad_b = 8, 8, 14, 22
    iw, ih = w - pad_l - pad_r, h - pad_t - pad_b
    vals = [p[1] for p in pts] + [p[2] for p in pts]
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or (hi or 1.0)
    lo = max(0.0, lo - span * 0.15)
    hi = hi + span * 0.15
    rng = (hi - lo) or 1.0
    step = iw / (len(pts) - 1)

    def xy(i, v):
        return pad_l + i * step, pad_t + ih - (v - lo) / rng * ih

    chain = " ".join("%.1f,%.1f" % xy(i, p[1]) for i, p in enumerate(pts))
    track = " ".join("%.1f,%.1f" % xy(i, p[2]) for i, p in enumerate(pts))
    x0, _ = xy(0, pts[0][1])
    xn, _ = xy(len(pts) - 1, pts[-1][1])
    area = "%s %.1f,%.1f %.1f,%.1f" % (chain, xn, pad_t + ih, x0, pad_t + ih)

    ticks = []
    for i, p in enumerate(pts):
        if len(pts) <= 6 or i in (0, len(pts) - 1) or i % max(1, len(pts) // 5) == 0:
            x, _ = xy(i, p[1])
            ticks.append("<text x='%.1f' y='%d' class='ax' text-anchor='middle'>%s</text>"
                         % (x, h - 6, p[0].strftime("%m/%d %H시")))

    last_c, last_t = pts[-1][1], pts[-1][2]
    delta = ""
    if len(pts) >= 2 and pts[-2][1]:
        d = (last_c - pts[-2][1]) / pts[-2][1] * 100.0
        delta = "<tspan class='%s'>%+.1f%%</tspan>" % ("up" if d >= 0 else "down", d)

    return ("""<svg viewBox="0 0 {w} {h}" class="chart" preserveAspectRatio="none" role="img">
<defs><linearGradient id="vg" x1="0" y1="0" x2="0" y2="1">
<stop offset="0%" stop-color="var(--acc)" stop-opacity=".35"/>
<stop offset="100%" stop-color="var(--acc)" stop-opacity="0"/></linearGradient></defs>
<polygon points="{area}" fill="url(#vg)"/>
<polyline points="{chain}" fill="none" stroke="var(--acc)" stroke-width="2"/>
<polyline points="{track}" fill="none" stroke="var(--dim)" stroke-width="1.4" stroke-dasharray="4 3"/>
{ticks}</svg>
<div class="legend"><span><i class="sw acc"></i>체인 전체 ${cur} {delta}</span>
<span><i class="sw dash"></i>추적 {n}종 합계 ${trk}</span>
<span class="dim">고 ${hi} / 저 ${lo}</span></div>""").format(
        w=w, h=h, area=area, chain=chain, track=track, ticks="".join(ticks),
        cur=human(last_c), trk=human(last_t), delta=delta,
        hi=human(max(p[1] for p in pts)), lo=human(min(p[1] for p in pts)),
        n=len(pts))


def volume_share_chart(rows, top=8):
    """현재 스냅샷의 거래대금 구성 — 이력이 없어도 오늘 바로 읽히는 정보."""
    ranked = sorted(rows, key=lambda r: -(r.get("v24") or 0.0))[:top]
    total = sum(r.get("v24") or 0.0 for r in rows) or 1.0
    if not ranked:
        return "<div class='empty'>표시할 종목이 없습니다.</div>"
    top_v = ranked[0].get("v24") or 1.0
    bars = []
    for row in ranked:
        v = row.get("v24") or 0.0
        bars.append(
            "<div class='bar'><span class='bl'>%s</span>"
            "<span class='bt'><i style='width:%.1f%%'></i></span>"
            "<span class='bv'>$%s<em>%.1f%%</em></span></div>" % (
                esc(label(row)), max(2.0, v / top_v * 100.0), human(v), v / total * 100.0))
    head = ("<div class='note' style='margin-bottom:8px'>추적 종목 합계 $%s 중 상위 %d종이 "
            "<b>%.0f%%</b>를 차지합니다.</div>" % (
                human(total), len(ranked),
                sum(r.get("v24") or 0.0 for r in ranked) / total * 100.0))
    return head + "".join(bars)


def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def sparkline(series, w=120, h=28):
    vals = [v for v in series if v is not None]
    if len(vals) < 2:
        return "<svg width='%d' height='%d'></svg>" % (w, h)
    lo, hi = min(vals), max(vals)
    rng = (hi - lo) or 1.0
    step = w / (len(vals) - 1)
    pts = " ".join("%.1f,%.1f" % (i * step, h - 2 - (v - lo) / rng * (h - 4)) for i, v in enumerate(vals))
    color = "var(--up)" if vals[-1] >= vals[0] else "var(--down)"
    return ("<svg width='%d' height='%d' viewBox='0 0 %d %d'>"
            "<polyline points='%s' fill='none' stroke='%s' stroke-width='1.6'/></svg>" % (w, h, w, h, pts, color))


DASH_TMPL = """<!doctype html><html lang="ko"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>HOOD RADAR — 로빈후드 체인 밈코인</title>
<style>
:root{{--bg:#0b0f14;--card:#131a23;--line:#1f2a37;--fg:#e6edf3;--dim:#8b98a5;--up:#3fb950;--down:#f85149;--acc:#4ea1ff;}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Noto Sans KR",sans-serif}}
.wrap{{max-width:1040px;margin:0 auto;padding:16px}}
h1{{font-size:19px;margin:0 0 4px}} .sub{{color:var(--dim);font-size:12px;margin-bottom:14px}}
.kpis{{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px}}
.kpi{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:9px 12px;flex:1;min-width:120px}}
.kpi b{{display:block;font-size:17px}} .kpi span{{color:var(--dim);font-size:11px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px;margin-bottom:14px}}
h2{{font-size:14px;margin:0 0 10px;color:var(--acc)}}
table{{width:100%;border-collapse:collapse;font-size:12.5px}}
th{{text-align:left;color:var(--dim);font-weight:500;font-size:11px;padding:6px 5px;border-bottom:1px solid var(--line);white-space:nowrap}}
td{{padding:7px 5px;border-bottom:1px solid rgba(31,42,55,.6);vertical-align:top}}
td.num{{text-align:right;white-space:nowrap}} td.rk{{color:var(--dim);width:26px}}
.nm{{color:var(--dim);font-size:10.5px}} .addr{{color:#5b6470;font-size:10px;font-family:ui-monospace,monospace}}
.up{{color:var(--up)}} .down{{color:var(--down)}} .dim{{color:var(--dim)}} .ok{{color:#3a4553}}
.flag{{display:inline-block;font-size:9.5px;padding:1px 5px;border-radius:20px;border:1px solid var(--line);color:var(--dim);margin:1px 1px 0 0;white-space:nowrap}}
.f-LIQ_DRAIN,.f-DATA_WARN{{color:var(--down);border-color:#5a2226}}
.f-LIQ_THIN,.f-COPYCAT,.f-OWNER_ACTIVE,.f-PROMOTED{{color:#d29922;border-color:#5a4a1e}}
.f-HONEYPOT,.f-CANNOT_SELL_ALL,.f-MINTABLE,.f-PAUSABLE,.f-SRC_DIVERGENCE{{color:var(--down);border-color:#5a2226}}
.f-UNVERIFIED,.f-HP_UNKNOWN{{color:#8b7cd6;border-color:#3d3466}}
.secbits{{font-size:10px;line-height:1.7}} .ok2{{color:var(--up)}} .bad{{color:var(--down)}} .warn{{color:#d29922}}
.verdict{{font-size:12.5px;padding:8px;border-radius:8px;border:1px solid var(--line);margin-bottom:8px}}
ul{{margin:0;padding-left:2px;list-style:none}} li{{padding:5px 0;border-bottom:1px solid rgba(31,42,55,.6);font-size:12.5px}}
.evi{{margin-right:4px}} .evc{{color:var(--dim);font-size:10.5px;margin-right:5px}}
.sp{{display:flex;align-items:center;gap:8px;font-size:11.5px;color:var(--dim)}} .sp span{{width:78px}}
.note{{color:var(--dim);font-size:11px;line-height:1.6}}
.tw{{overflow-x:auto}}
.chart{{width:100%;height:190px;display:block}} .ax{{fill:var(--dim);font-size:9px}}
.empty{{color:var(--dim);font-size:12px;padding:14px 4px;text-align:center;border:1px dashed var(--line);border-radius:8px}}
.legend{{display:flex;flex-wrap:wrap;gap:10px;font-size:11px;color:var(--fg);margin-top:6px;align-items:center}}
.sw{{display:inline-block;width:14px;height:0;border-top:2px solid var(--acc);margin-right:4px;vertical-align:middle}}
.sw.dash{{border-top:2px dashed var(--dim)}}
.bar{{display:flex;align-items:center;gap:7px;margin-bottom:6px;font-size:11.5px}}
.bl{{width:88px;flex:none;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.bt{{flex:1;height:9px;background:rgba(78,161,255,.10);border-radius:5px;overflow:hidden}}
.bt i{{display:block;height:100%;background:var(--acc);border-radius:5px}}
.bv{{width:96px;flex:none;text-align:right;color:var(--dim);white-space:nowrap}}
.bv em{{font-style:normal;color:#5b6470;margin-left:5px}}
</style></head><body><div class="wrap">
<h1>🏹 HOOD RADAR</h1>
<div class="sub">로빈후드 체인(Arbitrum Orbit L2) 커뮤니티 토큰 시총 순위 · 6시간 주기 갱신 · 기준 {as_of} KST · 상태 {status}</div>
<div class="kpis">
<div class="kpi"><b>{n}</b><span>추적 종목</span></div>
<div class="kpi"><b>${vol}</b><span>24h DEX 거래대금</span></div>
<div class="kpi"><b>{pools}</b><span>스캔 풀</span></div>
<div class="kpi"><b>6h</b><span>탐지 주기</span></div>
</div>
<div class="card"><h2>순위 변동·이벤트</h2><ul>{events}</ul>
<div class="note" style="margin-top:8px">임계 — 6시간 {th6}계단 / 24시간 {th24}계단 이상, 24h 시총 ±40%, 6시간 유동성 −40%.</div></div>
<div class="card"><h2>24h DEX 거래대금 추세</h2>{volchart}
<div class="note" style="margin-top:8px">스냅샷마다 기록되는 <b>그 시점의 직전 24시간</b> 거래대금입니다(누적 아님).
소급 이력 스냅샷에는 거래대금이 없어 실측 관측치만 그립니다 — 6시간마다 한 점씩 채워집니다.</div></div>
<div class="card"><h2>거래대금 구성 (현재)</h2>{volshare}</div>
<div class="card"><h2>시총 순위 TOP {top_n}</h2><div class="tw"><table>
<tr><th>#</th><th>토큰</th><th>시총</th><th>6h</th><th>24h</th><th>가격24h</th><th>거래대금</th><th>유동성</th><th>플래그</th><th>컨트랙트 검증</th></tr>
{rows}</table></div></div>
<div class="card"><h2>시총 추이 (최근 스냅샷)</h2>{sparks}</div>
<div class="card"><h2>예측력 검증</h2>{verdict}
<div class="note" style="margin-top:8px">순위 급등 종목의 이후 24시간 시총 변화에서 유니버스 중앙값을 뺀 초과수익을 측정합니다.
결과가 음(-)이나 무의미로 나와도 그대로 표기합니다 — 이 시스템이 매수 신호로 오독되지 않게 하는 것이 이 측정의 목적입니다.</div></div>
<div class="card"><h2>데이터 건전성</h2>{crosscheck}
<div class="note" style="margin-top:6px">DexScreener 유료 프로모션 등재 <b>{promoted}종</b> — 홍보 집행 신호이며 품질 보증이 아닙니다.</div></div>
<div class="card"><h2>읽는 법과 한계</h2><div class="note">
· <b>관측기이지 예측기가 아닙니다.</b> 순위와 변동은 "지금 자금이 어디에 반응하는가"의 서술이며 미래 수익률을 주장하지 않습니다.<br>
· <b>시총 기준</b> — MC는 유통시총, FDV는 완전희석시총. 교차체인 브릿지 토큰은 타 체인 시총이 섞여 들어오므로 FDV로 대체하고 BRIDGED_MC로 표시합니다.<br>
· <b>토큰 식별은 컨트랙트 주소</b>로 합니다. 이 체인은 동일 티커 카피캣이 다수 존재해 심볼로는 구분되지 않습니다(COPYCAT 플래그).<br>
· <b>제외 대상</b> — 스테이블·랩드 토큰, 토큰화 주식/ETF(<code>Robinhood Token</code> 표기 또는 CoinGecko id 접미사로 판별). 티커만 겹치는 코인을 오제외하지 않기 위해 티커 목록만으로는 제외하지 않습니다.<br>
· <b>편입 규칙</b> — 유동성 ≥ $25K, 24h 거래대금 ≥ $50K, 시총 ≥ $300K를 모두 충족하면 자동 편입됩니다. 고정 목록이 아니므로 신규 상장분도 다음 실행에 잡힙니다.<br>
· <b>LIQ_THIN / LIQ_DRAIN / YOUNG / SINGLE_POOL</b>은 러그풀·허니팟 개연성이 높은 조건입니다. 진입 전 컨트랙트와 유동성 락을 직접 확인하십시오.<br>
· <b>컨트랙트 검증</b>은 GoPlus(chain 4663) 기준입니다. LP 락 비율은 이 체인에서 전 종목 0%로 관측되는데,
락커 컨트랙트를 스캐너가 인식하지 못하는 것일 수 있어 "락 없음"이 아니라 <b>확인 불가</b>로 봅니다.<br>
· <b>미색인</b>은 안전이 아니라 검증 불가입니다. 신생·소형일수록 색인되지 않습니다.<br>
· <b>소급 이력</b>(source=ohlcv_backfill)은 현재 공급량을 과거에 그대로 적용해 재구성한 값이라, 추가발행·소각이 있었다면 왜곡됩니다. 실측 스냅샷이 우선합니다.<br>
· 데이터: GeckoTerminal 공개 API(온체인 DEX 집계) + DexScreener 교차검증 + GoPlus 보안. 수집 실패 시 "판정 불가"로 표기하며 정상 브리프를 발송하지 않습니다.
</div></div>
</div></body></html>"""


# ---------------------------------------------------------------- main
def main():
    cfg = load_cfg()
    now = now_kst()
    hist_path = os.path.join(DATA_DIR, "history.json")
    latest_path = os.path.join(DATA_DIR, "latest.json")
    sec_path = os.path.join(DATA_DIR, "security.json")
    tracked_path = os.path.join(DATA_DIR, "tracked.json")
    bf_marker = os.path.join(DATA_DIR, "backfill_done.json")

    history = read_json(hist_path, [])
    prev = None
    for snap in reversed(history):
        if snap.get("source") != "ohlcv_backfill":
            prev = snap
            break

    try:
        pools, tokens = fetch_pools(cfg)
    except Exception as exc:
        print("[fatal] 수집 실패: %s" % exc)
        write_json_if_changed(latest_path, {
            "as_of_kst": now.strftime("%Y-%m-%d %H:%M"),
            "data_status": "판정 불가 (수집 실패)",
            "message": "🏹 <b>로빈후드 체인 밈코인 레이더</b>\n%s KST\n\n⚠️ <b>판정 불가</b> — 온체인 데이터 수집에 실패했습니다.\n순위 변동 여부를 판단할 수 없어 정상 브리프를 발송하지 않습니다.\n사유: %s" % (
                now.strftime("%Y-%m-%d %H:%M"), esc(str(exc))[:200]),
            "rows": [], "events": [],
        })
        return 1

    universe = build_universe(pools, tokens, cfg)

    tracked = read_json(tracked_path, {})
    revived = 0
    if cfg.get("sticky_universe") and tracked:
        universe, revived = apply_sticky(universe, list(tracked.keys()), cfg)

    rows, dropped = gate(universe, cfg)

    if len(rows) < cfg["coverage_min_tokens"]:
        print("[fatal] coverage 부족: %d종" % len(rows))
        write_json_if_changed(latest_path, {
            "as_of_kst": now.strftime("%Y-%m-%d %H:%M"),
            "data_status": "판정 불가 (coverage %d종)" % len(rows),
            "message": "🏹 <b>로빈후드 체인 밈코인 레이더</b>\n%s KST\n\n⚠️ <b>판정 불가</b> — 임계를 통과한 종목이 %d종뿐입니다(최소 %d종).\n데이터 소스 이상 또는 체인 활동 급감 가능성이 있어 순위 판정을 보류합니다." % (
                now.strftime("%Y-%m-%d %H:%M"), len(rows), cfg["coverage_min_tokens"]),
            "rows": [], "events": [],
        })
        return 1

    rows = tag_risks(rows, cfg, prev)

    # ---- 보안 검증 (GoPlus, 캐시·로테이션) ----
    sec_cache = {}
    if cfg.get("security_enabled"):
        try:
            sec_cache = security.refresh(
                [r["address"] for r in rows[: cfg["security_top_n"]]], sec_path,
                refresh_per_run=cfg["security_refresh_per_run"],
                ttl_hours=cfg["security_ttl_hours"])
        except Exception as exc:
            print("[security] 전체 실패: %s" % exc)
            sec_cache = security.load_cache(sec_path)
    rows = attach_security(rows, sec_cache, cfg)

    # ---- 프로모션(부스트) 신호 ----
    boosts = {}
    if cfg.get("boost_enabled"):
        try:
            boosts = crosscheck.boosted_addresses()
        except Exception as exc:
            print("[boost] 실패: %s" % exc)
    rows = attach_promotion(rows, boosts)

    # ---- 2차 소스 교차검증 ----
    cross, cross_summary = {}, {"status": "SKIPPED", "checked": 0}
    if cfg.get("crosscheck_enabled"):
        try:
            cross, cross_summary = crosscheck.compare(
                rows, top_n=cfg["crosscheck_top_n"],
                tolerance_pct=cfg["crosscheck_tolerance_pct"])
        except Exception as exc:
            print("[crosscheck] 실패: %s" % exc)
            cross_summary = {"status": "FAILED", "checked": 0}
    rows = attach_crosscheck(rows, cross, cfg["crosscheck_tolerance_pct"])

    # ---- OHLCV 소급 백필 (1회) ----
    real_snaps = [s for s in history if s.get("source") != "ohlcv_backfill"]
    want_backfill = os.environ.get("HOOD_BACKFILL", "auto")
    do_backfill = (want_backfill == "1") or (
        want_backfill == "auto" and len(real_snaps) < 4 and not os.path.exists(bf_marker))
    if do_backfill:
        try:
            synth = backfill.build_snapshots(
                rows, hours_back=cfg["backfill_hours"], step_hours=cfg["backfill_step_hours"],
                sleep_sec=float(cfg["page_sleep_sec"]), max_tokens=cfg["backfill_max_tokens"],
                deadline_sec=float(cfg.get("backfill_deadline_sec", 480)))
            if synth:
                history = backfill.merge(history, synth)
                with open(bf_marker, "w", encoding="utf-8") as fh:
                    json.dump({"done_at": now.isoformat(), "snapshots": len(synth)}, fh)
        except Exception as exc:
            print("[backfill] 실패: %s" % exc)

    events = detect_changes(rows, history, cfg, now)

    # 보안 경보를 이벤트로 승격 (상위권만)
    for row in rows[:20]:
        for flag in row["flags"]:
            if flag["code"] in ("HONEYPOT", "CANNOT_SELL_ALL", "MINTABLE", "PAUSABLE",
                                "SELL_TAX", "BUY_TAX", "CLOSED_SOURCE"):
                events.append({
                    "code": "SECURITY", "window": "now",
                    "symbol": label(row), "address": row["address"],
                    "detail": flag["detail"], "mcap": row["mcap"],
                    "severity": 12.0 if flag["code"] in ("HONEYPOT", "CANNOT_SELL_ALL") else 7.0,
                })
    events.sort(key=lambda e: -e["severity"])

    chain_vol = sum(fnum((p.get("attributes") or {}).get("volume_usd", {}).get("h24")) for p in pools)
    payload = {
        "version": cfg.get("version", "2.0"),
        "as_of_kst": now.strftime("%Y-%m-%d %H:%M"),
        "as_of_utc": now.astimezone(timezone.utc).isoformat(),
        "data_status": "OK" if cross_summary.get("status") != "DIVERGENT" else "OK (소스 괴리 감지)",
        "meta": {
            "network": cfg["network_label"],
            "pools_scanned": len(pools),
            "tokens_seen": len(universe),
            "chain_volume_24h": chain_vol,
            "dropped": dropped,
            "revived": revived,
            "crosscheck": cross_summary,
            "security_cached": len(sec_cache),
            "promoted": sum(1 for r in rows if r.get("promoted")),
        },
        "rows": rows,
        "events": events,
    }

    snap = snapshot_of(rows, now.isoformat(), chain_v24=chain_vol)
    snap["source"] = "live"
    history.append(snap)
    history.sort(key=lambda s: s.get("ts") or "")
    history = history[-int(cfg["history_max_snapshots"]):]

    # ---- 예측력 검증 ----
    try:
        payload["backtest"] = backtest.run(
            history, rank_threshold=cfg["rank_move_24h_threshold"],
            forward_hours=cfg["backtest_forward_hours"],
            min_picks=cfg["backtest_min_picks"])
    except Exception as exc:
        print("[backtest] 실패: %s" % exc)
        payload["backtest"] = {"verdict": "INSUFFICIENT", "n_picks": 0}

    spark = {}
    for row in rows[:8]:
        spark[row["symbol"]] = [(s.get("mcap") or {}).get(row["address"]) for s in history[-24:]]
    payload["spark"] = spark

    dash_url = os.environ.get(
        "HOOD_DASH_URL", "https://jinhae8971.github.io/korea-etf-calmar/hood-radar/")
    payload["message"] = render_telegram(payload, cfg, dash_url)
    payload["dashboard_url"] = dash_url

    changed = write_json_if_changed(latest_path, payload)
    with open(hist_path, "w", encoding="utf-8") as fh:
        json.dump(history, fh, ensure_ascii=False, separators=(",", ":"))

    # 추적 목록 갱신
    run_no = int(tracked.get("__run__", {}).get("n", 0)) + 1 if isinstance(tracked.get("__run__"), dict) else 1
    new_tracked = {"__run__": {"n": run_no}}
    for row in rows[: cfg["sticky_max"]]:
        new_tracked[row["address"]] = {"symbol": row["symbol"], "run": run_no}
    for addr, info in tracked.items():
        if addr == "__run__" or addr in new_tracked:
            continue
        if run_no - int(info.get("run", 0)) < cfg["sticky_ttl_runs"]:
            new_tracked[addr] = info
    write_json_if_changed(tracked_path, new_tracked)

    docs = os.path.abspath(os.path.join(BASE, "..", "docs", "hood-radar", "index.html"))
    render_dashboard(payload, cfg, docs, history=history)

    print("[ok] %s · 추적 %d종 · 이벤트 %d건 · 풀 %d개 · 보안캐시 %d · 교차검증 %s (latest 갱신=%s)" % (
        payload["as_of_kst"], len(rows), len(events), len(pools),
        len(sec_cache), cross_summary.get("status"), changed))
    for e in events[:8]:
        print("   %-10s %-12s %s" % (e["code"], e["symbol"], e["detail"]))
    print("   %s" % backtest.render_line(payload["backtest"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
