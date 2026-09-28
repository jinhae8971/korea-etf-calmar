"""lit-rh-watch 원천 수집기 — 외부 의존성 없이 표준 라이브러리만 쓴다.

각 수집기는 (결과, 오류문자열|None)을 돌려주고 예외를 밖으로 던지지 않는다.
판정 로직은 judge.py에 있고, 여기서는 '관측'만 한다.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

UA = "Mozilla/5.0 (X11; Linux x86_64) lit-rh-watch/1.0"
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
_DOC_NOISE = re.compile(r"^> For the complete documentation index.*$", re.M)


def normalize_doc(text: str) -> str:
    text = _DOC_NOISE.sub("", text)
    text = text.replace("&#x20;", " ").replace("\r", "")
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
    return normalize_doc(http(url).decode("utf-8", "replace"))


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
    last = None
    for h in hosts:
        try:
            raw = http(f"{h}/{account}/rss", tries=1)
            root = ET.fromstring(raw)
            return [{"title": (it.findtext("title") or "").strip()[:280],
                     "link": (it.findtext("link") or "").strip()} for it in root.iter("item")]
        except Exception as e:  # noqa: BLE001
            last = e
    raise RuntimeError(f"X 수집 실패: {last}")


def polymarket(query: str) -> list[dict]:
    q = urllib.parse.quote(query)
    d = get_json(f"https://gamma-api.polymarket.com/public-search?q={q}&limit_per_type=20")
    out = []
    for e in d.get("events", []) or []:
        if e.get("closed"):
            continue
        odds = None
        for mk in e.get("markets") or []:
            try:
                prices = json.loads(mk.get("outcomePrices") or "[]")
                if prices:
                    odds = round(float(prices[0]) * 100, 1)
                    break
            except Exception:  # noqa: BLE001
                pass
        out.append({"slug": e.get("slug", ""), "title": e.get("title", ""), "yes_pct": odds,
                    "end": e.get("endDate")})
    return out
