#!/usr/bin/env python3
"""AGI Thesis Radar 릴레이.

이 파일은 **hynix-correction-monitor 레포**에 배치된다.
(전략 Agent 봇 토큰을 보유한 레포이기 때문 — Secret 값은 조회가 불가하므로
 토큰을 옮길 수 없고, 발송만 토큰 보유 레포에서 수행한다.)

포매팅은 원본(agi-thesis-radar)이 latest.json 의 `message` 필드에 실어 보낸다.
릴레이는 전달만 하므로 포맷이 두 곳에서 이중 관리되지 않는다.

[운영 주의] raw.githubusercontent 는 최대 5분 CDN 캐시가 걸리고 ?t= 로도
우회되지 않는다. 수집 직후 읽는 릴레이는 옛 스냅샷을 읽을 수 있으므로
GitHub Contents API 를 1순위, raw 를 폴백으로 둔다.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

# 서브디렉터리 배포 방식이므로 원본은 호스트 레포 안에 있다.
# 환경변수로 덮어쓸 수 있게 두어, 레포를 옮겨도 코드 수정이 필요 없다.
OWNER = os.environ.get("RADAR_OWNER", "jinhae8971")
REPO = os.environ.get("RADAR_REPO", "korea-etf-calmar")
BASE = os.environ.get("RADAR_BASE", "docs/agi-radar/data")
DAILY_PATH = f"{BASE}/latest.json"
WEEKEND_PATH = f"{BASE}/weekend.json"
KST = timezone(timedelta(hours=9))
MAX_AGE_HOURS = 30
WEEKEND_MAX_AGE_HOURS = 8  # 주말본은 당일 생성분만 발송한다


def target_path() -> tuple[str, bool]:
    """KST 요일로 읽을 파일을 정한다. (경로, 주말여부)"""
    forced = (os.environ.get("RELAY_SOURCE") or "").strip().lower()
    if forced == "daily":
        return DAILY_PATH, False
    if forced == "weekend":
        return WEEKEND_PATH, True
    is_weekend = datetime.now(KST).weekday() >= 5  # 토=5, 일=6
    return (WEEKEND_PATH, True) if is_weekend else (DAILY_PATH, False)


def _get(url: str, headers: dict, timeout: int = 25) -> bytes:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def fetch_latest(path: str) -> dict:
    token = os.environ.get("GITHUB_TOKEN", "")
    if token:
        try:
            raw = _get(
                f"https://api.github.com/repos/{OWNER}/{REPO}/contents/{path}",
                {
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github.raw",
                    "User-Agent": "agi-radar-relay",
                },
            )
            return json.loads(raw.decode("utf-8"))
        except (urllib.error.URLError, json.JSONDecodeError, OSError) as exc:
            # 다른 레포를 읽는 경우 GITHUB_TOKEN 은 권한이 없어 404 가 정상이다.
            # 원본 레포가 public 이므로 raw 폴백으로 읽는다. 수집(07:10)과
            # 릴레이(07:20) 간격이 10분이라 raw 의 5분 CDN 캐시보다 길다.
            print(f"[relay] Contents API 실패, raw 폴백: {exc}")
    raw = _get(
        f"https://raw.githubusercontent.com/{OWNER}/{REPO}/main/{path}",
        {"User-Agent": "agi-radar-relay"},
    )
    return json.loads(raw.decode("utf-8"))


def send(text: str) -> None:
    token = os.environ.get("TELEGRAM_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        print("[telegram] 자격증명 없음 - 발송 생략")
        return
    payload = json.dumps(
        {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
         "disable_web_page_preview": True}
    ).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=25) as resp:
        print(f"[telegram] 발송 완료 (HTTP {resp.status})")


def main() -> int:
    path, is_weekend = target_path()
    print(f"[relay] 대상 {path} (주말모드={is_weekend})")
    try:
        report = fetch_latest(path)
    except Exception as exc:  # noqa: BLE001
        if is_weekend:
            # 주말본이 아직 없으면 조용히 종료 — 오래된 일간 브리프를 대신 보내지 않는다
            print(f"::warning::주말 스냅샷 없음, 발송 생략: {exc}")
            return 0
        print(f"::error::{path} 취득 실패: {exc}", file=sys.stderr)
        return 1

    message = (report.get("message") or "").strip()
    if not message:
        print("::error::message 필드가 비어 있습니다", file=sys.stderr)
        return 1

    # 원본이 멈췄는데 옛 브리프를 매일 재발송하는 사고를 막는다
    generated = report.get("generated_at")
    limit = WEEKEND_MAX_AGE_HOURS if is_weekend else MAX_AGE_HOURS
    if generated:
        try:
            when = datetime.fromisoformat(generated)
            if when.tzinfo is None:
                when = when.replace(tzinfo=KST)
            age = (datetime.now(KST) - when).total_seconds() / 3600
            if age > limit:
                if is_weekend:
                    # 지난 주말본을 이번 주말에 재발송하는 사고를 원천 차단
                    print(f"::warning::주말 스냅샷이 {age:.0f}시간 경과 — 발송 생략")
                    return 0
                message = (
                    f"⚠️ <b>원본 스냅샷이 {age:.0f}시간 경과</b> — 수집이 멈췄을 수 있습니다\n\n"
                    + message
                )
        except ValueError:
            pass

    send(message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
