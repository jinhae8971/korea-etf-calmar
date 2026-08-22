# -*- coding: utf-8 -*-
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import alpha_radar as ar  # noqa: E402
import backtest as bt  # noqa: E402
import tracking as tk  # noqa: E402


def series_from(prices, aid_start=0, dates=None):
    """prices: {aid: [close...]} → 백테스트가 쓰는 date-indexed 시리즈"""
    n = max(len(v) for v in prices.values())
    dates = dates or ["2026-01-%02d" % (i + 1) for i in range(n)]
    out = {}
    for aid, cs in prices.items():
        s = {}
        for i, c in enumerate(cs):
            s[dates[i]] = {"o": c, "h": c * 1.01, "l": c * 0.99, "c": c,
                           "qv": 1_000_000.0, "t": 1_700_000_000_000 + i * 86_400_000}
        out[aid] = s
    return out, dates


class TestNoLookahead(unittest.TestCase):
    """가장 중요한 검증: t 시점 점수가 t 이후 가격을 절대 보지 않는다."""

    def test_bars_upto_excludes_future(self):
        s, dates = series_from({"A": [1.0 * (1.01 ** i) for i in range(40)]})
        bars = bt.bars_upto(s["A"], dates, 20)
        self.assertEqual(len(bars), 21)
        self.assertAlmostEqual(bars[-1]["c"], s["A"][dates[20]]["c"])

    def test_score_unchanged_when_future_is_rewritten(self):
        base = {"T%d" % k: [1.0 + 0.01 * k * i for i in range(90)] for k in range(1, 26)}
        s1, dates = series_from(base)
        r1 = bt.score_at(s1, dates, 70)
        tampered = {k: v[:71] + [v[70] * 100] * (len(v) - 71) for k, v in base.items()}
        s2, _ = series_from(tampered, dates=dates)
        r2 = bt.score_at(s2, dates, 70)
        self.assertEqual(len(r1), len(r2))
        self.assertEqual([round(r["score"], 9) for r in sorted(r1, key=lambda x: x["aid"])],
                         [round(r["score"], 9) for r in sorted(r2, key=lambda x: x["aid"])])

    def test_fwd_return_uses_future_only(self):
        s, dates = series_from({"A": [1.0, 1.1, 1.2, 1.5, 2.0]})
        self.assertAlmostEqual(bt.fwd_return(s["A"], dates, 0, 3), 0.5, places=9)
        self.assertIsNone(bt.fwd_return(s["A"], dates, 3, 3))

    def test_fwd_return_carries_last_price_when_delisted(self):
        s, dates = series_from({"A": [1.0] * 6})
        del s["A"][dates[4]]
        del s["A"][dates[5]]
        s["A"][dates[3]]["c"] = 0.5      # 마지막 체결가
        self.assertAlmostEqual(bt.fwd_return(s["A"], dates, 0, 5), -0.5, places=9)

    def test_adv_screen_excludes_illiquid(self):
        s, dates = series_from({"T%d" % k: [1.0 + 0.01 * i for i in range(90)] for k in range(30)})
        for d in s["T0"]:
            s["T0"][d]["qv"] = 1000.0     # 거래대금 미달
        rows = bt.score_at(s, dates, 80)
        self.assertNotIn("T0", [r["aid"] for r in rows])


class TestStats(unittest.TestCase):
    def test_spearman_monotonic(self):
        xs = list(range(20))
        self.assertAlmostEqual(bt.spearman(xs, [x * 2 for x in xs]), 1.0, places=6)
        self.assertAlmostEqual(bt.spearman(xs, [-x for x in xs]), -1.0, places=6)

    def test_spearman_small_sample_guard(self):
        self.assertEqual(bt.spearman([1, 2], [1, 2]), 0.0)

    def test_bootstrap_ci_brackets_mean(self):
        vals = [0.02] * 60
        lo, hi = bt.block_bootstrap_ci(vals, iters=200)
        self.assertAlmostEqual(lo, 0.02, places=6)
        self.assertAlmostEqual(hi, 0.02, places=6)

    def test_bootstrap_ci_includes_zero_for_noise(self):
        import random
        rnd = random.Random(1)
        vals = [rnd.gauss(0, 0.05) for _ in range(120)]
        lo, hi = bt.block_bootstrap_ci(vals, iters=400)
        self.assertLess(lo, 0)
        self.assertGreater(hi, 0)

    def test_summarize_shape(self):
        pd = [{"excess": 0.01 * i} for i in range(-30, 31)]
        s = bt.summarize(pd, "excess")
        self.assertEqual(s["n_dates"], 61)
        self.assertAlmostEqual(s["pct_positive"], 30 / 61, places=6)


class TestVerdict(unittest.TestCase):
    def _hz(self, ex_mean, ci_low, abs_mean, uni_mean=-0.04, ic=0.05):
        return {"14": {
            "excess": {"mean": ex_mean, "ci_low": ci_low, "ci_high": ci_low + 0.04,
                       "median": ex_mean, "pct_positive": 0.6, "n_dates": 200},
            "excess_net": {"mean": ex_mean - 0.01, "ci_low": ci_low - 0.01, "ci_high": ci_low + 0.03,
                           "median": 0, "pct_positive": 0.5, "n_dates": 200},
            "ic": {"mean": ic, "ci_low": 0, "ci_high": 0.1, "median": ic,
                   "pct_positive": 0.6, "n_dates": 200},
            "top_med": {"mean": abs_mean, "ci_low": 0, "ci_high": 0, "median": abs_mean,
                        "pct_positive": 0.5, "n_dates": 200},
            "uni_med": {"mean": uni_mean, "ci_low": 0, "ci_high": 0, "median": uni_mean,
                        "pct_positive": 0.3, "n_dates": 200},
            "hit": {"mean": 0.53, "ci_low": 0.5, "ci_high": 0.56, "median": 0.5,
                    "pct_positive": 1.0, "n_dates": 200}}}

    def test_relative_only_when_absolute_negative(self):
        v, note, h = bt.verdict(self._hz(0.03, 0.01, -0.015))
        self.assertEqual(v, "RELATIVE_ONLY")
        self.assertIn("마이너스", note)

    def test_positive_requires_absolute_gain(self):
        v, _, _ = bt.verdict(self._hz(0.03, 0.01, 0.02))
        self.assertEqual(v, "POSITIVE")

    def test_inconclusive_when_ci_includes_zero(self):
        v, note, _ = bt.verdict(self._hz(0.02, -0.01, 0.01))
        self.assertEqual(v, "INCONCLUSIVE")
        self.assertIn("신뢰구간", note)

    def test_negative_when_no_excess(self):
        v, _, _ = bt.verdict(self._hz(-0.02, -0.05, -0.03))
        self.assertEqual(v, "NEGATIVE")

    def test_unknown_when_empty(self):
        v, _, h = bt.verdict({})
        self.assertEqual(v, "UNKNOWN")
        self.assertIsNone(h)


class TestTracking(unittest.TestCase):
    def _hist(self):
        return [
            {"date": "2026-08-20", "tokens": {"A": {"p": 1.0}, "B": {"p": 2.0}, "C": {"p": 5.0}}},
            {"date": "2026-08-21", "tokens": {"A": {"p": 1.1}, "B": {"p": 1.9}, "C": {"p": 5.0}}},
            {"date": "2026-08-22", "tokens": {"A": {"p": 1.3}, "B": {"p": 1.8}, "C": {"p": 4.5}}},
            {"date": "2026-08-23", "tokens": {"A": {"p": 1.5}, "B": {"p": 1.7}, "C": {"p": 4.0}}},
        ]

    def _promo(self):
        return [{"alpha_id": "A", "symbol": "A", "score": 1.0, "rank": 1,
                 "streak": 2, "s": {"price": 1.0}}]

    def test_excess_is_relative_to_universe(self):
        h = self._hist()
        t = tk.update({"entries": [], "results": {"3": [], "7": [], "14": []}},
                      self._promo(), h[:1], "2026-08-20")
        t = tk.update(t, [], h, "2026-08-23")
        r = t["results"]["3"][0]
        self.assertAlmostEqual(r["ret"], 0.50, places=6)
        self.assertAlmostEqual(r["uni"], -0.15, places=6)
        self.assertAlmostEqual(r["excess"], 0.65, places=6)

    def test_no_duplicate_entry_same_day(self):
        h = self._hist()
        t = tk.update({"entries": [], "results": {"3": [], "7": [], "14": []}},
                      self._promo(), h[:1], "2026-08-20")
        t = tk.update(t, self._promo(), h[:1], "2026-08-20")
        self.assertEqual(len(t["entries"]), 1)

    def test_result_recorded_once_per_horizon(self):
        h = self._hist()
        t = tk.update({"entries": [], "results": {"3": [], "7": [], "14": []}},
                      self._promo(), h[:1], "2026-08-20")
        t = tk.update(t, [], h, "2026-08-23")
        t = tk.update(t, [], h, "2026-08-23")
        self.assertEqual(len(t["results"]["3"]), 1)

    def test_summary_withholds_below_min_sample(self):
        s = tk.summarize({"entries": [], "results": {"3": [{"excess": 0.1, "ret": 0.1}] * 5,
                                                     "7": [], "14": []}})
        self.assertEqual(s["horizons"]["3"]["status"], "표본 부족")
        self.assertNotIn("excess_median", s["horizons"]["3"])

    def test_summary_reports_above_min_sample(self):
        rs = [{"excess": 0.02, "ret": 0.01} for _ in range(25)]
        s = tk.summarize({"entries": [], "results": {"3": rs, "7": [], "14": []}})
        self.assertEqual(s["horizons"]["3"]["status"], "집계")
        self.assertAlmostEqual(s["horizons"]["3"]["excess_median"], 0.02, places=6)
        self.assertEqual(s["horizons"]["3"]["win_rate"], 1.0)

    def test_universe_return_needs_both_dates(self):
        h = self._hist()
        self.assertEqual(tk.universe_return(h, "2026-08-20", "2026-99-99"), (None, 0))


class TestDashboardSections(unittest.TestCase):
    def test_backtest_section_absent_is_graceful(self):
        html = ar.render_backtest_section(None)
        self.assertIn("아직 실행", html)

    def test_backtest_section_renders_verdict(self):
        bt_payload = {
            "verdict": "RELATIVE_ONLY", "verdict_note": "상대우위만",
            "as_of_kst": "2026-08-22 09:00",
            "window": {"first": "2025-11-25", "last": "2026-08-21",
                       "rebalance_days": 245, "tokens_with_history": 529},
            "config": {"round_trip_cost": 0.01},
            "horizons": {"14": {
                "excess": {"mean": 0.0267, "ci_low": 0.0065, "ci_high": 0.047,
                           "median": 0.02, "pct_positive": 0.63, "n_dates": 245},
                "excess_net": {"mean": 0.0167, "ci_low": 0, "ci_high": 0, "median": 0,
                               "pct_positive": 0.5, "n_dates": 245},
                "ic": {"mean": 0.069, "ci_low": 0, "ci_high": 0, "median": 0,
                       "pct_positive": 0.6, "n_dates": 245},
                "top_med": {"mean": -0.0147, "ci_low": 0, "ci_high": 0, "median": 0,
                            "pct_positive": 0.4, "n_dates": 245},
                "uni_med": {"mean": -0.0414, "ci_low": 0, "ci_high": 0, "median": 0,
                            "pct_positive": 0.25, "n_dates": 245},
                "hit": {"mean": 0.53, "ci_low": 0, "ci_high": 0, "median": 0,
                        "pct_positive": 1.0, "n_dates": 245}}},
            "decay": {"3": {"mean": 0.005, "n": 240}, "14": {"mean": 0.027, "n": 240}},
            "monthly": [{"month": "2026-03", "n": 20, "excess_mean": 0.083}],
            "promoted": {"14": {"excess": {"mean": 0.038, "ci_low": 0.013, "ci_high": 0.063,
                                           "median": 0.03, "pct_positive": 0.6, "n_dates": 200},
                                "abs": {"mean": -0.002, "ci_low": 0, "ci_high": 0, "median": 0,
                                        "pct_positive": 0.5, "n_dates": 200}}},
            "breakout": {"14": {"mean": -0.046, "ci_low": 0, "ci_high": 0, "median": 0,
                                "pct_positive": 0.39, "n_dates": 200}},
            "limits": ["<b>주의</b>"],
        }
        html = ar.render_backtest_section(bt_payload)
        self.assertIn("RELATIVE_ONLY", html)
        self.assertIn("유니버스 절대", html)
        self.assertNotIn("<b>주의</b>", html)     # limits 는 이스케이프되어야 함

    def test_tracking_section_shows_sample_shortfall(self):
        html = ar.render_tracking_section({"open_entries": 3, "horizons": {
            "3": {"n": 2, "status": "표본 부족", "need": 18}}})
        self.assertIn("표본 부족", html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
