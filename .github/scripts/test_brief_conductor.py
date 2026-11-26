"""brief_conductor 시뮬레이션 테스트 — 가짜 시계·가짜 GitHub 로 하루 아침을 돌려 본다 (네트워크 없음)."""
import os
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import brief_conductor as bc  # noqa: E402

KST = bc.KST
CFG = bc.load_schedule()
SRC = {s["id"]: s for s in CFG["sources"]}
WF_SOURCES = {c["workflow"]: [k] + [s for s, o in bc.SAME_JOB.items() if o == k] for k, c in bc.COLLECTORS.items()}


class World:
    """가짜 레포: 수집기는 디스패치 1분 뒤 오늘 데이터를 쓰고, 허브는 20초 뒤 earliest 가 지난 신선한 소스를 보낸다."""

    def __init__(self, start: datetime, fresh=(), sent=(), broken=(), bear_ready="09:00"):
        self.now = start
        self.today = start.strftime("%Y-%m-%d")
        self.yday = (start - timedelta(days=1)).strftime("%Y-%m-%d")
        self.files = {s["path"]: {s["fresh_key"]: (self.today if s["id"] in fresh else self.yday)}
                      for s in CFG["sources"] if s["mode"] != "outbox"}
        self.remote = {k: {"as_of_kst": f"{self.today if k in fresh else self.yday} 06:10"} for k in bc.REMOTE}
        self.sent = {sid: {"last_key": self.today} for sid in sent}
        self.broken, self.bear_ready = set(broken), bc.at_time(start, bear_ready)
        self.pending: list[tuple[datetime, str]] = []
        self.dispatches: list[tuple[str, str]] = []
        self.calls = 0

    # 시계
    def clock(self):
        self._apply()
        return self.now

    def sleep(self, sec):
        self.now += timedelta(seconds=sec)

    def _apply(self):
        for t, wf in sorted(self.pending):
            if t > self.now:
                continue
            self.pending.remove((t, wf))
            if wf == bc.HUB_WORKFLOW:
                self._hub(t)
                continue
            for sid in WF_SOURCES[wf]:
                if sid in self.broken:
                    continue
                if sid == "bear_rally" and t < self.bear_ready:
                    continue        # 일봉이 아직 반영 안 됨 → 새 확정 봉 없음
                s = SRC[sid]
                self.files[s["path"]][s["fresh_key"]] = self.today

    def _hub(self, t):
        for s in CFG["sources"]:
            if s["mode"] == "outbox" or t < bc.at_time(t, s["earliest"]):
                continue
            d = self.remote[s["id"]] if s["id"] in bc.REMOTE else self.files[s["path"]]
            if bc.data_date(d, s["fresh_key"]) == self.today and (s["mode"] != "daily" or t.weekday() in s["days"]):
                self.sent[s["id"]] = {"last_key": self.today}

    # GhApi 흉내
    def get_json(self, path):
        self.calls += 1
        self._apply()
        for k in bc.REMOTE:
            if bc.REMOTE[k] == path:
                return self.remote[k]
        return None

    def contents(self, path):
        self.calls += 1
        self._apply()
        if path == bc.SENT_PATH:
            return dict(self.sent)
        return self.files.get(path)

    def active(self, wf):
        self.calls += 1
        return any(w == wf for _, w in self.pending)

    def dispatch(self, wf):
        self.dispatches.append((self.now.strftime("%H:%M:%S"), wf))
        self.pending.append((self.now + timedelta(seconds=20 if wf == bc.HUB_WORKFLOW else 60), wf))


def run_day(start, **kw):
    w = World(start, **kw)
    c = bc.Conductor(CFG, w, now_fn=w.clock, sleep_fn=w.sleep, out=lambda *_: None)
    c.run()
    return w, c


def hub_times(w):
    return [t for t, wf in w.dispatches if wf == bc.HUB_WORKFLOW]


class ScheduleTest(unittest.TestCase):
    def test_parses_hub_schedule(self):
        ids = {s["id"] for s in CFG["sources"]}
        self.assertTrue({"kospi_levels", "topsignal", "btc_macro", "bear_rally", "ai_credit"} <= ids)
        self.assertEqual(SRC["bear_rally"]["fresh_key"], "hub_date")

    def test_data_date(self):
        self.assertEqual(bc.data_date({"a": "2026-11-26 13:13"}, "a"), "2026-11-26")
        self.assertEqual(bc.data_date({"a": "2026-11-25T23:30:00Z"}, "a"), "2026-11-26")
        self.assertEqual(bc.data_date({"a": 1795660800}, "a"), "2026-11-26")
        self.assertIsNone(bc.data_date({"a": None}, "a"))
        self.assertIsNone(bc.data_date(None, "a"))

    def test_plan_thursday(self):
        p = bc.build_plan(CFG, datetime(2026, 11, 26, 7, 41, tzinfo=KST))
        self.assertEqual(set(p.must_send), {"kospi_levels", "kospi_wave", "ai_credit", "topsignal", "btc_macro", "bear_rally"})
        self.assertEqual([c.strftime("%H:%M:%S") for c in p.checkpoints],
                         ["08:00:30", "08:10:30", "08:30:30", "08:40:30", "08:50:30", "09:05:30"])
        self.assertIn("hood-chainrev.yml", [j.workflow for j in p.jobs])        # 수집은 매일
        bear = next(j for j in p.jobs if j.workflow == "bear-rally.yml")
        self.assertEqual(bear.at.strftime("%H:%M"), "09:01")

    def test_plan_sunday_monday(self):
        sun = bc.build_plan(CFG, datetime(2026, 11, 29, 7, 41, tzinfo=KST))
        self.assertEqual(set(sun.must_send), {"topsignal", "bear_rally"})
        mon = bc.build_plan(CFG, datetime(2026, 11, 30, 7, 41, tzinfo=KST))
        self.assertEqual(mon.checkpoints[-1].strftime("%H:%M:%S"), "10:00:30")   # 체인 수익 순위 월요일 정기


class DayTest(unittest.TestCase):
    def test_normal_thursday(self):
        w, c = run_day(datetime(2026, 11, 26, 7, 41, tzinfo=KST), fresh={"ai_credit"})
        collects = {wf for t, wf in w.dispatches if t.startswith("07:41") and wf != bc.HUB_WORKFLOW}
        self.assertEqual(collects, {"kospi-levels.yml", "top-signal.yml", "btc-macro.yml",
                                    "onchain-liquidity.yml", "hood-chainrev.yml"})
        self.assertIn(("09:01:00", "bear-rally.yml"), w.dispatches)
        self.assertEqual(hub_times(w), ["08:00:30", "08:10:30", "08:30:30", "08:40:30", "08:50:30", "09:05:30"])
        self.assertTrue(set(bc.build_plan(CFG, w.now).must_send) <= set(w.sent))
        self.assertLess(w.now, datetime(2026, 11, 26, 9, 10, tzinfo=KST))           # 다 보내면 바로 끝
        self.assertLess(w.calls, 150)

    def test_monday_waits_for_chainrev(self):
        w, _ = run_day(datetime(2026, 11, 30, 7, 41, tzinfo=KST), fresh={"ai_credit"})
        self.assertEqual(hub_times(w)[-1], "10:00:30")
        self.assertIn("hood_chainrev", w.sent)

    def test_late_start_everything_done(self):
        everything = {s["id"] for s in CFG["sources"]}
        w, _ = run_day(datetime(2026, 11, 26, 13, 40, tzinfo=KST), fresh=everything, sent=everything)
        self.assertEqual([wf for _, wf in w.dispatches], [bc.HUB_WORKFLOW])            # 수집 없음, 허브 한 번
        self.assertLess(w.now, datetime(2026, 11, 26, 13, 45, tzinfo=KST))

    def test_late_start_catches_up(self):
        w, _ = run_day(datetime(2026, 11, 26, 13, 40, tzinfo=KST), fresh={"ai_credit"})
        self.assertTrue(set(bc.build_plan(CFG, w.now).must_send) <= set(w.sent))
        self.assertLess(w.now, datetime(2026, 11, 26, 13, 50, tzinfo=KST))

    def test_bear_rally_bar_late(self):
        w, _ = run_day(datetime(2026, 11, 26, 7, 41, tzinfo=KST), fresh={"ai_credit"}, bear_ready="09:04")
        bears = [t for t, wf in w.dispatches if wf == "bear-rally.yml"]
        self.assertEqual(bears, ["09:01:00", "09:06:00"])                              # 첫 회 새 봉 없음 → 5분 뒤 재시도
        self.assertIn("bear_rally", w.sent)
        self.assertIn("09:07:00", hub_times(w))                                         # 데이터 도착 즉시 허브

    def test_ai_credit_arrives_late(self):
        w = World(datetime(2026, 11, 26, 7, 41, tzinfo=KST))
        c = bc.Conductor(CFG, w, now_fn=w.clock, sleep_fn=w.sleep, out=lambda *_: None)
        orig = w.sleep

        def sleep(sec):
            orig(sec)
            if w.now >= datetime(2026, 11, 26, 8, 22, tzinfo=KST):
                w.remote["ai_credit"]["as_of_kst"] = w.today + " 08:20"
        c.sleep = sleep
        c.run()
        self.assertIn("ai_credit", w.sent)
        self.assertTrue(any("08:2" in t for t in hub_times(w)))                       # 5분 간격 확인 → 도착 뒤 허브

    def test_broken_collector_gives_up(self):
        w, _ = run_day(datetime(2026, 11, 26, 7, 41, tzinfo=KST), fresh={"ai_credit"}, broken={"topsignal"})
        self.assertEqual(len([1 for _, wf in w.dispatches if wf == "top-signal.yml"]), 3)
        self.assertNotIn("topsignal", w.sent)
        self.assertEqual(w.now.strftime("%H:%M"), "10:40")                             # 상한에서 멈춤
        self.assertLess(w.calls, 400)


if __name__ == "__main__":
    unittest.main(verbosity=1)
