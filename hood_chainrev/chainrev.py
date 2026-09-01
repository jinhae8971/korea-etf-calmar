#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HOOD CHAIN REVENUE RADAR
로빈후드 체인을 기준점으로, 체인별 프로토콜 수익(protocol revenue)을 순위화한다.

원천: DefiLlama Fees/Revenue dimensions (무인증 공개 API)
  - /overview/fees?dataType=dailyRevenue  : 프로토콜별 체인 분해(24h·30d)
  - /overview/fees?dataType=dailyFees     : 동일 구조의 수수료 → 전환율 산출
  - /v2/chains                            : 체인 TVL → 자본효율(TVL당 연수익)

설계 원칙
  1) 관측기이지 예측기가 아니다. 순위·점유율은 "지금 수익이 어디서 발생하는가"의 서술이다.
  2) 모집단이 달라진 순위를 그대로 빼지 않는다(교집합 재산정) — hood-radar v1 결함 교훈.
  3) 수집 실패 시 "변화 없음"을 보내지 않는다. 판정 불가로 명시하고 비정상 종료한다.
  4) 외부 파이썬 의존성 0 (stdlib only).
"""

import json
import math
import os
import random
import sys
import time
import urllib.error
import urllib.request
import datetime as dt
from collections import Counter, defaultdict

# ----------------------------------------------------------------------------- 상수

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE, "data")
LATEST_PATH = os.path.join(DATA_DIR, "latest.json")
HISTORY_PATH = os.path.join(DATA_DIR, "history.json")

KST = dt.timezone(dt.timedelta(hours=9))

FEES_URL = (
    "https://api.llama.fi/overview/fees"
    "?excludeTotalDataChart=true&excludeTotalDataChartBreakdown=true&dataType={dt}"
)
CHAINS_URL = "https://api.llama.fi/v2/chains"

ANCHOR_SLUG = "robinhood"          # 로빈후드 체인
ANCHOR_NAME = "Robinhood Chain"

# 체인이 아닌 집계 버킷 — 순위에서 제외한다.
EXCLUDE_SLUGS = {"off_chain", "offchain", "off-chain"}

# 슬러그 자동추론이 실패할 때만 쓰는 최소 폴백표.
SLUG_FALLBACK = {
    "robinhood": "Robinhood Chain",
    "bsc": "BSC",
    "avax": "Avalanche",
    "era": "ZKsync Era",
    "optimism": "OP Mainnet",
    "xdai": "Gnosis",
    "polygon_zkevm": "Polygon zkEVM",
    "arbitrum_nova": "Arbitrum Nova",
    "hyperliquid": "Hyperliquid L1",
    "op_bnb": "opBNB",
    "zklighter": "zkLighter",
    "edgex": "edgeX L1",
}

USER_AGENTS = [
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36",
]

BACKOFF = [5, 13, 30, 60]

DASHBOARD_URL = "https://jinhae8971.github.io/korea-etf-calmar/hood-chainrev/"

# 순위표에 올릴 최소 규모 — 잡음 차단
MIN_REV_30D = 20_000.0      # 30일 수익 $20K 미만은 순위 제외
TOP_N_TABLE = 40            # 대시보드 표 길이
TOP_N_MSG = 10              # 텔레그램 순위 길이
HISTORY_KEEP = 180          # 보관 일수
HISTORY_CHAINS = 60         # 스냅샷당 저장 체인 수

# 이벤트 임계
RANK_MOVE_MIN = 2           # 계단
SURGE_RATIO = 3.0           # 24h / 30일 일평균
COLLAPSE_RATIO = 0.30
EVENT_MIN_REV_30D = 1_000_000.0   # 이벤트 승격 최소 규모(30일)
EVENT_MIN_REV_24H = 100_000.0     # 급증 이벤트 최소 절대 규모(24시간)
SHARE_SHIFT_PP = 1.5        # %p
NEW_ENTRANT_RANK = 30


# ----------------------------------------------------------------------------- 유틸

def log(msg):
    print(msg, flush=True)


def fetch_json(url, timeout=120, label=""):
    """지수 백오프 + Retry-After 존중 + UA 로테이션."""
    last = None
    for attempt, wait in enumerate([0] + BACKOFF):
        if wait:
            time.sleep(wait + random.uniform(0, 2.0))
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": random.choice(USER_AGENTS),
                "Accept": "application/json",
                "Accept-Encoding": "identity",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
            data = json.loads(raw)
            log(f"[fetch] {label or url[:60]} ok ({len(raw)/1e6:.1f}MB, try {attempt+1})")
            return data
        except urllib.error.HTTPError as e:
            last = e
            ra = e.headers.get("Retry-After") if e.headers else None
            log(f"[fetch] {label} HTTP {e.code} (try {attempt+1})")
            if ra:
                try:
                    time.sleep(min(float(ra), 90))
                except ValueError:
                    pass
        except Exception as e:  # noqa: BLE001
            last = e
            log(f"[fetch] {label} 실패 (try {attempt+1}): {type(e).__name__}: {e}")
    raise RuntimeError(f"{label or url} 수집 실패: {last}")


def num(x):
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) else 0.0


def safe_div(a, b):
    return a / b if b else None


def fmt_usd(v):
    v = float(v or 0)
    if v >= 1e9:
        return f"${v/1e9:.2f}B"
    if v >= 1e6:
        return f"${v/1e6:.1f}M"
    if v >= 1e3:
        return f"${v/1e3:.0f}K"
    return f"${v:.0f}"


def fmt_delta_rank(d):
    if d is None:
        return "NEW"
    if d > 0:
        return f"▲{d}"
    if d < 0:
        return f"▼{-d}"
    return "—"


def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        log(f"[load] {path} 읽기 실패 → 기본값 사용: {e}")
        return default


# ----------------------------------------------------------------------------- 집계

def build_slug_map(protocols):
    """
    단일 체인에만 존재하는 프로토콜에서 (breakdown 슬러그 ↔ chains 표시명)을 투표로 추론한다.
    DefiLlama가 슬러그→표시명 매핑을 공개하지 않으므로 자기충족적으로 만든다.
    """
    votes = defaultdict(Counter)
    for pr in protocols:
        bd = pr.get("breakdown30d") or pr.get("breakdown24h") or {}
        chains = pr.get("chains") or []
        if isinstance(bd, dict) and len(bd) == 1 and len(chains) == 1:
            votes[next(iter(bd))][chains[0]] += 1
    out = {s: c.most_common(1)[0][0] for s, c in votes.items()}
    for s, n in SLUG_FALLBACK.items():
        out.setdefault(s, n)
    return out


def display_name(slug, slug_map):
    if slug in slug_map:
        return slug_map[slug]
    return slug.replace("_", " ").title()


def aggregate_by_chain(protocols):
    """
    프로토콜 배열 → 체인 슬러그별 합계.
    반환: {slug: {"v24":float, "v30":float, "app24":float, "app30":float,
                  "items":[(name, category, v30, v24)]}}
    'Chain' 카테고리는 체인 자체(가스·시퀀서) 수익이므로 앱레이어 합계에서 분리한다.
    """
    out = defaultdict(lambda: {"v24": 0.0, "v30": 0.0, "app24": 0.0, "app30": 0.0, "items": []})
    for pr in protocols:
        name = pr.get("displayName") or pr.get("name") or "?"
        cat = pr.get("category") or "?"
        is_chain_native = (cat == "Chain")
        per_chain = defaultdict(lambda: [0.0, 0.0])  # slug -> [v30, v24]
        for key, idx in (("breakdown30d", 0), ("breakdown24h", 1)):
            bd = pr.get(key)
            if not isinstance(bd, dict):
                continue
            for slug, sub in bd.items():
                if not isinstance(sub, dict):
                    continue
                per_chain[slug][idx] += sum(num(v) for v in sub.values())
        for slug, (v30, v24) in per_chain.items():
            if v30 <= 0 and v24 <= 0:
                continue
            rec = out[slug]
            rec["v30"] += v30
            rec["v24"] += v24
            if not is_chain_native:
                rec["app30"] += v30
                rec["app24"] += v24
            rec["items"].append((name, cat, v30, v24))
    return out


def hhi(shares):
    """허핀달 지수(0~1). 상위 편중도."""
    tot = sum(shares)
    if tot <= 0:
        return None
    return sum((s / tot) ** 2 for s in shares)


def build_rows(rev_agg, fee_agg, tvl_map, slug_map):
    rows = []
    for slug, rec in rev_agg.items():
        if slug in EXCLUDE_SLUGS:
            continue
        v30, v24 = rec["v30"], rec["v24"]
        if v30 < MIN_REV_30D and v24 <= 0:
            continue
        name = display_name(slug, slug_map)
        fee = fee_agg.get(slug, {})
        f30, f24 = fee.get("v30", 0.0), fee.get("v24", 0.0)
        tvl = tvl_map.get(name)
        ann = v30 * (365.0 / 30.0)
        daily_avg = v30 / 30.0 if v30 > 0 else 0.0
        items = sorted(rec["items"], key=lambda x: -x[2])[:12]
        rows.append({
            "slug": slug,
            "name": name,
            "rev24": v24,
            "rev30": v30,
            "app30": rec["app30"],
            "app24": rec["app24"],
            "fee24": f24,
            "fee30": f30,
            "take": safe_div(v30, f30),                       # 전환율 = 수익/수수료
            "ann": ann,
            "momentum": safe_div(v24, daily_avg),             # 24h ÷ 30일 일평균
            "tvl": tvl,
            "rpt": (safe_div(ann, tvl) if (tvl and tvl >= 5e6) else None),  # TVL당 연수익
            "hhi": hhi([max(0.0, i[2]) for i in items]),
            "top_items": [
                {"name": n, "cat": c, "rev30": r30, "rev24": r24} for (n, c, r30, r24) in items
            ],
        })
    rows.sort(key=lambda r: -r["rev30"])
    for i, r in enumerate(rows, 1):
        r["rank30"] = i
    by24 = sorted(rows, key=lambda r: -r["rev24"])
    for i, r in enumerate(by24, 1):
        r["rank24"] = i
    return rows


# ----------------------------------------------------------------------------- 순위 비교

def intersect_rank_delta(rows, prev_snapshot):
    """
    교집합 재산정 방식.
    모집단(유니버스)이 실행마다 달라지므로 어제 순위와 오늘 순위를 그대로 빼면
    허위 급변이 생긴다. 양쪽에 모두 존재하는 체인만 남겨 순위를 다시 매긴 뒤 비교한다.
    반환: {slug: delta(양수=상승)} / 신규는 None
    """
    if not prev_snapshot:
        return {}, set()
    prev_chains = prev_snapshot.get("chains") or {}
    cur = {r["slug"]: r["rev30"] for r in rows}
    common = [s for s in cur if s in prev_chains]
    if len(common) < 5:
        return {}, set()

    cur_rank = {s: i for i, s in enumerate(
        sorted(common, key=lambda s: -cur[s]), 1)}
    prev_rank = {s: i for i, s in enumerate(
        sorted(common, key=lambda s: -num(prev_chains[s].get("rev30"))), 1)}

    deltas = {s: prev_rank[s] - cur_rank[s] for s in common}   # 양수 = 상승
    newcomers = {s for s in cur if s not in prev_chains}
    return deltas, newcomers


# ----------------------------------------------------------------------------- 이벤트

def detect_events(rows, deltas, newcomers, prev_snapshot, anchor, total_on_chain_30d):
    ev = []
    by_slug = {r["slug"]: r for r in rows}

    # 1) 앵커(로빈후드 체인) 순위 변동 — 전용 이벤트
    if anchor:
        d = deltas.get(ANCHOR_SLUG)
        if d is not None and abs(d) >= 1:
            ev.append({
                "code": "HOOD_RANK_SHIFT",
                "level": "high" if abs(d) >= RANK_MOVE_MIN else "mid",
                "chain": anchor["name"],
                "text": f"{ANCHOR_NAME} 30일 수익 순위 {fmt_delta_rank(d)} → 현재 {anchor['rank30']}위",
            })

    # 2) 앵커 점유율 변화
    if anchor and prev_snapshot:
        prev_share = prev_snapshot.get("hood_share_30d")
        cur_share = safe_div(anchor["rev30"], total_on_chain_30d)
        if prev_share is not None and cur_share is not None:
            pp = (cur_share - prev_share) * 100
            if abs(pp) >= SHARE_SHIFT_PP:
                ev.append({
                    "code": "SHARE_SHIFT",
                    "level": "high",
                    "chain": anchor["name"],
                    "text": (f"{ANCHOR_NAME} 온체인 수익 점유율 "
                             f"{prev_share*100:.1f}% → {cur_share*100:.1f}% ({pp:+.1f}%p)"),
                })

    # 3) 급증·급감 (상위 25위 이내 + 절대규모 하한 — 소형 체인의 %폭발 차단)
    prev_chains = (prev_snapshot or {}).get("chains") or {}
    for r in rows[:25]:
        m = r["momentum"]
        if m is None or r["rev30"] < EVENT_MIN_REV_30D:
            continue
        if m >= SURGE_RATIO and r["rev24"] >= EVENT_MIN_REV_24H:
            ev.append({
                "code": "REV_SURGE", "level": "high", "chain": r["name"],
                "text": f"{r['name']} 24h 수익이 30일 일평균의 {m:.1f}배 ({fmt_usd(r['rev24'])})",
            })
        elif m <= COLLAPSE_RATIO:
            # 하루 반짝은 승격하지 않는다. DefiLlama 어댑터 지연으로 24h가 비는 경우가
            # 실제로 있어, 이틀 연속 위축된 것만 이벤트로 올린다.
            prev_m = (prev_chains.get(r["slug"]) or {}).get("mom")
            if prev_m is not None and prev_m <= COLLAPSE_RATIO:
                ev.append({
                    "code": "REV_COLLAPSE", "level": "mid", "chain": r["name"],
                    "text": f"{r['name']} 24h 수익이 30일 일평균의 {m:.2f}배로 이틀 연속 위축",
                })

    # 4) 순위 급변 (상위 30위 이내)
    for r in rows[:30]:
        d = deltas.get(r["slug"])
        if d is None or r["slug"] == ANCHOR_SLUG:
            continue
        if abs(d) >= RANK_MOVE_MIN:
            ev.append({
                "code": "RANK_UP" if d > 0 else "RANK_DOWN",
                "level": "mid", "chain": r["name"],
                "text": f"{r['name']} {fmt_delta_rank(d)} → {r['rank30']}위 ({fmt_usd(r['rev30'])}/30d)",
            })

    # 5) 상위권 신규 진입 — 실측 스냅샷 기준(백필 추정 금지)
    for r in rows[:NEW_ENTRANT_RANK]:
        if r["slug"] in newcomers:
            ev.append({
                "code": "NEW_ENTRANT", "level": "high", "chain": r["name"],
                "text": f"{r['name']} 상위 {NEW_ENTRANT_RANK}위권 신규 관측 → {r['rank30']}위",
            })

    # 6) 앵커 내부 1위 프로토콜 교체
    if anchor and anchor["top_items"] and prev_snapshot:
        prev_top = prev_snapshot.get("hood_top_protocol")
        cur_top = anchor["top_items"][0]["name"]
        if prev_top and prev_top != cur_top:
            ev.append({
                "code": "LEADER_SHIFT", "level": "mid", "chain": anchor["name"],
                "text": f"{ANCHOR_NAME} 최대 수익원 교체: {prev_top} → {cur_top}",
            })

    order = {"high": 0, "mid": 1, "low": 2}
    ev.sort(key=lambda e: order.get(e["level"], 3))
    return ev


# ----------------------------------------------------------------------------- 렌더링

def render_telegram(payload):
    p = payload
    rows = p["rows"]
    a = p.get("anchor")
    L = []
    L.append(f"📊 <b>체인 프로토콜 수익 순위</b> · {p['as_of_kst'][:10]}")
    L.append(f"<i>DefiLlama Revenue 기준 · 온체인 {p['universe_size']}개 체인</i>")

    if p["data_status"] != "OK":
        L.append(f"\n⚠️ 데이터 상태: <b>{p['data_status']}</b> — 해석 주의")

    if a:
        L.append("")
        adelta = fmt_delta_rank(p["deltas"].get(ANCHOR_SLUG)) if p["deltas"] else "기준일"
        L.append(f"🎯 <b>{esc(ANCHOR_NAME)}</b> — 30일 <b>{a['rank30']}위</b>"
                 f" {adelta} / 24시간 {a['rank24']}위")
        L.append(f" · 30일 <b>{fmt_usd(a['rev30'])}</b> · 24시간 {fmt_usd(a['rev24'])}"
                 + (f" (일평균의 {a['momentum']:.1f}배)" if a["momentum"] else ""))
        share = safe_div(a["rev30"], p["total_on_chain_30d"])
        line = f" · 온체인 점유율 {share*100:.1f}%" if share else " · 점유율 n/a"
        if a["take"] is not None:
            line += f" · 전환율 {a['take']*100:.0f}%"
        L.append(line)
        if a["rpt"] is not None:
            L.append(f" · TVL당 연수익 {a['rpt']*100:.1f}% (TVL {fmt_usd(a['tvl'])})")
        tops = a["top_items"][:3]
        if tops and a["rev30"] > 0:
            frag = " / ".join(f"{esc(t['name'])} {t['rev30']/a['rev30']*100:.0f}%" for t in tops)
            L.append(f" · 구성: {frag}")
    else:
        L.append(f"\n⚠️ {ANCHOR_NAME} 데이터 미관측 — 원천 확인 필요")

    L.append("")
    L.append(f"🏆 <b>30일 수익 TOP {TOP_N_MSG}</b>")
    for r in rows[:TOP_N_MSG]:
        mark = "🎯" if r["slug"] == ANCHOR_SLUG else "  "
        d = fmt_delta_rank(p["deltas"].get(r["slug"])) if p["deltas"] else "—"
        L.append(f"{mark}{r['rank30']:2d}. {esc(r['name'])[:16]:<16s} {fmt_usd(r['rev30']):>8s}  {d}")

    ev = p["events"]
    L.append("")
    if ev:
        L.append(f"⚡ <b>변화 감지 {len(ev)}건</b>")
        for e in ev[:5]:
            icon = "🔴" if e["level"] == "high" else "🟡"
            L.append(f"{icon} {esc(e['text'])}")
        if len(ev) > 5:
            L.append(f"   … 외 {len(ev)-5}건 (대시보드)")
    else:
        L.append("⚪ 임계 초과 변화 없음")

    L.append("")
    L.append(f'📈 <a href="{DASHBOARD_URL}">전체 순위·추이 대시보드</a>')
    L.append("<i>관측 리포트입니다. 수익 순위는 현재 자금 흐름의 서술이며 가격 전망이 아닙니다.</i>")
    return "\n".join(L)


def _spark(values, w=260, h=34):
    """의존성 없는 SVG 스파크라인."""
    vals = [v for v in values if v is not None]
    if len(vals) < 2:
        return '<span class="dim">이력 축적 중</span>'
    lo, hi = min(vals), max(vals)
    rng = (hi - lo) or 1.0
    n = len(vals)
    pts = " ".join(
        f"{i/(n-1)*w:.1f},{h - (v-lo)/rng*(h-4) - 2:.1f}" for i, v in enumerate(vals)
    )
    return (f'<svg viewBox="0 0 {w} {h}" preserveAspectRatio="none" class="spark">'
            f'<polyline points="{pts}" fill="none" stroke="#4ade80" stroke-width="2"/></svg>')


def render_dashboard(payload, history):
    p = payload
    rows = p["rows"]
    a = p.get("anchor")

    hood_rank_series = []
    hood_share_series = []
    for snap in history[-60:]:
        ch = (snap.get("chains") or {}).get(ANCHOR_SLUG)
        hood_rank_series.append(-num(ch.get("rank30")) if ch else None)
        s = snap.get("hood_share_30d")
        hood_share_series.append(num(s) * 100 if s is not None else None)

    trs = []
    for r in rows[:TOP_N_TABLE]:
        cls = ' class="anchor"' if r["slug"] == ANCHOR_SLUG else ""
        d = p["deltas"].get(r["slug"])
        dcell = fmt_delta_rank(d) if p["deltas"] else "—"
        dcls = "up" if (d or 0) > 0 else ("down" if (d or 0) < 0 else "flat")
        share = safe_div(r["rev30"], p["total_on_chain_30d"]) or 0
        mom = "{:.2f}x".format(r["momentum"]) if r["momentum"] else "—"
        take = "{:.0f}%".format(r["take"] * 100) if r["take"] is not None else "—"
        rpt = "{:.1f}%".format(r["rpt"] * 100) if r["rpt"] is not None else "—"
        trs.append(
            '<tr{cls}><td>{rk}</td><td class="nm">{nm}</td>'
            '<td class="n">{r30}</td><td class="n">{sh:.1f}%</td>'
            '<td class="n">{r24}</td><td class="n">{mom}</td>'
            '<td class="n">{take}</td><td class="n">{rpt}</td>'
            '<td class="n {dcls}">{dcell}</td></tr>'.format(
                cls=cls, rk=r["rank30"], nm=esc(r["name"]),
                r30=fmt_usd(r["rev30"]), sh=share * 100, r24=fmt_usd(r["rev24"]),
                mom=mom, take=take, rpt=rpt, dcls=dcls, dcell=dcell)
        )

    comp = ""
    if a and a["rev30"] > 0:
        bars = []
        for t in a["top_items"][:8]:
            pct = t["rev30"] / a["rev30"] * 100
            bars.append(
                f'<div class="bar"><span class="bl">{esc(t["name"])}'
                f'<em>{esc(t["cat"])}</em></span>'
                f'<span class="bt"><i style="width:{min(100,pct):.1f}%"></i></span>'
                f'<span class="bv">{pct:.1f}% · {fmt_usd(t["rev30"])}</span></div>'
            )
        comp = "".join(bars)

    evs = "".join(
        '<li class="{lv}"><b>{cd}</b> {tx}</li>'.format(lv=e["level"], cd=e["code"], tx=esc(e["text"]))
        for e in p["events"]
    ) or '<li class="flat">임계를 넘은 변화 없음</li>'

    # KPI 셀 사전 계산 (f-string 중첩 따옴표 회피)
    k_rank = "{}위".format(a["rank30"]) if a else "—"
    k_delta = (fmt_delta_rank(p["deltas"].get(ANCHOR_SLUG)) if (a and p["deltas"]) else "기준일")
    k_rev = fmt_usd(a["rev30"]) if a else "—"
    k_share = (safe_div(a["rev30"], p["total_on_chain_30d"]) or 0) * 100 if a else 0.0
    k_mom = "{:.2f}x".format(a["momentum"]) if (a and a["momentum"]) else "—"
    k_take = "{:.0f}%".format(a["take"] * 100) if (a and a["take"] is not None) else "—"
    k_rpt = "{:.1f}%".format(a["rpt"] * 100) if (a and a["rpt"] is not None) else "—"
    k_app = fmt_usd(a["app30"]) if a else "—"

    return f"""<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>체인 프로토콜 수익 레이더</title>
<style>
:root{{--bg:#0b0f14;--card:#131a22;--line:#22303c;--fg:#e6edf3;--dim:#8b9bab;--acc:#4ade80;--warn:#fbbf24;--dn:#f87171}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--fg);font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Noto Sans KR",sans-serif}}
.wrap{{max-width:1080px;margin:0 auto;padding:18px 14px 60px}}
h1{{font-size:19px;margin:0 0 4px}} h2{{font-size:15px;margin:26px 0 10px;color:var(--acc)}}
.sub{{color:var(--dim);font-size:12px;margin-bottom:14px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px;margin-bottom:14px}}
.kpis{{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px}}
.kpi{{background:#0f151c;border:1px solid var(--line);border-radius:10px;padding:10px}}
.kpi b{{display:block;font-size:20px}} .kpi span{{color:var(--dim);font-size:11px}}
table{{width:100%;border-collapse:collapse;font-size:12.5px}}
th,td{{padding:7px 6px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}}
th{{color:var(--dim);font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.04em}}
td.n{{text-align:right;font-variant-numeric:tabular-nums}}
tr.anchor{{background:rgba(74,222,128,.10)}} tr.anchor td.nm{{color:var(--acc);font-weight:700}}
.up{{color:var(--acc)}} .down{{color:var(--dn)}} .flat{{color:var(--dim)}} .dim{{color:var(--dim)}}
.tblwrap{{overflow-x:auto}}
.bar{{display:grid;grid-template-columns:170px 1fr 150px;gap:10px;align-items:center;margin:6px 0}}
.bl{{font-size:12px}} .bl em{{display:block;color:var(--dim);font-style:normal;font-size:10.5px}}
.bt{{background:#0f151c;border-radius:5px;height:9px;overflow:hidden}}
.bt i{{display:block;height:100%;background:var(--acc)}}
.bv{{text-align:right;color:var(--dim);font-size:11.5px;font-variant-numeric:tabular-nums}}
ul.ev{{list-style:none;padding:0;margin:0}}
ul.ev li{{padding:7px 10px;border-left:3px solid var(--line);margin-bottom:6px;background:#0f151c;font-size:12.5px}}
ul.ev li.high{{border-color:var(--dn)}} ul.ev li.mid{{border-color:var(--warn)}}
ul.ev li b{{color:var(--dim);font-size:11px;margin-right:6px}}
.spark{{width:100%;height:34px;display:block}}
.note{{color:var(--dim);font-size:11.5px;line-height:1.7}}
</style></head><body><div class="wrap">
<h1>🎯 체인 프로토콜 수익 레이더</h1>
<div class="sub">기준시각 {p['as_of_kst']} KST · 원천 DefiLlama Revenue · 유니버스 {p['universe_size']}개 체인 · 상태 {p['data_status']}</div>

<div class="card"><h2 style="margin-top:0">{ANCHOR_NAME}</h2>
<div class="kpis">
<div class="kpi"><b>{k_rank}</b><span>30일 수익 순위 {k_delta}</span></div>
<div class="kpi"><b>{k_rev}</b><span>30일 프로토콜 수익</span></div>
<div class="kpi"><b>{k_share:.1f}%</b><span>온체인 점유율</span></div>
<div class="kpi"><b>{k_mom}</b><span>24h ÷ 30일 일평균</span></div>
<div class="kpi"><b>{k_take}</b><span>수수료→수익 전환율</span></div>
<div class="kpi"><b>{k_rpt}</b><span>TVL당 연환산 수익</span></div>
</div></div>

<div class="card"><h2 style="margin-top:0">수익 구성 (30일)</h2>{comp or '<div class="dim">구성 데이터 없음</div>'}
<div class="note" style="margin-top:8px">‘Chain’ 카테고리는 체인 자체(가스·시퀀서) 수익입니다. 앱레이어만 보면 {k_app} 입니다.</div>
</div>

<div class="card"><h2 style="margin-top:0">추이</h2>
<div class="note">{ANCHOR_NAME} 순위(위로 갈수록 상위)</div>{_spark(hood_rank_series)}
<div class="note" style="margin-top:10px">{ANCHOR_NAME} 온체인 수익 점유율(%)</div>{_spark(hood_share_series)}
</div>

<h2>전체 순위 (30일 수익)</h2>
<div class="card tblwrap"><table>
<thead><tr><th>#</th><th>체인</th><th class="n">30일 수익</th><th class="n">점유율</th>
<th class="n">24시간</th><th class="n">모멘텀</th><th class="n">전환율</th><th class="n">TVL당</th><th class="n">순위변동</th></tr></thead>
<tbody>{''.join(trs)}</tbody></table></div>

<h2>변화 감지</h2><div class="card"><ul class="ev">{evs}</ul></div>

<h2>방법론과 한계</h2><div class="card note">
· <b>수익(Revenue)</b>은 DefiLlama 정의로 프로토콜·체인이 실제로 취한 몫이며, 사용자가 낸 총 수수료(Fees)와 다릅니다. 전환율 = 수익 ÷ 수수료.<br>
· 체인 수익 = 그 체인에서 발생한 모든 프로토콜 수익의 합입니다. 체인 자체 가스 수익과 앱레이어 수익을 함께 포함하므로, 순수 체인 사업성만 보려면 앱레이어 수치를 따로 보십시오.<br>
· DefiLlama 집계 버킷 <code>off_chain</code>(중앙화 거래소 등)은 체인이 아니므로 순위에서 제외했습니다.<br>
· 순위 변동은 <b>어제와 오늘 양쪽에 모두 존재하는 체인만 남겨 재산정</b>한 결과입니다. 유니버스 크기가 달라진 순위를 그대로 빼면 허위 급변이 생기기 때문입니다.<br>
· 30일 수익 {fmt_usd(MIN_REV_30D)} 미만 체인은 잡음이라 순위에서 제외합니다. TVL {fmt_usd(5e6)} 미만은 TVL당 수익을 계산하지 않습니다(분모 폭발 방지).<br>
· 이 리포트는 <b>관측기이지 예측기가 아닙니다.</b> 수익 순위는 현재 자금이 어디서 발생하는지에 대한 서술이며 미래 수익률을 주장하지 않습니다.
</div>
</div></body></html>"""


# ----------------------------------------------------------------------------- 메인

def collect():
    rev = fetch_json(FEES_URL.format(dt="dailyRevenue"), label="revenue")
    time.sleep(2.0)
    fee = fetch_json(FEES_URL.format(dt="dailyFees"), label="fees")
    time.sleep(1.0)
    chains = fetch_json(CHAINS_URL, label="chains-tvl")
    return rev, fee, chains


def build_payload(rev, fee, chains, prev_snapshot):
    rev_protocols = rev.get("protocols") or []
    fee_protocols = fee.get("protocols") or []
    if len(rev_protocols) < 500:
        raise RuntimeError(f"revenue 프로토콜 수 비정상: {len(rev_protocols)}")

    slug_map = build_slug_map(rev_protocols + fee_protocols)
    rev_agg = aggregate_by_chain(rev_protocols)
    fee_agg = aggregate_by_chain(fee_protocols)
    tvl_map = {c.get("name"): num(c.get("tvl")) for c in (chains or []) if c.get("name")}

    rows = build_rows(rev_agg, fee_agg, tvl_map, slug_map)
    if not rows:
        raise RuntimeError("체인 집계 결과가 비었음")

    anchor = next((r for r in rows if r["slug"] == ANCHOR_SLUG), None)
    total_on_chain_30d = sum(r["rev30"] for r in rows)
    deltas, newcomers = intersect_rank_delta(rows, prev_snapshot)
    events = detect_events(rows, deltas, newcomers, prev_snapshot, anchor, total_on_chain_30d)

    status = "OK"
    if anchor is None:
        status = "DEGRADED"
    elif len(rows) < 40:
        status = "DEGRADED"

    now = dt.datetime.now(KST)
    return {
        "as_of_kst": now.strftime("%Y-%m-%d %H:%M"),
        "as_of_date": now.strftime("%Y-%m-%d"),
        "data_status": status,
        "universe_size": len(rows),
        "total_on_chain_30d": total_on_chain_30d,
        "rows": rows,
        "anchor": anchor,
        "deltas": deltas,
        "events": events,
    }


def make_snapshot(payload):
    rows = payload["rows"][:HISTORY_CHAINS]
    a = payload.get("anchor")
    return {
        "as_of_date": payload["as_of_date"],
        "universe_size": payload["universe_size"],
        "total_on_chain_30d": round(payload["total_on_chain_30d"], 2),
        "hood_share_30d": (safe_div(a["rev30"], payload["total_on_chain_30d"]) if a else None),
        "hood_top_protocol": (a["top_items"][0]["name"] if (a and a["top_items"]) else None),
        "chains": {
            r["slug"]: {
                "name": r["name"],
                "rank30": r["rank30"],
                "rev30": round(r["rev30"], 2),
                "rev24": round(r["rev24"], 2),
                "mom": (round(r["momentum"], 3) if r["momentum"] is not None else None),
            } for r in rows
        },
    }


def upsert_history(history, snapshot):
    """같은 날짜는 덮어쓴다(재실행 멱등). 새 날짜만 append."""
    out = [s for s in history if s.get("as_of_date") != snapshot["as_of_date"]]
    out.append(snapshot)
    out.sort(key=lambda s: s.get("as_of_date", ""))
    return out[-HISTORY_KEEP:]


def send_telegram(text, token, chat_id):
    if not token or not chat_id:
        log("[telegram] 자격증명 없음 — 발송 생략 (릴레이가 담당)")
        return False
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=json.dumps({
            "chat_id": chat_id, "text": text,
            "parse_mode": "HTML", "disable_web_page_preview": True,
        }).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        log(f"[telegram] 발송 완료 (HTTP {r.status})")
    return True


def main():
    history = load_json(HISTORY_PATH, [])
    prev_snapshot = history[-1] if history else None
    # 같은 날 재실행이면 그 전날을 비교 기준으로 삼는다(자기 자신과 비교 금지)
    today = dt.datetime.now(KST).strftime("%Y-%m-%d")
    if prev_snapshot and prev_snapshot.get("as_of_date") == today:
        prev_snapshot = history[-2] if len(history) >= 2 else None

    try:
        rev, fee, chains = collect()
    except Exception as e:  # noqa: BLE001
        log(f"::error::수집 실패 — 판정 불가: {e}")
        return 1

    payload = build_payload(rev, fee, chains, prev_snapshot)
    message = render_telegram(payload)
    payload["message"] = message

    save_json(LATEST_PATH, payload)
    history = upsert_history(history, make_snapshot(payload))
    save_json(HISTORY_PATH, history)

    docs_dir = os.environ.get("DOCS_DIR", os.path.join(BASE, "..", "docs", "hood-chainrev"))
    os.makedirs(docs_dir, exist_ok=True)
    html = render_dashboard(payload, history)
    with open(os.path.join(docs_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write(html)

    log(f"[done] {payload['universe_size']}개 체인 / 상태 {payload['data_status']} / 이벤트 {len(payload['events'])}건")
    if payload.get("anchor"):
        log(f"[anchor] {ANCHOR_NAME} 30일 {payload['anchor']['rank30']}위 {fmt_usd(payload['anchor']['rev30'])}")

    send_telegram(message,
                  os.environ.get("TELEGRAM_TOKEN", ""),
                  os.environ.get("TELEGRAM_CHAT_ID", ""))

    if payload["data_status"] != "OK":
        log("::warning::데이터 상태 DEGRADED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
