import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import metrics, render, verdict_rules  # noqa: E402


def series_from(prices, start_day=1):
    return {f"2026-01-{start_day + i:02d}": p for i, p in enumerate(prices)}


class TestStats(unittest.TestCase):
    def test_correlation_perfect(self):
        xs = [0.01, -0.02, 0.03, 0.01, -0.01, 0.02]
        self.assertAlmostEqual(metrics.correlation(xs, xs), 1.0, places=6)

    def test_correlation_inverse(self):
        xs = [0.01, -0.02, 0.03, 0.01, -0.01, 0.02]
        self.assertAlmostEqual(metrics.correlation(xs, [-x for x in xs]), -1.0, places=6)

    def test_correlation_short_input(self):
        self.assertEqual(metrics.correlation([0.1], [0.2]), 0.0)

    def test_stdev_constant(self):
        self.assertEqual(metrics.stdev([0.5] * 10), 0.0)

    def test_beta_identity(self):
        xs = [0.01, -0.02, 0.03, 0.01, -0.01]
        self.assertAlmostEqual(metrics.beta(xs, xs), 1.0, places=6)

    def test_cumulative(self):
        self.assertAlmostEqual(metrics.cumulative([0.1, 0.1]), 0.21, places=9)

    def test_percentile(self):
        self.assertEqual(metrics.percentile([1, 2, 3, 4, 5], 0.0), 1)
        self.assertEqual(metrics.percentile([1, 2, 3, 4, 5], 1.0), 5)
        self.assertEqual(metrics.percentile([], 0.5), 0.0)

    def test_clamp(self):
        self.assertEqual(metrics.clamp(-5), 0.0)
        self.assertEqual(metrics.clamp(500), 100.0)


class TestReturns(unittest.TestCase):
    def test_to_returns_handles_gaps(self):
        dates = ["2026-01-01", "2026-01-02", "2026-01-03"]
        series = {"2026-01-01": 100.0, "2026-01-03": 110.0}
        rets = metrics.to_returns(series, dates)
        self.assertEqual(len(rets), 2)
        self.assertEqual(rets[0], 0.0)  # 결측일은 0으로 흡수

    def test_basket_skips_missing_tickers(self):
        dates = ["2026-01-01", "2026-01-02"]
        smap = {"A": {"2026-01-01": 100.0, "2026-01-02": 110.0}}
        out = metrics.basket_returns(smap, ["A", "MISSING"], dates)
        self.assertAlmostEqual(out[0], 0.1, places=9)

    def test_basket_all_missing(self):
        out = metrics.basket_returns({}, ["X"], ["2026-01-01", "2026-01-02"])
        self.assertEqual(out, [0.0])


class TestHedge(unittest.TestCase):
    def test_no_data(self):
        out = metrics.hedge_efficacy([0.01] * 5, [0.01] * 5)
        self.assertEqual(out["status"], "NO_DATA")

    def test_both_legs_lose_flags_broken(self):
        """롱은 내리고 숏 다리는 오르는 SA 패턴은 BROKEN 이어야 한다."""
        long_r = [-0.02, 0.01] * 30
        short_r = [0.02, -0.01] * 30
        out = metrics.hedge_efficacy(long_r, short_r, 20, {"corr20_broken": -0.40})
        self.assertTrue(out["both_legs_lose"])
        self.assertEqual(out["status"], "BROKEN")

    def test_aligned_legs_not_broken(self):
        """롱·숏이 같이 움직이면 헤지가 작동 중이므로 BROKEN 이 아니어야 한다."""
        base = [0.01, -0.015, 0.02, -0.005] * 20
        out = metrics.hedge_efficacy(base, base, 20, {})
        self.assertNotEqual(out["status"], "BROKEN")

    def test_base_rate_present(self):
        out = metrics.hedge_efficacy([0.01, -0.01] * 40, [0.005, -0.005] * 40, 20, {})
        self.assertIsNotNone(out["base_rate_broken_1y"])
        self.assertGreaterEqual(out["base_rate_broken_1y"], 0.0)
        self.assertLessEqual(out["base_rate_broken_1y"], 1.0)


class TestCrowding(unittest.TestCase):
    def test_insufficient_data(self):
        out = metrics.crowding_index([0.01] * 10, [0.01] * 10, 0.5)
        self.assertEqual(out["level"], "NO_DATA")

    def test_score_bounded(self):
        import random

        random.seed(7)
        long_r = [random.gauss(0.001, 0.02) for _ in range(300)]
        bench_r = [random.gauss(0.0005, 0.01) for _ in range(300)]
        out = metrics.crowding_index(long_r, bench_r, 0.65)
        self.assertGreaterEqual(out["score"], 0.0)
        self.assertLessEqual(out["score"], 100.0)
        self.assertIn(out["level"], ("NORMAL", "WATCH", "ALERT"))
        self.assertIn("cutoffs", out)


class TestNodes(unittest.TestCase):
    def _fixture(self):
        dates = [f"2026-01-{i:02d}" for i in range(1, 100)]
        up = {d: 100 * (1.01 ** i) for i, d in enumerate(dates)}
        flat = {d: 100.0 for d in dates}
        smap = {"UP": up, "FLAT": flat, "SPY": flat}
        nodes = [
            {"id": "a", "label": "A", "role": "long", "stage": 1, "tickers": ["UP"]},
            {"id": "b", "label": "B", "role": "long", "stage": 2, "tickers": ["FLAT"]},
        ]
        return smap, nodes, dates

    def test_ranking_and_breadth(self):
        smap, nodes, dates = self._fixture()
        ranked = metrics.node_strength(smap, nodes, dates, "SPY")
        self.assertEqual(ranked[0]["id"], "a")
        self.assertEqual(ranked[0]["rank"], 1)
        info = metrics.breadth(ranked)
        self.assertEqual(info["leading"], 1)
        self.assertEqual(info["total"], 2)

    def test_bottleneck_shift_shape(self):
        smap, nodes, dates = self._fixture()
        ranked = metrics.node_strength(smap, nodes, dates, "SPY")
        shift = metrics.bottleneck_shift(ranked)
        self.assertIn("shifted", shift)

    def test_breadth_empty(self):
        self.assertIsNone(metrics.breadth([])["ratio"])


class TestVerdict(unittest.TestCase):
    TH = {"breadth": {"intact": 0.6, "broken": 0.25}, "crowding": {}, "funding": {}, "hedge": {}}

    def test_intact_path(self):
        out = verdict_rules.evaluate(
            [], {"ratio": 0.8, "leading": 4, "total": 5},
            {"status": "OK"}, {"level": "NORMAL", "score": 40},
            {"level": "NORMAL"}, self.TH,
        )
        self.assertEqual(out["state"], "THESIS_INTACT")

    def test_broken_path(self):
        out = verdict_rules.evaluate(
            [], {"ratio": 0.1, "leading": 0, "total": 5},
            {"status": "BROKEN", "spread_vol_ratio": 1.7, "corr20": -0.7},
            {"level": "ALERT", "score": 90}, {"level": "ALERT"}, self.TH,
        )
        self.assertEqual(out["state"], "THESIS_BROKEN")
        codes = {a["code"] for a in out["alerts"]}
        self.assertIn("HEDGE_BROKEN", codes)
        self.assertIn("FUNDING_STRESS", codes)


class TestRender(unittest.TestCase):
    def test_render_escapes_and_includes_disclaimer(self):
        report = {
            "date": "2026-08-16",
            "nodes": [{"label": "<b>주입</b>", "role": "long", "rank": 1,
                       "rank_delta": 0, "rs20": 0.05}],
            "hedge": {"status": "BROKEN", "corr20": -0.7, "spread_vol_ratio": 1.7,
                      "long_return_20d": -0.1, "short_return_20d": 0.05, "spread_return": -0.15},
            "crowding": {"score": 70.0, "level": "WATCH"},
            "funding": {"level": "NORMAL", "values": {}},
            "rule_verdict": {"alerts": [{"severity": "HIGH", "text": "테스트 경보", "code": "X"}]},
            "verdict": {"final_state": "THESIS_BROKEN", "confidence_score": 70,
                        "summary": "요약", "key_insights": ["a"], "action_items": ["b"], "llm": False},
            "data_status": {"mode": "OK"},
            "bottleneck_shift": {"shifted": False},
        }
        msg = render.render_brief(report, "https://example.com")
        self.assertIn("매매 권유가 아닙니다", msg)
        self.assertNotIn("<b>주입</b>", msg.replace("<b>AGI", ""))
        self.assertIn("&lt;b&gt;", msg)


if __name__ == "__main__":
    unittest.main()
