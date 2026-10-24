"""perp-dex-radar 원천 수집기 — 외부 의존성 없이 표준 라이브러리만 쓴다.

수집기는 실패하면 예외를 던지고, 부르는 쪽(radar.py)이 원천 상태로 기록한다.
판정 로직은 judge.py에 있고, 여기서는 '관측'만 한다.
"""
from __future__ import annotations

import datetime
import email.utils
import hashlib
import html as htmllib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

UA = "Mozilla/5.0 (X11; Linux x86_64) perp-dex-radar/2.0"
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
DECIMALS = 10 ** 18


def http(url: str, data: bytes | None = None, headers: dict | None = None,
         timeout: int = 25, tries: int = 3) -> bytes:
    h = {"User-Agent": UA, "Accept": "*/*"}
    h.update(headers or {})
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, data=data, headers=h,
                                         method="POST" if data is not None else "GET")
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
            if e.code in (400, 401, 403, 404):
                break
            ra = e.headers.get("Retry-After") if e.headers else None
            wait = min(float(ra), 8.0) if ra and ra.replace(".", "").isdigit() else [2, 5, 9][i]
            time.sleep(wait)
        except Exception as e:  # noqa: BLE001
            last = f"{type(e).__name__}: {e}"
            time.sleep([2, 5, 9][i])
    raise RuntimeError(f"{url[:90]} → {last}")


def get_json(url: str, **kw):
    return json.loads(http(url, headers={"Accept": "application/json"}, **kw).decode())


# ───────────────────────── RPC ─────────────────────────
def rpc(urls: list[str], method: str, params: list):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    errs = []
    for u in urls:
        try:
            d = json.loads(http(u, data=body, headers={"Content-Type": "application/json"}).decode())
            if "error" in d:
                errs.append(f"{u.split('/')[2]}: {d['error'].get('message', d['error'])}")
                continue
            return d["result"]
        except Exception as e:  # noqa: BLE001
            errs.append(f"{u.split('/')[2]}: {e}")
    raise RuntimeError("; ".join(errs)[:300])


def _topic_addr(t: str) -> str:
    return "0x" + t[-40:].lower()


def transfer_logs(urls, token, from_block, to_block, chunk, min_lit):
    """[from_block, to_block] 구간의 Transfer 로그 중 min_lit 이상만 반환."""
    out = []
    b = from_block
    while b <= to_block:
        e = min(b + chunk - 1, to_block)
        logs = rpc(urls, "eth_getLogs", [{
            "address": token, "fromBlock": hex(b), "toBlock": hex(e),
            "topics": [TRANSFER_TOPIC]}])
        for lg in logs:
            val = int(lg["data"], 16) / DECIMALS if lg.get("data") not in (None, "0x") else 0.0
            if val < min_lit:
                continue
            out.append({
                "block": int(lg["blockNumber"], 16),
                "tx": lg["transactionHash"],
                "log_index": int(lg.get("logIndex", "0x0"), 16),
                "from": _topic_addr(lg["topics"][1]),
                "to": _topic_addr(lg["topics"][2]),
                "amount": round(val, 2),
            })
        b = e + 1
    return out


def block_number(urls) -> int:
    return int(rpc(urls, "eth_blockNumber", []), 16)


def block_time(urls, n: int) -> int:
    blk = rpc(urls, "eth_getBlockByNumber", [hex(n), False])
    return int(blk["timestamp"], 16)


def total_supply(urls, token) -> float:
    return int(rpc(urls, "eth_call", [{"to": token, "data": "0x18160ddd"}, "latest"]), 16) / DECIMALS


# ───────────────────────── 주소 메타 (Blockscout) ─────────────────────────
def address_meta(blockscout: str, addr: str) -> dict:
    """이름·태그·컨트랙트 여부·생성시각. 실패해도 최소 정보는 돌려준다."""
    meta = {"addr": addr.lower(), "is_contract": None, "name": "", "tags": [], "created_ts": None}
    try:
        d = get_json(f"{blockscout}/api/v2/addresses/{addr}")
        meta["is_contract"] = bool(d.get("is_contract"))
        meta["name"] = d.get("name") or ""
        meta["tags"] = [t.get("display_name") or t.get("label") or ""
                        for t in (d.get("public_tags") or []) + (d.get("metadata", {}) or {}).get("tags", [])
                        if isinstance(t, dict)]
        ctx = d.get("creation_transaction_hash") or d.get("creation_tx_hash")
        if ctx:
            tx = get_json(f"{blockscout}/api/v2/transactions/{ctx}")
            ts = tx.get("timestamp")
            if ts:
                meta["created_ts"] = int(datetime.datetime.strptime(
                    ts[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=datetime.timezone.utc).timestamp())
    except Exception as e:  # noqa: BLE001
        meta["error"] = str(e)[:120]
    return meta


# ───────────────────────── 문서 ─────────────────────────
_DOC_NOISE = re.compile(
    r"^> (?:For the complete documentation index|## Documentation Index|Fetch the complete documentation index"
    r"|Use this file to discover all available pages).*$"
    r"|^This documentation is built and hosted on \[Mintlify\].*$",
    re.M)
# GitBook이 2026-10-22부터 모든 .md 끝에 붙이는 AI 에이전트용 안내문 — 본문이 아니다.
_AGENT_BLOCK = re.compile(r"(?:^|\n)[ \t]*-{3,}[ \t]*\n(?:[ \t]*\n)*#[ \t]+Agent Instructions\b.*\Z", re.S)
_AGENT_TAIL = re.compile(r"(?:^|\n)#[ \t]+Agent Instructions\b.*\Z", re.S)


def is_not_found(text: str) -> bool:
    """GitBook의 '페이지 없음' 응답(HTTP 200으로 온다) — 문서가 옮겨졌거나 지워졌다."""
    return (text or "").lstrip().startswith("# Page Not Found")


def normalize_doc(text: str) -> str:
    text = text.replace("\r", "")
    text = _AGENT_TAIL.sub("", _AGENT_BLOCK.sub("", text))
    text = _DOC_NOISE.sub("", text)
    text = text.replace("&#x20;", " ")
    lines = [ln.rstrip() for ln in text.split("\n")]
    out, blank = [], False
    for ln in lines:
        if not ln.strip():
            if not blank:
                out.append("")
            blank = True
            continue
        blank = False
        out.append(ln)
    return "\n".join(out).strip()


def fetch_doc(url: str) -> str:
    """문서 한 장 — 마크다운(.md·llms.txt)은 그대로, HTML이면 본문 글자만 뽑아 같은 규칙으로 정리."""
    raw = http(url).decode("utf-8", "replace")
    if re.match(r"\s*(<!doctype html|<html)", raw, re.I):
        raw = html_text(raw)
    return normalize_doc(raw)


_DROP = re.compile(r"<(script|style|noscript|svg|nav|footer|header)\b.*?</\1>", re.I | re.S)
_BREAK = re.compile(r"<\s*(br|/p|/div|/li|/h[1-6]|/tr|/section|/article)\b[^>]*>", re.I)
_HEAD = re.compile(r"<\s*h([1-6])\b[^>]*>", re.I)
_LI = re.compile(r"<\s*li\b[^>]*>", re.I)
_TAG = re.compile(r"<[^>]+>")


def html_text(raw: str) -> str:
    """HTML → 읽을 수 있는 글자(제목은 #, 목록은 -). 스크립트·내비게이션은 버린다."""
    s = _DROP.sub(" ", raw)
    s = _HEAD.sub(lambda m: "\n" + "#" * int(m.group(1)) + " ", s)
    s = _LI.sub("\n- ", s)
    s = _BREAK.sub("\n", s)
    s = htmllib.unescape(_TAG.sub(" ", s))
    lines = [re.sub(r"[ \t ]+", " ", ln).strip() for ln in s.split("\n")]
    return "\n".join(ln for ln in lines if ln)


_A = re.compile(r"<a\b[^>]*?href=[\"']([^\"'#]+)[\"'][^>]*>(.*?)</a>", re.I | re.S)


def link_items(url: str, pattern: str) -> list[dict]:
    """블로그·공지 목록 페이지에서 글 링크(pattern에 맞는 href)와 제목을 뽑는다 — 처음 나온 순서, 주소 중복 제거."""
    raw = http(url).decode("utf-8", "replace")
    rx = re.compile(pattern)
    out, seen = [], set()
    for href, inner in _A.findall(raw):
        full = urllib.parse.urljoin(url, href.strip())
        if not rx.search(full) or full in seen:
            continue
        h = re.search(r"<h[1-6][^>]*>(.*?)</h[1-6]>", inner, re.S)  # 카드형 링크는 안의 제목만
        title = re.sub(r"\s+", " ", htmllib.unescape(_TAG.sub(" ", h.group(1) if h else inner))).strip()
        if len(title) < 4 or len(title) > 160:  # 그림만 있거나 카드 전체 글 — 주소에서 제목을 만든다
            slug = full.rstrip("/").split("/")[-1]
            title = re.sub(r"[-_]+", " ", slug).strip().title()
        seen.add(full)
        out.append({"title": title[:300], "link": full})
    if not out:
        raise RuntimeError("글 링크를 찾지 못함(목록 구조 변경?)")
    return out


def feed_items(url: str) -> list[dict]:
    """RSS·Atom 피드 → [{title, link, ts}]."""
    root = ET.fromstring(http(url))
    out = []
    for it in root.iter():
        tag = it.tag.split("}")[-1]
        if tag not in ("item", "entry"):
            continue
        title, link_, ts = "", "", None
        for ch in it:
            t = ch.tag.split("}")[-1]
            if t == "title":
                title = (ch.text or "").strip()
            elif t == "link":
                link_ = (ch.text or ch.get("href") or "").strip() or link_
            elif t in ("pubDate", "published", "updated") and ch.text and ts is None:
                ts = parse_time(ch.text.strip())
        out.append({"title": title, "link": link_, "ts": ts})
    return out


def parse_time(s: str) -> int | None:
    try:
        return int(email.utils.parsedate_to_datetime(s).timestamp())
    except (TypeError, ValueError, IndexError):
        pass
    try:
        return int(datetime.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


_TG_MSG = re.compile(r'<div class="tgme_widget_message [^"]*"[^>]*data-post="([^"]+)"(.*?)(?=<div class="tgme_widget_message |\Z)',
                     re.S)
_TG_TEXT = re.compile(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', re.S)
_TG_TIME = re.compile(r'<time[^>]*datetime="([^"]+)"')


def telegram_posts(channel: str) -> list[dict]:
    """공개 텔레그램 채널(t.me/s/채널) 최근 글 → [{title(본문), link, ts}]. X가 막힌 곳의 공식 발표 대체."""
    raw = http(f"https://t.me/s/{channel}").decode("utf-8", "replace")
    out = []
    for post, body in _TG_MSG.findall(raw):
        m = _TG_TEXT.search(body)
        if not m:
            continue
        text = re.sub(r"\s+", " ", htmllib.unescape(_TAG.sub(" ", m.group(1).replace("<br/>", "\n")))).strip()
        t = _TG_TIME.search(body)
        out.append({"title": text[:400], "link": f"https://t.me/{post}", "ts": parse_time(t.group(1)) if t else None})
    if not out:
        raise RuntimeError("채널 글을 찾지 못함")
    return out


def flatten(obj, prefix: str = "", ignore: frozenset = frozenset()) -> dict:
    """JSON → {경로: 값}. ignore에 든 키 이름은 어느 깊이에서든 뺀다(매번 바뀌는 값)."""
    out = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in ignore:
                continue
            out.update(flatten(v, f"{prefix}{k}.", ignore))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out.update(flatten(v, f"{prefix}{i}.", ignore))
    else:
        out[prefix[:-1]] = obj
    return out


def json_path(obj, path: str):
    """'data.entries' · 'result.0.items' 같은 점 경로. 빈 경로면 그대로."""
    for part in [p for p in (path or "").split(".") if p]:
        obj = obj[int(part)] if isinstance(obj, list) else obj[part]
    return obj


def leaderboard_json(url: str, list_path: str, field: str, top: int = 10) -> list[float]:
    """공개 리더보드 JSON → 상위 top명의 포인트(큰 순)."""
    rows = json_path(get_json(url), list_path)
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("리더보드 목록이 비어 있음")
    pts = sorted((float(json_path(r, field) or 0) for r in rows), reverse=True)
    return pts[:top]


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


# ───────────────────────── Lighter RH 공개 API ─────────────────────────
def announcements(api: str) -> list[dict]:
    d = get_json(f"{api}/api/v1/announcement")
    return [{"title": a.get("title", "").strip(), "content": (a.get("content") or "").strip(),
             "created_at": int(a.get("created_at") or 0)} for a in d.get("announcements", [])]


def leaderboard(api: str) -> list[dict]:
    d = get_json(f"{api}/api/v1/leaderboard?type=all")
    return [{"addr": e.get("l1_address", ""), "points": float(e.get("points") or 0),
             "rank": int(e.get("entry") or 0)} for e in d.get("entries", [])]


# ───────────────────────── 프론트엔드 ─────────────────────────
_CHUNK = re.compile(r"assets/([A-Za-z0-9_-]+?)-[A-Za-z0-9_-]{8}\.js")
_APIPATH = re.compile(r"/api/v1/[A-Za-z_/]+")


def frontend_surface(base: str) -> dict:
    html = http(base + "/").decode("utf-8", "replace")
    m = re.search(r'src="(/assets/index-[A-Za-z0-9_-]+\.js)"', html)
    if not m:
        raise RuntimeError("index 번들 경로를 찾지 못함")
    idx = http(base + m.group(1), timeout=40).decode("utf-8", "replace")
    chunks = sorted(set(_CHUNK.findall(html + idx)))
    paths: set[str] = set()
    vz = re.search(r"assets/(vendor-zklighter-[A-Za-z0-9_-]+\.js)", html + idx)
    if vz:
        vtxt = http(f"{base}/assets/{vz.group(1)}", timeout=40).decode("utf-8", "replace")
        paths = set(_APIPATH.findall(vtxt))
    return {"chunks": chunks, "api_paths": sorted(paths)}


# ───────────────────────── 보조: 뉴스·X·Polymarket ─────────────────────────
def google_news(query: str) -> list[dict]:
    q = urllib.parse.quote(query)
    raw = http(f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en")
    root = ET.fromstring(raw)
    items = []
    for it in root.iter("item"):
        items.append({"title": (it.findtext("title") or "").strip(),
                      "link": (it.findtext("link") or "").strip(),
                      "pub": (it.findtext("pubDate") or "").strip()})
    return items


def x_feed(hosts: list[str], account: str) -> list[dict]:
    """공식 X 글 — Nitter RSS를 먼저, 막히면 fxtwitter 공개 API. 리트윗은 제목이 'RT by'로 시작한다.
    링크는 x.com 주소로 통일한다(같은 글을 어느 경로로 받아도 같은 키)."""
    last = None
    for h in hosts:
        try:
            raw = http(f"{h}/{account}/rss", tries=1)
            root = ET.fromstring(raw)
            items = []
            for it in root.iter("item"):
                link_ = (it.findtext("link") or "").strip()
                m = re.search(r"/status/(\d+)", link_)
                items.append({"title": (it.findtext("title") or "").strip()[:400],
                              "link": f"https://x.com/{account}/status/{m.group(1)}" if m else link_,
                              "ts": parse_time((it.findtext("pubDate") or "").strip())})
            if items:
                return items
            last = "빈 피드"
        except Exception as e:  # noqa: BLE001
            last = e
    try:
        return fx_posts(account)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"X 수집 실패: {last} / fxtwitter {e}") from None


def fx_posts(account: str) -> list[dict]:
    d = get_json(f"https://api.fxtwitter.com/2/profile/{account}/statuses", tries=2)
    out = []
    for r in d.get("results") or []:
        text = (r.get("text") or "").strip()
        art = (r.get("article") or {}).get("title")
        if art:
            text = f"{art} — {text}" if text else art
        if r.get("retweeted_status") or r.get("reposted_by"):
            text = "RT by @" + account + ": " + text
        sid = r.get("id")
        out.append({"title": text[:400], "link": f"https://x.com/{account}/status/{sid}" if sid else "",
                    "ts": parse_time(r.get("created_at") or "") if r.get("created_at") else None})
    if not out:
        raise RuntimeError("빈 응답")
    return out


_SCRIPT = re.compile(r"""<script[^>]+src=["']([^"']+\.js)["']""", re.I)
_CHUNK_REF = re.compile(r"""["']((?:/_next/static/chunks|/assets|/static/js)/[A-Za-z0-9_./-]+\.js)["']""")


def js_scan(page: str, patterns: dict[str, str], max_files: int = 60, max_bytes: int = 12_000_000) -> dict[str, list[str]]:
    """앱 첫 화면이 싣는 같은 출처 JS를 훑어 정규식 캡처값을 모은다 — {이름: [캡처값...]}.
    청구 API 경로처럼 '아직 404 자리표시자'인 값이 실제 경로로 바뀌는 순간을 잡는 데 쓴다."""
    base = re.match(r"https?://[^/]+", page).group(0)
    html_ = http(page).decode("utf-8", "replace")
    queue = [urllib.parse.urljoin(page, s) for s in _SCRIPT.findall(html_)]
    queue += [base + s for s in _CHUNK_REF.findall(html_)]
    seen, total = set(), 0
    rx = {k: re.compile(v) for k, v in patterns.items()}
    found: dict[str, set] = {k: set() for k in patterns}
    while queue and len(seen) < max_files and total < max_bytes:
        u = queue.pop(0)
        if not u.startswith(base) or u in seen:
            continue
        seen.add(u)
        try:
            js = http(u, timeout=40, tries=2).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            continue
        total += len(js)
        for k, r in rx.items():
            found[k].update(m if isinstance(m, str) else m[0] for m in r.findall(js))
        queue += [base + s for s in _CHUNK_REF.findall(js) if base + s not in seen]
    if not seen:
        raise RuntimeError("스크립트를 찾지 못함")
    return {k: sorted(v) for k, v in found.items()}


def polymarket(query: str) -> list[dict]:
    """Polymarket 검색 → 열린 시장 하나하나(이벤트 안의 날짜별 시장까지) [{slug, title, yes_pct, end, event}]."""
    q = urllib.parse.quote(query)
    d = get_json(f"https://gamma-api.polymarket.com/public-search?q={q}&limit_per_type=20")
    out = []
    for e in d.get("events", []) or []:
        if e.get("closed"):
            continue
        for mk in e.get("markets") or []:
            if mk.get("closed"):
                continue
            yes = None
            try:
                prices = json.loads(mk.get("outcomePrices") or "[]")
                if prices:
                    yes = round(float(prices[0]) * 100, 1)
            except Exception:  # noqa: BLE001
                pass
            try:
                vol = float(mk.get("volumeNum") or mk.get("volume") or 0)
            except (TypeError, ValueError):
                vol = 0.0
            out.append({"slug": mk.get("slug") or e.get("slug", ""), "title": mk.get("question") or e.get("title", ""),
                        "yes_pct": yes, "end": mk.get("endDate") or e.get("endDate"), "event": e.get("slug", ""),
                        "volume": vol})
    return out


def json_posts(spec: dict) -> list[dict]:
    """앱 공지 JSON → [{title, content, link, ts, key}]. spec: {url, list, title, content, time, id, link}."""
    rows = json_path(get_json(spec["url"]), spec.get("list", ""))
    out = []
    for r in rows or []:
        title = str(json_path(r, spec.get("title", "title")) or "").strip()
        content = str(json_path(r, spec["content"]) or "").strip() if spec.get("content") else ""
        t = json_path(r, spec["time"]) if spec.get("time") else None
        ts = int(t / 1000 if isinstance(t, (int, float)) and t > 1e12 else t) if isinstance(t, (int, float)) \
            else (parse_time(str(t)) if t else None)
        rid = json_path(r, spec["id"]) if spec.get("id") else None
        # 키는 예전 라이터RH 공지 기록(생성시각:제목)과 같게 — 다시 알리지 않도록
        key = str(rid) if rid is not None else f"{int(t) if isinstance(t, (int, float)) else t}:{title}"
        out.append({"title": title, "content": content, "link": spec.get("link", ""), "ts": ts, "key": key})
    return out
