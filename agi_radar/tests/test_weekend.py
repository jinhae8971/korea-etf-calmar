import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import history, render, weekly  # noqa: E402


def make_report(close, state="THESIS_INTACT", hedge_status="OK", corr=-0.1,
                crowd=50.0, breadth=0.8, top="연산", alerts=None):
    return {
        "date": "2026-08-16",
        "as_of_close": close,
        "verdict": {"final_state": state, "confidence_score": 70},
        "rule_verdict": {"score": 60.0, "alerts": alerts or []},
        "hedge": {"status": hedge_status, "corr20": corr, "spread_vol_ratio": 1.5,
                  "both_legs_lose": False, "spread_return": -0.02},
        "crowding": {"score": crowd, "level": "NORMAL",
                     "cutoffs": {"watch": 62.0, "alert": 68.0}},
        "funding": {"level": "NORMAL", "values": {"hy_oas": {"value": 3.2}}},
        "breadth": {"ratio": breadth, "leading": 4, "total": 5},
        "nodes": [{"id": "a", "label": top, "role": "long", "rank": 1, "long_rank": 1,
                   "rank_delta": 2, "rs20": 0.05, "rs60": 0.08}],
    }


class TestHistory(unittest.TestCase):
    def test_append_and_dedupe(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "h.json")
            history.append(path, make_report("2026-08-10"))
            history.append(path, make_report("2026-08-11"))
            # 같은 종가일 재실행 → 교체되어야 하고 중복이 쌓이면 안 된다
            history.append(path, make_report("2026-08-11", crowd=55.0))
            records = history.load(path)
            self.assertEqual(len(records), 2)
            self.assertEqual(records[-1]["crowd"], 55.0)

    def test_sorted_by_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "h.json")
            history.append(path, make_report("2026-08-12"))
            history.append(path, make_report("2026-08-10"))
            records = history.load(path)
            self.assertEqual([r["d"] for r in records], ["2026-08-10", "2026-08-12"])

    def test_missing_close_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "h.json")
            report = make_report("2026-08-10")
            report["as_of_close"] = None
            self.assertEqual(history.append(path, report), [])

    def test_corrupt_file_recovers(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "h.json")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("{not json")
            history.append(path, make_report("2026-08-10"))
            self.assertEqual(len(history.load(path)), 1)


class TestWeeklyReview(unittest.TestCase):
    def _hist(self):
        return [history.compact(make_report(f"2026-08-{d:02d}")) for d in range(10, 16)]

    def test_unavailable_when_empty(self):
        out = weekly.weekly_review([], make_report("2026-08-15"))
        self.assertFalse(out["available"])

    def test_unavailable_with_single_record(self):
        out = weekly.weekly_review(self._hist()[:1], make_report("2026-08-15"))
        self.assertFalse(out["available"])

    def test_span_and_counts(self):
        records = self._hist()
        records[2]["hedge"] = "BROKEN"
        records[3]["hedge"] = "BROKEN"
        records[4]["alerts"] = ["HEDGE_BROKEN"]
        out = weekly.weekly_review(records, make_report("2026-08-15"))
        self.assertTrue(out["available"])
        self.assertEqual(out["span"]["sessions"], 5)
        self.assertEqual(out["hedge"]["broken_days"], 2)
        self.assertEqual(dict(out["alerts"])["HEDGE_BROKEN"], 1)

    def test_state_change_detected(self):
        records = self._hist()
        records[0]["state"] = "THESIS_BROKEN"
        out = weekly.weekly_review(records, make_report("2026-08-15"))
        self.assertIsNotNone(out["state"]["changed"])
        self.assertEqual(out["state"]["changed"]["from"], "THESIS_BROKEN")


class TestWatchlist(unittest.TestCase):
    TH = {"hedge": {"corr20_broken": -0.40}, "funding": {"hy_oas_watch": 4.0},
          "breadth": {"intact": 0.60, "broken": 0.25}}

    def test_breached_correlation_is_armed(self):
        report = make_report("2026-08-15", corr=-0.55)
        out = weekly.watchlist(report, self.TH)
        corr_item = next(i for i in out["items"] if "상관" in i["name"])
        self.assertTrue(corr_item["breached"])
        self.assertTrue(corr_item["armed"])
        self.assertIsNotNone(corr_item["note"])

    def test_safe_correlation_not_armed(self):
        report = make_report("2026-08-15", corr=0.3)
        out = weekly.watchlist(report, self.TH)
        corr_item = next(i for i in out["items"] if "상관" in i["name"])
        self.assertFalse(corr_item["breached"])
        self.assertFalse(corr_item["armed"])

    def test_armed_count(self):
        out = weekly.watchlist(make_report("2026-08-15", corr=-0.9), self.TH)
        self.assertGreaterEqual(out["armed_count"], 1)


class TestWeekendRender(unittest.TestCase):
    def test_weekly_unavailable_message(self):
        msg = render.render_weekly(make_report("2026-08-15"), {"available": False, "reason": "이력 없음"})
        self.assertIn("이력 없음", msg)
        self.assertIn("주간 리뷰", msg)

    def test_weekly_full_message(self):
        records = [history.compact(make_report(f"2026-08-{d:02d}")) for d in range(10, 16)]
        review = weekly.weekly_review(records, make_report("2026-08-15"))
        msg = render.render_weekly(make_report("2026-08-15"), review, "https://x.io")
        self.assertIn("주간 리뷰", msg)
        self.assertIn("매매 권유가 아닙니다", msg)
        self.assertIn("https://x.io", msg)

    def test_watchlist_marks_breach(self):
        report = make_report("2026-08-15", corr=-0.55)
        watch = weekly.watchlist(report, TestWatchlist.TH)
        msg = render.render_watchlist(report, watch)
        self.assertIn("이미 통과", msg)
        self.assertIn("워치리스트", msg)

    def test_weekend_escapes_html(self):
        report = make_report("2026-08-15", top="<script>x</script>")
        records = [history.compact(make_report(f"2026-08-{d:02d}", top="<script>x</script>"))
                   for d in range(10, 16)]
        review = weekly.weekly_review(records, report)
        msg = render.render_weekly(report, review)
        self.assertNotIn("<script>", msg)


if __name__ == "__main__":
    unittest.main()
