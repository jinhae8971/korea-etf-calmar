import json, os, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import optical_radar as R


def mk(closes, vol=1000.0):
    return [{"d": f"2026-{(i//28)+1:02d}-{(i%28)+1:02d}", "o": c, "h": c*1.01, "l": c*0.99, "c": c, "v": vol}
            for i, c in enumerate(closes)]


class Indicators(unittest.TestCase):
    def test_rsi_bounds(self):
        self.assertEqual(R.rsi([100 + i for i in range(40)]), 100.0)
        self.assertAlmostEqual(R.rsi([100 - i for i in range(40)]), 0.0, places=6)
        self.assertIsNone(R.rsi([1, 2, 3]))

    def test_returns(self):
        c = [100.0] * 30 + [110.0]
        self.assertAlmostEqual(R.ret_n(c, 1), 10.0)
        self.assertAlmostEqual(R.ret_n(c, 21), 10.0)
        self.assertIsNone(R.ret_n(c, 100))

    def test_basket_index_equal_weight(self):
        idx = R.basket_index({"A": mk([100, 110, 121]), "B": mk([100, 90, 81])}, ["A", "B"])
        self.assertAlmostEqual(idx[-1][1], 100.0)

    def test_rs_line_and_stats(self):
        a = [(f"d{i:03d}", 100 * 1.01 ** i) for i in range(300)]
        b = [(f"d{i:03d}", 100.0) for i in range(300)]
        st = R.rs_stats(R.rs_line(a, b))
        self.assertGreater(st["rs_1m"], 0)
        self.assertEqual(st["rs_pct_1y"], 100.0)

    def test_flow_metrics(self):
        bars = mk([100 + i for i in range(80)])
        self.assertAlmostEqual(R.up_volume_ratio(bars), 100.0)
        self.assertEqual(R.mfi(bars), 100.0)
        self.assertAlmostEqual(R.obv_slope(bars), 20.0)
        self.assertEqual(R.consecutive_up([1, 2, 3, 2, 3, 4]), 2)

    def test_percentile(self):
        self.assertIsNone(R.percentile_rank(list(range(10)), 5))
        self.assertEqual(R.percentile_rank(list(range(100)), 1000), 100.0)


class Gauge(unittest.TestCase):
    def synth(self, closes, vol=1000.0):
        return R.symbol_metrics("X", mk(closes, vol))

    def test_gauge_range_and_regime(self):
        cool = self.synth([100 - i * 0.2 for i in range(260)])
        g = R.overheat_gauge([cool], {"rs_pct_1y": 10}, R.basket_flow([cool]))
        self.assertGreaterEqual(g["score"], 0); self.assertLessEqual(g["score"], 100)
        self.assertEqual(g["regime"], "냉각")
        hot = self.synth([100 * 1.004 ** i * (1.03 if i > 250 else 1) for i in range(260)], vol=5000)
        g2 = R.overheat_gauge([hot], {"rs_pct_1y": 99}, R.basket_flow([hot]))
        self.assertGreater(g2["score"], g["score"])
        self.assertIn(g2["regime"], ("과열", "극단과열"))

    def test_empty(self):
        self.assertEqual(R.overheat_gauge([], {}, {})["regime"], "판정불가")

    def test_patterns_parabolic(self):
        seq, c = [100.0] * 200, 100.0
        for i in range(60):
            c *= 1 + 0.004 * (i + 1); seq.append(c)
        m = self.synth(seq)
        self.assertIn("파라볼릭", m["patterns"])
        self.assertIn("200일선 극단 이격", m["patterns"])


class Pipeline(unittest.TestCase):
    def test_merge_bars_dedup_and_cap(self):
        old = [{"d": f"2020-01-{i:02d}", "c": 1} for i in range(1, 10)]
        new = [{"d": "2020-01-09", "c": 2}, {"d": "2020-01-10", "c": 3}]
        m = R.merge_bars(old, new)
        self.assertEqual(len(m), 10); self.assertEqual(m[-2]["c"], 2)
        self.assertEqual(len(R.merge_bars([{"d": f"d{i:04d}", "c": 1} for i in range(1000)], [])), R.MAX_BARS)

    def test_messages_html_and_length(self):
        uni = R.load_json(os.path.join(R.HERE, "universe.json"), None)
        import random
        random.seed(7)
        bars = {}
        for s in list(uni["optical"]["symbols"]) + list(uni.get("optical_ext",{}).get("symbols",{})) + list(uni["memory"]["symbols"]) + ["^IXIC", "QQQ"]:
            c, seq = 100.0, []
            for _ in range(300):
                c *= 1 + random.uniform(-0.03, 0.035); seq.append(c)
            bars[s] = mk(seq, vol=random.uniform(500, 5000))
        snap = R.build_snapshot(uni, bars, {s: "fresh" for s in bars}, "2026-09-17")
        self.assertEqual(len(snap["optical"]["top5"]), min(5, len(uni["optical"]["symbols"])))
        self.assertEqual(snap["data_status"], "OK")
        msgs = R.build_messages(snap, "https://x")
        self.assertEqual(len(msgs), 2)
        for m in msgs:
            self.assertLess(len(m), 4000)
            self.assertEqual(m.count("<pre>"), m.count("</pre>"))
            self.assertEqual(m.count("<b>"), m.count("</b>"))
        self.assertEqual(len(R.append_history([{"as_of": snap["as_of"], "gauge": 1}], snap)), 1)

    def test_date_regex(self):
        self.assertTrue(R.DATE_RE.match("2026-09-17"))
        self.assertFalse(R.DATE_RE.match("2026-09-17; echo PWNED"))


if __name__ == "__main__":
    unittest.main()
