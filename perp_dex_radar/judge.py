"""perp-dex-radar 판정 엔진 — 거래소별 관측값을 이벤트로 바꾸고 등급을 매긴다.

순수 함수만 둔다(네트워크 없음). 그래서 tests/에서 전부 오프라인 검증된다.

등급 (거래소마다 따로 — 대시보드·메시지 표기)
  NONE      조용함
  WATCH     관찰 — 약신호. 대시보드에만 남긴다.
  ALERT     경보 — 강신호. 낮 시간에 바로 보낸다.
  IMMINENT  임박 — 서로 다른 근거(온체인·공식 문서·공식 발표·리더보드·앱) 2가지 이상이 7일 안에 경보이거나,
            공식 발표에 TGE·청구와 날짜가 함께 나온 경우.
S4(뉴스·Polymarket)는 관찰까지만 — 등급을 끌어올리지 않는다.

메시지는 텔레그램 HTML(parse_mode=HTML)로 만든다 — 바깥에서 온 글자는 전부 escape 한다.
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
FAMILY_KO = {"S1": "온체인", "S2": "공식", "S3": "앱", "S4": "보조"}
GROUP_KO = {"onchain": "온체인", "docs": "공식 문서", "post": "공식 발표", "board": "리더보드", "app": "앱",
            "aux": "뉴스·예측시장"}
WEEKDAY_KO = "월화수목금토일"
NAME = "퍼프덱스 레이더"

# ───────────────────────── 키워드 (3단) ─────────────────────────
# 2026-10-22 'recommended' 속 'ended'·'integration' 속 'ratio', 2026-10-24 토큰 언급 없는 'distributed'
# (포인트 배분 설명) 오탐 뒤로 — 단어 경계 + 문맥을 함께 본다.
# ① 그 자체로 신호
STRONG = re.compile(
    r"\b(?:airdrops?|TGE|token generation(?: event)?|tokenomics|eligibility check(?:er)?"
    r"|points? (?:will |to |are to )?(?:be )?conver\w*|conver\w* (?:of |your |all )?points?"
    r"|claims? (?:opens?|is (?:now )?(?:live|open)|are (?:now )?(?:live|open)|goes? live|portal|page|window)"
    r"|launch(?:ed|es|ing)? (?:of )?(?:the |our |its )?(?:\$?[A-Z][A-Za-z0-9]{1,9} )?token)\b",
    re.I)
# ② 토큰 문맥(토큰·에어드랍·TGE·티커)이 같은 문장에 있을 때만 신호
CONTEXT = re.compile(
    r"\b(?:distribut\w*|allocat\w*|rewards?|ratios?|per[ -]points?|claim\w*|snapshots?|vest(?:ing|ed)?"
    r"|unlock\w*|redeem\w*|redemptions?|conver(?:t|ts|ted|ting|sion|sions)|launch\w*|listing|listed)\b",
    re.I)
# ③ 포인트·시즌·프로그램 문맥에서 '끝났다'
ENDING = re.compile(
    r"\b(?:ended|ends on|end date|will end|(?:has |have |was |is )?concluded|conclusion|wind[- ]down"
    r"|final (?:week(?:ly)?|drop|distribution|epoch|season))\b",
    re.I)
POINTS_CTX = re.compile(r"\b(?:points?|seasons?|programs?|epochs?|campaigns?|incentives?)\b", re.I)
TOKEN_WORDS = r"tokens?|airdrops?|TGE|tokenomics|token generation"
# 토큰 문맥 — 'API token'·'stock tokens'(로빈후드 체인 주식 토큰) 같은 말은 빼고 본다.
TOKEN_CTX = re.compile(
    r"(?<!api )(?<!access )(?<!auth )(?<!bearer )(?<!session )(?<!refresh )(?<!stock )(?<!jwt )(?<!security )"
    r"\btokens?\b|\b(?:airdrops?|TGE|tokenomics|token generation)\b",
    re.I)


def token_ctx(line: str, tickers=()) -> bool:
    return bool(TOKEN_CTX.search(line)) or any(re.search(rf"\b{re.escape(t)}\b", line) for t in tickers if t)
# 공식 발표(X·블로그·공지) 경보용 — 더 좁게. 'listing·launch'는 토큰이 아직 없는 곳에만.
TGE_POST = re.compile(
    r"\b(?:TGE|token generation(?: event)?|tokenomics|airdrop (?:checker|claim\w*|is (?:now )?live|allocation\w*|eligib\w*)"
    r"|claims? (?:opens?|is (?:now )?(?:live|open)|are (?:now )?(?:live|open)|goes? live|portal|page|window)"
    r"|eligibility check(?:er)?|snapshot (?:has been |was |will be |is )?taken"
    r"|points? (?:will |to )?(?:be )?conver\w*|conver\w* (?:of |your |all )?points?"
    r"|(?:season|points program|program|campaign|epoch)(?: \d+)? (?:has |is |will )?(?:ended|concluded|is over|end(?:s|ing)? (?:on|at))"
    r"|final (?:week(?:ly)?(?: points)? (?:drop|distribution)|points drop|epoch))\b",
    re.I)
TGE_LAUNCH = re.compile(
    r"\b(?:token (?:is (?:now )?live|launch(?:es|ed|ing)?|listing)|launch(?:ing|es)? (?:our|the|its) token"
    r"|(?:listing|listed|lists) on (?:binance|bybit|okx|coinbase|upbit|bithumb|hyperliquid|kraken|bitget))\b",
    re.I)
MONTHS = r"jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec"
DATE = re.compile(
    rf"\b(?:{MONTHS})[a-z]*\.? \d{{1,2}}(?:st|nd|rd|th)?\b|\b\d{{1,2}}(?:st|nd|rd|th)? (?:of )?(?:{MONTHS})[a-z]*\b"
    r"|\b20\d\d-\d{2}-\d{2}\b|\b(?:0?[1-9]|1[0-2])/(?:0?[1-9]|[12]\d|3[01])\b",
    re.I)
# 예전 규칙(경계 없음) — 재검토 때 '왜 잘못 걸렸는지' 설명하는 데만 쓴다.
KW_LEGACY = re.compile(
    r"(convert|conversion|ratio|per[ -]point|claim|snapshot|end date|ends? on|ended|final (week|drop|distribution)"
    r"|season ?3|distribut|airdrop|allocation|LIT per|redeem|vesting|unlock|program (end|conclu)|wind[- ]down)",
    re.I)
# 프론트 모듈명은 camelCase(ClaimRewards)라 단어 경계를 쓰지 않는다.
KW_FE = re.compile(r"(claim|airdrop|reward|convert|distribut|vesting|season|allocation|redeem)", re.I)


def _uniq(xs):
    out = []
    for x in xs:
        if x.lower() not in [o.lower() for o in out]:
            out.append(x)
    return out


def signal_words(line: str, tickers=(), extra: str | None = None) -> list[str]:
    """한 문장에서 전환·TGE 신호로 볼 단어들. 비면 신호가 아니다.
    extra: 거래소별 추가 규칙(예: 포인트 프로그램이 아직 없는 QFEX는 'points program' 자체가 신호)."""
    words = [m.group(0) for m in STRONG.finditer(line)]
    if extra:
        words += [m.group(0) for m in re.finditer(extra, line, re.I)]
    tok = token_ctx(line, tickers)
    if tok:
        words += [m.group(0) for m in CONTEXT.finditer(line)]
    if tok or POINTS_CTX.search(line):
        words += [m.group(0) for m in ENDING.finditer(line)]
    return _uniq(words)


def post_words(text: str, token_live: bool = False) -> list[str]:
    """공식 발표를 경보로 올릴 단어 — 토큰이 이미 나온 곳은 상장·출시 문구를 뺀다."""
    words = [m.group(0) for m in TGE_POST.finditer(text)]
    if not token_live:
        words += [m.group(0) for m in TGE_LAUNCH.finditer(text)]
    if token_live:
        words = [w for w in words if not re.fullmatch(r"TGE|tokenomics|token generation(?: event)?", w, re.I)]
    return _uniq(words)


def has_date(text: str) -> bool:
    return bool(DATE.search(text or ""))


# 신호별 '의미' — 메시지의 의미 칸에 쓴다.
MEANING = {
    "eth_pool_band": "에어드랍 물량(1,100만 LIT)을 배포용 지갑으로 옮기는 단계일 수 있어요.",
    "eth_pool_cumulative": "에어드랍 물량을 여러 번에 나눠 옮기는 모습일 수 있어요.",
    "eth_fresh_contract": "청구(claim) 컨트랙트에 물량을 채우는 모습일 수 있어요.",
    "rh_supply": "전환할 LIT가 로빈후드 체인으로 넘어오는 모습일 수 있어요.",
    "doc_keyword": "전환·TGE 일정이 공식 문서에 들어가기 시작했다는 뜻일 수 있어요.",
    "index_keyword": "전환·청구 안내 문서가 새로 생겼을 수 있어요.",
    "announcement": "앱 공지에 포인트 전환·에어드랍 관련 내용이 올라왔어요.",
    "x_post": "공식 계정이 토큰·에어드랍 일정을 직접 언급했어요.",
    "telegram_post": "공식 공지 채널에 토큰·에어드랍 관련 글이 올라왔어요.",
    "blog_post": "공식 블로그에 토큰·에어드랍 관련 글이 올라왔어요.",
    "feed_post": "공식 블로그에 토큰·에어드랍 관련 글이 올라왔어요.",
    "ann_post": "앱 공지에 토큰·에어드랍 관련 내용이 올라왔어요.",
    "api_change": "포인트 시스템 값이 바뀌었어요 — 시즌 종료일·포인트 공개·청구 항목은 배분이 가까웠다는 뜻일 수 있어요.",
    "drop_late": "매주 들어오던 포인트 배분이 멈췄어요 — 프로그램 종료나 전환 준비일 수 있어요.",
    "claim_api": "앱에 청구 기능이 실제로 연결되고 있어요 — 확인(checker)·청구 오픈이 가깝다는 뜻이에요.",
    "lb_slash": "시빌·워시 포인트를 정리한 것으로 보여요. 라이터는 LIT 출시 직전에 같은 일이 있었어요.",
    "lb_frozen": "포인트 적립이 멈췄어요 — 프로그램이 끝났을 수 있어요.",
    "fe_path": "앱이 청구·보상 기능을 준비하는 모습일 수 있어요.",
    "fe_chunk": "앱이 청구·보상 화면을 준비하는 모습일 수 있어요.",
}
GROUP_OF_SIGNAL = {
    "eth_pool_band": "onchain", "eth_fresh_contract": "onchain", "eth_core_deposit": "onchain", "eth_large": "onchain",
    "eth_pool_cumulative": "onchain", "rh_supply": "onchain", "rh_mint": "onchain", "rh_large": "onchain",
    "doc_keyword": "docs", "doc_change": "docs", "doc_moved": "docs", "doc_missing": "docs",
    "index_keyword": "docs", "index_change": "docs",
    "announcement": "post", "x_post": "post", "blog_post": "post",
    "lb_slash": "board", "lb_frozen": "board", "lb_slowdown": "board",
    "fe_path": "app", "fe_chunk": "app", "claim_api": "app",
    "news": "aux", "x": "aux", "polymarket": "aux",
}
FAMILY_GROUP = {"S1": "onchain", "S2": "docs", "S3": "app", "S4": "aux"}


def group_of(e: dict) -> str:
    s = e.get("signal", "")
    if s.endswith("_post"):
        return "post"
    if s in ("api_change", "drop_late"):
        return "board"
    return GROUP_OF_SIGNAL.get(s, FAMILY_GROUP.get(e.get("family", ""), "aux"))


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


# ───────────────────────── S1 온체인 (토큰이 나온 곳 — 지금은 라이터 LIT) ─────────────────────────
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
    segs = [s for s in url.split("?")[0].split("#")[0].split("/") if s]
    last = segs[-1] if segs else url
    last = last[:-3] if last.endswith(".md") else last
    if last in ("readme", "index") and len(segs) > 1:
        return f"{segs[-2]}/{last}"
    return last


def doc_name(url: str, names: dict | None) -> str:
    """names: {doc_key: 이름} — 거래소 설정의 docs.names."""
    return (names or {}).get(doc_key(url)) or doc_key(url)


def short_path(url: str) -> str:
    p = re.sub(r"^https?://[^/]+/", "", url)
    return p[:-3] if p.endswith(".md") else p


def web_url(url: str) -> str:
    """docs의 .md 주소 → 사람이 읽는 페이지 주소."""
    return url[:-3] if url.endswith(".md") else url


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


_CHECK = re.compile(r"\[( |x|X)\]")


def minor_edit(line: str, removed: list[str], tickers=(), extra: str | None = None) -> bool:
    """원래 있던 문장을 조금 고친 것인가 — 같은 신호 단어가 비슷한 옛 문장에도 있었으면 새 신호가 아니다.
    단, 체크박스가 바뀌었거나([ ]→[x], 로드맵 '토큰 출시' 완료) 날짜가 새로 들어갔으면 의미 있는 변경이다."""
    hits = {h.lower() for h in signal_words(line, tickers, extra)}
    for r in removed:
        if difflib.SequenceMatcher(None, line, r).ratio() < 0.85:
            continue
        if not hits <= {h.lower() for h in signal_words(r, tickers, extra)}:
            continue
        a, b = _CHECK.search(line), _CHECK.search(r)
        if a and b and a.group(1).lower() != b.group(1).lower():
            return False
        if has_date(line) and not has_date(r):
            return False
        return True
    return False


def tge_line(line: str, tickers=()) -> bool:
    """토큰 출시·청구·TGE 자체를 말하는 문장인가 — 날짜와 함께 나오면 '임박'."""
    if post_words(line):
        return True
    return token_ctx(line, tickers) and bool(re.search(r"\b(?:launch\w*|listing|listed|claim\w*|distribut\w*)\b",
                                                       line, re.I))


def plain_md(line: str) -> str:
    """마크다운 줄을 읽기 쉬운 문장으로 — 제목 기호·글머리표·링크·강조 제거."""
    s = re.sub(r"^\s*(#{1,6}\s+|[-*+]\s+|\d+\.\s+|>\s*)", "", line)
    s = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", s)
    s = re.sub(r"[*_`]{1,3}", "", s).replace("\\", "")
    return re.sub(r"\s+", " ", s).strip()


def detect_doc(url: str, old: str | None, new: str, now: int, name: str | None = None,
               tickers=(), watch_any: bool = False, extra: str | None = None) -> list[dict]:
    if old is None or old == new:
        return []
    if url.endswith("llms.txt"):
        return detect_index(url, old, new, now, name, tickers, extra)
    name = name or doc_key(url)
    added, removed = doc_diff(old, new)
    hits = [ln for ln in added if signal_words(ln, tickers, extra) and not minor_edit(ln, removed, tickers, extra)]
    key = f"{url}:{hash_text(new)}"
    if hits:
        words = _uniq([w for ln in hits for w in signal_words(ln, tickers, extra)])
        dated = any(tge_line(ln, tickers) and has_date(ln) for ln in hits)
        return [event("S2", "doc_keyword", "IMMINENT" if dated else "ALERT",
                      f"공식 문서 '{name}'에 전환·TGE 관련 문구 추가",
                      plain_md(hits[0])[:280], "일정·비율이 아닌 일반 설명·약관 정정으로 확인되면 해제", now,
                      {"url": url, "name": name, "added": hits[:3], "words": words[:6]}, key)]
    if watch_any and added:  # 토큰 FAQ처럼 바뀌기만 해도 의미 있는 문서
        return [event("S2", "doc_keyword", "ALERT", f"공식 문서 '{name}' 답변이 바뀜",
                      plain_md(added[0])[:280], "토큰·일정과 무관한 문구 수정으로 확인되면 해제", now,
                      {"url": url, "name": name, "added": added[:3], "words": [], "watch_any": True}, key)]
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


def detect_index(url: str, old: str, new: str, now: int, name: str | None = None, tickers=(),
                 extra: str | None = None) -> list[dict]:
    """문서 전체 목록(llms.txt) — 새로 생긴 페이지만 본다. 주소만 옮긴 페이지는 신호가 아니다."""
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

    def hot(u):
        t, d = n[u]
        return signal_words(f"{t}. {d}", tickers, extra) or signal_words(doc_key(u).replace("-", " "), tickers, extra)
    hots = [u for u in fresh if hot(u)]
    if hots:
        t0, d0 = n[hots[0]]
        evs.append(event("S2", "index_keyword", "ALERT", f"공식 문서에 새 페이지 '{t0}'",
                         (d0 or short_path(hots[0]))[:200], "전환·청구와 무관한 문서로 확인되면 해제", now,
                         {"url": hots[0], "name": t0, "added": [f"{n[u][0]} — {u}" for u in hots[:3]],
                          "words": _uniq([w for u in hots for w in hot(u)])[:6]}, key + ":hot"))
    rest = [u for u in fresh if u not in hots]
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


def _whole_word_hit(word: str) -> bool:
    return bool(STRONG.fullmatch(word) or CONTEXT.fullmatch(word) or ENDING.fullmatch(word))


def legacy_why(lines: list[str]) -> str:
    """예전 규칙이 단어 일부에 걸린 경우를 사람 말로 — 예: 'recommended' 속 'ended'."""
    for ln in lines:
        for m in KW_LEGACY.finditer(ln):
            a, b = m.start(), m.end()
            while a > 0 and ln[a - 1].isalnum():
                a -= 1
            while b < len(ln) and ln[b].isalnum():
                b += 1
            word = ln[a:b]
            if word.lower() != m.group(0).lower() and not _whole_word_hit(word):
                return f"'{word}' 속 '{m.group(0)}'"
    return ""


def rejudge(events: list[dict], doc_moves: dict, now: int, tickers=(), extra: str | None = None) -> int:
    """지금 규칙으로 지난 문서 경보를 다시 본다 — 규칙을 고치면 과거 오탐도 자동으로 풀린다.
    '바뀌기만 해도 의미 있는 문서'(토큰 FAQ 등)의 경보는 다시 보지 않는다."""
    n = 0
    for e in events:
        if e["refuted"] or e["family"] != "S2" or e["signal"] != "doc_keyword" or RANK[e["grade"]] < 2:
            continue
        ref = e.get("ref") or {}
        if ref.get("watch_any"):
            continue
        lines = list(ref.get("added") or [])
        if any(signal_words(x, tickers, extra) for x in lines):
            continue
        mv = (doc_moves or {}).get(ref.get("url", ""))
        if mv:
            reason = ("라이터가 문서 주소를 옮겼어요(내용은 그대로)" if mv.get("same")
                      else "라이터가 문서 주소를 옮기면서 내용도 바꿨어요(바뀐 내용은 새 주소에서 따로 판정)") \
                + ". 옛 주소의 '페이지 없음' 안내문을 변경으로 잘못 읽었어요"
        else:
            why = legacy_why(lines)
            words = ref.get("words") or []
            if why:
                reason = f"{why}처럼 단어 일부만 맞은 키워드 오탐이에요"
            elif words:
                reason = (f"'{' · '.join(words[:3])}'가 토큰·에어드랍 언급 없이 쓰인 포인트 설명이라 "
                          "전환 신호가 아니에요")
            else:
                reason = "다시 보니 전환·TGE 관련 문구가 아니에요"
        lift(e, reason, "false_positive", now)
        n += 1
    return n


# ───────────────────────── S2 공식 발표 (앱 공지·공식 X·블로그) ─────────────────────────
def ann_key(a: dict) -> str:
    return f"{a['created_at']}:{a['title']}"


def detect_announcements(items: list[dict], seen: list[str], now: int, tickers=()) -> list[dict]:
    """앱 공지 — 전환·TGE 신호 단어가 있으면 경보, 포인트 이야기뿐이면 관찰."""
    evs = []
    for a in items:
        k = ann_key(a)
        if k in seen:
            continue
        text = f"{a['title']}. {a['content']}"
        words = signal_words(text, tickers)
        if words:
            evs.append(event("S2", "announcement", "ALERT", f"앱 공지: {a['title'][:60]}",
                             a["content"].replace("\n", " ")[:240], "상장·점검 등 전환과 무관한 공지로 확인되면 해제",
                             a["created_at"] or now, {"title": a["title"], "words": words[:6]}, k))
        elif POINTS_CTX.search(text):
            evs.append(event("S2", "announcement", "WATCH", f"앱 공지(포인트): {a['title'][:60]}",
                             a["content"].replace("\n", " ")[:240], "", a["created_at"] or now,
                             {"title": a["title"]}, k))
    return evs


def post_key(it: dict) -> str:
    return it.get("key") or hash_text(it.get("link") or it.get("title", ""))


POST_LABEL = {"x": "공식 X", "telegram": "공식 텔레그램", "blog": "공식 블로그", "feed": "공식 블로그", "ann": "앱 공지"}


def detect_posts(items: list[dict], seen: list[str], now: int, kind: str, venue: str,
                 tickers=(), token_live: bool = False, extra: str | None = None) -> list[dict]:
    """공식 발표 새 글 — kind: x · telegram · blog · feed · ann(앱 공지).
    TGE·청구·시즌 종료 문구 → 경보(날짜까지 있으면 임박), 토큰 문맥의 신호 단어 → 관찰.
    리트윗은 건너뛰고, 답글은 관찰까지만."""
    evs = []
    label = POST_LABEL.get(kind, "공식 발표")
    xr = re.compile(extra, re.I) if extra else None
    for it in items:
        k = post_key(it)
        title = (it.get("title") or "").strip()
        if k in seen or not title or title.startswith("RT by"):
            continue
        text = f"{title}. {it.get('content') or ''}".strip()
        reply = title.startswith("R to @")
        strong = post_words(text, token_live) + ([m.group(0) for m in xr.finditer(text)] if xr else [])
        weak = signal_words(text, tickers)
        if strong and not reply:
            grade = "IMMINENT" if any(has_date(s) for s in re.split(r"(?<=[.!?\n])\s+", text)
                                      if post_words(s, token_live)) else "ALERT"
        elif strong or weak:
            grade = "WATCH"
        else:
            continue
        ts = it.get("ts") or now
        evs.append(event("S2", f"{kind}_post", grade, f"{venue} {label}: {plain_md(title)[:120]}",
                         it.get("link", "")[:160],
                         "토큰·에어드랍과 무관한 이벤트 안내로 확인되면 해제" if grade != "WATCH" else "",
                         ts, {"url": it.get("link"), "text": text[:500], "words": _uniq(strong or weak)[:6],
                              "label": label}, k))
    return evs


# ───────────────────────── S2 포인트 API · 앱 코드 ─────────────────────────
API_ALERT = r"ends?_at|visible|claim|token|airdrop|tge|vest|convert|unlock|snapshot"


def _meaningful(v) -> bool:
    return v not in (None, False, "", 0, [], {})


def detect_api(old: dict, new: dict, now: int, cfg: dict, venue: str) -> list[dict]:
    """포인트 API 값 감시(시즌 종료일·포인트 공개·청구 키 등). old/new: 평평하게 편 {경로: 값}.
    경보: 경로 이름이 alert 규칙에 맞고 값이 비었다가 채워짐(null→날짜, false→true)·새 키 생김.
    관찰: 그 밖의 변화(새 시즌 항목 등)."""
    if old is None or old == new:
        return []
    rx = re.compile(cfg.get("alert") or API_ALERT, re.I)
    strong, weak = [], []
    for p in sorted(set(old) | set(new)):
        a, b = old.get(p), new.get(p)
        if p in old and p in new and a == b:
            continue
        change = (p, a, b)
        if rx.search(p) and _meaningful(b) and not _meaningful(a):
            strong.append(change)
        else:
            weak.append(change)
    if not strong and not weak:
        return []
    top = (strong or weak)[0]
    grade = "ALERT" if strong else "WATCH"

    def fmt(c):
        return f"{c[0]}: {c[1] if c[0] in old else '(없음)'} → {c[2] if c[0] in new else '(삭제)'}"
    title = f"{venue} 포인트 API 변경: {fmt(top)}"[:160]
    return [event("S2", "api_change", grade, title, " · ".join(fmt(c) for c in (strong + weak)[:4])[:300],
                  "임시 값·표시 변경으로 확인되면 해제" if strong else "", now,
                  {"url": cfg.get("page") or cfg["url"], "changes": [fmt(c) for c in (strong + weak)[:10]]},
                  hash_text(json_dumps_sorted(new)))]


def json_dumps_sorted(d: dict) -> str:
    return "|".join(f"{k}={d[k]}" for k in sorted(d))


def detect_drop_delay(prev: dict | None, cur: dict, now: int, cfg: dict, venue: str) -> list[dict]:
    """주간 포인트 배분이 예정 시각을 grace 시간 넘게 지나도 안 들어오면 경보.
    prev/cur: {next: 다음 배분 예정(ts), week: 마지막 배분 주}."""
    if not prev or not prev.get("next") or cur.get("week") != prev.get("week"):
        return []
    late = now - prev["next"]
    if late < cfg.get("grace_hours", 24) * 3600:
        return []
    return [event("S2", "drop_late", "ALERT", f"{venue} 주간 포인트 배분이 {late // 3600}시간째 안 들어옴",
                  f"예정 {when(prev['next'])} · 마지막 배분 주 {cur.get('week')}", "다음 배분이 들어오면 해제", now,
                  {"next": prev["next"], "week": cur.get("week")}, f"late:{cur.get('week')}")]


def detect_jsscan(old: dict | None, new: dict, now: int, cfg: dict, venue: str) -> list[dict]:
    """앱 코드 감시 — 청구 API 경로 같은 캡처값이 바뀌면(예: '404' 자리표시자 → 실제 경로) 경보."""
    if not old:
        return []
    evs = []
    for k, vals in new.items():
        before = old.get(k)
        if before is None or sorted(before) == sorted(vals):
            continue
        name = (cfg.get("names") or {}).get(k, k)
        if not vals:  # 감시 문구가 번들에서 사라짐 — 구조 변경일 수 있어 관찰만
            evs.append(event("S3", "claim_api", "WATCH", f"{venue} 앱 코드에서 {name}를 찾지 못함",
                             f"이전 값 {', '.join(before)} · 앱 구조가 바뀌었을 수 있어요", "", now,
                             {"url": cfg.get("url"), "key": k, "before": before, "after": vals},
                             f"{k}:gone:{dt.datetime.fromtimestamp(now, KST):%Y-%m-%d}"))
            continue
        evs.append(event("S3", "claim_api", "ALERT", f"{venue} 앱 코드: {name} 바뀜",
                         f"{', '.join(before) or '(없음)'} → {', '.join(vals)}"[:240],
                         "배포 뒤 7일 안에 되돌려지면 해제", now,
                         {"url": cfg.get("url"), "key": k, "before": before, "after": vals},
                         f"{k}:{'|'.join(vals)}"))
    return evs


def detect_odds(odds: dict, titles: dict, now: int, venue: str, jump_pp: float = 15.0) -> list[dict]:
    """Polymarket '토큰 출시' 확률이 48시간 안에 jump_pp 이상 뛰면 관찰(보조 신호)."""
    evs = []
    for slug, hist in odds.items():
        t = titles.get(slug, "")
        if not re.search(r"launch a token", t, re.I) or len(hist) < 2:
            continue
        cur_ts, cur = hist[-1]
        past = [y for ts, y in hist if cur_ts - 48 * 3600 <= ts < cur_ts]
        if not past or cur is None:
            continue
        lo = min(y for y in past if y is not None) if any(y is not None for y in past) else None
        if lo is not None and cur - lo >= jump_pp:
            day = dt.datetime.fromtimestamp(now, KST).strftime("%Y-%m-%d")
            evs.append(event("S4", "polymarket", "WATCH", f"Polymarket: {t[:70]} {lo:.0f}% → {cur:.0f}%",
                             slug, "", now, {"url": f"https://polymarket.com/market/{slug}"}, f"jump:{slug}:{day}"))
    return evs


def odds_line(odds: dict, titles: dict, limit: int = 3, now: int | None = None) -> str:
    """'토큰 출시 확률 11/30까지 33% · 12/31까지 78%' — 열린 '출시' 시장 중 가까운 날짜부터."""
    rows = []
    for slug, hist in odds.items():
        t = titles.get(slug, "")
        m = re.search(r"launch a token by ([A-Za-z]+) (\d{1,2}),? (\d{4})", t)
        if not m or not hist or hist[-1][1] is None:
            continue
        try:
            d = dt.datetime.strptime(f"{m.group(1)[:3]} {m.group(2)} {m.group(3)}", "%b %d %Y").date()
        except ValueError:
            continue
        rows.append((d, hist[-1][1]))
    rows.sort()
    today = (dt.datetime.fromtimestamp(now, KST) if now else dt.datetime.now(KST)).date()
    rows = [r for r in rows if r[0] >= today][:limit]
    if not rows:
        return ""
    return "토큰 출시 확률 " + " · ".join(
        f"{'' if d.year == today.year else f'{d.year % 100}년 '}{d.month}/{d.day}까지 {y:.0f}%" for d, y in rows)


# ───────────────────────── S2 리더보드 ─────────────────────────
def detect_leaderboard(hist: list[list], cfg: dict, now: int) -> list[dict]:
    """hist: [[ts, top_sum, top1], ...]. 적립 정지·상위권 삭감·주간 둔화."""
    evs = []
    if len(hist) < 2:
        return evs
    cur_ts, cur_sum, _ = hist[-1]
    prev_ts, prev_sum, _ = hist[-2]
    day = dt.datetime.fromtimestamp(now, KST).strftime("%Y-%m-%d")
    top = cfg.get("top", 10)
    # 1) 상위권 합계 감소 — 시빌·워시 포인트 정리(라이터 LIT 출시 직전 선례)
    if prev_sum > 0 and (prev_sum - cur_sum) / prev_sum * 100 >= cfg["lb_drop_pct"]:
        evs.append(event("S2", "lb_slash", "ALERT",
                         f"포인트 상위 {top}명 합계 {((cur_sum / prev_sum) - 1) * 100:.1f}% 감소",
                         f"{prev_sum:,.0f} → {cur_sum:,.0f}",
                         "다음 주간 배분 뒤 합계가 원래 추세로 돌아오면 해제", now,
                         {"prev": prev_sum, "cur": cur_sum}, f"slash:{day}"))
    # 2) 적립 정지 — freeze_hours 이상 합계가 그대로
    lim = cur_ts - cfg["lb_freeze_hours"] * 3600
    window = [h for h in hist if h[0] >= lim]
    older = [h for h in hist if h[0] < lim]
    if older and window and all(abs(h[1] - cur_sum) < 1e-6 for h in window + [older[-1]]):
        evs.append(event("S2", "lb_frozen", "ALERT", f"포인트 적립이 {cfg['lb_freeze_hours']}시간 넘게 멈춤",
                         f"상위 {top}명 합계 변화 없음", "합계가 다시 늘면 해제", now,
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


# ───────────────────────── S3 앱 화면 ─────────────────────────
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
def detect_news(items: list[dict], seen: list[str], now: int, source: str, match: str, tickers=()) -> list[dict]:
    """뉴스·비공식 X — 거래소 이름이 맞고 전환·TGE 단어가 있으면 관찰."""
    evs = []
    rx = re.compile(match, re.I)
    for it in items:
        k = hash_text(it.get("link") or it.get("title", ""))
        title = it.get("title", "")
        if k in seen or not rx.search(title):
            continue
        words = post_words(title) or signal_words(title, tickers)
        if not words:
            continue
        evs.append(event("S4", source, "WATCH", f"{'뉴스' if source == 'news' else 'X'}: {title[:100]}",
                         it.get("link", "")[:160], "", now, {"link": it.get("link"), "words": words[:6]}, k))
    return evs


def detect_polymarket(items: list[dict], seen: list[str], now: int, match: str) -> list[dict]:
    evs = []
    rx = re.compile(match, re.I)
    for m in items:
        if m["slug"] in seen:
            continue
        if not rx.search(m["title"] + " " + m["slug"]):
            continue
        odds = f" · Yes {m['yes_pct']}%" if m.get("yes_pct") is not None else ""
        evs.append(event("S4", "polymarket", "WATCH", f"Polymarket 새 시장: {m['title'][:80]}", f"{m['slug']}{odds}",
                         "", now, {"url": f"https://polymarket.com/event/{m['slug']}", **m}, m["slug"]))
    return evs


# ───────────────────────── 등급 ─────────────────────────
def active(events: list[dict], now: int, window_days: int) -> list[dict]:
    lim = now - window_days * 86400
    return [e for e in events if not e["refuted"] and e["ts"] >= lim]


def level_of(events: list[dict]) -> tuple[str, list[str]]:
    """(등급, 근거 묶음). 근거 묶음이 서로 다른 강신호 2가지면 임박."""
    strong = [e for e in events if RANK[e["grade"]] >= 2 and e["family"] != "S4"]
    groups = sorted({group_of(e) for e in strong})
    if any(e["grade"] == "IMMINENT" for e in strong) or len(groups) >= 2:
        return "IMMINENT", groups
    if groups:
        return "ALERT", groups
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
           "ALERT": "강한 신호 1가지가 남아 있어요",
           "IMMINENT": "서로 다른 강한 신호가 함께 남아 있어요"}

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


def span_text(t0: int, t1: int) -> str:
    d0, d1 = dt.datetime.fromtimestamp(t0, KST), dt.datetime.fromtimestamp(t1, KST)
    if when(t0) == when(t1):
        return when(t0)
    return f"{when(t0)} ~ " + (f"{d1:%H:%M}" if d0.date() == d1.date() else when(t1))


def _quote(e: dict) -> str:
    """문서·공지·발표 신호는 새로 들어간 문장을 그대로 보여 준다."""
    ref = e.get("ref") or {}
    if e["signal"] == "doc_keyword" and ref.get("added"):
        return plain_md(ref["added"][0])[:240]
    if e["signal"].endswith("_post") and ref.get("text"):
        return plain_md(ref["text"])[:280]
    if e["signal"] == "announcement" and e.get("detail"):
        return e["detail"][:240]
    return ""


def _title_for_quote(e: dict) -> str:
    """원문을 따로 보여 줄 때 제목에서 원문을 뺀다 — 공식 발표는 '이름 공식 X에 새 글'."""
    if e["signal"].endswith("_post"):
        return e["title"].split(":", 1)[0] + "에 새 글"
    return e["title"]


def _links(events: list[dict], cfg: dict, vcfg: dict | None = None) -> list[str]:
    out = []
    src = next(((e.get("ref") or {}).get("url") for e in events
                if e["family"] == "S2" and str((e.get("ref") or {}).get("url") or "").startswith("http")), None)
    if src:
        out.append(link(web_url(src), "원문"))
    tx = next((e.get("ref") for e in events if (e.get("ref") or {}).get("tx")), None)
    if tx:
        oc = (vcfg or {}).get("onchain") or {}
        base = oc.get("rh_blockscout") if tx.get("chain") == "rh" else oc.get("eth_blockscout")
        if base:
            out.append(link(f"{base}/tx/{tx['tx']}", "거래 내역"))
    out.append(link(cfg["dashboard_url"], "대시보드"))
    return out


def render_venue_alert(vcfg: dict, level: str, groups: list[str], events: list[dict], now: int, cfg: dict,
                       note: str = "", head: bool = True) -> str:
    """한 거래소의 경보·임박 — 무슨 일 → 의미 → 해제 조건 → 링크. head=False면 여러 곳 메시지의 한 칸."""
    items = collapse([e for e in events if RANK[e["grade"]] >= 2 and e["family"] != "S4"]) or collapse(events)
    items = items[-4:]
    strong = [e for _, e in items]
    vname = vcfg["name"]
    t0, t1 = (items[0][0], items[-1][0]) if items else (now, now)
    kinds = " · ".join(GROUP_KO.get(g, g) for g in groups) if groups else "신호"
    lines = []
    if head:
        badge = BADGE.get(level, "")
        lines.append(f"{badge + ' ' if badge else ''}<b>{NAME} · {esc(vname)} {LEVEL_KO[level]}</b>")
    else:
        lines.append(f"<b>━ {esc(vname)} · {LEVEL_KO[level]}</b>")
    lines.append(f"{span_text(t0, t1)} 감지 · {kinds}")
    if vcfg.get("status_note"):
        lines.append(f"알려진 상태: {esc(vcfg['status_note'])}")
    lines += ["", "<b>무슨 일</b>"]
    for e in strong:
        q = _quote(e)
        lines.append(("· " if len(strong) > 1 else "") + esc(_title_for_quote(e) if q else e["title"]))
        if q:
            lines.append(f"  <i>“{esc(q)}”</i>")
            words = (e.get("ref") or {}).get("words")
            if words:
                lines.append(f"  잡힌 단어: {esc(' · '.join(words[:5]))}")
        elif e.get("detail"):
            lines.append(f"  {esc(e['detail'])}")
    lines += ["", "<b>의미</b>"]
    if level == "IMMINENT":
        if len(groups) >= 2:
            lines.append(f"서로 다른 근거 {len(groups)}가지가 7일 안에 함께 나왔어요. 토큰 전환이 가까웠을 가능성이 높아요.")
        else:
            lines.append("공식 문서·발표에 TGE·청구 일정(날짜)이 함께 나왔어요. 일정이 잡혔다는 뜻이에요.")
    seen: list[str] = []
    for e in strong:
        m = MEANING.get(e["signal"])
        if m and m not in seen:
            seen.append(m)
    lines += seen[:3]
    if level == "ALERT":
        lines.append("다른 종류 근거(온체인·공식 문서·공식 발표·리더보드·앱 중)가 7일 안에 하나 더 나오면 '임박'으로 올려요.")
    refs: list[str] = []
    for e in strong:
        if e.get("refute") and e["refute"] not in refs:
            refs.append(e["refute"])
    if refs:
        lines += ["", "<b>해제 조건</b>"] + [esc(r) for r in refs[:3]]
    if note:
        lines += ["", esc(note)]
    links = _links(strong, cfg, vcfg)
    if not head:
        links = links[:-1]  # 여러 곳 메시지는 대시보드 링크를 맨 끝에 한 번만
    if links:
        lines += ["", " · ".join(links)]
    return "\n".join(lines)


def render_alert(sections: list[tuple[str, str]], cfg: dict) -> str:
    """sections: [(등급, 거래소 칸)] — 한 곳이면 그 칸 그대로, 여러 곳이면 머리글을 달고 이어 붙인다."""
    if len(sections) == 1:
        return sections[0][1]
    top = max((lv for lv, _ in sections), key=lambda lv: RANK[lv])
    cnt = {}
    for lv, _ in sections:
        cnt[lv] = cnt.get(lv, 0) + 1
    summary = " · ".join(f"{LEVEL_KO[lv]} {cnt[lv]}곳" for lv in ("IMMINENT", "ALERT") if cnt.get(lv))
    badge = BADGE.get(top, "")
    body = "\n\n".join(s for _, s in sections)
    return f"{badge + ' ' if badge else ''}<b>{NAME} · {summary}</b>\n\n{body}\n\n{link(cfg['dashboard_url'], '대시보드')}"


def lifted_title(e: dict, names: dict | None = None) -> str:
    """해제 메시지의 한 줄 이름 — 문서 신호는 문서 이름, 나머지는 제목."""
    ref = e.get("ref") or {}
    if e["family"] == "S2" and e["signal"].startswith("doc") and ref.get("url"):
        return ref.get("name") or doc_name(ref["url"], names)
    return e["title"]


def render_release_section(vcfg: dict, lifted: list[dict], level: str, notified: str, notified_at: int | None,
                           moves: dict | None = None) -> str:
    """보낸 경보가 풀렸을 때의 한 거래소 칸 — 오탐이면 '정정', 해제 조건 충족이면 '해제'."""
    fp = all(e.get("refute_kind") == "false_positive" for e in lifted)
    names = (vcfg.get("docs") or {}).get("names")
    sent = when(notified_at) if notified_at else "앞서 보낸"
    was = LEVEL_KO.get(notified, notified)
    if fp and RANK[level] < 2:
        first = f"{sent} '{was}' 알림은 오탐이었어요. 토큰 전환 관련 변화는 없어요."
    elif fp:
        first = f"{sent} '{was}' 알림 중 일부 신호는 오탐이었어요."
    else:
        first = f"{sent} '{was}' 알림의 신호가 풀렸어요."
    lines = [f"<b>{esc(vcfg['name'])} · {'정정' if fp else '해제'}</b>", first]
    for e in lifted[:5]:
        lines.append(f"· {esc(lifted_title(e, names))} — {esc(e.get('refute_reason') or '해제 조건을 충족했어요')}")
    lines.append(f"지금: {LEVEL_KO[level]} — {VERDICT[level]}")
    if moves:
        targets = []
        for m in moves.values():
            to = m.get("to", "")
            if to and to not in [t for t, _ in targets]:
                targets.append((to, doc_name(to, names)))
        lines.append("옮겨진 문서는 새 주소로 계속 감시해요: "
                     + " · ".join(link(web_url(t), n) for t, n in targets[:3]))
    return "\n".join(lines)


def render_release(sections: list[str], cfg: dict, fp: bool) -> str:
    head = f"<b>{NAME} · {'정정' if fp else '해제'}</b>"
    return head + "\n\n" + "\n\n".join(sections) + "\n\n" + link(cfg["dashboard_url"], "대시보드")


def venue_line(s: dict) -> str:
    """요약 한 줄 — '· Arcus — 관찰 · 원천 4/4 · 알려진 상태'."""
    parts = [f"{LEVEL_KO[s['level']]}"]
    if s.get("sources_all"):
        parts.append(f"수집 {s['sources_ok']}/{s['sources_all']} 정상")
    line = f"· <b>{esc(s['name'])}</b> — " + " · ".join(parts)
    if s.get("status_note"):
        line += f"\n  {esc(s['status_note'])}"
    if s.get("odds"):
        line += f"\n  {esc(s['odds'])} (Polymarket)"
    return line


def render_kickoff(summaries: list[dict], now: int, cfg: dict, release_sections: list[str] | None = None,
                   added: list[str] | None = None, removed: list[str] | None = None, renamed: bool = False) -> str:
    """감시 범위가 바뀌었을 때 한 번 — 거래소별 지금 상태와 보내는 규칙."""
    lines = [f"<b>{NAME} · 감시 범위 변경</b>", when(now)]
    if renamed:
        lines.append(f"라이터RH 전환감시를 '{NAME}'로 바꾸고 {len(summaries)}곳의 포인트→토큰 전환(TGE·에어드랍) 신호를 함께 봐요.")
    else:
        if added:
            lines.append("추가: " + ", ".join(esc(a) for a in added))
        if removed:
            lines.append("제외: " + ", ".join(esc(r) for r in removed))
    lines += ["", "<b>거래소별 지금 상태</b>"] + [venue_line(s) for s in summaries]
    for sec in release_sections or []:
        lines += ["", sec]
    lines += ["", "<b>보내는 때</b>",
              "경보·임박은 감지 즉시(07~21시, 밤에 잡히면 아침에), 보낸 경보가 풀리면 정정·해제 한 번, 매월 초 점검 한 번.",
              "", link(cfg["dashboard_url"], "대시보드")]
    return "\n".join(lines)


def render_heartbeat(summaries: list[dict], now: int, cfg: dict) -> str:
    return "\n".join([f"<b>{NAME} · 월간 점검</b>", when(now), ""]
                     + [venue_line(s) for s in summaries]
                     + ["", link(cfg["dashboard_url"], "대시보드")])
