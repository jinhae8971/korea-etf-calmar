"""HTTP 계층 — stdlib only. 429/403 백오프, gzip, UA 로테이션.

러너 실측 이력 반영:
  · CoinGecko 무료 티어는 연속 호출 시 즉시 429 → 호출 수를 설계로 억제하고 Retry-After 존중
  · GitHub 미인증은 60회/시 → 반드시 GITHUB_TOKEN 을 실어 보낸다(러너 기본 토큰으로 5,000회/시)
"""
import gzip
import io
import json
import os
import random
import time
import urllib.error
import urllib.request

UAS = [
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17 Safari/605.1.15",
    "catalyst-radar/1.0 (+https://github.com/jinhae8971)",
]
BACKOFF = (6, 15, 35, 75)
TIMEOUT = 45

STATS = {"calls": 0, "fail": 0, "by_host": {}}


class FetchError(Exception):
    pass


def _host(url):
    return url.split("/")[2] if "://" in url else url


BREAKER = {}          # host → 남은 호출을 건너뛸 사유
BREAKER_AT = 2        # 같은 호스트에서 rate-limit 을 이만큼 맞으면 차단


def circuit_open(url):
    return BREAKER.get(_host(url))


def fetch(url, headers=None, data=None, timeout=TIMEOUT, retries=3, allow_404=False):
    """본문(bytes) 반환. 실패 시 FetchError. 호출 통계를 STATS 에 누적."""
    host = _host(url)
    if host in BREAKER:
        raise FetchError("%s → 차단됨(%s)" % (url, BREAKER[host]))
    STATS["calls"] += 1
    h = STATS["by_host"].setdefault(host, {"ok": 0, "fail": 0})
    last = None
    for attempt in range(retries):
        req_headers = {"User-Agent": random.choice(UAS), "Accept-Encoding": "gzip"}
        if headers:
            req_headers.update(headers)
        req = urllib.request.Request(url, headers=req_headers, data=data)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
                h["ok"] += 1
                return raw
        except urllib.error.HTTPError as e:
            last = "HTTP %d" % e.code
            if e.code == 404 and allow_404:
                h["ok"] += 1
                return b""
            body = b""
            try:
                body = e.read()[:300]
            except Exception:
                pass
            if e.code in (403, 429) and b"rate limit" in body.lower():
                h["ratelimit"] = h.get("ratelimit", 0) + 1
                if h["ratelimit"] >= BREAKER_AT:
                    BREAKER[host] = "rate limit 소진"
                last = "HTTP %d (rate limit)" % e.code
                break                        # 재시도해도 한도는 안 풀린다
            if e.code in (403, 429, 500, 502, 503) and attempt < retries - 1:
                wait = BACKOFF[min(attempt, len(BACKOFF) - 1)]
                ra = e.headers.get("Retry-After") if e.headers else None
                if ra and str(ra).isdigit():
                    wait = max(wait, min(int(ra), 120))
                time.sleep(wait + random.uniform(0, 2))
                continue
            break
        except Exception as e:                      # 연결 실패·타임아웃
            last = type(e).__name__
            if attempt < retries - 1:
                time.sleep(BACKOFF[min(attempt, len(BACKOFF) - 1)])
                continue
            break
    h["fail"] += 1
    STATS["fail"] += 1
    raise FetchError("%s → %s" % (url, last))


def get_json(url, **kw):
    raw = fetch(url, **kw)
    if not raw:
        return None
    return json.loads(raw)


CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "cache")


def cached_json(url, ttl=3600, **kw):
    """rate limit 이 빡빡한 원천 전용. 실패 시 만료된 캐시라도 반환(열화 운영)."""
    import hashlib
    key = hashlib.sha256(url.encode()).hexdigest()[:20]
    path = os.path.join(CACHE_DIR, key + ".json")
    fresh = os.path.exists(path) and (time.time() - os.path.getmtime(path)) < ttl
    if fresh:
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            pass
    try:
        data = get_json(url, **kw)
        os.makedirs(CACHE_DIR, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, path)
        return data
    except Exception:
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                print("[cache] %s 원천 실패 → 만료 캐시 사용" % _host(url))
                return json.load(f)
        raise


def gh_json(path, **kw):
    """GitHub API. 러너에서는 GITHUB_TOKEN 이 주입돼 5,000회/시를 받는다."""
    headers = {"Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28"}
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = "Bearer " + token
    return get_json("https://api.github.com" + path, headers=headers, **kw)


def post_json(url, payload, headers=None, **kw):
    h = {"Content-Type": "application/json"}
    if headers:
        h.update(headers)
    raw = fetch(url, headers=h, data=json.dumps(payload).encode("utf-8"), **kw)
    return json.loads(raw) if raw else None
