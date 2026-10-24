#!/usr/bin/env python3
"""S3b — 라이터 RH 계정 청구 API 감시기 (로컬 도커 Lighter RH 스택 옆에서 돈다).

인증이 필요한 두 엔드포인트를 주기적으로 읽는다.
  /api/v1/airdrop?l1_address=…&auth=…        청구·배정 정보 (프론트 번들에 존재 확인)
  /api/v1/livePoints/total?account_index=…   내 누적 포인트

판정
  IMMINENT  airdrop 응답에 0이 아닌 수량·청구 가능 값이 처음 등장
  ALERT     응답 스키마(키 경로 집합)가 기준선과 달라짐
  (정상)    변화 없음 → 발송 없음

환경변수 (스택 .env를 그대로 물려받는 것을 전제로, 이름이 다르면 *_ENV로 가리킨다)
  LIGHTER_API_URL            기본 https://api.rh.lighter.xyz
  LIGHTER_L1_ADDRESS         지갑 주소 (필수)
  LIGHTER_ACCOUNT_INDEX      계정 인덱스 (필수)
  LIGHTER_API_KEY_INDEX      API 키 인덱스 (토큰 자동 생성 시)
  LIGHTER_API_PRIVATE_KEY    API 개인키 (토큰 자동 생성 시, lighter-sdk 필요)
  LIGHTER_AUTH_TOKEN         직접 준 인증 토큰 (있으면 생성 생략)
  PROBE_TG_TOKEN / PROBE_TG_CHAT_ID   발송 채널(Lighter RH 스택 봇)
  PROBE_INTERVAL_MIN         기본 30. 0이면 1회 실행 후 종료(cron용)
  PROBE_STATE                기본 /data/lit_rh_probe_state.json
  *_ENV 변형: 예) LIGHTER_API_PRIVATE_KEY_ENV=LRH_API_SECRET 이면 LRH_API_SECRET 값을 읽는다.

야간(KST 22~07)에는 발송을 보류했다가 07시 이후 첫 주기에 보낸다.
비밀값은 로그에 찍지 않는다.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

KST = dt.timezone(dt.timedelta(hours=9))
UA = "lit-rh-probe/1.0"
ALLOC_KEYS = ("amount", "allocation", "claimable", "lit", "token", "reward", "airdrop", "total", "quantity")


def env(name: str, default: str = "") -> str:
    alias = os.environ.get(name + "_ENV")
    if alias:
        return os.environ.get(alias, default)
    return os.environ.get(name, default)


def get(url: str, timeout: int = 20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode() or "{}")
        except Exception:  # noqa: BLE001
            body = {}
        return e.code, body


def auth_token() -> str:
    tok = env("LIGHTER_AUTH_TOKEN")
    if tok:
        return tok
    pk, ai, ki = env("LIGHTER_API_PRIVATE_KEY"), env("LIGHTER_ACCOUNT_INDEX"), env("LIGHTER_API_KEY_INDEX")
    if not (pk and ai and ki):
        raise RuntimeError("인증 수단 없음: LIGHTER_AUTH_TOKEN 또는 (API_PRIVATE_KEY·ACCOUNT_INDEX·API_KEY_INDEX)")
    import lighter  # lighter-sdk
    url = env("LIGHTER_API_URL", "https://api.rh.lighter.xyz")
    try:
        client = lighter.SignerClient(url=url, account_index=int(ai), api_private_keys={int(ki): pk})
        token, err = client.create_auth_token_with_expiry(api_key_index=int(ki))
    except TypeError:  # 구버전 SDK 시그니처
        client = lighter.SignerClient(url=url, private_key=pk, account_index=int(ai), api_key_index=int(ki))
        token, err = client.create_auth_token_with_expiry()
    if err:
        raise RuntimeError(f"토큰 생성 실패: {err}")
    return token


def key_paths(obj, prefix="") -> list[str]:
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{prefix}.{k}" if prefix else k
            out.append(p)
            out += key_paths(v, p)
    elif isinstance(obj, list):
        for v in obj[:3]:
            out += key_paths(v, prefix + "[]")
    return sorted(set(out))


def allocation_values(obj, prefix="") -> list[tuple[str, float]]:
    """배정·청구로 보이는 키의 0 아닌 수치."""
    hits = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{prefix}.{k}" if prefix else k
            if isinstance(v, (int, float, str)) and any(a in k.lower() for a in ALLOC_KEYS):
                try:
                    f = float(v)
                    if f > 0:
                        hits.append((p, f))
                except (TypeError, ValueError):
                    pass
            hits += allocation_values(v, p)
    elif isinstance(obj, list):
        for v in obj:
            hits += allocation_values(v, prefix + "[]")
    return hits


def judge(prev: dict, airdrop_status: int, airdrop: dict, points: dict) -> tuple[str, list[str]]:
    """(등급, 근거줄) — 순수 함수."""
    lines = []
    ap = key_paths(airdrop)
    pp = key_paths(points)
    if not prev.get("seeded"):
        return "SEED", []
    alloc = allocation_values(airdrop)
    if airdrop_status == 200 and alloc and not prev.get("alloc_seen"):
        lines = [f"{p} = {v:,.4f}" for p, v in alloc[:4]]
        return "IMMINENT", lines
    if airdrop_status != prev.get("airdrop_status"):
        lines.append(f"청구 API 응답코드 {prev.get('airdrop_status')} → {airdrop_status}")
    new_a = sorted(set(ap) - set(prev.get("airdrop_keys", [])))
    new_p = sorted(set(pp) - set(prev.get("points_keys", [])))
    if new_a:
        lines.append("청구 API 신규 필드: " + ", ".join(new_a[:5]))
    if new_p:
        lines.append("포인트 API 신규 필드: " + ", ".join(new_p[:5]))
    return ("ALERT", lines) if lines else ("OK", [])


def send(text: str) -> bool:
    tok, chat = env("PROBE_TG_TOKEN"), env("PROBE_TG_CHAT_ID")
    if not tok or not chat:
        print("[telegram] 자격증명 없음 — 발송 생략")
        return False
    data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{tok}/sendMessage", data=data)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            print(f"[telegram] 발송 완료 (HTTP {r.status})")
            return True
    except Exception as e:  # noqa: BLE001
        print(f"[telegram] 실패: {type(e).__name__}")
        return False


def _save(path: str, obj: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def cycle(state_path: str) -> int:
    api = env("LIGHTER_API_URL", "https://api.rh.lighter.xyz")
    l1, ai = env("LIGHTER_L1_ADDRESS"), env("LIGHTER_ACCOUNT_INDEX")
    if not l1 or not ai:
        print("LIGHTER_L1_ADDRESS·LIGHTER_ACCOUNT_INDEX 필요")
        return 2
    try:
        prev = json.load(open(state_path, encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        prev = {}
    tok = urllib.parse.quote(auth_token(), safe="")
    s1, air = get(f"{api}/api/v1/airdrop?l1_address={l1}&auth={tok}")
    s2, pts = get(f"{api}/api/v1/livePoints/total?account_index={ai}&auth={tok}")
    if s2 >= 500 or s1 >= 500:
        print(f"[probe] 서버 오류 airdrop={s1} points={s2} — 이번 주기 판정 보류")
        return 0
    if s1 in (401, 403) or s2 in (401, 403):
        # 인증 실패는 스키마 변화로 오인하지 않도록 기준선을 건드리지 않는다
        prev["auth_fail"] = prev.get("auth_fail", 0) + 1
        print(f"[probe] 인증 실패 {prev['auth_fail']}회 (airdrop={s1} points={s2}) — 판정 불가")
        if prev["auth_fail"] == 6 and 7 <= dt.datetime.now(KST).hour <= 21:
            send("라이터RH 청구 API 감시 · 판정 불가\n인증 6회 연속 실패 — 스택 API 키 확인 필요")
        _save(state_path, prev)
        return 1
    prev["auth_fail"] = 0
    grade, lines = judge(prev, s1, air, pts)
    now = int(time.time())
    hour = dt.datetime.fromtimestamp(now, KST).hour
    pending = prev.get("pending")
    if grade in ("IMMINENT", "ALERT"):
        head = "🔴 라이터RH 청구 API · IMMINENT" if grade == "IMMINENT" else "🟡 라이터RH 청구 API · ALERT"
        body = [head, dt.datetime.fromtimestamp(now, KST).strftime("%m-%d %H:%M KST"), ""] + lines
        if grade == "IMMINENT":
            body += ["", "내 계정에 배정 값이 생겼습니다. 청구 화면 확인 필요."]
        else:
            body += ["", "반증: 다음 주기 응답이 원복되면 일시 변경"]
        pending = {"text": "\n".join(body), "id": hashlib.sha1("\n".join(lines).encode()).hexdigest()[:10]}
    if pending and 7 <= hour <= 21 and pending.get("id") != prev.get("sent_id"):
        if send(pending["text"]):
            prev["sent_id"] = pending["id"]
            pending = None
    points_val = None
    for k in ("total", "points", "total_points", "live_points"):
        if isinstance(pts, dict) and k in pts:
            points_val = pts[k]
            break
    new = {
        "seeded": True, "airdrop_status": s1, "airdrop_keys": key_paths(air), "points_keys": key_paths(pts),
        "alloc_seen": prev.get("alloc_seen") or grade == "IMMINENT", "pending": pending,
        "sent_id": prev.get("sent_id"), "last": now, "points": points_val,
        "airdrop_hash": hashlib.sha256(json.dumps(air, sort_keys=True).encode()).hexdigest()[:12],
    }
    _save(state_path, new)
    print(f"[probe] {grade} airdrop={s1} points={s2} pts={points_val}" + (" · 보류 중" if pending else ""))
    return 0


def main():
    state = env("PROBE_STATE", "/data/lit_rh_probe_state.json")
    interval = int(env("PROBE_INTERVAL_MIN", "30") or 30)
    while True:
        try:
            rc = cycle(state)
        except Exception as e:  # noqa: BLE001
            print(f"[probe] 오류: {type(e).__name__}: {str(e)[:160]}")
            rc = 1
        if interval <= 0:
            return rc
        time.sleep(interval * 60)


if __name__ == "__main__":
    sys.exit(main())
