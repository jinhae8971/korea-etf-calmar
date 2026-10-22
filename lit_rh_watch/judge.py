"""lit-rh-watch 판정 엔진 — 관측값을 이벤트로 바꾸고 등급을 매긴다.

순수 함수만 둔다(네트워크 없음). 그래서 tests/에서 전부 오프라인 검증된다.

등급 (대시보드·메시지 표기)
  NONE      조용함
  WATCH     관찰 — 약신호. 대시보드에만 남긴다.
  ALERT     경보 — 강신호 1종. 낮 시간 즉시 발송.
  IMMINENT  임박 — 서로 다른 신호군(S1 온체인·S2 공식·S3 앱) 2종 이상이 7일 안에 ALERT,
            또는 계정 청구 API(S3b)에 배정이 뜬 경우.
S4(뉴스·X·Polymarket)는 WATCH까지만 올린다 — 등급을 끌어올리지 않는다.

메시지는 텔레그램 HTML(parse_mode=HTML)로 만든다 — 바깥 글자는 전부 escape 한다.
"""
from __future__ import annotations

import datetime as dt
import difflib
import hashlib
import html
import re

KST = dt.timezone(dt.timedelta(hours=9))
RANK = {"NONE": 0, "WATCH": 1, "ALERT": 2, "IMMINENT": 3}
LEVEL_KO = {"NONE": "조용함", "WATCH": "관찰", "ALERT": "경보", "IMMINENT": "임박"}
BADGE = {"IMMINENT": "🔴", "ALERT": "🟡", "WATCH": "", "NONE": ""}
FAMILY_KO = {"S1": "온체인", "S2": "공식 문서·공지", "S3": "앱 화면·API", "S4": "보조"}
WEEKDAY_KO = "월화수목금토일"

# 단어 경계(\b)를 둔다 — 2026-10-22 'recommended' 속 'ended', 'integration' 속 'ratio'를 잡은 오탐 뒤로.
KW = re.compile(
    r"\b(?:convert(?:s|ed|ing)?|conversions?|ratios?|per[ -]points?|claim(?:s|ed|ing|able)?|snapshots?"
    r"|end date|ends? on|ended|final (?:week|drop|distribution)|season ?3|distribut\w*|airdrops?"
    r"|allocat\w*|LIT per|redeem\w*|redemptions?|vest(?:ing|ed)?|unlock\w*|program (?:end|conclu)\w*"
    r"|wind[- ]down)\b",
    re.I)
# 예전 규칙(경계 없음) — 재검토 때 '왜 잘못 걸렸는지' 설명하는 데만 쓴다.
KW_LEGACY = re.compile(
    r"(convert|conversion|ratio|per[ -]point|claim|snapshot|end date|ends? on|ended|final (week|drop|distribution)"
    r"|season ?3|distribut|airdrop|allocation|LIT per|redeem|vesting|unlock|program (end|conclu)|wind[- ]down)",
    re.I)
KW_ANN = re.compile(
    r"\b(?:points?|LIT|airdrops?|claim\w*|seasons?|convert\w*|conversions?|rewards?|distribut\w*|snapshots?"
    r"|allocat\w*|incentives?)\b",
    re.I)
# 프론트 모듈명은 camelCase(ClaimRewards)라 단어 경계를 쓰지 않는다.
KW_FE = re.compile(r"(claim|airdrop|reward|convert|distribut|vesting|season|allocation|redeem)", re.I)

# 신호별 '의미' — 메시지의 의미 칸에 쓴다.
MEANING = {
    "eth_pool_band": "에어드랍 물량(1,100만 LIT)을 배포용 지갑으로 옮기는 단계일 수 있어요.",
    "eth_pool_cumulative": "에어드랍 물량을 여러 번에 나눠 옮기는 모습일 수 있어요.",
    "eth_fresh_contract": "청구(claim) 컨트랙트에 물량을 채우는 모습일 수 있어요.",
    "rh_supply": "전환할 LIT가 로빈후드 체인으로 넘어오는 모습일 수 있어요.",
    "doc_keyword": "전환 일정·비율이 공식 문서에 들어가기 시작했다는 뜻일 수 있어요.",
    "index_keyword": "전환·청구 안내 문서가 새로 생겼을 수 있어요.",
    "announcement": "앱 공지에 포인트·전환 관련 내용이 올라왔어요.",
    "lb_slash": "시빌·워시 포인트를 정리한 것으로 보여요. 지난번 LIT 출시 직전에도 같은 일이 있었어요.",
    "lb_frozen": "포인트 프로그램이 끝났을 수 있어요.",
    "fe_path": "앱이 청구·보상 기능을 준비하는 모습일 수 있어요.",
    "fe_chunk": "앱이 청구·보상 화면을 준비하는 모습일 수 있어요.",
}


def eid(*parts) -> str:
    return hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:12]


def event(family, signal, grade, title, detail="", refute="", ts=0, ref=None, key=None):
    return {"id": eid(family, signal, key if key is not None else title), "ts": int(ts),
            "family": family, "signal": signal, "grade": grade, "title": title,
            "detail": detail, "refute": refute, "refuted": False, "ref": ref or {}}


def lift(e: dict, reason: str, kind: str, now: int) -> None:
    """신호를 푼다. kind: data(해제 조건 충족) | false_positive(오탐으로 판명)."""
    e["refuted"] = True
    e["refute_reason"] = reason
    e["refute_kind"] = kind
    e["refuted_at"] = int(now)


def fmt_m(x: float) -> str:
    return f"{x / 1e6:.2f}M" if abs(x) >= 1e6 else f"{x / 1e3:.0f}K"


def hash_text(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()[:12]


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
        refute = f"{cfg['refute_hours']}시간 안에 받은 지갑에서 보낸 지갑으로 되돌아오면 해제"
        ref = {"tx": t["tx"], "from": t["from"], "to": t["to"], "amount": t["amount"], "chain": "eth"}
        tmeta = metas.get(t["to"]) or {}
        fresh = bool(tmeta.get("is_contract") and tmeta.get("created_ts")
                     and ts - tmeta["created_ts"] <= fresh_sec)
        route = f"{t['from'][:10]} → {t['to'][:10]}"
        if in_pool_band(t["amount"], cfg):
            evs.append(event("S1", "eth_pool_band", "ALERT",
                             f"이더리움 LIT {fmt_m(t['amount'])}개 한 번에 이동 (포인트 풀 규모)",
                             route, refute, ts, ref, key))
        elif fresh:
            evs.append(event("S1", "eth_fresh_contract", "ALERT",
                             f"이더리움 LIT {fmt_m(t['amount'])}개 새 컨트랙트로 이동",
                             f"만든 지 {int((ts - tmeta['created_ts']) / 86400)}일 된 컨트랙트 · {route}",
                             refute, ts, ref, key))
        elif ct == "lighter_core":
            evs.append(event("S1", "eth_core_deposit", "WATCH",
                             f"이더리움 LIT {fmt_m(t['amount'])}개 라이터 코어 입금", f"{t['from'][:10]} 발신",
                             refute, ts, ref, key))
        else:
            evs.append(event("S1", "eth_large", "WATCH", f"이더리움 LIT {fmt_m(t['amount'])}개 대량 이동",
                             route, refute, ts, ref, key))
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
                             f"같은 지갑에서 7일간 LIT {fmt_m(tot)}개 출금 (포인트 풀 규모)", f"{src[:10]} · {n}건",
                             "누적분이 원래 지갑으로 돌아오면 해제", now, {"from": src, "total": tot}, f"{src}:{wk}"))
    return evs


def detect_rh_supply(hist: list[list], cfg: dict, now: int) -> list[dict]:
    """hist: [[ts, supply], ...] 최근 14일. 7일 최저 대비 증가분으로 판정."""
    if len(hist) < 2:
        return []
    cur_ts, cur = hist[-1]
    base = min(s for ts, s in hist if ts >= cur_ts - 7 * 86400)
    delta = cur - base
    if delta >= cfg["rh_supply_strong_lit"]:
        g, tag = "ALERT", "포인트 풀 규모"
    elif delta >= cfg["rh_supply_alert_lit"]:
        g, tag = "ALERT", "대량"
    else:
        return []
    day = dt.datetime.fromtimestamp(now, KST).strftime("%Y-%m-%d")
    return [event("S1", "rh_supply", g, f"로빈후드 체인 LIT 공급 +{fmt_m(delta)} ({tag})",
                  f"7일 최저 {fmt_m(base)} → 지금 {fmt_m(cur)}", "공급이 7일 최저 수준으로 돌아오면 해제",
                  now, {"base": base, "cur": cur, "delta": delta}, f"{tag}:{day}")]


def detect_rh_transfers(transfers: list[dict], cfg: dict, now: int) -> list[dict]:
    evs = []
    for t in transfers:
        mint = t["from"] == "0x0000000000000000000000000000000000000000"
        evs.append(event("S1", "rh_mint" if mint else "rh_large", "WATCH",
                         f"로빈후드 체인 LIT {fmt_m(t['amount'])}개 {'브리지 유입' if mint else '대량 이동'}",
                         f"→ {t['to'][:10]}", "", t.get("ts") or now,
                         {"tx": t["tx"], "from": t["from"], "to": t["to"], "amount": t["amount"], "chain": "rh"},
                         f"{t['tx']}:{t.get('log_index', 0)}"))
    return evs


def apply_refutes(events: list[dict], transfers: list[dict], cfg: dict, now: int = 0) -> int:
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
                lift(e, f"이동한 LIT가 {cfg['refute_hours']}시간 안에 원래 지갑으로 돌아왔어요", "data",
                     now or t.get("ts") or e["ts"])
                n += 1
                break
    return n


# ───────────────────────── S2 공식 문서 ─────────────────────────
def doc_key(url: str) -> str:
    """문서 이름표 키 — 마지막 경로(.md 제외). readme·index는 상위 폴더를 붙인다."""
    segs = [s for s in url.split("?")[0].split("/") if s]
    last = segs[-1] if segs else url
    last = last[:-3] if last.endswith(".md") else last
    if last in ("readme", "index") and len(segs) > 1:
        return f"{segs[-2]}/{last}"
    return last


def doc_name(url: str, cfg: dict | None) -> str:
    return ((cfg or {}).get("doc_names") or {}).get(doc_key(url)) or doc_key(url)


def short_path(url: str) -> str:
    p = re.sub(r"^https?://[^/]+/", "", url)
    return p[:-3] if p.endswith(".md") else p


def web_url(url: str) -> str:
    """docs의 .md 주소 → 사람이 읽는 페이지 주소."""
    return url[:-3] if url.endswith(".md") else url


def kw_hits(line: str) -> list[str]:
    return [m.group(0) for m in KW.finditer(line)]


def doc_diff(old: str, new: str) -> tuple[list[str], list[str]]:
    """(추가된 줄, 삭제된 줄). diff 머리글(---/+++)과 내용 줄을 섞지 않는다."""
    added, removed, body = [], [], False
    for ln in difflib.unified_diff(old.split("\n"), new.split("\n"), lineterm="", n=0):
        if ln.startswith("@@"):
            body = True
            continue
        if not body or not ln:
            continue
        s = ln[1:].strip()
        if not s:
            continue
        if ln[0] == "+":
            added.append(s)
        elif ln[0] == "-":
            removed.append(s)
    return added, removed


def doc_added_lines(old: str, new: str) -> list[str]:
    return doc_diff(old, new)[0]


def minor_edit(line: str, removed: list[str]) -> bool:
    """원래 있던 문장을 조금 고친 것인가 — 같은 키워드가 비슷한 옛 문장에도 있었으면 새 신호가 아니다."""
    hits = {h.lower() for h in kw_hits(line)}
    for r in removed:
        if difflib.SequenceMatcher(None, line, r).ratio() >= 0.85 and hits <= {h.lower() for h in kw_hits(r)}:
            return True
    return False


def plain_md(line: str) -> str:
    """마크다운 줄을 읽기 쉬운 문장으로 — 제목 기호·글머리표·링크·강조 제거."""
    s = re.sub(r"^\s*(#{1,6}\s+|[-*+]\s+|\d+\.\s+|>\s*)", "", line)
    s = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", s)
    s = re.sub(r"[*_`]{1,3}", "", s).replace("\\", "")
    return re.sub(r"\s+", " ", s).strip()


def detect_doc(url: str, old: str | None, new: str, now: int, name: str | None = None) -> list[dict]:
    if old is None or old == new:
        return []
    if url.endswith("llms.txt"):
        return detect_index(url, old, new, now, name)
    name = name or doc_key(url)
    added, removed = doc_diff(old, new)
    hits = [ln for ln in added if KW.search(ln) and not minor_edit(ln, removed)]
    key = f"{url}:{hash_text(new)}"
    if hits:
        words = sorted({h.lower() for ln in hits for h in kw_hits(ln)})
        return [event("S2", "doc_keyword", "ALERT", f"공식 문서 '{name}'에 전환 관련 문구 추가",
                      plain_md(hits[0])[:280], "일정·비율이 아닌 일반 약관 정정으로 확인되면 해제", now,
                      {"url": url, "name": name, "added": hits[:3], "words": words[:6]}, key)]
    detail = plain_md(added[0])[:280] if added else "문장 삭제·서식 변경"
    return [event("S2", "doc_change", "WATCH", f"공식 문서 '{name}' 문구 변경", detail, "", now,
                  {"url": url, "name": name, "added": added[:5], "removed": len(removed)}, key)]


_IDX = re.compile(r"^\s*[-*]\s*\[(?P<title>[^\]]+)\]\((?P<url>[^)\s]+)\)(?::\s*(?P<desc>.*))?\s*$")


def parse_index(text: str) -> dict[str, tuple[str, str]]:
    """llms.txt 목록 → {주소: (제목, 설명)}."""
    out: dict[str, tuple[str, str]] = {}
    for ln in (text or "").split("\n"):
        m = _IDX.match(ln)
        if m:
            out.setdefault(m.group("url"), (m.group("title").replace("\\", "").strip(),
                                            (m.group("desc") or "").strip()))
    return out


def detect_index(url: str, old: str, new: str, now: int, name: str | None = None) -> list[dict]:
    """문서 전체 목록(llms.txt) — 새로 생긴 페이지만 키워드로 본다. 주소만 옮긴 페이지는 신호가 아니다."""
    name = name or "문서 전체 목록"
    o, n = parse_index(old), parse_index(new)
    gone_by_title: dict[str, list[str]] = {}
    for u in o:
        if u not in n:
            gone_by_title.setdefault(o[u][0], []).append(u)
    moved, fresh = [], []
    for u in n:
        if u in o:
            continue
        if gone_by_title.get(n[u][0]):
            moved.append((gone_by_title[n[u][0]].pop(0), u, n[u][0]))
        else:
            fresh.append(u)
    removed = [u for us in gone_by_title.values() for u in us]
    key = f"{url}:{hash_text(new)}"
    evs = []
    hot = [u for u in fresh if KW.search(f"{n[u][0]} {n[u][1]}") or KW.search(doc_key(u).replace("-", " "))]
    if hot:
        t0, d0 = n[hot[0]]
        evs.append(event("S2", "index_keyword", "ALERT", f"공식 문서에 새 페이지 '{t0}'",
                         (d0 or short_path(hot[0]))[:200], "전환·청구와 무관한 문서로 확인되면 해제", now,
                         {"url": hot[0], "name": t0, "added": [f"{n[u][0]} — {u}" for u in hot[:3]]}, key + ":hot"))
    rest = [u for u in fresh if u not in hot]
    if rest or moved or removed:
        parts = ([f"새 문서 {len(rest)}개"] if rest else []) + ([f"주소 이전 {len(moved)}개"] if moved else []) \
            + ([f"삭제 {len(removed)}개"] if removed else [])
        sample = [n[u][0] for u in rest[:3]] or [t for _, _, t in moved[:3]] or [o[u][0] for u in removed[:3]]
        evs.append(event("S2", "index_change", "WATCH", f"공식 문서 목록 변경: {' · '.join(parts)}",
                         ", ".join(sample)[:200], "", now,
                         {"url": url, "name": name, "new": [f"{n[u][0]} — {u}" for u in rest[:10]],
                          "moved": [f"{t}: {short_path(a)} → {short_path(b)}" for a, b, t in moved[:10]],
                          "removed": [f"{o[u][0]} — {u}" for u in removed[:10]]}, key))
    if not evs:  # 목록 밖 문구(머리말 등)만 바뀜
        added, _ = doc_diff(old, new)
        evs.append(event("S2", "doc_change", "WATCH", f"공식 문서 '{name}' 문구 변경",
                         plain_md(added[0])[:200] if added else "문장 삭제·서식 변경", "", now,
                         {"url": url, "name": name}, key))
    return evs


def first_heading(text: str | None) -> str:
    for ln in (text or "").split("\n"):
        if ln.startswith("# "):
            return ln[2:].strip()
    return ""


def resolve_move(url: str, index: dict, base_text: str | None = None) -> str | None:
    """'페이지 없음'이 된 문서의 새 주소를 문서 전체 목록에서 찾는다 — 같은 파일 이름, 없으면 같은 제목."""
    key = doc_key(url)
    cands = [u for u in index if doc_key(u) == key and u != url]
    if len(cands) == 1:
        return cands[0]
    title = first_heading(base_text)
    if title:
        cands = [u for u, (t, _) in index.items() if t.strip().lower() == title.lower() and u != url]
        if len(cands) == 1:
            return cands[0]
    return None


def doc_moved_event(old_url: str, new_url: str, name: str, same: bool, now: int) -> dict:
    return event("S2", "doc_moved", "WATCH",
                 f"공식 문서 '{name}' 주소 이전" + (" (내용 그대로)" if same else " (내용도 바뀜)"),
                 f"{short_path(old_url)} → {short_path(new_url)}", "", now,
                 {"url": new_url, "from": old_url, "name": name, "same": same}, f"{old_url}->{new_url}")


def doc_missing_event(url: str, name: str, now: int) -> dict:
    return event("S2", "doc_missing", "WATCH", f"공식 문서 '{name}'를 찾을 수 없음",
                 f"{short_path(url)} 가 '페이지 없음' — 새 주소를 문서 목록에서 찾지 못함 (이전 내용 기준으로 계속 비교)",
                 "", now, {"url": url, "name": name}, f"missing:{url}")


def legacy_why(lines: list[str]) -> str:
    """예전 규칙이 단어 일부에 걸린 경우를 사람 말로 — 예: 'recommended' 속 'ended'."""
    for ln in lines:
        for m in KW_LEGACY.finditer(ln):
            a, b = m.start(), m.end()
            while a > 0 and ln[a - 1].isalnum():
                a -= 1
            while b < len(ln) and ln[b].isalnum():
                b += 1
            if ln[a:b].lower() != m.group(0).lower():
                return f"'{ln[a:b]}' 속 '{m.group(0)}'"
    return ""


def rejudge(events: list[dict], doc_moves: dict, now: int) -> int:
    """지금 규칙으로 지난 문서 경보를 다시 본다 — 규칙을 고치면 과거 오탐도 자동으로 풀린다."""
    n = 0
    for e in events:
        if e["refuted"] or e["family"] != "S2" or e["signal"] != "doc_keyword" or RANK[e["grade"]] < 2:
            continue
        ref = e.get("ref") or {}
        lines = list(ref.get("added") or [])
        if any(KW.search(x) for x in lines):
            continue
        mv = (doc_moves or {}).get(ref.get("url", ""))
        if mv:
            reason = ("라이터가 문서 주소를 옮겼어요(내용은 그대로)" if mv.get("same")
                      else "라이터가 문서 주소를 옮기면서 내용도 바꿨어요(바뀐 내용은 새 주소에서 따로 판정)") \
                + ". 옛 주소의 '페이지 없음' 안내문을 변경으로 잘못 읽었어요"
        else:
            why = legacy_why(lines)
            reason = (f"{why}처럼 단어 일부만 맞은 키워드 오탐이에요" if why
                      else "다시 보니 전환·일정 관련 문구가 아니에요")
        lift(e, reason, "false_positive", now)
        n += 1
    return n


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
            evs.append(event("S2", "announcement", "ALERT", f"라이터 RH 앱 공지: {a['title'][:60]}",
                             a["content"].replace("\n", " ")[:240], "상장·점검 등 전환과 무관한 공지로 확인되면 해제",
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
        evs.append(event("S2", "lb_slash", "ALERT",
                         f"포인트 상위 10명 합계 {((cur_sum / prev_sum) - 1) * 100:.1f}% 감소",
                         f"{prev_sum:,.0f} → {cur_sum:,.0f}",
                         "다음 주간 배분 뒤 합계가 원래 추세로 돌아오면 해제", now,
                         {"prev": prev_sum, "cur": cur_sum}, f"slash:{day}"))
    # 2) 적립 정지 — freeze_hours 이상 합계가 그대로
    lim = cur_ts - cfg["lb_freeze_hours"] * 3600
    window = [h for h in hist if h[0] >= lim]
    older = [h for h in hist if h[0] < lim]
    if older and window and all(abs(h[1] - cur_sum) < 1e-6 for h in window + [older[-1]]):
        evs.append(event("S2", "lb_frozen", "ALERT", f"포인트 적립이 {cfg['lb_freeze_hours']}시간 넘게 멈춤",
                         "상위 10명 합계 변화 없음", "합계가 다시 늘면 해제", now,
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
                evs.append(event("S2", "lb_slowdown", "WATCH", f"상위권 주간 적립이 지난주의 {ratio * 100:.0f}%로 둔화",
                                 "주간 배분 축소 가능성", "", now, {"ratio": ratio}, f"slow:{wk}"))
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
        path = name.startswith("/")
        evs.append(event("S3", "fe_" + ("path" if path else "chunk"),
                         "ALERT" if hot else "WATCH",
                         f"앱에 새 {'API 경로' if path else '화면 모듈'}: {name[:60]}",
                         "청구·보상 관련 이름" if hot else "", "배포 후 7일 안에 쓰이지 않거나 롤백되면 해제" if hot else "",
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
        evs.append(event("S4", source, "WATCH", f"{'뉴스' if source == 'news' else 'X'}: {it['title'][:90]}",
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
        evs.append(event("S4", "polymarket", "WATCH", f"Polymarket 신규: {m['title'][:70]}", f"{m['slug']}{odds}",
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


# ───────────────────────── 렌더 (텔레그램 HTML) ─────────────────────────
def esc(s) -> str:
    return html.escape(str(s), quote=False)


def link(url: str, text: str) -> str:
    return f'<a href="{html.escape(url, quote=True)}">{esc(text)}</a>'


def when(ts: int) -> str:
    d = dt.datetime.fromtimestamp(ts, KST)
    return f"{d:%m/%d}({WEEKDAY_KO[d.weekday()]}) {d:%H:%M}"


VERDICT = {"NONE": "전환 신호 없음",
           "WATCH": "경보 신호는 없고, 약한 신호만 대시보드에 남겨요",
           "ALERT": "강한 신호 1종이 남아 있어요",
           "IMMINENT": "서로 다른 강한 신호가 함께 남아 있어요"}


def _quote(e: dict) -> str:
    """문서·공지 신호는 새로 들어간 문장을 그대로 보여 준다."""
    ref = e.get("ref") or {}
    if e["signal"] == "doc_keyword" and ref.get("added"):
        return plain_md(ref["added"][0])[:240]
    if e["signal"] in ("announcement",) and e.get("detail"):
        return e["detail"][:240]
    return ""


def _links(events: list[dict], cfg: dict) -> list[str]:
    out = []
    doc = next(((e.get("ref") or {}).get("url") for e in events
                if e["family"] == "S2" and str((e.get("ref") or {}).get("url", "")).startswith("http")), None)
    if doc:
        out.append(link(web_url(doc), "문서 원문"))
    tx = next((e.get("ref") for e in events if (e.get("ref") or {}).get("tx")), None)
    if tx:
        base = cfg.get("rh_blockscout") if tx.get("chain") == "rh" else cfg.get("eth_blockscout")
        if base:
            out.append(link(f"{base}/tx/{tx['tx']}", "거래 내역"))
    out.append(link(cfg["dashboard_url"], "대시보드"))
    return out


# 같은 상태가 날마다 다시 잡히는 신호 — 메시지에는 최신 값 하나만, 시각은 처음 감지한 때로.
STATE_SIGNALS = {"rh_supply", "lb_frozen", "lb_slash", "eth_pool_cumulative", "lb_slowdown"}


def collapse(events: list[dict]) -> list[tuple[int, dict]]:
    """[(처음 감지 시각, 최신 이벤트)] — 처음 감지 순."""
    latest: dict = {}
    first: dict = {}
    for e in sorted(events, key=lambda e: e["ts"]):
        k = e["signal"] if e["signal"] in STATE_SIGNALS else (e["signal"], e["title"], e.get("detail"))
        first.setdefault(k, e["ts"])
        latest[k] = e
    return sorted(((first[k], e) for k, e in latest.items()), key=lambda x: x[0])


def render(level: str, fams: list[str], events: list[dict], now: int, cfg: dict,
           status_note: str = "") -> str:
    """경보·임박 메시지 — 무슨 일 → 의미 → 해제 조건 → 링크."""
    items = collapse([e for e in events if RANK[e["grade"]] >= 2]) or collapse(events)
    items = items[-4:]
    strong = [e for _, e in items]
    badge = BADGE.get(level, "")
    lines = [f"{badge + ' ' if badge else ''}<b>라이터RH 전환 감시 · {LEVEL_KO[level]}</b>"]
    t0, t1 = (items[0][0], items[-1][0]) if items else (now, now)
    d0, d1 = dt.datetime.fromtimestamp(t0, KST), dt.datetime.fromtimestamp(t1, KST)
    if when(t0) == when(t1):
        span = when(t0)
    else:
        span = f"{when(t0)} ~ " + (f"{d1:%H:%M}" if d0.date() == d1.date() else when(t1))
    kinds = " · ".join(FAMILY_KO[f] for f in fams) if fams else "신호"
    lines.append(f"{span} 감지 · {kinds}")
    lines += ["", "<b>무슨 일</b>"]
    for e in strong:
        lines.append(("· " if len(strong) > 1 else "") + esc(e["title"]))
        q = _quote(e)
        if q:
            lines.append(f"  <i>“{esc(q)}”</i>")
            words = (e.get("ref") or {}).get("words")
            if words:
                lines.append(f"  잡힌 단어: {esc(' · '.join(words[:5]))}")
        elif e.get("detail"):
            lines.append(f"  {esc(e['detail'])}")
    lines += ["", "<b>의미</b>"]
    if level == "IMMINENT":
        lines.append(f"서로 다른 종류의 신호 {len(fams)}개가 7일 안에 함께 나왔어요. "
                     "포인트→LIT 전환이 가까웠을 가능성이 높아요.")
    seen: list[str] = []
    for e in strong:
        m = MEANING.get(e["signal"])
        if m and m not in seen:
            seen.append(m)
    lines += seen[:3]
    if level == "ALERT":
        lines.append("다른 종류 신호(온체인·공식 문서·앱 중)가 7일 안에 하나 더 나오면 '임박'으로 올려요.")
    refs: list[str] = []
    for e in strong:
        if e.get("refute") and e["refute"] not in refs:
            refs.append(e["refute"])
    if refs:
        lines += ["", "<b>해제 조건</b>"] + [esc(r) for r in refs[:3]]
    if status_note:
        lines += ["", esc(status_note)]
    lines += ["", " · ".join(_links(strong, cfg))]
    return "\n".join(lines)


def lifted_title(e: dict, cfg: dict) -> str:
    """해제 메시지의 한 줄 이름 — 문서 신호는 문서 이름, 나머지는 제목."""
    ref = e.get("ref") or {}
    if e["family"] == "S2" and e["signal"].startswith("doc") and ref.get("url"):
        return ref.get("name") or doc_name(ref["url"], cfg)
    return e["title"]


def render_release(lifted: list[dict], level: str, notified: str, notified_at: int | None, now: int,
                   cfg: dict, moves: dict | None = None) -> str:
    """보낸 경보가 풀렸을 때 — 오탐이면 '정정', 해제 조건 충족이면 '해제'.

    moves: 풀린 신호와 관련해 옮겨진 문서 {옛 주소: {to, same, name}} — 새 주소 링크를 붙인다."""
    fp = all(e.get("refute_kind") == "false_positive" for e in lifted)
    head = "정정" if fp else "해제"
    sent = when(notified_at) if notified_at else "앞서 보낸"
    was = LEVEL_KO.get(notified, notified)
    if fp and RANK[level] < 2:
        first = f"{sent} '{was}' 알림은 오탐이었어요. 포인트→LIT 전환 관련 변화는 없어요."
    elif fp:
        first = f"{sent} '{was}' 알림 중 일부 신호는 오탐이었어요."
    else:
        first = f"{sent} '{was}' 알림의 신호가 풀렸어요."
    lines = [f"<b>라이터RH 전환 감시 · {head}</b>", first]
    lines += ["", "<b>무슨 일이었나</b>" if fp else "<b>풀린 신호</b>"]
    for e in lifted[:5]:
        lines.append(f"· {esc(lifted_title(e, cfg))} — {esc(e.get('refute_reason') or '해제 조건을 충족했어요')}")
    lines += ["", "<b>지금</b>", f"{LEVEL_KO[level]} — {VERDICT[level]}"]
    if moves:
        targets = []
        for m in moves.values():
            to = m.get("to", "")
            if to and to not in [t for t, _ in targets]:
                targets.append((to, doc_name(to, cfg)))
        lines.append("옮겨진 문서는 새 주소로 계속 감시해요: "
                     + " · ".join(link(web_url(t), n) for t, n in targets[:3]))
    lines += ["", link(cfg["dashboard_url"], "대시보드")]
    return "\n".join(lines)


def render_heartbeat(now: int, cfg: dict, level: str, n_ok: int, n_all: int) -> str:
    return "\n".join([
        "<b>라이터RH 전환 감시 · 월간 점검</b>", when(now), "",
        f"지금 등급: {LEVEL_KO[level]} — {VERDICT[level]}",
        f"수집 원천 정상 {n_ok}/{n_all}", "",
        link(cfg["dashboard_url"], "대시보드"),
    ])
