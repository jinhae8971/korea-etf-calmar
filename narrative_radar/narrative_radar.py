#!/usr/bin/env python3
"""
Narrative Radar — 크립토 내러티브 상대강도 관측 + 변화 탐지

설계 원칙 (consensus-gap 4연속 가설 기각에서 얻은 교훈):
  1. 이 시스템은 예측기가 아니라 *관측기*다. 점수는 "지금 자금이 어디에 반응하는가"를
     기술할 뿐이며, 미래 수익률을 주장하지 않는다.
  2. 구성종목 매핑은 universe.json에 고정한다. 성과를 보고 갈아끼우면 사후선택 편향.
  3. 섹터 대표값은 반드시 중앙값(median). 평균은 소수 극단치가 지배한다.
  4. 수집 실패 시 "변화 없음"을 절대 발송하지 않는다. "판정 불가"로 명시한다.
"""
from __future__ import annotations

import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import tvl_divergence as tvl
import discovery as disc

KST = timezone(timedelta(hours=9))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
HISTORY_PATH = os.path.join(DATA_DIR, "history.json")
LATEST_PATH = os.path.join(DATA_DIR, "latest.json")
DISCOVERY_PATH = os.path.join(DATA_DIR, "discovery_state.json")

CG_HOSTS = [
    "https://api.coingecko.com/api/v3",
    "https://api.coingecko.com/api/v3",  # 동일 호스트 재시도(버스트성 429 대응)
]
UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/125.0 Safari/537.36",
]

# 변화 탐지 임계값 — 전부 여기 모아둔다
TH_BREADTH_JUMP = 25.0     # 폭 확대(%p, 7일 평균 대비)
TH_TURNOVER_Z = 2.0        # 개별 종목 회전율 z
TH_RANK_PERSIST = 3        # 순위 변화가 유효하려면 유지돼야 하는 일수
TH_DOM_LOOKBACK = 20       # BTC 도미넌스 저점 갱신 확인 구간


# ────────────────────────────── 설정·유틸 ──────────────────────────────

def load_config() -> dict:
    cfg = {
        "telegram_token": os.environ.get("TELEGRAM_TOKEN", ""),
        "telegram_chat_id": os.environ.get("TELEGRAM_CHAT_ID", ""),
        "pages_url": os.environ.get("PAGES_URL", ""),
    }
    p = os.path.join(BASE_DIR, "config.json")
    if os.path.exists(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            print(f"[config] 읽기 실패 - 환경변수만 사용: {e}")
            data = {}
        for k, v in data.items():
            key = k.lower()
            if key in cfg and not cfg[key]:
                cfg[key] = v
    return cfg


def save_json(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def load_json(path: str, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default


def median(xs):
    v = sorted(x for x in xs if x is not None)
    if not v:
        return None
    n = len(v)
    return v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2.0


def zscores(values: dict) -> dict:
    """None은 제외하고 z 계산. 표본이 2개 미만이거나 분산 0이면 전부 0."""
    xs = [v for v in values.values() if v is not None]
    if len(xs) < 2:
        return {k: 0.0 for k in values}
    m = sum(xs) / len(xs)
    var = sum((x - m) ** 2 for x in xs) / (len(xs) - 1)
    sd = var ** 0.5
    if sd <= 1e-12:
        return {k: 0.0 for k in values}
    return {k: (0.0 if v is None else (v - m) / sd) for k, v in values.items()}


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


# ────────────────────────────── 수집 ──────────────────────────────

def http_get_json(url: str, tries: int = 5):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": random.choice(UA_POOL), "Accept": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last = e
            wait = None
            try:
                ra = e.headers.get("Retry-After") if e.headers else None
                if ra:
                    wait = float(ra)
            except (TypeError, ValueError):
                wait = None
            if wait is None:
                wait = [8, 20, 45, 90, 150][min(i, 4)] + random.uniform(0, 5)
            print(f"[http] {e.code} — {wait:.0f}s 후 재시도 ({i + 1}/{tries})")
            time.sleep(wait)
        except Exception as e:  # noqa: BLE001
            last = e
            wait = [5, 12, 25, 50, 90][min(i, 4)] + random.uniform(0, 4)
            print(f"[http] {type(e).__name__}: {e} — {wait:.0f}s 후 재시도 ({i + 1}/{tries})")
            time.sleep(wait)
    raise RuntimeError(f"요청 실패: {url} ({last})")


def fetch_markets(ids: list) -> dict:
    """/coins/markets 1회 호출로 전 종목 수집. 무료 티어 rate limit 때문에 호출을 최소화한다."""
    q = urllib.parse.urlencode({
        "vs_currency": "usd",
        "ids": ",".join(ids),
        "price_change_percentage": "24h,7d,30d",
        "per_page": 250,
        "page": 1,
    })
    for host in CG_HOSTS:
        try:
            rows = http_get_json(f"{host}/coins/markets?{q}", tries=3)
            if isinstance(rows, list) and rows:
                return {r["id"]: r for r in rows}
        except Exception as e:  # noqa: BLE001
            print(f"[markets] {host} 실패: {e}")
            time.sleep(5)
    return {}


def fetch_global() -> dict:
    for host in CG_HOSTS:
        try:
            d = http_get_json(f"{host}/global", tries=3)
            return d.get("data", {}) or {}
        except Exception as e:  # noqa: BLE001
            print(f"[global] {host} 실패: {e}")
            time.sleep(5)
    return {}


# ────────────────────────────── 계산 ──────────────────────────────

def build_coin_rows(universe: dict, mk: dict) -> tuple:
    """종목별 원지표 추출. (rows, btc) 반환."""
    btc = mk.get("bitcoin")
    rows = []
    for code, nar in universe["narratives"].items():
        for m in nar["members"]:
            r = mk.get(m["id"])
            if not r:
                continue
            mcap = r.get("market_cap") or 0
            vol = r.get("total_volume") or 0
            rows.append({
                "id": m["id"],
                "symbol": m["symbol"],
                "narrative": code,
                "fit": m["fit"],
                "role": m["role"],
                "price": r.get("current_price"),
                "mcap": mcap,
                "vol": vol,
                "turnover": (vol / mcap) if mcap > 0 else None,
                "r24": r.get("price_change_percentage_24h_in_currency"),
                "r7": r.get("price_change_percentage_7d_in_currency"),
                "r30": r.get("price_change_percentage_30d_in_currency"),
            })
    return rows, btc


def excess(v, base):
    if v is None or base is None:
        return None
    return v - base


def score_coins(rows: list, btc: dict) -> list:
    """내러티브 부합도(정성 고정) × 자금 반응(정량 관측). 예측 점수가 아니다."""
    b24 = btc.get("price_change_percentage_24h_in_currency") if btc else None
    b7 = btc.get("price_change_percentage_7d_in_currency") if btc else None
    b30 = btc.get("price_change_percentage_30d_in_currency") if btc else None

    for r in rows:
        r["x24"] = excess(r["r24"], b24)
        r["x7"] = excess(r["r7"], b7)
        r["x30"] = excess(r["r30"], b30)

    z30 = zscores({r["id"]: r["x30"] for r in rows})
    z7 = zscores({r["id"]: r["x7"] for r in rows})
    zt = zscores({r["id"]: r["turnover"] for r in rows})

    for r in rows:
        r["z30"] = round(z30[r["id"]], 3)
        r["z7"] = round(z7[r["id"]], 3)
        r["zturn"] = round(zt[r["id"]], 3)
        flow = 0.45 * r["z30"] + 0.30 * r["z7"] + 0.25 * r["zturn"]
        # fit은 곱이 아니라 가중 혼합 — fit이 낮다고 음수 점수를 뒤집지 않도록
        r["flow"] = round(flow, 3)
        r["score"] = round(flow * (0.55 + 0.45 * r["fit"]), 3)

    rows.sort(key=lambda r: -r["score"])
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    return rows


def aggregate_narratives(universe: dict, rows: list) -> list:
    out = []
    for code, nar in universe["narratives"].items():
        mem = [r for r in rows if r["narrative"] == code]
        if not mem:
            continue
        x30 = [r["x30"] for r in mem if r["x30"] is not None]
        breadth = (100.0 * sum(1 for v in x30 if v > 0) / len(x30)) if x30 else None
        out.append({
            "code": code,
            "name": nar["name"],
            "thesis": nar["thesis"],
            "n": len(mem),
            "rs30": median([r["x30"] for r in mem]),
            "rs7": median([r["x7"] for r in mem]),
            "rs24": median([r["x24"] for r in mem]),
            "breadth": breadth,
            "turnover": median([r["turnover"] for r in mem]),
            "top": sorted(mem, key=lambda r: -r["score"])[0]["symbol"],
        })
    out.sort(key=lambda d: (d["rs30"] is None, -(d["rs30"] or -1e9)))
    for i, d in enumerate(out, 1):
        d["rank"] = i
        for k in ("rs30", "rs7", "rs24", "breadth"):
            if d[k] is not None:
                d[k] = round(d[k], 2)
        if d["turnover"] is not None:
            d["turnover"] = round(d["turnover"], 4)
    return out


# ────────────────────────────── 변화 탐지 ──────────────────────────────

def detect_changes(narratives: list, rows: list, glob: dict, history: list) -> list:
    """
    관측된 변화만 보고한다. 원인을 단정하지 않는다.
    history는 오래된 것 → 최신 순으로 정렬된 일별 스냅샷 리스트.
    """
    ev = []
    prev = history[-1] if history else None

    # [1] 리더 교체 — 3일 연속 유지돼야 유효 (노이즈 제거)
    cur_leader = narratives[0]["code"] if narratives else None
    if cur_leader and prev:
        prev_leader = (prev.get("narratives") or [{}])[0].get("code")
        if prev_leader and prev_leader != cur_leader:
            streak = 1
            for h in reversed(history):
                lead = (h.get("narratives") or [{}])[0].get("code")
                if lead == prev_leader:
                    streak += 1
                else:
                    break
            ev.append({
                "kind": "LEADER_SHIFT",
                "level": "high" if streak >= TH_RANK_PERSIST else "watch",
                "text": f"1위 내러티브 교체: {_nm(prev_leader, narratives)} → {narratives[0]['name']}"
                        f" (직전 리더 {streak}일 유지)",
            })

    # [2] 폭 확대 — 상승이 소수 종목이 아니라 섹터 전반으로 번지는지
    if len(history) >= 4:
        for n in narratives:
            past = []
            for h in history[-7:]:
                for x in h.get("narratives", []):
                    if x["code"] == n["code"] and x.get("breadth") is not None:
                        past.append(x["breadth"])
            if len(past) >= 3 and n["breadth"] is not None:
                avg = sum(past) / len(past)
                if n["breadth"] - avg >= TH_BREADTH_JUMP:
                    ev.append({
                        "kind": "BREADTH_EXPANSION",
                        "level": "high",
                        "text": f"{n['name']} 폭 확대: {avg:.0f}% → {n['breadth']:.0f}%"
                                f" (구성종목 중 BTC 초과 비율)",
                    })

    # [3] 개별 종목 회전율 급증 — 서사 유입의 1차 흔적
    hot = [r for r in rows if r["zturn"] >= TH_TURNOVER_Z]
    for r in sorted(hot, key=lambda r: -r["zturn"])[:3]:
        ev.append({
            "kind": "TURNOVER_SPIKE",
            "level": "watch",
            "text": f"{r['symbol']} 거래회전율 이상치 (z={r['zturn']:.1f}, "
                    f"{_nm(r['narrative'], narratives)})",
        })

    # [4] BTC 도미넌스 저점 갱신 — 알트 로테이션 개시 여부
    dom = (glob.get("market_cap_percentage") or {}).get("btc")
    if dom is not None and len(history) >= 5:
        past = [h.get("btc_dominance") for h in history[-TH_DOM_LOOKBACK:]
                if h.get("btc_dominance") is not None]
        if past and dom < min(past):
            ev.append({
                "kind": "DOMINANCE_BREAK",
                "level": "high",
                "text": f"BTC 도미넌스 {len(past)}일 최저 갱신 ({dom:.2f}%, 직전 최저 {min(past):.2f}%)",
            })
        elif past and dom > max(past):
            ev.append({
                "kind": "DOMINANCE_BID",
                "level": "watch",
                "text": f"BTC 도미넌스 {len(past)}일 최고 ({dom:.2f}%) — 자금이 BTC로 회귀",
            })

    # [5] 순위 대형 이동
    if prev:
        pmap = {x["code"]: x.get("rank") for x in prev.get("narratives", [])}
        for n in narratives:
            p = pmap.get(n["code"])
            if p and abs(p - n["rank"]) >= 3:
                arrow = "▲" if n["rank"] < p else "▼"
                ev.append({
                    "kind": "RANK_MOVE",
                    "level": "watch",
                    "text": f"{n['name']} 순위 {arrow} {p}위 → {n['rank']}위",
                })

    order = {"high": 0, "watch": 1}
    ev.sort(key=lambda e: order.get(e["level"], 9))
    return ev


def _nm(code, narratives):
    for n in narratives:
        if n["code"] == code:
            return n["name"]
    return code or "?"


# ────────────────────────────── 렌더링 ──────────────────────────────

def esc(s) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def fmt_pct(v, digits=1):
    if v is None:
        return "n/a"
    return f"{v:+.{digits}f}%"


def render_telegram(payload: dict) -> str:
    st = payload["data_status"]
    d = payload["as_of_kst"]
    L = [f"<b>🧭 내러티브 레이더</b>  {esc(d)}"]

    if st != "OK":
        L.append("")
        L.append("⚠️ <b>판정 불가</b> — 시세 수집에 실패했습니다.")
        L.append(f"상태: <code>{esc(st)}</code>")
        L.append("데이터를 받지 못한 상태입니다. 정상 관측 결과로 해석하지 마십시오.")
        if payload.get("pages_url"):
            L.append(f"\n{esc(payload['pages_url'])}")
        return "\n".join(L)

    g = payload["market"]
    L.append(f"BTC ${g['btc_price']:,.0f} ({fmt_pct(g['btc_r24'])}) · "
             f"도미넌스 {g['btc_dominance']:.1f}% · 시총 {g['total_mcap_t']:.2f}T")
    L.append(f"국면: <b>{esc(g['regime'])}</b>")

    L.append("\n<b>■ 내러티브 순위</b> <i>(30일 상대강도, BTC 대비 중앙값)</i>")
    medals = ["🥇", "🥈", "🥉"]
    for n in payload["narratives"]:
        badge = medals[n["rank"] - 1] if n["rank"] <= 3 else f"{n['rank']}."
        bre = f"{n['breadth']:.0f}%" if n["breadth"] is not None else "n/a"
        L.append(f"{badge} {esc(n['name'])}  <b>{fmt_pct(n['rs30'])}</b>  "
                 f"<i>폭 {bre} · 7일 {fmt_pct(n['rs7'])}</i>")

    L.append("\n<b>■ 부합도 상위 종목</b> <i>(고정매핑 부합도 × 자금반응)</i>")
    for r in payload["coins"][:8]:
        L.append(f"{r['rank']}. <b>{esc(r['symbol'])}</b> "
                 f"<code>{r['score']:+.2f}</code> · {esc(r['narrative_name'])}")
        L.append(f"    30일 {fmt_pct(r['x30'])} · 7일 {fmt_pct(r['x7'])} · "
                 f"회전 z{r['zturn']:+.1f} · 시총 ${r['mcap'] / 1e6:,.0f}M")

    dv = payload.get("divergence") or []
    if dv:
        L.append("\n<b>■ TVL 괴리</b> <i>(가격 변화 − 예치금 변화, %p)</i>")
        for d in dv[:5]:
            icon = "🔺" if d["div"] > 0 else "🔻"
            L.append(f"{icon} <b>{esc(d['symbol'])}</b> <code>{d['div']:+.0f}%p</code> "
                     f"({esc(d['horizon'])}) · {esc(d['direction'])}")
            mct = f" · MC/TVL {d['mc_tvl']:.2f}" if d.get("mc_tvl") else ""
            L.append(f"    가격 {d['price']:+.0f}% vs TVL {d['tvl_chg']:+.0f}% "
                     f"(${d['tvl_usd'] / 1e6:,.0f}M){mct}")
    elif payload.get("tvl_covered") == 0:
        L.append("\n<b>■ TVL 괴리</b>")
        L.append("· 예치금 데이터를 받지 못했습니다 (괴리 산출 생략)")

    if payload.get("discovery"):
        L += disc.render(payload["discovery"])

    ev = payload["events"]
    L.append("\n<b>■ 내러티브 변화</b>")
    if not ev:
        L.append("· 임계값을 넘는 변화 없음 (관측 정상)")
    else:
        for e in ev[:6]:
            icon = "🔴" if e["level"] == "high" else "🟡"
            L.append(f"{icon} {esc(e['text'])}")

    if payload.get("watch"):
        L.append("\n<b>■ 확인 대기</b>")
        for w in payload["watch"]:
            L.append(f"· {esc(w)}")

    L.append("\n<i>관측 지표입니다. 미래 수익률을 주장하지 않으며 투자 권유가 아닙니다.</i>")
    if payload.get("pages_url"):
        L.append(esc(payload["pages_url"]))
    return "\n".join(L)


def classify_regime(glob: dict, narratives: list, btc: dict) -> str:
    dom = (glob.get("market_cap_percentage") or {}).get("btc")
    if dom is None:
        return "판정 보류"
    lead = narratives[0]["rs30"] if narratives and narratives[0]["rs30"] is not None else 0
    if dom >= 58 and lead <= 5:
        return "BTC 집중 (알트 로테이션 미개시)"
    if dom >= 58 and lead > 5:
        return "BTC 집중 · 선별 회전 시작"
    if dom < 55 and lead > 10:
        return "알트 로테이션 진행"
    return "혼조 (선별 장세)"


def render_dashboard(payload: dict, history: list) -> str:
    st = payload["data_status"]
    nar = payload.get("narratives", [])
    coins = payload.get("coins", [])
    ev = payload.get("events", [])

    def bar(v, vmax):
        if v is None or vmax <= 0:
            return 0
        return clamp(abs(v) / vmax * 100.0, 0, 100)

    vmax = max([abs(n["rs30"]) for n in nar if n["rs30"] is not None] or [1])

    nrow = []
    for n in nar:
        col = "#16a34a" if (n["rs30"] or 0) > 0 else "#dc2626"
        w = bar(n["rs30"], vmax)
        side = "left:50%" if (n["rs30"] or 0) > 0 else f"right:50%"
        nrow.append(f"""
        <div class="nrow">
          <div class="nname"><span class="rk">{n['rank']}</span>{esc(n['name'])}</div>
          <div class="track"><div class="fill" style="{side};width:{w / 2:.1f}%;background:{col}"></div>
            <div class="axis"></div></div>
          <div class="nval" style="color:{col}">{fmt_pct(n['rs30'])}</div>
          <div class="nsub">폭 {('%.0f%%' % n['breadth']) if n['breadth'] is not None else 'n/a'}
            · 7d {fmt_pct(n['rs7'])} · {n['n']}종목</div>
          <div class="thesis">{esc(n['thesis'])}</div>
        </div>""")

    crow = []
    for r in coins[:15]:
        col = "#16a34a" if r["score"] > 0 else "#dc2626"
        crow.append(f"""
        <tr><td class="rk2">{r['rank']}</td>
        <td><b>{esc(r['symbol'])}</b><div class="role">{esc(r['role'])}</div></td>
        <td class="nm">{esc(r['narrative_name'])}</td>
        <td style="color:{col};font-weight:700">{r['score']:+.2f}</td>
        <td>{fmt_pct(r['x30'])}</td><td>{fmt_pct(r['x7'])}</td>
        <td>{r['zturn']:+.1f}</td>
        <td>${r['mcap'] / 1e6:,.0f}M</td></tr>""")

    erow = "".join(
        f'<li class="{e["level"]}">{esc(e["text"])}</li>' for e in ev
    ) or "<li class='none'>임계값을 넘는 변화 없음</li>"

    # 내러티브 순위 추이 (최근 20일, 순수 SVG)
    spark = ""
    if len(history) >= 3:
        codes = [n["code"] for n in nar]
        H = history[-20:]
        W, HT, PAD = 640, 200, 28
        n_codes = len(codes)
        lines = []
        palette = ["#2563eb", "#16a34a", "#d97706", "#dc2626", "#7c3aed", "#0891b2", "#64748b"]
        for i, c in enumerate(codes):
            pts = []
            for j, h in enumerate(H):
                rk = next((x.get("rank") for x in h.get("narratives", []) if x["code"] == c), None)
                if rk is None:
                    continue
                x = PAD + (W - 2 * PAD) * (j / max(1, len(H) - 1))
                y = PAD + (HT - 2 * PAD) * ((rk - 1) / max(1, n_codes - 1))
                pts.append(f"{x:.1f},{y:.1f}")
            if len(pts) >= 2:
                lines.append(f'<polyline fill="none" stroke="{palette[i % len(palette)]}" '
                             f'stroke-width="2" points="{" ".join(pts)}"/>')
        legend = " ".join(
            f'<span class="lg"><i style="background:{palette[i % len(palette)]}"></i>'
            f'{esc(_nm(c, nar))}</span>' for i, c in enumerate(codes))
        spark = f"""
        <h2>내러티브 순위 추이 <small>최근 {len(H)}일 · 위가 1위</small></h2>
        <svg viewBox="0 0 {W} {HT}" class="sp">{''.join(lines)}</svg>
        <div class="legend">{legend}</div>"""

    dsc = payload.get("discovery") or {}
    lag = dsc.get("lagging") or []
    if lag:
        lrows = []
        for x in lag:
            lrows.append(f"""
            <tr><td><b>{esc(x['symbol'])}</b><div class="role">{esc(x.get('source', ''))}</div></td>
            <td style="color:#2563eb;font-weight:700">{x['div']:+.0f}%p</td>
            <td>{esc(x['horizon'])}</td>
            <td style="color:#16a34a">{x['tvl_chg']:+.0f}%</td><td>{x['price_chg']:+.0f}%</td>
            <td>${x['tvl'] / 1e6:,.0f}M</td>
            <td>{x['mc_tvl'] if x['mc_tvl'] is not None else 'n/a'}</td>
            <td>{x['fdv_tvl'] if x.get('fdv_tvl') else 'n/a'}</td>
            <td class="nm">{esc(x.get('status', ''))}</td></tr>""")
        hotrows = "".join(
            f"<li class='watch'>{esc(h['symbol'])} {h['div']:+.0f}%p — "
            f"TVL {h['tvl_chg']:+.0f}% vs 가격 {h['price_chg']:+.0f}%</li>"
            for h in (dsc.get("hot") or [])[:5])
        discsec = f"""
<h2>괴리 발굴 <small>전체 시장 스캔 {dsc.get('scanned', 0)}개 · 예치금은 느는데 가격이 안 따라온 종목</small></h2>
<div class="tblwrap"><table>
<tr><th>종목</th><th>괴리</th><th>기간</th><th>TVL</th><th>가격</th><th>TVL 규모</th><th>MC/TVL</th><th>FDV/TVL</th><th>상태</th></tr>
{''.join(lrows)}</table></div>
<div class="note">고정 유니버스와 무관하게 DefiLlama 전체에서 gecko_id가 붙은 체인·프로토콜을 훑습니다.
필터: TVL 증가 ≥20%(30일)/10%(7일), 괴리 ≤ -15%p, MC/TVL ≤ 1.5, 30일 가격 ≤ +50%, 거래대금 ≥ $1M,
30일 증가분의 60% 이상이 하루에 발생한 종목은 제외(고래·인센티브 개시 배제).
<b>MC/TVL은 유통량이 적은 신규 토큰에서 착시를 줍니다</b> — FDV/TVL을 함께 보세요.
하루 반짝 등장은 알림으로 승격하지 않고 2일 이상 유지된 것만 알립니다.</div>
{('<h2>과열 주의 <small>예치금은 빠지는데 가격만 오른 종목</small></h2><ul class="ev">' + hotrows + '</ul>') if hotrows else ''}"""
    else:
        discsec = ""

    dv = payload.get("divergence") or []
    if dv:
        drows = []
        for d in dv[:12]:
            col = "#dc2626" if d["div"] > 0 else "#2563eb"
            drows.append(f"""
            <tr><td><b>{esc(d['symbol'])}</b><div class="role">{esc(d['narrative_name'])}</div></td>
            <td style="color:{col};font-weight:700">{d['div']:+.0f}%p</td>
            <td>{esc(d['horizon'])}</td>
            <td>{d['price']:+.0f}%</td><td>{d['tvl_chg']:+.0f}%</td>
            <td>${d['tvl_usd'] / 1e6:,.0f}M</td>
            <td>{('%.2f' % d['mc_tvl']) if d.get('mc_tvl') else 'n/a'}</td>
            <td class="nm">{esc(d['direction'])}</td></tr>""")
        divsec = f"""
<h2>TVL 괴리 <small>가격 변화 − 예치금 변화 · 절대값 큰 순</small></h2>
<div class="tblwrap"><table>
<tr><th>종목</th><th>괴리</th><th>기간</th><th>가격</th><th>TVL</th><th>TVL 규모</th><th>MC/TVL</th><th>방향</th></tr>
{''.join(drows)}</table></div>
<div class="note">TVL은 펀더멘털이 아니라 예치금입니다. 인센티브 파밍으로 부풀 수 있고 중복 계상도 있습니다.
프라이버시·DePIN·오라클처럼 TVL 개념이 무의미한 종목은 애초에 계산하지 않습니다(표에 없음).
괴리는 판정이 아니라 "가격과 예치금이 다른 말을 하고 있다"는 관측입니다.</div>"""
    else:
        divsec = ""

    status_banner = "" if st == "OK" else f"""
      <div class="alert">⚠️ 데이터 상태 <b>{esc(st)}</b> — 시세 수집 실패.
      정상 관측 결과로 해석하지 마십시오.</div>"""

    m = payload.get("market", {})
    head = ""
    if st == "OK":
        head = f"""<div class="kpis">
          <div class="kpi"><span>BTC</span><b>${m['btc_price']:,.0f}</b><i>{fmt_pct(m['btc_r24'])}</i></div>
          <div class="kpi"><span>도미넌스</span><b>{m['btc_dominance']:.1f}%</b><i>BTC 점유</i></div>
          <div class="kpi"><span>총 시총</span><b>{m['total_mcap_t']:.2f}T</b><i>USD</i></div>
          <div class="kpi wide"><span>국면</span><b>{esc(m['regime'])}</b><i>도미넌스 × 선두 상대강도</i></div>
        </div>"""

    return f"""<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Narrative Radar</title>
<style>
*{{box-sizing:border-box}}
body{{margin:0;background:#0b1020;color:#e5e9f5;
font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Noto Sans KR',sans-serif;
font-size:15px;line-height:1.55}}
.wrap{{max-width:900px;margin:0 auto;padding:20px 16px 60px}}
h1{{font-size:22px;margin:0 0 4px}} .sub{{color:#8b93ab;font-size:13px;margin-bottom:18px}}
h2{{font-size:16px;margin:28px 0 12px;padding-bottom:7px;border-bottom:1px solid #1e2740}}
h2 small{{color:#8b93ab;font-weight:400;font-size:12px;margin-left:6px}}
.alert{{background:#3b1111;border:1px solid #7f1d1d;padding:12px;border-radius:8px;margin:14px 0}}
.kpis{{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin-bottom:8px}}
.kpi{{background:#121a30;border:1px solid #1e2740;border-radius:10px;padding:11px 13px}}
.kpi.wide{{grid-column:span 2}}
.kpi span{{display:block;color:#8b93ab;font-size:11px;letter-spacing:.4px}}
.kpi b{{display:block;font-size:19px;margin:2px 0}} .kpi i{{font-size:11px;font-style:normal;color:#7d86a0}}
.nrow{{background:#121a30;border:1px solid #1e2740;border-radius:10px;padding:12px 14px;margin-bottom:9px}}
.nname{{font-weight:700;font-size:15px}} .rk{{display:inline-block;width:22px;height:22px;line-height:22px;
text-align:center;background:#1e2740;border-radius:6px;font-size:12px;margin-right:8px}}
.track{{position:relative;height:8px;background:#0b1020;border-radius:4px;margin:9px 0 6px}}
.fill{{position:absolute;top:0;height:8px;border-radius:4px}}
.axis{{position:absolute;left:50%;top:-2px;width:1px;height:12px;background:#39415c}}
.nval{{font-weight:700;font-size:15px;display:inline-block}}
.nsub{{color:#8b93ab;font-size:12px;display:inline-block;margin-left:10px}}
.thesis{{color:#7d86a0;font-size:12px;margin-top:7px}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
th,td{{padding:8px 6px;border-bottom:1px solid #1e2740;text-align:right}}
th:nth-child(2),td:nth-child(2),th:nth-child(3),td:nth-child(3){{text-align:left}}
th{{color:#8b93ab;font-weight:500;font-size:11px}}
.rk2{{color:#8b93ab;width:26px;text-align:center!important}}
.role{{color:#7d86a0;font-size:11px;font-weight:400}}
.nm{{color:#9aa3bb;font-size:12px}}
ul.ev{{list-style:none;padding:0;margin:0}}
ul.ev li{{padding:9px 12px;border-radius:8px;margin-bottom:7px;background:#121a30;border-left:3px solid #39415c}}
ul.ev li.high{{border-left-color:#dc2626}} ul.ev li.watch{{border-left-color:#d97706}}
ul.ev li.none{{color:#7d86a0}}
.sp{{width:100%;background:#121a30;border:1px solid #1e2740;border-radius:10px}}
.legend{{margin-top:8px;font-size:11px;color:#8b93ab}}
.lg{{margin-right:12px;white-space:nowrap}} .lg i{{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:4px}}
.foot{{margin-top:34px;color:#6d7590;font-size:11px;line-height:1.7}}
.tblwrap{{overflow-x:auto}}
.note{{color:#7d86a0;font-size:11px;margin-top:9px;line-height:1.65}}
</style></head><body><div class="wrap">
<h1>🧭 Narrative Radar</h1>
<div class="sub">{esc(payload['as_of_kst'])} · 고정매핑 {payload.get('n_coins', 0)}종목 / {len(nar)}개 내러티브</div>
{status_banner}{head}
<h2>내러티브 순위 <small>30일 상대강도 · BTC 대비 구성종목 중앙값</small></h2>
{''.join(nrow)}
<h2>부합도 상위 종목 <small>고정 부합도 × 자금 반응 (관측치)</small></h2>
<div class="tblwrap"><table>
<tr><th></th><th>종목</th><th>내러티브</th><th>점수</th><th>30d</th><th>7d</th><th>회전z</th><th>시총</th></tr>
{''.join(crow)}</table></div>
{divsec}
{discsec}
<h2>내러티브 변화 <small>임계값 기반 관측</small></h2>
<ul class="ev">{erow}</ul>
{spark}
<div class="foot">
<b>이 대시보드가 하는 일:</b> 고정된 내러티브-종목 매핑에 대해 BTC 대비 초과수익 중앙값, 구성종목 확산 폭,
거래회전율을 매일 측정하고 그 순위 변화를 기록합니다.<br>
<b>하지 않는 일:</b> 미래 수익률 예측. 이 지표들은 사전 검증된 예측력이 없으며,
"지금 자금이 어디에 반응하는가"에 대한 서술일 뿐입니다.<br>
섹터 대표값은 평균이 아닌 중앙값입니다(소수 극단치 지배 방지). 구성종목은 성과를 보고 교체하지 않습니다(사후선택 편향 방지).<br>
데이터: CoinGecko 공개 API · 투자 권유가 아닙니다.
</div>
</div></body></html>"""


# ────────────────────────────── 메인 ──────────────────────────────

def main(argv):
    target = argv[1] if len(argv) > 1 and argv[1] else ""
    if target and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", target):
        print(f"[error] date 형식 오류: {target}")
        return 1

    cfg = load_config()
    universe = load_json(os.path.join(BASE_DIR, "universe.json"), None)
    if not universe:
        print("[error] universe.json 없음")
        return 1

    ids = [m["id"] for n in universe["narratives"].values() for m in n["members"]]
    ids += [b["id"] for b in universe["benchmarks"]]

    now = datetime.now(KST)
    as_of = target or now.strftime("%Y-%m-%d")

    mk = fetch_markets(sorted(set(ids)))
    glob = fetch_global()

    coverage = len(mk) / len(set(ids)) if ids else 0
    if not mk or "bitcoin" not in mk:
        status = "UNAVAILABLE"
    elif coverage < 0.7:
        status = "DEGRADED"
    else:
        status = "OK"
    print(f"[collect] {len(mk)}/{len(set(ids))} 종목 (coverage {coverage:.0%}) → {status}")

    history = load_json(HISTORY_PATH, [])

    payload = {
        "as_of": as_of,
        "as_of_kst": now.strftime("%Y-%m-%d %H:%M KST"),
        "data_status": status,
        "coverage": round(coverage, 3),
        "pages_url": cfg.get("pages_url", ""),
        "universe_frozen_at": universe.get("frozen_at"),
    }

    if status == "UNAVAILABLE":
        payload.update({"narratives": [], "coins": [], "events": [], "market": {}, "n_coins": 0})
        payload["message"] = render_telegram(payload)
        save_json(LATEST_PATH, payload)
        write_dashboard(payload, history)
        send(cfg, payload["message"])
        return 1  # 워크플로우가 실패로 인지하도록

    rows, btc = build_coin_rows(universe, mk)
    rows = score_coins(rows, btc)
    narratives = aggregate_narratives(universe, rows)
    for r in rows:
        r["narrative_name"] = _nm(r["narrative"], narratives)

    # TVL 괴리 — 실패해도 레이더 본체는 계속 간다
    try:
        tvl_map = tvl.collect(universe)
        tvl.backfill_30d(tvl_map, history)
        tvl.attach(rows, tvl_map)
        div_rows = tvl.rank_divergence(rows)
    except Exception as e:  # noqa: BLE001
        print(f"[llama] TVL 단계 실패 — 괴리 없이 진행: {e}")
        tvl_map, div_rows = {}, []
    payload["divergence"] = div_rows
    payload["tvl_covered"] = len(tvl_map)

    # 전체 시장 스캔 — 고정 유니버스 밖의 '예치금 선행' 후보 발굴
    disc_state = load_json(DISCOVERY_PATH, {})
    try:
        cand = disc.collect_candidates()
        prices = disc.fetch_prices(sorted(cand.keys()), fetch_markets)
        hist_tvl = {}
        target = (now - timedelta(days=30)).strftime("%Y-%m-%d")
        for h in history:
            if h.get("as_of", "") <= target:
                hist_tvl = h.get("discovery_tvl") or {}
        found = disc.screen(cand, prices, hist_tvl)
        disc_state = disc.track(found, disc_state, as_of)
        payload["discovery"] = found
    except Exception as e:  # noqa: BLE001
        print(f"[discovery] 스캔 실패 — 생략: {e}")
        payload["discovery"] = {}
        cand = {}

    dom = (glob.get("market_cap_percentage") or {}).get("btc")
    total = (glob.get("total_market_cap") or {}).get("usd")
    payload["market"] = {
        "btc_price": btc.get("current_price"),
        "btc_r24": btc.get("price_change_percentage_24h_in_currency"),
        "btc_r30": btc.get("price_change_percentage_30d_in_currency"),
        "btc_dominance": dom if dom is not None else 0.0,
        "total_mcap_t": (total / 1e12) if total else 0.0,
        "regime": classify_regime(glob, narratives, btc),
    }
    payload["narratives"] = narratives
    payload["coins"] = rows
    payload["n_coins"] = len(rows)
    payload["events"] = (detect_changes(narratives, rows, glob, history)
                         + tvl.events(div_rows) + disc.events(payload.get("discovery") or {}))
    payload["watch"] = [
        "SEC innovation exemption 확정 시 RWA_EQUITY 재평가",
        "EU AMLR(2027.7) 관련 상장 공지 시 PRIVACY_PQ 하방",
        "BTC 도미넌스 55% 하향 이탈 시 로테이션 확인",
    ]
    payload["message"] = render_telegram(payload)

    # 이력 누적 — 같은 날짜는 덮어쓴다 (재실행 멱등)
    snap = {
        "as_of": as_of,
        "btc_dominance": dom,
        "btc_r30": btc.get("price_change_percentage_30d_in_currency"),
        "narratives": [{"code": n["code"], "rank": n["rank"], "rs30": n["rs30"],
                        "breadth": n["breadth"]} for n in narratives],
        "tvl": {cid: round(rec["tvl"], 2) for cid, rec in tvl_map.items()},
        "discovery_tvl": {g: round(r["tvl"], 2) for g, r in (cand or {}).items()},
    }
    history = [h for h in history if h.get("as_of") != as_of] + [snap]
    history.sort(key=lambda h: h.get("as_of", ""))
    history = history[-400:]

    if load_json(HISTORY_PATH, []) != history:
        save_json(HISTORY_PATH, history)
    if load_json(DISCOVERY_PATH, {}) != disc_state:
        save_json(DISCOVERY_PATH, disc_state)

    prev_latest = load_json(LATEST_PATH, {})
    if _comparable(prev_latest) != _comparable(payload):
        save_json(LATEST_PATH, payload)
    else:
        print("[idempotent] latest.json 내용 동일 — 쓰기 생략")

    write_dashboard(payload, history)
    send(cfg, payload["message"])
    return 0


def _comparable(p: dict) -> str:
    q = {k: v for k, v in (p or {}).items() if k not in ("as_of_kst", "message")}
    return json.dumps(q, sort_keys=True, ensure_ascii=False)


def write_dashboard(payload: dict, history: list) -> None:
    out_dir = os.environ.get("DASHBOARD_DIR", "")
    if not out_dir:
        out_dir = os.path.abspath(os.path.join(BASE_DIR, "..", "docs", "narrative-radar"))
    os.makedirs(out_dir, exist_ok=True)
    html = render_dashboard(payload, history)
    path = os.path.join(out_dir, "index.html")
    old = ""
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            old = f.read()
    # 타임스탬프 줄만 다른 경우도 갱신은 하되, 완전 동일하면 생략
    if old == html:
        print("[idempotent] dashboard 동일 — 쓰기 생략")
        return
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(html)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    print(f"[dashboard] {path}")


def send(cfg: dict, msg: str) -> None:
    token, chat = cfg.get("telegram_token"), cfg.get("telegram_chat_id")
    if not token or not chat:
        print("[telegram] 자격증명 없음 - 발송 생략 (릴레이 모드)")
        return
    body = urllib.parse.urlencode({
        "chat_id": chat, "text": msg, "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=body)
    with urllib.request.urlopen(req, timeout=25) as r:
        print(f"[telegram] 발송 완료 (HTTP {r.status})")


if __name__ == "__main__":
    sys.exit(main(sys.argv))
