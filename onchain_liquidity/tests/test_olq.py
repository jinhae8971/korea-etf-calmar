# -*- coding: utf-8 -*-
import datetime as dt
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import olq  # noqa: E402


def series(n, start, step=0.0, t0=1_700_000_000):
    return [(t0 + 86400 * i, start + step * i) for i in range(n)]


def price_map(days, start, step=0.0, anchor="2026-09-07"):
    a = dt.datetime.strptime(anchor, "%Y-%m-%d")
    return {(a - dt.timedelta(days=days - 1 - i)).strftime("%Y-%m-%d"): start + step * i
            for i in range(days)}


class TestMath(unittest.TestCase):
    def test_pct(self):
        self.assertAlmostEqual(olq.pct(110, 100), 10.0)
        self.assertAlmostEqual(olq.pct(90, 100), -10.0)
        self.assertEqual(olq.pct(100, 0), 0.0)

    def test_robust_z_uses_median_not_mean(self):
        # 과거 스파이크 1회가 기준선을 부풀리면 안 된다
        base = [10.0] * 29 + [1000.0]
        z_spiked = olq.robust_z(base, 10.0)
        self.assertIsNotNone(z_spiked)
        self.assertLess(abs(z_spiked), 0.5)

    def test_robust_z_short_history_returns_none(self):
        self.assertIsNone(olq.robust_z([1.0] * 5, 1.0))

    def test_robust_z_capped(self):
        z = olq.robust_z([10.0, 10.5, 9.5] * 10, 1e9)
        self.assertLessEqual(z, 3.0)

    def test_robust_z_zero_scale(self):
        self.assertEqual(olq.robust_z([5.0] * 30, 5.0), 0.0)


class TestCompute(unittest.TestCase):
    def _run(self, stable, tvl, px, dex):
        return olq.compute(stable, [], [], tvl, px, dex)

    def test_price_neutral_removes_price_effect(self):
        # TVL이 10% 올랐지만 담보 가격도 10% 올랐다면 실질 예치 변화는 0
        tvl = series(40, 100.0)
        for i in range(33, 40):
            tvl[i] = (tvl[i][0], 110.0)
        days = [olq.day_of(t) for t, _ in tvl]
        px = {d: 100.0 for d in days}
        for d in days[-7:]:
            px[d] = 110.0
        m = self._run(series(40, 1000.0), tvl, px, series(40, 5.0))
        self.assertIsNotNone(m["tvl"]["real7"])
        self.assertLess(abs(m["tvl"]["real7"]), 0.5)

    def test_price_neutral_detects_real_outflow(self):
        # 가격은 그대로인데 TVL만 20% 빠지면 실질 이탈로 잡혀야 한다
        tvl = series(40, 100.0)
        for i in range(33, 40):
            tvl[i] = (tvl[i][0], 80.0)
        px = {olq.day_of(t): 100.0 for t, _ in tvl}
        m = self._run(series(40, 1000.0), tvl, px, series(40, 5.0))
        self.assertLess(m["tvl"]["real7"], -15.0)

    def test_missing_price_series_yields_none_not_crash(self):
        m = self._run(series(40, 1000.0), series(40, 100.0), {}, series(40, 5.0))
        self.assertIsNone(m["tvl"]["real7"])

    def test_stable_windows(self):
        s = series(40, 100.0, step=1.0)
        m = self._run(s, series(40, 100.0), {}, series(40, 5.0))
        self.assertAlmostEqual(m["stable"]["net1"], 1.0, places=6)
        self.assertAlmostEqual(m["stable"]["net7"], 7.0, places=6)
        self.assertAlmostEqual(m["stable"]["net30"], 30.0, places=6)


class TestJudge(unittest.TestCase):
    def _m(self, s7=0.0, s1=0.0, real7=0.0, z=0.0):
        return {
            "stable": {"d1": s1, "d7": s7, "d30": 0.0, "level": 3e11,
                       "net1": -1e9, "net7": 0.0, "net30": 0.0},
            "tvl": {"real7": real7, "d7": 0.0, "d30": 0.0, "d1": 0.0,
                    "level": 8e10, "px7": 0.0, "real30": None, "px_anchor": None},
            "dex": {"z": z, "level": 1e10, "avg7": 1e10, "d7": 0.0},
            "chains": [], "assets": [],
        }

    def test_neutral(self):
        self.assertEqual(olq.judge(self._m())[0], "NEUTRAL")

    def test_critical_needs_two_hits(self):
        one = olq.judge(self._m(s7=-2.0))[0]
        two = olq.judge(self._m(s7=-2.0, real7=-20.0))[0]
        self.assertEqual(one, "CONTRACT")
        self.assertEqual(two, "CRITICAL")

    def test_expansion(self):
        self.assertEqual(olq.judge(self._m(s7=2.0, real7=10.0))[0], "EXPAND")

    def test_soften_on_dex_only(self):
        self.assertEqual(olq.judge(self._m(z=-1.4))[0], "SOFTEN")

    def test_reasons_never_empty(self):
        self.assertTrue(olq.judge(self._m())[1])

    def test_none_z_does_not_crash(self):
        self.assertEqual(olq.judge(self._m(z=None))[0], "NEUTRAL")


class TestStreakGate(unittest.TestCase):
    """하루 반짝은 경보로 승격하지 않는다 (narrative-radar v4 오탐 차단 원칙)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "state.json")

    def test_first_day_alert_is_demoted(self):
        state, streak, demoted = olq.apply_streak("CRITICAL", self.path)
        self.assertEqual(state, "SOFTEN")
        self.assertEqual(streak, 1)
        self.assertTrue(demoted)

    def test_second_day_alert_promotes(self):
        olq.apply_streak("CRITICAL", self.path, day="2026-09-08")
        state, streak, demoted = olq.apply_streak("CRITICAL", self.path, day="2026-09-09")
        self.assertEqual(state, "CRITICAL")
        self.assertEqual(streak, 2)
        self.assertFalse(demoted)

    def test_streak_resets_on_state_change(self):
        olq.apply_streak("CRITICAL", self.path, day="2026-09-08")
        olq.apply_streak("CRITICAL", self.path, day="2026-09-09")
        _, streak, _ = olq.apply_streak("NEUTRAL", self.path, day="2026-09-10")
        self.assertEqual(streak, 1)

    def test_same_day_rerun_does_not_increment(self):
        """같은 날 두 번 돌려도 2일 게이트가 뚫리면 안 된다."""
        s1, k1, d1 = olq.apply_streak("CRITICAL", self.path, day="2026-09-08")
        s2, k2, d2 = olq.apply_streak("CRITICAL", self.path, day="2026-09-08")
        self.assertEqual((s1, k1, d1), (s2, k2, d2))
        self.assertEqual(k2, 1)
        self.assertTrue(d2)

    def test_next_day_after_rerun_promotes_once(self):
        olq.apply_streak("CRITICAL", self.path, day="2026-09-08")
        olq.apply_streak("CRITICAL", self.path, day="2026-09-08")
        state, streak, _ = olq.apply_streak("CRITICAL", self.path, day="2026-09-09")
        self.assertEqual((state, streak), ("CRITICAL", 2))

    def test_non_alert_states_not_demoted(self):
        state, _, demoted = olq.apply_streak("EXPAND", self.path)
        self.assertEqual(state, "EXPAND")
        self.assertFalse(demoted)

    def test_corrupt_state_file_recovers(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{not json")
        state, streak, _ = olq.apply_streak("NEUTRAL", self.path)
        self.assertEqual(streak, 1)


class TestRender(unittest.TestCase):
    def _full(self):
        return {
            "stable": {"level": 3.12e11, "d1": 0.0, "d7": 0.55, "d30": 1.44,
                       "net1": 1e7, "net7": 1.7e9, "net30": 4.4e9},
            "tvl": {"level": 8.8e10, "d1": 0.2, "d7": 3.3, "d30": 19.2,
                    "real7": -0.06, "real30": 2.0, "px7": 2.0, "px_anchor": "2026-09-06"},
            "dex": {"level": 1.03e10, "avg7": 9.5e9, "z": 0.43, "d7": 1.0},
            "chains": [{"chain": "Solana", "cur": 1.6e10, "p1": 0.3,
                        "p7": 4.86, "abs7": 7.7e8}],
            "assets": [{"symbol": "USDT", "cur": 1.8e11, "p1": 0.0,
                        "p7": 0.0, "p30": 0.1, "net7": 1e7}],
            "stable_series": [("2026-09-0%d" % i, 3e11 + i) for i in range(1, 8)],
            "tvl_series": [("2026-09-0%d" % i, 8e10 + i) for i in range(1, 8)],
            "dex_series": [("2026-09-0%d" % i, 1e10 + i) for i in range(1, 8)],
        }

    def test_telegram_has_emphasis_header(self):
        msg = olq.render_telegram(self._full(), "CRITICAL", ["r"], 3, False, False, "2026-09-08")
        self.assertIn("━━━", msg)
        self.assertIn("온체인 유동성", msg)
        self.assertLess(len(msg), 4000)  # 텔레그램 단일 메시지 한도

    def test_degraded_and_demoted_banners(self):
        msg = olq.render_telegram(self._full(), "SOFTEN", ["r"], 1, True, True, "2026-09-08")
        self.assertIn("DEGRADED", msg)
        self.assertIn("1일차", msg)

    def test_dashboard_is_selfcontained(self):
        html = olq.render_dashboard(self._full(), "NEUTRAL", ["r"], 2, False, "2026-09-08")
        self.assertIn("<!DOCTYPE html>", html)
        self.assertNotIn("<script", html.lower())      # 외부 스크립트 0
        self.assertNotIn("http://", html)
        self.assertIn("관측기이며 예측기가 아닙니다", html)

    def test_dashboard_escapes_injection(self):
        m = self._full()
        m["chains"][0]["chain"] = "<script>alert(1)</script>"
        html = olq.render_dashboard(m, "NEUTRAL", ["<img onerror=x>"], 1, False, "2026-09-08")
        self.assertNotIn("<script>alert", html)
        self.assertNotIn("<img onerror", html)

    def test_failure_message_never_says_all_clear(self):
        msg = olq.render_failure(RuntimeError("boom"), "2026-09-08")
        self.assertIn("판정 불가", msg)
        self.assertIn("아닙니다", msg)

    def test_money_formats(self):
        self.assertEqual(olq.money(1.5e12), "$1.50T")
        self.assertEqual(olq.money(2.5e9), "$2.50B")
        self.assertEqual(olq.money(-3e6), "$-3M")


class TestIO(unittest.TestCase):
    def test_atomic_save_and_load(self):
        tmp = tempfile.mkdtemp()
        p = os.path.join(tmp, "sub", "x.json")
        olq.save_json(p, {"a": 1})
        self.assertEqual(olq.load_json(p), {"a": 1})
        self.assertFalse(os.path.exists(p + ".tmp"))

    def test_load_missing_returns_default(self):
        self.assertEqual(olq.load_json("/nonexistent/x.json", []), [])

    def test_thresholds_frozen(self):
        # 성과를 보고 임계를 조정하면 사후편향이다. 변경 시 고정일 갱신 필수.
        self.assertEqual(olq.THRESHOLDS_FROZEN_AT, "2026-09-08")


if __name__ == "__main__":
    unittest.main(verbosity=2)
