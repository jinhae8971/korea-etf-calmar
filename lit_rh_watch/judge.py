"""lit-rh-watch 판정 엔진 — 관측값을 이벤트로 바꾸고 등급을 매긴다.

순수 함수만 둔다(네트워크 없음). 그래서 tests/에서 전부 오프라인 검증된다.

등급
  WATCH     약신호. 대시보드에만 남긴다.
  ALERT     강신호 1종. 즉시 발송.
  IMMINENT  서로 다른 신호군(S1 온체인·S2 공식·S3 UI/API) 2종 이상이 7일 안에 ALERT,
            또는 계정 청구 API(S3b)에 배정이 뜬 경우.
S4(뉴스·X·Polymarket)는 WATCH까지만 올린다 — 등급을 끌어올리지 않는다.
"""
from __future__ import annotations

import datetime as dt
import difflib
import hashlib
import re
import unicodedata

KST = dt.timezone(dt.timedelta(hours=9))
RANK = {"NONE": 0, "WATCH": 1, "ALERT": 2, "IMMINENT": 3}
BADGE = {"IMMINENT": "🔴", "ALERT": "🟡", "WATCH": "", "NONE": ""}
FAMILY_KO = {"S1": "온체인", "S2": "공식", "S3": "UI·API", "S4": "보조"}

KW = re.compile(
    r"(convert|conversion|ratio|per[ -]point|claim|snapshot|end date|ends? on|ended|final (week|drop|distribution)"
    r"|season ?3|distribut|airdrop|allocation|LIT per|redeem|vesting|unlock|program (end|conclu)|wind[- ]down)",
    re.I)
KW_ANN = re.compile(r"(point|\bLIT\b|airdrop|claim|season|convert|reward|distribut|snapshot|allocation|incentive)",
                    re.I)
KW_FE = re.compile(r"(claim|airdrop|reward|convert|distribut|vesting|season|allocation|redeem)", re.I)


def eid(*parts) -> str:
    return hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:12]


def event(family, signal, grade, title, detail="", refute="", ts=0, ref=None, key=None):
    return {"id": eid(family, signal, key if key is not None else title), "ts": int(ts),
            "family": family, "signal": signal, "grade": grade, "title": title,
            "detail": detail, "refute": refute, "refuted": False, "ref": ref or {}}


def fmt_m(x: float) -> str:
    return f"{x / 1e6:.2f}M" if abs(x) >= 1e6 else f"{x / 1e3:.0f}K"


# ───────────────────────── S1 온체인 ─────────────────────────
def classify(addr: str, meta: dict | None, cfg: dict) -> str:
    a = addr.lower()
    if a in cfg["known"]:
        return cfg["known"][a]
    if not meta:
        return "unknown"
    blob = " ".join([meta.get("name") or ""] + list(meta.get("tags") or [])).lower()
    if any(k in blob for k in cfg["exclude_name_keywords"]):
        return "excluded"
    if meta.get("is_contract"):
        return "contract"
    return "eoa"


def in_pool_band(amount: float, cfg: dict) -> bool:
    pool, pct = cfg["pool_lit"], cfg["pool_band_pct"] / 100
    return pool * (1 - pct) <= amount <= pool * (1 + pct)


def detect_eth(transfers: list[dict], metas: dict, cfg: dict, now: int, hist: list[dict]) -> list[dict]:
    """transfers: 이번 실행 신규 대량 이동. hist: 최근 7일 대량 이동(누적 판정용, 이번 것 포함)."""
    evs = []
    fresh_sec = cfg["fresh_contract_days"] * 86400
    for t in transfers:
        cf, ct = classify(t["from"], metas.get(t["from"]), cfg), classify(t["to"], metas.get(t["to"]), cfg)
        if cf in ("burn", "excluded") or ct in ("burn", "excluded", "zero"):
            continue
        if cf == "lighter_core":  # 롤업 출금 — 사용자 인출
            continue
        ts = t.get("ts") or now
        key = f"{t['tx']}:{t.get('log_index', 0)}"
        refute = f"{cfg['refute_hours']}시간 내 수신처→송신처 원위치 시 해제"
        ref = {"tx": t["tx"], "from": t["from"], "to": t["to"], "amount": t["amount"], "chain": "eth"}
        tmeta = metas.get(t["to"]) or {}
        fresh = bool(tmeta.get("is_contract") and tmeta.get("created_ts")
                     and ts - tmeta["created_ts"] <= fresh_sec)
        if in_pool_band(t["amount"], cfg):
            evs.append(event("S1", "eth_pool_band", "ALERT", f"ETH LIT {fmt_m(t['amount'])} 단건 이동(풀 규모)",
                             f"{t['from'][:10]}→{t['to'][:10]}", refute, ts, ref, key))
        elif fresh:
            evs.append(event("S1", "eth_fresh_contract", "ALERT",
                             f"ETH LIT {fmt_m(t['amount'])} → 신규 컨트랙트",
                             f"생성 {int((ts - tmeta['created_ts']) / 86400)}일 전 컨트랙트", refute, ts, ref, key))
        elif ct == "lighter_core":
            evs.append(event("S1", "eth_core_deposit", "WATCH", f"ETH LIT {fmt_m(t['amount'])} 라이터 코어 입금",
                             f"{t['from'][:10]} 발신", refute, ts, ref, key))
        else:
            evs.append(event("S1", "eth_large", "WATCH", f"ETH LIT {fmt_m(t['amount'])} 대량 이동",
                             f"{t['from'][:10]}→{t['to'][:10]}", refute, ts, ref, key))
    # 같은 송신처 7일 누적이 풀 규모에 들어오면 ALERT
    by_src: dict[str, float] = {}
    for t in hist:
        if classify(t["from"], metas.get(t["from"]), cfg) in ("burn", "excluded", "lighter_core", "zero"):
            continue
        if classify(t["to"], metas.get(t["to"]), cfg) in ("burn", "excluded"):
            continue
        by_src[t["from"]] = by_src.get(t["from"], 0) + t["amount"]
    for src, tot in by_src.items():
        n = sum(1 for t in hist if t["from"] == src)
        if n >= 2 and in_pool_band(tot, cfg):
            wk = dt.datetime.fromtimestamp(now, KST).strftime("%G-%V")
            evs.append(event("S1", "eth_pool_cumulative", "ALERT",
                             f"ETH 동일 송신처 7일 누적 {fmt_m(tot)}", f"{src[:10]} · {n}건",
                             "누적분이 원 지갑으로 복귀 시 해제", now, {"from": src, "total": tot}, f"{src}:{wk}"))
    return evs


def detect_rh_supply(hist: list[list], cfg: dict, now: int) -> list[dict]:
    """hist: [[ts, supply], ...] 최근 14일. 7일 최저 대비 증가분으로 판정."""
    if len(hist) < 2:
        return []
    cur_ts, cur = hist[-1]
    base = min(s for ts, s in hist if ts >= cur_ts - 7 * 86400)
    delta = cur - base
    if delta >= cfg["rh_supply_strong_lit"]:
        g, tag = "ALERT", "풀 규모"
    elif delta >= cfg["rh_supply_alert_lit"]:
        g, tag = "ALERT", "대량"
    else:
        return []
    day = dt.datetime.fromtimestamp(now, KST).strftime("%Y-%m-%d")
    return [event("S1", "rh_supply", g, f"RH체인 LIT 공급 +{fmt_m(delta)} ({tag})",
                  f"7일 최저 {fmt_m(base)} → {fmt_m(cur)}", "공급이 7일 최저 수준으로 복귀 시 해제",
                  now, {"base": base, "cur": cur, "delta": delta}, f"{tag}:{day}")]


def detect_rh_transfers(transfers: list[dict], cfg: dict, now: int) -> list[dict]:
    evs = []
    for t in transfers:
        mint = t["from"] == "0x0000000000000000000000000000000000000000"
        evs.append(event("S1", "rh_mint" if mint else "rh_large", "WATCH",
                         f"RH LIT {fmt_m(t['amount'])} {'브리지 유입' if mint else '대량 이동'}",
                         f"→{t['to'][:10]}", "", t.get("ts") or now,
                         {"tx": t["tx"], "from": t["from"], "to": t["to"], "amount": t["amount"], "chain": "rh"},
                         f"{t['tx']}:{t.get('log_index', 0)}"))
    return evs


def apply_refutes(events: list[dict], transfers: list[dict], cfg: dict) -> int:
    """이번에 관측한 이동 중 기존 S1 이동 이벤트의 역방향(≥90%)이 있으면 해제."""
    n = 0
    lim = cfg["refute_hours"] * 3600
    for e in events:
        r = e.get("ref") or {}
        if e["family"] != "S1" or e["refuted"] or "to" not in r or "from" not in r:
            continue
        for t in transfers:
            if (t["from"] == r["to"] and t["to"] == r["from"] and t["amount"] >= 0.9 * r["amount"]
                    and 0 <= (t.get("ts") or e["ts"]) - e["ts"] <= lim):
                e["refuted"] = True
                n += 1
    return n


# ───────────────────────── S2 공식 ─────────────────────────
def doc_added_lines(old: str, new: str) -> list[str]:
    return [ln[1:].strip() for ln in difflib.unified_diff(old.split("\n"), new.split("\n"), lineterm="", n=0)
            if ln.startswith("+") and not ln.startswith("+++") and ln[1:].strip()]


def detect_doc(url: str, old: str | None, new: str, now: int) -> list[dict]:
    if old is None or old == new:
        return []
    added = doc_added_lines(old, new)
    hits = [ln for ln in added if KW.search(ln)]
    page = url.rstrip("/").split("/")[-1].replace(".md", "")
    if hits:
        return [event("S2", "doc_keyword", "ALERT", f"문서 변경: {page}", hits[0][:120],
                      "문구가 일정·비율이 아닌 일반 약관 정정이면 해제", now,
                      {"url": url, "added": hits[:5]}, f"{url}:{hash_text(new)}")]
    return [event("S2", "doc_change", "WATCH", f"문서 문구 변경: {page}",
                  (added[0][:120] if added else "삭제·서식 변경"), "", now,
                  {"url": url, "added": added[:5]}, f"{url}:{hash_text(new)}")]


def hash_text(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()[:12]


def ann_key(a: dict) -> str:
    return f"{a['created_at']}:{a['title']}"


def detect_announcements(items: list[dict], seen: list[str], now: int) -> list[dict]:
    evs = []
    for a in items:
        k = ann_key(a)
        if k in seen:
            continue
        text = f"{a['title']} {a['content']}"
        if KW_ANN.search(text):
            evs.append(event("S2", "announcement", "ALERT", f"라이터 RH 공지: {a['title'][:40]}",
                             a["content"].replace("\n", " ")[:140], "상장·점검 등 무관한 공지로 확인되면 해제",
                             a["created_at"] or now, {"title": a["title"]}, k))
    return evs


def detect_leaderboard(hist: list[list], cfg: dict, now: int) -> list[dict]:
    """hist: [[ts, top10_sum, top1], ...]. 적립 정지·상위권 삭감·주간 둔화."""
    evs = []
    if len(hist) < 2:
        return evs
    cur_ts, cur_sum, _ = hist[-1]
    prev_ts, prev_sum, _ = hist[-2]
    day = dt.datetime.fromtimestamp(now, KST).strftime("%Y-%m-%d")
    # 1) 상위권 합계 감소 — 시빌·워시 포인트 정리(작년 TGE 직전 선례)
    if prev_sum > 0 and (prev_sum - cur_sum) / prev_sum * 100 >= cfg["lb_drop_pct"]:
        evs.append(event("S2", "lb_slash", "ALERT", f"상위10 포인트 합계 {((cur_sum / prev_sum) - 1) * 100:.1f}%",
                         f"{prev_sum:,.0f} → {cur_sum:,.0f} · 시빌 정리 추정",
                         "다음 주간 드롭 후 합계가 원래 추세로 복귀하면 해제", now,
                         {"prev": prev_sum, "cur": cur_sum}, f"slash:{day}"))
    # 2) 적립 정지 — freeze_hours 이상 합계가 그대로
    lim = cur_ts - cfg["lb_freeze_hours"] * 3600
    window = [h for h in hist if h[0] >= lim]
    older = [h for h in hist if h[0] < lim]
    if older and window and all(abs(h[1] - cur_sum) < 1e-6 for h in window + [older[-1]]):
        evs.append(event("S2", "lb_frozen", "ALERT", f"포인트 적립 {cfg['lb_freeze_hours']}시간+ 정지",
                         "상위10 합계 변화 없음 · 프로그램 종료 가능성", "합계가 다시 증가하면 해제", now,
                         {"sum": cur_sum}, f"frozen:{day}"))
    # 3) 주간 증가분 둔화 — 3주 이상 이력이 있을 때만
    if hist[-1][0] - hist[0][0] >= 21 * 86400:
        def at(t):
            c = [h for h in hist if h[0] <= t]
            return c[-1][1] if c else None
        w0, w1, w2 = at(cur_ts), at(cur_ts - 7 * 86400), at(cur_ts - 14 * 86400)
        if None not in (w0, w1, w2) and (w1 - w2) > 0:
            ratio = (w0 - w1) / (w1 - w2)
            if 0 <= ratio < 0.5:
                wk = dt.datetime.fromtimestamp(now, KST).strftime("%G-%V")
                evs.append(event("S2", "lb_slowdown", "WATCH", f"상위권 주간 적립 {ratio * 100:.0f}% 수준으로 둔화",
                                 "주간 드롭 축소 가능성", "", now, {"ratio": ratio}, f"slow:{wk}"))
    return evs


# ───────────────────────── S3 프론트엔드 ─────────────────────────
def detect_frontend(old: dict | None, new: dict, now: int) -> list[dict]:
    if not old:
        return []
    evs = []
    add_c = sorted(set(new["chunks"]) - set(old["chunks"]))
    add_p = sorted(set(new["api_paths"]) - set(old["api_paths"]))
    for name in add_c + add_p:
        hot = bool(KW_FE.search(name))
        evs.append(event("S3", "fe_" + ("path" if name.startswith("/") else "chunk"),
                         "ALERT" if hot else "WATCH",
                         f"{'API 경로' if name.startswith('/') else '화면 모듈'} 추가: {name[:40]}",
                         "청구·보상 관련 명칭" if hot else "", "배포 후 7일 내 미사용·롤백 시 해제" if hot else "",
                         now, {"name": name}, name))
    return evs


# ───────────────────────── S4 보조 ─────────────────────────
def detect_news(items: list[dict], seen: list[str], now: int, source: str) -> list[dict]:
    evs = []
    for it in items:
        k = hash_text(it.get("link") or it.get("title", ""))
        if k in seen or not KW.search(it.get("title", "")):
            continue
        if not re.search(r"lighter|\bLIT\b", it.get("title", ""), re.I):
            continue
        evs.append(event("S4", source, "WATCH", f"{'뉴스' if source == 'news' else 'X'}: {it['title'][:60]}",
                         it.get("link", "")[:140], "", now, {"link": it.get("link")}, k))
    return evs


def detect_polymarket(items: list[dict], seen: list[str], now: int) -> list[dict]:
    evs = []
    for m in items:
        if m["slug"] in seen:
            continue
        if not re.search(r"airdrop|robinhood|point|season|claim|convert", m["title"] + m["slug"], re.I):
            continue
        odds = f" · Yes {m['yes_pct']}%" if m.get("yes_pct") is not None else ""
        evs.append(event("S4", "polymarket", "WATCH", f"Polymarket 신규: {m['title'][:50]}", f"{m['slug']}{odds}",
                         "", now, m, m["slug"]))
    return evs


# ───────────────────────── 등급 ─────────────────────────
def active(events: list[dict], now: int, window_days: int) -> list[dict]:
    lim = now - window_days * 86400
    return [e for e in events if not e["refuted"] and e["ts"] >= lim]


def level_of(events: list[dict]) -> tuple[str, list[str]]:
    if any(e["grade"] == "IMMINENT" for e in events):
        return "IMMINENT", sorted({e["family"] for e in events if RANK[e["grade"]] >= 2})
    fams = sorted({e["family"] for e in events if e["grade"] == "ALERT" and e["family"] != "S4"})
    if len(fams) >= 2:
        return "IMMINENT", fams
    if fams:
        return "ALERT", fams
    if events:
        return "WATCH", []
    return "NONE", []


# ───────────────────────── 렌더 ─────────────────────────
def cell_width(s: str) -> int:
    w = 0
    for ch in s:
        if unicodedata.east_asian_width(ch) in ("W", "F") or ord(ch) > 0x2500:
            w += 2
        else:
            w += 1
    return w


def clip(s: str, width: int = 40) -> str:
    if cell_width(s) <= width:
        return s
    out = ""
    for ch in s:
        if cell_width(out + ch) > width - 2:
            break
        out += ch
    return out + "…"


def render(level: str, fams: list[str], events: list[dict], now: int, cfg: dict,
           status_note: str = "") -> str:
    t = dt.datetime.fromtimestamp(now, KST).strftime("%m-%d %H:%M KST")
    badge = BADGE.get(level, "")
    head = f"{badge} " if badge else ""
    lines = [clip(f"{head}라이터RH 전환감시 · {level}"), t, ""]
    if level == "IMMINENT":
        lines.append(clip(f"판정: 신호 {len(fams)}종 동시 ({'·'.join(FAMILY_KO[f] for f in fams)})"))
    elif level == "ALERT":
        lines.append(clip(f"판정: {FAMILY_KO[fams[0]]} 강신호"))
    strong = sorted([e for e in events if RANK[e["grade"]] >= 2], key=lambda e: -e["ts"])[:4]
    for e in strong:
        lines.append(clip(f"- {FAMILY_KO[e['family']]}: {e['title']}"))
        if e.get("detail"):
            lines.append(clip(f"  {e['detail']}"))
    refs = [e["refute"] for e in strong if e.get("refute")]
    if refs:
        lines += ["", clip(f"반증: {refs[0]}")]
    if status_note:
        lines += ["", clip(status_note)]
    lines += ["", cfg["dashboard_url"]]
    return "\n".join(lines)


def render_heartbeat(now: int, cfg: dict, level: str, n_ok: int, n_all: int) -> str:
    t = dt.datetime.fromtimestamp(now, KST).strftime("%m-%d %H:%M KST")
    return "\n".join([
        "라이터RH 전환감시 · 월간 생존신고", t, "",
        f"현재 등급: {level}", f"원천 정상: {n_ok}/{n_all}", "",
        cfg["dashboard_url"],
    ])
