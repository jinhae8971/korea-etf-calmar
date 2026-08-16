"""다중 소스 HTTP 클라이언트.

운영 제약:
  - Yahoo Finance 는 GitHub Actions 러너 IP 대역을 429 로 차단한다.
  - Stooq 는 일부 IP 에서 JS 챌린지를 반환한다.
  - FRED 는 컨테이너/일부 IP 에서 503 을 반환한다.
따라서 모든 수집기는 (a) 복수 소스 폴백, (b) 레포 커밋된 캐시 병합,
(c) 서킷 브레이커를 전제로 설계한다. 한 소스가 죽어도 시스템은 멈추지 않는다.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
DEFAULT_HEADERS = {
    "User-Agent": UA,
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
}


class FetchError(RuntimeError):
    pass


def fetch(url: str, *, timeout: int = 25, retries: int = 2, headers: dict | None = None) -> bytes:
    """단일 URL 취득. 429/503 은 백오프 후 재시도."""
    hdrs = dict(DEFAULT_HEADERS)
    if headers:
        hdrs.update(headers)
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:  # noqa: PERF203
            last = exc
            if exc.code in (403, 404):
                break
            time.sleep(1.5 * (attempt + 1))
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(1.5 * (attempt + 1))
    raise FetchError(f"{url} :: {last}")


def fetch_json(url: str, **kw) -> dict:
    return json.loads(fetch(url, **kw).decode("utf-8", "replace"))


def looks_like_challenge(payload: bytes) -> bool:
    """Stooq 등이 반환하는 JS 챌린지 페이지 판별."""
    head = payload[:400].lower()
    return b"<!doctype html" in head or b"<html" in head or b"noscript" in head
