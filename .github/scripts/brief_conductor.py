#!/usr/bin/env python3
"""Brief Conductor — 아침 브리프 지휘자 (2026-11-26).

배경: 이 계정의 GitHub 예약 실행(cron)이 5~10시간씩 밀리고 슬롯 대부분이 빠진다.
  11/24~25 실측: 이 레포는 설정 약 73회/일 중 19~26회만 실행, top-signal 07:48 슬롯이 13~15시에,
  bear-rally 09:07 슬롯이 17~19시에 돎. ai-credit-radar 의 06:07 슬롯도 12:30에 돎.
  → 11/26 13:10 허브가 '베어랠리 레이더 오늘 데이터가 아직 없습니다' 지연 알림을 보냄.

그래서 아침 브리프의 시각은 cron 대신 이 지휘자가 잡는다. 매일 07:41 KST Claude 예약작업이 workflow_dispatch 로 깨우면
  ① 오늘 데이터가 없는 수집기를 바로 돌리고(끝났는데도 오늘 데이터가 아니면 몇 분 뒤 재시도)
  ② 각 소스의 earliest 시각마다 Brief Hub 를 깨워 발송시키고, 늦게 들어온 데이터는 들어오는 즉시 허브를 깨우며
  ③ 09:01 에 베어랠리를 돌리고(크립토 일봉 09:00 마감 뒤)
  ④ 오늘 보낼 daily 소스가 다 나가면 끝낸다(평소 09:07 전후, 월요일 10:01, 상한 10:40).
발송 규칙(요일·시각·신선도)은 brief-hub.yml 의 schedule.json 하나만 기준으로 삼는다.
기존 cron 은 지휘자가 못 돈 날을 위한 예비로 그대로 둔다(수집·발송 모두 멱등).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
REPO = os.environ.get("GITHUB_REPOSITORY") or "jinhae8971/korea-etf-calmar"
HUB_WORKFLOW = "brief-hub.yml"
HUB_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "workflows", "brief-hub.yml")
SENT_PATH = "brief_hub/data/sent.json"

# 소스 id → 이 레포의 수집 워크플로. at = 그 시각(KST) 이후에만 수집, 없으면 지휘자 시작 즉시.
COLLECTORS = {
    "kospi_levels": {"workflow": "kospi-levels.yml"},                   # kospi_wave 도 같은 잡이 만든다
    "topsignal": {"workflow": "top-signal.yml"},
    "btc_macro": {"workflow": "btc-macro.yml"},
    "onchain_liquidity": {"workflow": "onchain-liquidity.yml"},
    "hood_chainrev": {"workflow": "hood-chainrev.yml"},
    "bear_rally": {"workflow": "bear-rally.yml", "at": "09:01", "attempts": 4},   # 일봉 반영이 늦으면 5분 간격 재시도
}
SAME_JOB = {"kospi_wave": "kospi_levels"}
# 다른 레포에서 수집하고 허브가 실행마다 받아 가는 소스 — 여기선 신선도만 본다(수집 깨우기는 Claude 예약작업 몫).
REMOTE = {"ai_credit": "repos/jinhae8971/ai-credit-radar/contents/docs/hub/latest.json?ref=main"}

DEADLINE = "10:40"        # 여기까지 못 끝낸 건 허브의 지연 알림과 기존 cron 에 맡긴다
LATE_GRACE = timedelta(minutes=25)   # 상한 뒤에 시작하면(수동 복구) 시작 후 이만큼만 따라잡기
RETRY_GAP = timedelta(minutes=5)
CHECK_GAP = timedelta(seconds=60)    # 수집 직후 신선도 확인 간격
REMOTE_GAP = timedelta(minutes=5)    # 다른 레포 소스 신선도 확인 간격
SENT_GAP = timedelta(seconds=90)     # 허브를 깨운 뒤 발송 상태를 다시 읽기까지
POLL_SEC = 30


# ---------- 허브와 같은 판정 규칙 ----------
def load_schedule(path: str = HUB_FILE) -> dict:
    text = open(path, encoding="utf-8").read()
    m = re.search(r"cat > brief_hub/schedule\.json <<'JSON_EOF'\n(.*?)\n[ \t]*JSON_EOF", text, re.S)
    if not m:
        sys.exit("brief-hub.yml 에서 schedule.json 을 찾지 못했습니다")
    return json.loads(m.group(1))


def data_date(d: dict | None, key: str) -> str | None:
    """brief-hub.yml 의 data_date 와 같은 정규화: fresh_key 값을 KST 날짜(YYYY-MM-DD)로."""
    if not d:
        return None
    v = d.get(key)
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v, KST).strftime("%Y-%m-%d")
    m = re.match(r"(\d{4}-\d{2}-\d{2})", str(v))
    if not m:
        return None
    s = str(v)
    if "T" in s and ("+" in s[10:] or s.endswith("Z")):
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(KST).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return m.group(1)


def at_time(day: datetime, hhmm: str, sec: int = 0) -> datetime:
    h, m = hhmm.split(":")
    return day.replace(hour=int(h), minute=int(m), second=sec, microsecond=0)


@dataclass
class Plan:
    must_send: list[str]                 # 오늘 꼭 나가야 하는 daily 소스
    checkpoints: list[datetime]          # 허브를 깨울 시각(earliest + 30초)
    earliest: dict[str, datetime]        # 소스별 earliest
    jobs: list["Job"]
    watch: dict[str, dict]               # 신선도를 볼 소스 → schedule 항목


@dataclass
class Job:
    workflow: str
    sources: list[str]
    at: datetime
    max_attempts: int = 3
    attempts: int = 0
    dispatched_at: datetime | None = None
    done: bool = False


def build_plan(cfg: dict, now: datetime) -> Plan:
    """오늘(KST) 지휘 계획. 요일·earliest·모드는 schedule.json 을 그대로 따른다."""
    wd = now.weekday()
    must, cps, earliest, watch = [], set(), {}, {}
    for s in cfg["sources"]:
        mode, sid = s["mode"], s["id"]
        if mode == "outbox":
            continue
        if mode == "daily":
            if wd not in s["days"]:
                continue
            must.append(sid)
        elif mode == "weekly_or_event" and wd != s.get("weekly_day"):
            # 큰 변화(high) 발송은 요일 무관하지만 느린 지표라 그날 다음 허브 실행에 맡긴다 — 수집만 돌린다
            watch[sid] = s
            continue
        # on_change 는 변화가 생기면 어느 요일이든 earliest 뒤에 보낸다
        cps.add(s["earliest"])
        earliest[sid] = at_time(now, s["earliest"])
        watch[sid] = s
    jobs: dict[str, Job] = {}
    for sid in watch:
        owner = SAME_JOB.get(sid, sid)
        c = COLLECTORS.get(owner)
        if not c:
            continue
        j = jobs.get(c["workflow"])
        if j is None:
            j = jobs[c["workflow"]] = Job(c["workflow"], [], at_time(now, c["at"]) if c.get("at") else now,
                                          c.get("attempts", 3))
        j.sources.append(sid)
    return Plan(must, sorted(at_time(now, x, 30) for x in cps), earliest, list(jobs.values()), watch)


# ---------- GitHub API (gh CLI — 러너에선 GH_TOKEN=github.token) ----------
class GhApi:
    def __init__(self, repo: str = REPO, dry: bool = False):
        self.repo, self.dry, self.calls = repo, dry, 0

    def _gh(self, args: list[str]) -> str:
        last = ""
        for i in range(3):
            self.calls += 1
            r = subprocess.run(["gh", "api", *args], capture_output=True, text=True)
            if r.returncode == 0:
                return r.stdout
            last = (r.stderr or r.stdout).strip()[:200]
            if "Not Found" in last or "404" in last:
                raise FileNotFoundError(last)
            time.sleep(3 * (i + 1))
        raise RuntimeError(f"gh api {' '.join(args[:3])} 실패: {last}")

    def get_json(self, api_path: str) -> dict | None:
        try:
            out = self._gh(["-H", "Accept: application/vnd.github.raw", api_path])
            return json.loads(out)
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    def contents(self, path: str) -> dict | None:
        return self.get_json(f"repos/{self.repo}/contents/{path}?ref=main")

    def active(self, workflow: str) -> bool:
        for st in ("in_progress", "queued"):
            out = self._gh([f"repos/{self.repo}/actions/workflows/{workflow}/runs?status={st}&per_page=1"])
            if json.loads(out).get("total_count"):
                return True
        return False

    def dispatch(self, workflow: str) -> None:
        if self.dry:
            return
        self._gh(["-X", "POST", f"repos/{self.repo}/actions/workflows/{workflow}/dispatches", "-f", "ref=main"])


# ---------- 지휘 ----------
class Conductor:
    def __init__(self, cfg: dict, api, now_fn=lambda: datetime.now(KST), sleep_fn=time.sleep, dry: bool = False,
                 out=print):
        self.cfg, self.api, self.now_fn, self.sleep, self.dry, self.out = cfg, api, now_fn, sleep_fn, dry, out
        self.events: list[str] = []

    def log(self, msg: str) -> None:
        line = f"{self.now_fn():%H:%M:%S} {msg}"
        self.events.append(line)
        self.out(line)

    def fresh(self, sid: str, today: str) -> bool:
        src = self.plan.watch[sid]
        d = self.api.get_json(REMOTE[sid]) if sid in REMOTE else self.api.contents(src["path"])
        return data_date(d, src["fresh_key"]) == today

    def sent_today(self, today: str) -> set[str]:
        st = self.api.contents(SENT_PATH) or {}
        return {k for k, v in st.items() if isinstance(v, dict) and v.get("last_key") == today}

    def run(self) -> int:
        start = self.now_fn()
        today = start.strftime("%Y-%m-%d")
        self.plan = p = build_plan(self.cfg, start)
        deadline = max(at_time(start, DEADLINE), start + LATE_GRACE)
        self.log(f"{today}({'월화수목금토일'[start.weekday()]}) 지휘 시작 · 꼭 보낼 것 {p.must_send} · 허브 깨우기 "
                 f"{[c.strftime('%H:%M') for c in p.checkpoints]} · 상한 {deadline:%H:%M}" + (" · DRY RUN" if self.dry else ""))
        fresh = {sid: self.fresh(sid, today) for sid in p.watch}
        sent = self.sent_today(today)
        self.log(f"신선 {sorted(k for k, v in fresh.items() if v)} · 이미 발송 {sorted(sent & set(p.watch))}")
        last_check: dict[str, datetime] = {}
        hub_at: datetime | None = None
        sent_checked: datetime | None = None
        pending_cps = list(p.checkpoints)
        for j in p.jobs:
            j.done = all(fresh[s] for s in j.sources)

        while True:
            now = self.now_fn()
            # ① 수집기: 오늘 데이터가 없고 돌고 있지 않으면 깨운다. 끝났는데도 그대로면 RETRY_GAP 뒤 재시도.
            for j in p.jobs:
                if j.done or now < j.at or j.attempts >= j.max_attempts:
                    continue
                if j.dispatched_at and now - j.dispatched_at < RETRY_GAP:
                    continue
                if self.api.active(j.workflow):
                    continue
                if j.dispatched_at and all(fresh[s] for s in j.sources):
                    j.done = True
                    continue
                self.api.dispatch(j.workflow)
                j.attempts += 1
                j.dispatched_at = now
                self.log(f"수집 실행 {j.workflow} ({j.attempts}/{j.max_attempts}) → {j.sources}")
            # ② 신선도: 방금 수집한 소스(1분 간격)와 다른 레포 소스(earliest 뒤 5분 간격)만 확인한다.
            flipped = []
            for sid in p.watch:
                if fresh[sid] or sid in sent:
                    continue
                job = next((j for j in p.jobs if sid in j.sources), None)
                if job:
                    due = job.dispatched_at and now - job.dispatched_at >= CHECK_GAP
                    gap = CHECK_GAP
                elif sid in REMOTE:
                    due = sid in p.earliest and now >= p.earliest[sid]
                    gap = REMOTE_GAP
                else:
                    due, gap = False, CHECK_GAP
                if not due or (sid in last_check and now - last_check[sid] < gap):
                    continue
                last_check[sid] = now
                if self.fresh(sid, today):
                    fresh[sid] = True
                    flipped.append(sid)
                    if job and all(fresh[s] for s in job.sources):
                        job.done = True
            if flipped:
                self.log(f"데이터 들어옴 {flipped}")
            # ③ 허브: earliest 시각이 됐거나, earliest 가 지난 소스에 데이터가 새로 들어오면 깨운다.
            reached = [c for c in pending_cps if now >= c]
            late = [s for s in flipped if s in p.earliest and now >= p.earliest[s]]
            if reached or late:
                for c in reached:
                    pending_cps.remove(c)
                self.api.dispatch(HUB_WORKFLOW)
                hub_at = now
                why = ", ".join([f"{c:%H:%M} 발송 시각" for c in reached] + [f"{s} 데이터 도착" for s in late])
                self.log(f"허브 실행 ({why})")
            # ④ 끝: 허브 깨우기를 다 했고 꼭 보낼 것이 모두 나갔으면 종료.
            if not pending_cps and hub_at and now - hub_at >= SENT_GAP and \
                    (sent_checked is None or sent_checked < hub_at or now - sent_checked >= timedelta(minutes=2)):
                sent = self.sent_today(today)
                sent_checked = now
                left = [s for s in p.must_send if s not in sent]
                if not left:
                    self.log("오늘 보낼 것 모두 발송 확인 — 종료")
                    return 0
                self.log(f"아직 발송 전 {left}")
            if self.dry:
                self.log("DRY RUN — 지금 시각 기준 판단만 하고 종료")
                return 0
            if now >= deadline:
                left = [s for s in p.must_send if s not in sent]
                self.log(f"상한 {deadline:%H:%M} 도달 — 남은 것 {left} 는 허브 지연 알림·기존 cron 에 맡김")
                return 0
            self.sleep(POLL_SEC)


def summary(events: list[str]) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write("### Brief Conductor\n\n```\n" + "\n".join(events) + "\n```\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="디스패치 없이 지금 시각 기준 판단만 출력")
    a = ap.parse_args()
    api = GhApi(dry=a.dry_run)
    c = Conductor(load_schedule(), api, dry=a.dry_run)
    try:
        return c.run()
    finally:
        c.log(f"API 호출 {api.calls}회")
        summary(c.events)


if __name__ == "__main__":
    sys.exit(main())
