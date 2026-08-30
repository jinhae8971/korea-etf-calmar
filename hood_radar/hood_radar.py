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
        if ent["liq"] < cfg["min_liquidity_usd"]:
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


# ---------------------------------------------------------------- change detection
def snapshot_of(rows, ts):
    return {
        "ts": ts,
        "rank": {r["address"]: r["rank"] for r in rows},
        "mcap": {r["address"]: round(r["mcap"], 2) for r in rows},
        "liq": {r["address"]: round(r["liq"], 2) for r in rows},
        "symbol": {r["address"]: r["symbol"] for r in rows},
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


def detect_changes(rows, history, cfg, now_ts):
    ref6 = pick_reference(history, 5, now_ts)      # 6시간 주기 → 5시간 이상 경과분
    ref24 = pick_reference(history, 22, now_ts)    # 하루 전
    prev = history[-1] if history else None
    events = []

    def rank_of(snap, addr):
        if not snap:
            return None
        return (snap.get("rank") or {}).get(addr)

    def mcap_of(snap, addr):
        if not snap:
            return None
        v = (snap.get("mcap") or {}).get(addr)
        return v if v else None

    known_before = set()
    for snap in history[-8:]:
        known_before |= set((snap.get("rank") or {}).keys())

    for row in rows:
        addr = row["address"]
        r6 = rank_of(ref6, addr)
        r24 = rank_of(ref24, addr)
        row["rank_6h_ago"] = r6
        row["rank_24h_ago"] = r24
        row["d_rank_6h"] = (r6 - row["rank"]) if r6 else None   # 양수 = 순위 상승
        row["d_rank_24h"] = (r24 - row["rank"]) if r24 else None
        m24 = mcap_of(ref24, addr)
        row["d_mcap_24h_pct"] = round((row["mcap"] - m24) / m24 * 100.0, 1) if m24 else None

        if row["d_rank_24h"] is not None and abs(row["d_rank_24h"]) >= cfg["rank_move_24h_threshold"]:
            events.append({
                "code": "RANK_SURGE" if row["d_rank_24h"] > 0 else "RANK_DROP",
                "window": "24h",
                "symbol": label(row), "address": addr,
                "detail": "%d위 → %d위 (%+d계단)" % (r24, row["rank"], row["d_rank_24h"]),
                "mcap": row["mcap"], "severity": abs(row["d_rank_24h"]),
            })
        elif row["d_rank_6h"] is not None and abs(row["d_rank_6h"]) >= cfg["rank_move_6h_threshold"]:
            events.append({
                "code": "RANK_SURGE" if row["d_rank_6h"] > 0 else "RANK_DROP",
                "window": "6h",
                "symbol": label(row), "address": addr,
                "detail": "%d위 → %d위 (%+d계단, 6시간)" % (r6, row["rank"], row["d_rank_6h"]),
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

        if history and addr not in known_before and row["rank"] <= cfg["new_entry_rank"]:
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

    # 순위권에서 사라진 토큰
    if prev:
        current = {r["address"] for r in rows}
        for addr, rank in (prev.get("rank") or {}).items():
            if addr not in current and rank <= cfg["new_entry_rank"]:
                events.append({
                    "code": "DROPPED_OUT", "window": "6h",
                    "symbol": (prev.get("symbol") or {}).get(addr, "?"), "address": addr,
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

    meta = payload["meta"]
    lines.append("📊 추적 %d종 · 24h DEX 거래대금 $%s · 풀 %d개 스캔" % (
        len(rows), human(meta["chain_volume_24h"]), meta["pools_scanned"]))
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


def render_dashboard(payload, cfg, out_path):
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
            "<td class='num'>$%s</td><td class='num'>$%s</td><td>%s</td></tr>" % (
                row["rank"], esc(label(row)), esc(row["name"][:28]), row["address"][:10] + "…",
                human(row["mcap"]), row["mcap_basis"],
                darrow(row["d_rank_6h"]), darrow(row["d_rank_24h"]), chg_html,
                human(row["v24"]), human(row["liq"]), flag_html(row),
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

    html = DASH_TMPL.format(
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
.f-LIQ_THIN,.f-COPYCAT{{color:#d29922;border-color:#5a4a1e}}
ul{{margin:0;padding-left:2px;list-style:none}} li{{padding:5px 0;border-bottom:1px solid rgba(31,42,55,.6);font-size:12.5px}}
.evi{{margin-right:4px}} .evc{{color:var(--dim);font-size:10.5px;margin-right:5px}}
.sp{{display:flex;align-items:center;gap:8px;font-size:11.5px;color:var(--dim)}} .sp span{{width:78px}}
.note{{color:var(--dim);font-size:11px;line-height:1.6}}
.tw{{overflow-x:auto}}
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
<div class="card"><h2>시총 순위 TOP {top_n}</h2><div class="tw"><table>
<tr><th>#</th><th>토큰</th><th>시총</th><th>6h</th><th>24h</th><th>가격24h</th><th>거래대금</th><th>유동성</th><th>플래그</th></tr>
{rows}</table></div></div>
<div class="card"><h2>시총 추이 (최근 스냅샷)</h2>{sparks}</div>
<div class="card"><h2>읽는 법과 한계</h2><div class="note">
· <b>관측기이지 예측기가 아닙니다.</b> 순위와 변동은 "지금 자금이 어디에 반응하는가"의 서술이며 미래 수익률을 주장하지 않습니다.<br>
· <b>시총 기준</b> — MC는 유통시총, FDV는 완전희석시총. 교차체인 브릿지 토큰은 타 체인 시총이 섞여 들어오므로 FDV로 대체하고 BRIDGED_MC로 표시합니다.<br>
· <b>토큰 식별은 컨트랙트 주소</b>로 합니다. 이 체인은 동일 티커 카피캣이 다수 존재해 심볼로는 구분되지 않습니다(COPYCAT 플래그).<br>
· <b>제외 대상</b> — 스테이블·랩드 토큰, 토큰화 주식/ETF(<code>Robinhood Token</code> 표기 또는 CoinGecko id 접미사로 판별). 티커만 겹치는 코인을 오제외하지 않기 위해 티커 목록만으로는 제외하지 않습니다.<br>
· <b>편입 규칙</b> — 유동성 ≥ $25K, 24h 거래대금 ≥ $50K, 시총 ≥ $300K를 모두 충족하면 자동 편입됩니다. 고정 목록이 아니므로 신규 상장분도 다음 실행에 잡힙니다.<br>
· <b>LIQ_THIN / LIQ_DRAIN / YOUNG / SINGLE_POOL</b>은 러그풀·허니팟 개연성이 높은 조건입니다. 진입 전 컨트랙트와 유동성 락을 직접 확인하십시오.<br>
· 데이터: GeckoTerminal 공개 API(온체인 DEX 집계). 수집 실패 시 "판정 불가"로 표기하며 정상 브리프를 발송하지 않습니다.
</div></div>
</div></body></html>"""


# ---------------------------------------------------------------- main
def main():
    cfg = load_cfg()
    now = now_kst()
    hist_path = os.path.join(DATA_DIR, "history.json")
    latest_path = os.path.join(DATA_DIR, "latest.json")
    history = read_json(hist_path, [])
    prev = history[-1] if history else None

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
    events = detect_changes(rows, history, cfg, now)

    chain_vol = sum(fnum((p.get("attributes") or {}).get("volume_usd", {}).get("h24")) for p in pools)
    payload = {
        "as_of_kst": now.strftime("%Y-%m-%d %H:%M"),
        "as_of_utc": now.astimezone(timezone.utc).isoformat(),
        "data_status": "OK",
        "meta": {
            "network": cfg["network_label"],
            "pools_scanned": len(pools),
            "tokens_seen": len(universe),
            "chain_volume_24h": chain_vol,
            "dropped": dropped,
        },
        "rows": rows,
        "events": events,
    }

    # 스냅샷 누적
    snap = snapshot_of(rows, now.isoformat())
    history.append(snap)
    history = history[-int(cfg["history_max_snapshots"]):]

    # 스파크라인용 시총 시계열(상위 8종)
    spark = {}
    for row in rows[:8]:
        spark[row["symbol"]] = [
            (s.get("mcap") or {}).get(row["address"]) for s in history[-24:]
        ]
    payload["spark"] = spark

    dash_url = os.environ.get(
        "HOOD_DASH_URL",
        "https://jinhae8971.github.io/korea-etf-calmar/hood-radar/",
    )
    payload["message"] = render_telegram(payload, cfg, dash_url)
    payload["dashboard_url"] = dash_url

    changed_latest = write_json_if_changed(latest_path, payload)
    with open(hist_path, "w", encoding="utf-8") as fh:
        json.dump(history, fh, ensure_ascii=False, separators=(",", ":"))

    docs = os.path.abspath(os.path.join(BASE, "..", "docs", "hood-radar", "index.html"))
    render_dashboard(payload, cfg, docs)

    print("[ok] %s · 추적 %d종 · 이벤트 %d건 · 풀 %d개 (latest 갱신=%s)" % (
        payload["as_of_kst"], len(rows), len(events), len(pools), changed_latest))
    for e in events[:8]:
        print("   %s %-10s %s" % (e["code"], e["symbol"], e["detail"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
