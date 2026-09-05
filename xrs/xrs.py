#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
xrs.py — Cross-Track Relative Strength
프라이버시 / DePIN / 로빈후드체인 / 하이퍼리퀴드 / 비트코인 5개 트랙을
가격·TVL·시총·매출·회전율 등 여러 관점에서 상대 비교한다.

설계 원칙
  - 관측기이지 예측기가 아니다. 순위는 "지금 자금이 어디에 반응하는가"의 서술.
  - 성격이 다른 대상(섹터 바스켓 / 단일자산 / 체인 생태계)을 억지로 한 수치로
    합치지 않는다. 관점별 순위를 따로 보여주고, 종합은 이용 가능한 관점만으로
    가중 백분위를 낸 뒤 커버리지를 함께 표기한다.
  - 수집 실패 시 "변화 없음"을 보내지 않는다. 판정 불가를 명시하고 exit 1.
  - 외부 파이썬 의존성 0 (표준 라이브러리만).
"""

import json
import os
import random
import ssl
import statistics
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
VERSION = "1.0"
THRESHOLDS_FROZEN_AT = "2026-09-05"

DASHBOARD_URL = "https://jinhae8971.github.io/korea-etf-calmar/xrs/"

# ── 트랙 정의 ────────────────────────────────────────────────────────────────
# kind: basket(고정 바스켓) / asset(단일자산) / ecosystem(체인 생태계 집계)
TRACKS = [
    {"key": "PRIV",  "code": "PRIVACY_PQ",    "label": "프라이버시",   "kind": "basket",    "chain": None},
    {"key": "DEPIN", "code": "DEPIN_COMPUTE", "label": "DePIN",        "kind": "basket",    "chain": None},
    {"key": "HOOD",  "code": None,            "label": "로빈후드체인", "kind": "ecosystem", "chain": "Robinhood Chain"},
    {"key": "HYPE",  "code": None,            "label": "하이퍼리퀴드", "kind": "asset",     "chain": "Hyperliquid L1"},
    {"key": "BTC",   "code": None,            "label": "비트코인",     "kind": "asset",     "chain": "Bitcoin"},
]

SINGLE_ASSET_IDS = {"HYPE": "hyperliquid", "BTC": "bitcoin"}

# 섹터 구성종목 폴백 (narrative_radar/universe.json 을 못 읽을 때만 사용, frozen 2026-08-21)
FALLBACK_MEMBERS = {
    "PRIVACY_PQ": ["zcash", "monero", "dash", "railgun", "quantum-resistant-ledger", "secret"],
    "DEPIN_COMPUTE": ["bittensor", "render-token", "akash-network", "helium", "filecoin"],
}

# 로빈후드 생태계 바스켓 편입 규칙 (성과가 아니라 규칙으로 결정)
HOOD_RULE = {"top_n": 8, "min_liq_usd": 1_000_000, "min_mcap_usd": 10_000_000}

# 관점 가중치 (합 1.0 기준, 결측 관점은 제외 후 재정규화)
LENSES = [
    {"key": "px30",       "label": "가격 30일",  "w": 0.26, "unit": "%"},
    {"key": "px7",        "label": "가격 7일",   "w": 0.16, "unit": "%"},
    {"key": "mcap_chg24", "label": "시총 24시간", "w": 0.08, "unit": "%"},
    {"key": "tvl30",      "label": "TVL 30일",   "w": 0.17, "unit": "%"},
    {"key": "rev_chg30",  "label": "매출 30일",  "w": 0.21, "unit": "%"},
    {"key": "turn",       "label": "회전율",     "w": 0.12, "unit": "x"},
]

# 신규 상장 종목이 섞이면 30일 수익률이 착시가 된다 → 관측 기간을 충족한 종목만 사용
MIN_AGE_H = {"px30": 720, "px7": 168}
MIN_SAMPLE = 3

UAS = [
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36",
]

_CTX = ssl.create_default_context()


# ── HTTP ─────────────────────────────────────────────────────────────────────
def http_json(url, timeout=60, tries=4, backoff=(6, 16, 38, 75)):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": random.choice(UAS),
                "Accept": "application/json",
            })
            with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            last = e
            wait = backoff[min(i, len(backoff) - 1)]
            ra = e.headers.get("Retry-After") if e.headers else None
            if ra:
                try:
                    wait = max(wait, min(int(ra), 120))
                except ValueError:
                    pass
            if e.code in (400, 404):
                break
            print(f"[http] {e.code} {url[:80]} → {wait}s 후 재시도")
        except Exception as e:  # noqa: BLE001
            last = e
            wait = backoff[min(i, len(backoff) - 1)]
            print(f"[http] {type(e).__name__} {url[:80]} → {wait}s 후 재시도")
        if i < tries - 1:
            time.sleep(wait + random.uniform(0, 2))
    print(f"[http] 최종 실패: {url[:110]} ({last})")
    return None


def read_local_or_raw(local_rel, raw_url):
    """같은 레포 안의 파일을 우선 읽고, 없으면 raw.githubusercontent 폴백."""
    p = os.path.normpath(os.path.join(HERE, local_rel))
    if os.path.exists(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f), "local"
        except Exception as e:  # noqa: BLE001
            print(f"[src] 로컬 읽기 실패 {local_rel}: {e}")
    d = http_json(raw_url, tries=3)
    return (d, "raw") if d else (None, None)


# ── 유틸 ─────────────────────────────────────────────────────────────────────
def pct(new, old):
    if new is None or old in (None, 0):
        return None
    return (new / old - 1.0) * 100.0


def med(vals):
    vals = [v for v in vals if isinstance(v, (int, float))]
    return statistics.median(vals) if vals else None


def fmt_usd(v):
    if v is None:
        return "—"
    a = abs(v)
    if a >= 1e12:
        return f"${v/1e12:.2f}T"
    if a >= 1e9:
        return f"${v/1e9:.2f}B"
    if a >= 1e6:
        return f"${v/1e6:.1f}M"
    if a >= 1e3:
        return f"${v/1e3:.0f}K"
    return f"${v:.0f}"


def fmt_pct(v, digits=1):
    if v is None:
        return "—"
    return f"{v:+.{digits}f}%"


def color_dot(v, strong=15.0, mild=1.0):
    """증감을 색으로. 텔레그램은 글자색이 없으므로 색 블록 이모지로 대체한다."""
    if v is None:
        return "⬜"
    if v >= strong:
        return "🟩"
    if v >= mild:
        return "🟢"
    if v <= -strong:
        return "🟥"
    if v <= -mild:
        return "🔴"
    return "⚪"


def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


# ── 수집 ─────────────────────────────────────────────────────────────────────
def load_members():
    """섹터 구성종목은 narrative-radar 의 고정 유니버스를 정본으로 재사용한다."""
    uni, src = read_local_or_raw(
        "../narrative_radar/universe.json",
        "https://raw.githubusercontent.com/jinhae8971/korea-etf-calmar/main/narrative_radar/universe.json",
    )
    out, frozen = {}, None
    if uni and isinstance(uni.get("narratives"), dict):
        frozen = uni.get("frozen_at")
        for code in FALLBACK_MEMBERS:
            n = uni["narratives"].get(code)
            if n and n.get("members"):
                out[code] = [m["id"] for m in n["members"] if m.get("id")]
    for code, ids in FALLBACK_MEMBERS.items():
        if not out.get(code):
            out[code] = list(ids)
            src, frozen = (src or "fallback"), frozen or "2026-08-21"
    return out, {"source": src or "fallback", "frozen_at": frozen}


def load_hood_basket():
    """로빈후드 체인은 네이티브 토큰이 없다 → 생태계 토큰 집계로 대신한다."""
    snap, src = read_local_or_raw(
        "../hood_radar/data/latest.json",
        "https://raw.githubusercontent.com/jinhae8971/korea-etf-calmar/main/hood_radar/data/latest.json",
    )
    if not snap or not snap.get("rows"):
        return [], None, {"source": None}
    rows = [r for r in snap["rows"]
            if r.get("cg_id")
            and (r.get("liq") or 0) >= HOOD_RULE["min_liq_usd"]
            and (r.get("mcap") or 0) >= HOOD_RULE["min_mcap_usd"]]
    rows.sort(key=lambda r: r.get("mcap") or 0, reverse=True)
    rows = rows[: HOOD_RULE["top_n"]]
    meta = {
        "source": src,
        "as_of": snap.get("as_of_kst"),
        "chain_dex_24h": (snap.get("meta") or {}).get("chain_dex_24h"),
        "eco_mcap_all": sum((r.get("mcap") or 0) for r in snap["rows"]),
        "symbols": [r.get("symbol") for r in rows],
    }
    return [r["cg_id"] for r in rows], rows, meta


def fetch_markets(ids):
    """CoinGecko 무료 티어는 rate limit이 빡빡하다 → 실행당 1회 호출로 끝낸다."""
    url = ("https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd&ids="
           + urllib.parse.quote(",".join(sorted(set(ids))))
           + "&per_page=250&page=1&sparkline=false"
           + "&price_change_percentage=24h%2C7d%2C30d")
    data = http_json(url, timeout=70)
    out = {}
    for c in (data or []):
        out[c.get("id")] = {
            "symbol": (c.get("symbol") or "").upper(),
            "price": c.get("current_price"),
            "mcap": c.get("market_cap"),
            "fdv": c.get("fully_diluted_valuation"),
            "vol": c.get("total_volume"),
            "p24": c.get("price_change_percentage_24h_in_currency"),
            "p7": c.get("price_change_percentage_7d_in_currency"),
            "p30": c.get("price_change_percentage_30d_in_currency"),
        }
    return out


def fetch_chain_tvl(chain):
    d = http_json("https://api.llama.fi/v2/historicalChainTvl/" + urllib.parse.quote(chain), timeout=70)
    if not d or len(d) < 2:
        return None
    series = [(x.get("date"), x.get("tvl")) for x in d if x.get("tvl") is not None]
    cur = series[-1][1]
    d7 = series[-8][1] if len(series) >= 8 else None
    d30 = series[-31][1] if len(series) >= 31 else None
    return {"tvl": cur, "chg7": pct(cur, d7), "chg30": pct(cur, d30),
            "days": len(series), "series": [v for _, v in series[-90:]]}


def fetch_chain_fees(chain):
    base = ("https://api.llama.fi/overview/fees/" + urllib.parse.quote(chain)
            + "?excludeTotalDataChart=true&excludeTotalDataChartBreakdown=true&dataType=")
    out = {}
    for label, dt in (("rev", "dailyRevenue"), ("fees", "dailyFees")):
        d = http_json(base + dt, timeout=60, tries=3)
        if d:
            out[label] = {"d24": d.get("total24h"), "d7": d.get("total7d"),
                          "d30": d.get("total30d"), "chg_1m": d.get("change_1m"),
                          "chg_7d": d.get("change_7d")}
    return out or None


def fetch_btc_miner_fees():
    """DefiLlama의 Bitcoin '매출'은 BTC DeFi 한정이라 체인 경제를 대표하지 못한다.
    비트코인의 실제 체인 수익은 채굴 수수료이므로 별도 원천으로 병기한다."""
    d = http_json("https://api.blockchain.info/charts/transaction-fees-usd"
                  "?timespan=90days&format=json&sampled=false", timeout=60, tries=3)
    vals = [v.get("y") for v in (d or {}).get("values", []) if v.get("y") is not None]
    if len(vals) < 40:
        return None
    last30 = sum(vals[-30:])
    prev30 = sum(vals[-60:-30]) if len(vals) >= 60 else None
    return {"d24": vals[-1], "d7": sum(vals[-7:]), "d30": last30,
            "chg_1m": pct(last30, prev30), "chg_7d": pct(sum(vals[-7:]), sum(vals[-14:-7]))}


# ── 트랙 지표 산출 ───────────────────────────────────────────────────────────
def build_track(t, members, mkt, hood_rows, chain_tvl, chain_fee, btc_fee):
    r = {"key": t["key"], "label": t["label"], "kind": t["kind"], "chain": t["chain"],
         "members": [], "coverage": None, "notes": []}

    if t["kind"] == "basket":
        ids = members.get(t["code"], [])
    elif t["kind"] == "ecosystem":
        ids = [x["cg_id"] for x in (hood_rows or [])]
    else:
        ids = [SINGLE_ASSET_IDS[t["key"]]]

    got = [(i, mkt[i]) for i in ids if i in mkt]
    r["coverage"] = f"{len(got)}/{len(ids)}" if ids else "0/0"
    r["members"] = [g[1]["symbol"] for g in got]
    if not got:
        r["notes"].append("시세 수집 실패")
        return r

    # 가격: 바스켓은 중앙값(평균은 극단치가 지배한다)
    age = {}
    for row in (hood_rows or []):
        if row.get("cg_id"):
            age[row["cg_id"]] = row.get("age_hours")

    def px(field, need_h):
        vals = []
        for cid, c in got:
            if t["kind"] == "ecosystem":
                a = age.get(cid)
                if a is None or a < need_h:
                    continue
            if isinstance(c[field], (int, float)):
                vals.append(c[field])
        if t["kind"] == "ecosystem" and len(vals) < MIN_SAMPLE:
            return None, len(vals)
        return med(vals), len(vals)

    r["px24"], _ = px("p24", 24)
    r["px7"], n7 = px("p7", MIN_AGE_H["px7"])
    r["px30"], n30 = px("p30", MIN_AGE_H["px30"])
    r["px_sample"] = {"7d": n7, "30d": n30}

    # 시총·거래대금은 합계, 증감은 합계 기준으로 역산 (개별 %의 평균이 아니다)
    mc = sum(g[1]["mcap"] or 0 for g in got) or None
    vol = sum(g[1]["vol"] or 0 for g in got) or None
    prev = 0.0
    for _, c in got:
        if c["mcap"] and c["p24"] is not None and c["p24"] > -99:
            prev += c["mcap"] / (1 + c["p24"] / 100.0)
    r["mcap"] = mc
    r["mcap_chg24"] = pct(mc, prev) if prev else None
    r["vol24"] = vol
    r["turn"] = (vol / mc) if (vol and mc) else None

    if t["kind"] == "ecosystem":
        r["notes"].append(
            f"체인 토큰이 없어 생태계 상위 {HOOD_RULE['top_n']}종 집계로 대신함 "
            f"(30일 수익률은 상장 30일 경과 {r['px_sample']['30d']}종 기준)")

    # TVL
    tv = chain_tvl.get(t["chain"]) if t["chain"] else None
    if tv:
        r["tvl"] = tv["tvl"]
        r["tvl7"] = tv["chg7"]
        r["tvl30"] = tv["chg30"]
        r["tvl_days"] = tv["days"]
        r["tvl_series"] = tv["series"]
        if tv["chg30"] is None:
            r["notes"].append(f"체인 이력 {tv['days']}일 — 30일 변화 미산출")
    else:
        r["tvl"] = r["tvl7"] = r["tvl30"] = None
        if t["kind"] == "basket":
            r["notes"].append("섹터 특성상 TVL·매출 관점 해당 없음")

    # 매출
    fee = chain_fee.get(t["chain"]) if t["chain"] else None
    rev = (fee or {}).get("rev")
    if t["key"] == "BTC":
        if btc_fee:
            r["rev30"] = btc_fee["d30"]
            r["rev_chg30"] = btc_fee["chg_1m"]
            r["rev_basis"] = "채굴 수수료 (blockchain.info)"
            r["rev_defi30"] = (rev or {}).get("d30")
            r["notes"].append("매출은 채굴 수수료 기준 — BTC는 수수료가 아니라 화폐 프리미엄으로 값이 매겨지므로 P/S는 해석하지 말 것")
        else:
            r["rev30"] = r["rev_chg30"] = None
            r["rev_basis"] = None
    elif rev:
        r["rev30"] = rev["d30"]
        r["rev_chg30"] = rev["chg_1m"]
        r["rev_basis"] = "프로토콜 매출 (DefiLlama)"
    else:
        r["rev30"] = r["rev_chg30"] = None
        r["rev_basis"] = None

    # 파생 배수
    ann = r["rev30"] * 365.0 / 30.0 if r.get("rev30") else None
    r["ps"] = (r["mcap"] / ann) if (ann and r.get("mcap")) else None
    r["mc_tvl"] = (r["mcap"] / r["tvl"]) if (r.get("tvl") and r.get("mcap")) else None
    r["rev_yield"] = (ann / r["tvl"] * 100.0) if (ann and r.get("tvl")) else None
    return r


def rank_and_score(tracks):
    """관점별 순위 → 백분위 → 이용 가능한 관점만 가중평균."""
    lens_rank = {}
    for L in LENSES:
        vals = [(t["key"], t.get(L["key"])) for t in tracks if isinstance(t.get(L["key"]), (int, float))]
        vals.sort(key=lambda x: x[1], reverse=True)
        n = len(vals)
        lens_rank[L["key"]] = {
            k: {"rank": i + 1, "n": n, "pctl": (100.0 * (n - (i + 1)) / (n - 1)) if n > 1 else 50.0}
            for i, (k, _) in enumerate(vals)
        }
    for t in tracks:
        num = den = 0.0
        used = []
        for L in LENSES:
            e = lens_rank[L["key"]].get(t["key"])
            if e:
                num += L["w"] * e["pctl"]
                den += L["w"]
                used.append(L["key"])
        t["score"] = round(num / den, 1) if den else None
        t["lens_used"] = used
        t["lens_weight_covered"] = round(den, 2)
        t["lens_rank"] = {L["key"]: lens_rank[L["key"]].get(t["key"], {}).get("rank") for L in LENSES}
        t["lens_n"] = {L["key"]: lens_rank[L["key"]].get(t["key"], {}).get("n") for L in LENSES}
    ordered = sorted([t for t in tracks if t["score"] is not None], key=lambda x: -x["score"])
    for i, t in enumerate(ordered):
        t["overall_rank"] = i + 1
    for t in tracks:
        t.setdefault("overall_rank", None)
    return lens_rank


# ── 이력·변화량 ──────────────────────────────────────────────────────────────
DELTA_KEYS = ["score", "px30", "px7", "tvl30", "rev30", "mcap", "turn"]


def attach_delta(tracks, history):
    prev = history[-1] if history else None
    for t in tracks:
        d = {}
        if prev:
            p = (prev.get("tracks") or {}).get(t["key"], {})
            for k in DELTA_KEYS:
                a, b = t.get(k), p.get(k)
                if isinstance(a, (int, float)) and isinstance(b, (int, float)):
                    d[k] = round(a - b, 4)
            if isinstance(p.get("overall_rank"), int) and isinstance(t.get("overall_rank"), int):
                d["rank"] = p["overall_rank"] - t["overall_rank"]  # +면 상승
            d["_base"] = prev.get("as_of_kst")
        t["delta"] = d


# ── 렌더링 ───────────────────────────────────────────────────────────────────
MEDAL = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]


def arrow(v, eps=0.05):
    if v is None:
        return ""
    if v > eps:
        return f"▲{abs(v):.1f}"
    if v < -eps:
        return f"▼{abs(v):.1f}"
    return "─"


def dwidth(s):
    """한글은 등폭 폰트에서 2칸을 먹는다 — 정렬은 표시폭으로 계산해야 한다."""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in s)


def pad(s, width, right=False):
    gap = max(0, width - dwidth(s))
    return (" " * gap + s) if right else (s + " " * gap)


def lens_matrix(tracks):
    """관점 × 트랙 순위 매트릭스 (표시폭 기준 정렬)."""
    lw = max([dwidth(L["label"]) for L in LENSES] + [dwidth("종합")]) + 1
    cw = max(6, max(dwidth(t["key"]) for t in tracks) + 2)
    head = pad("관점", lw) + "".join(pad(t["key"], cw, True) for t in tracks)
    bar = "-" * dwidth(head)
    lines = [head, bar]
    for L in LENSES:
        cells = ""
        for t in tracks:
            r = t["lens_rank"].get(L["key"])
            n = t["lens_n"].get(L["key"])
            cells += pad(f"{r}/{n}" if r else "-", cw, True)
        lines.append(pad(L["label"], lw) + cells)
    lines.append(bar)
    cells = "".join(pad(str(t["overall_rank"]) if t["overall_rank"] else "-", cw, True) for t in tracks)
    lines.append(pad("종합", lw) + cells)
    return "\n".join(lines)


def render_telegram(payload):
    tracks = payload["tracks"]
    st = payload["status"]
    L = []
    L.append("📊 <b>5트랙 상대강도</b> — 프라이버시·DePIN·로빈후드·하이퍼리퀴드·비트코인")
    L.append(f"<i>{payload['as_of_kst']} KST · {st}</i>")
    L.append("")
    L.append("<pre>" + esc(lens_matrix(tracks)) + "</pre>")
    L.append("<i>숫자는 해당 관점의 순위/비교대상 수. ‘-’는 해당 관점 없음.</i>")
    L.append("")

    for t in sorted(tracks, key=lambda x: (x["overall_rank"] or 99)):
        d = t.get("delta") or {}
        rk = f"{MEDAL[t['overall_rank']-1]} " if t.get("overall_rank") else "▫️ "
        dr = ""
        if isinstance(d.get("rank"), int) and d["rank"] != 0:
            dr = f" ({'▲' if d['rank']>0 else '▼'}{abs(d['rank'])}계단)"
        sc = f"{t['score']:.0f}" if t.get("score") is not None else "—"
        ds = f" {arrow(d.get('score'))}" if d.get("score") is not None else ""
        L.append(f"{rk}<b>{esc(t['label'])}</b> · 종합 {sc}점{ds}{dr}")

        L.append(f"   가격 {color_dot(t.get('px30'))}{fmt_pct(t.get('px30'))} (30일) · "
                 f"{color_dot(t.get('px7'), 8, 0.5)}{fmt_pct(t.get('px7'))} (7일)")

        mc = f"시총 {fmt_usd(t.get('mcap'))} {color_dot(t.get('mcap_chg24'), 5, 0.3)}{fmt_pct(t.get('mcap_chg24'))}"
        if t.get("tvl") is not None:
            mc += f" · TVL {fmt_usd(t['tvl'])} {color_dot(t.get('tvl30'))}{fmt_pct(t.get('tvl30'))}"
        L.append("   " + mc)

        if t.get("rev30") is not None:
            line = (f"   매출 {fmt_usd(t['rev30'])}/30일 "
                    f"{color_dot(t.get('rev_chg30'), 30, 2)}{fmt_pct(t.get('rev_chg30'))}")
            bits = []
            if t.get("ps"):
                bits.append(f"P/S {t['ps']:.1f}")
            if t.get("mc_tvl"):
                bits.append(f"MC/TVL {t['mc_tvl']:.2f}")
            if t.get("rev_yield"):
                bits.append(f"TVL당 연수익 {t['rev_yield']:.0f}%")
            if bits:
                line += " · " + " · ".join(bits)
            L.append(line)
        elif t.get("mc_tvl"):
            L.append(f"   MC/TVL {t['mc_tvl']:.2f}")

        extra = []
        if t.get("turn") is not None:
            extra.append(f"회전율 {t['turn']*100:.1f}%")
        if t.get("coverage"):
            extra.append(f"구성 {t['coverage']}")
        if extra:
            L.append("   <i>" + " · ".join(extra) + "</i>")
        for n in t.get("notes", [])[:2]:
            L.append(f"   <i>· {esc(n)}</i>")
        L.append("")

    hi = payload.get("highlights") or []
    if hi:
        L.append("🔎 <b>주요 변화</b>")
        for h in hi:
            L.append(f"• {esc(h)}")
        L.append("")

    base = (tracks[0].get("delta") or {}).get("_base")
    L.append(f"<i>비교 기준: {base or '첫 관측 — 변화량은 다음 회차부터'}</i>")
    L.append("<i>성격이 다른 대상의 비교입니다(섹터 바스켓·단일자산·체인 생태계). "
             "순위는 현재 자금 반응의 서술이며 수익률 예측이 아닙니다.</i>")
    L.append(f"📈 {DASHBOARD_URL}")
    return "\n".join(L)


def build_highlights(tracks):
    out = []
    for t in tracks:
        d = t.get("delta") or {}
        if isinstance(d.get("rank"), int) and abs(d["rank"]) >= 2:
            out.append((abs(d["rank"]) * 30,
                        f"{t['label']} 종합 {abs(d['rank'])}계단 "
                        f"{'상승' if d['rank']>0 else '하락'} → {t['overall_rank']}위"))
        for k, lab, thr in (("px30", "30일 가격", 12), ("tvl30", "TVL 30일", 20), ("rev_chg30", "매출 30일", 60)):
            v = t.get(k)
            if isinstance(v, (int, float)) and abs(v) >= thr:
                out.append((abs(v), f"{t['label']} {lab} {fmt_pct(v, 0)}"))
    out.sort(key=lambda x: -x[0])
    seen, res = set(), []
    for _, s in out:
        h = s.split()[0]
        if s in seen:
            continue
        seen.add(s)
        res.append(s)
        if len(res) >= 3:
            break
    return res


# ── 대시보드 ─────────────────────────────────────────────────────────────────
def css_color(v, strong=15.0, mild=1.0):
    if v is None:
        return "var(--muted)"
    if v >= strong:
        return "#16a34a"
    if v >= mild:
        return "#4ade80"
    if v <= -strong:
        return "#dc2626"
    if v <= -mild:
        return "#f87171"
    return "var(--muted)"


def cell(v, fmt=fmt_pct, strong=15.0, mild=1.0):
    return f'<td style="color:{css_color(v, strong, mild)};font-weight:600">{fmt(v)}</td>'


def render_dashboard(payload):
    tracks = sorted(payload["tracks"], key=lambda x: (x["overall_rank"] or 99))
    rows = []
    for t in tracks:
        d = t.get("delta") or {}
        drank = ""
        if isinstance(d.get("rank"), int) and d["rank"]:
            drank = (f'<span style="color:{"#16a34a" if d["rank"]>0 else "#dc2626"}">'
                     f'{"▲" if d["rank"]>0 else "▼"}{abs(d["rank"])}</span>')
        score_td = f'<td>{t["score"]:.0f}</td>' if t.get("score") is not None else "<td>—</td>"
        rows.append(
            "<tr>"
            f'<td><b>{t["overall_rank"] or "-"}</b> {drank}</td>'
            f'<td><b>{esc(t["label"])}</b><div class="sub">{esc(t["kind"])} · {esc(t.get("coverage") or "")}</div></td>'
            + score_td
            + cell(t.get("px30")) + cell(t.get("px7"), strong=8, mild=0.5)
            + f'<td>{fmt_usd(t.get("mcap"))}</td>' + cell(t.get("mcap_chg24"), strong=5, mild=0.3)
            + f'<td>{fmt_usd(t.get("tvl"))}</td>' + cell(t.get("tvl30"))
            + f'<td>{fmt_usd(t.get("rev30"))}</td>' + cell(t.get("rev_chg30"), strong=30, mild=2)
            + f'<td>{"%.1f" % t["ps"] if t.get("ps") else "—"}</td>'
            + f'<td>{"%.2f" % t["mc_tvl"] if t.get("mc_tvl") else "—"}</td>'
            + f'<td>{"%.1f%%" % (t["turn"]*100) if t.get("turn") is not None else "—"}</td>'
            "</tr>")

    lens_rows = []
    for L in LENSES:
        cells = "".join(
            f'<td>{t["lens_rank"].get(L["key"]) or "—"}</td>' for t in tracks)
        lens_rows.append(f"<tr><td>{L['label']} <span class='sub'>w{L['w']:.2f}</span></td>{cells}</tr>")
    lens_head = "".join(f"<th>{esc(t['label'])}</th>" for t in tracks)

    notes = []
    for t in tracks:
        for n in t.get("notes", []):
            notes.append(f"<li><b>{esc(t['label'])}</b> — {esc(n)}</li>")

    return f"""<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>5트랙 상대강도</title>
<style>
:root{{--bg:#0b0f17;--card:#131a26;--fg:#e6edf7;--muted:#8b98ab;--line:#222e42}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--fg);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Noto Sans KR",sans-serif}}
.wrap{{max-width:1080px;margin:0 auto;padding:18px}}
h1{{font-size:19px;margin:0 0 4px}} .sub{{color:var(--muted);font-size:11px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px;margin:14px 0;overflow-x:auto}}
table{{border-collapse:collapse;width:100%;font-size:13px;white-space:nowrap}}
th,td{{padding:7px 9px;border-bottom:1px solid var(--line);text-align:right}}
th:first-child,td:first-child,th:nth-child(2),td:nth-child(2){{text-align:left}}
th{{color:var(--muted);font-weight:600;font-size:11px;text-transform:uppercase}}
ul{{margin:6px 0 0 18px;padding:0;color:var(--muted);font-size:12px;line-height:1.7}}
.badge{{display:inline-block;background:#1d2637;border:1px solid var(--line);border-radius:999px;padding:2px 9px;font-size:11px;color:var(--muted)}}
</style></head><body><div class="wrap">
<h1>5트랙 상대강도 <span class="badge">v{VERSION}</span></h1>
<div class="sub">{esc(payload['as_of_kst'])} KST · {esc(payload['status'])} · 임계값 고정 {THRESHOLDS_FROZEN_AT}</div>

<div class="card"><table>
<tr><th>순위</th><th>트랙</th><th>종합</th><th>가격30d</th><th>가격7d</th><th>시총</th><th>24h</th>
<th>TVL</th><th>TVL30d</th><th>매출30d</th><th>증감</th><th>P/S</th><th>MC/TVL</th><th>회전율</th></tr>
{''.join(rows)}
</table></div>

<div class="card">
<div class="sub" style="margin-bottom:8px">관점별 순위</div>
<table><tr><th>관점</th>{lens_head}</tr>{''.join(lens_rows)}</table>
</div>

<div class="card">
<div class="sub">읽는 법 · 한계</div>
<ul>
<li>성격이 다른 대상을 나란히 둔 비교다 — 프라이버시·DePIN은 <b>고정 바스켓의 중앙값</b>,
하이퍼리퀴드·비트코인은 <b>단일 자산</b>, 로빈후드는 토큰이 없어 <b>생태계 상위 종목 집계</b>다.</li>
<li>종합 점수는 이용 가능한 관점만의 가중 백분위다. 관점 수가 다른 트랙 간 비교는 참고치로만 볼 것.</li>
<li>TVL은 펀더멘털이 아니라 예치금이다(인센티브 파밍·중복계상 포함).</li>
<li>순위는 현재 자금 반응의 서술이며 미래 수익률을 주장하지 않는다.</li>
{''.join(notes)}
</ul>
</div>
</div></body></html>"""


# ── 메인 ─────────────────────────────────────────────────────────────────────
def main():
    now = datetime.now(KST)
    members, msrc = load_members()
    hood_ids, hood_rows, hmeta = load_hood_basket()

    ids = []
    for code in FALLBACK_MEMBERS:
        ids += members.get(code, [])
    ids += list(SINGLE_ASSET_IDS.values()) + hood_ids
    mkt = fetch_markets(ids)
    print(f"[cg] {len(mkt)}/{len(set(ids))} 종목 수집")

    chain_tvl, chain_fee = {}, {}
    for t in TRACKS:
        if not t["chain"]:
            continue
        tv = fetch_chain_tvl(t["chain"])
        if tv:
            chain_tvl[t["chain"]] = tv
        fe = fetch_chain_fees(t["chain"])
        if fe:
            chain_fee[t["chain"]] = fe
    btc_fee = fetch_btc_miner_fees()

    tracks = [build_track(t, members, mkt, hood_rows, chain_tvl, chain_fee, btc_fee) for t in TRACKS]
    live = [t for t in tracks if t.get("px30") is not None or t.get("mcap")]
    if len(live) < 4:
        print("::error::수집 커버리지 부족 — 판정 불가 (발송 금지)")
        save_json(os.path.join(DATA, "status.json"),
                  {"as_of_kst": now.strftime("%Y-%m-%d %H:%M"), "status": "판정 불가", "live": len(live)})
        sys.exit(1)

    rank_and_score(tracks)

    hist_path = os.path.join(DATA, "history.json")
    history = []
    if os.path.exists(hist_path):
        try:
            with open(hist_path, "r", encoding="utf-8") as f:
                history = json.load(f) or []
        except Exception as e:  # noqa: BLE001
            print(f"[hist] 읽기 실패: {e}")

    attach_delta(tracks, history)
    status = "OK" if len(live) == len(TRACKS) else f"DEGRADED ({len(live)}/{len(TRACKS)})"

    payload = {
        "version": VERSION,
        "as_of_kst": now.strftime("%Y-%m-%d %H:%M"),
        "as_of_utc": now.astimezone(timezone.utc).isoformat(),
        "status": status,
        "sources": {"members": msrc, "hood": hmeta,
                    "market": "coingecko/coins/markets", "tvl_fees": "defillama",
                    "btc_fees": "blockchain.info"},
        "lenses": LENSES,
        "tracks": tracks,
        "dashboard_url": DASHBOARD_URL,
    }
    payload["highlights"] = build_highlights(tracks)
    payload["message"] = render_telegram(payload)

    save_json(os.path.join(DATA, "latest.json"), payload)

    today = now.strftime("%Y-%m-%d")
    snap = {"as_of_kst": payload["as_of_kst"], "date": today,
            "tracks": {t["key"]: {k: t.get(k) for k in DELTA_KEYS + ["overall_rank"]} for t in tracks}}
    if history and history[-1].get("date") == today:
        history[-1] = snap
    else:
        history.append(snap)
    save_json(hist_path, history[-400:])

    docs = os.path.normpath(os.path.join(HERE, "..", "docs", "xrs"))
    os.makedirs(docs, exist_ok=True)
    with open(os.path.join(docs, "index.html"), "w", encoding="utf-8") as f:
        f.write(render_dashboard(payload))

    print(payload["message"])
    print(f"[done] status={status}")


if __name__ == "__main__":
    main()
