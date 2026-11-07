import os
import sys
import unittest

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import kospi_wave as W  # noqa: E402


def frame(path):
    """꺾은선 경로(주봉 종가)를 OHLC 주봉으로 — 고저는 ±1%."""
    pts = []
    for a, b, n in path:
        pts += list(np.linspace(a, b, n, endpoint=False))
    c = np.array(pts + [path[-1][1]])
    idx = pd.date_range("2024-01-01", periods=len(c), freq="W-MON")
    return pd.DataFrame({"Open": c, "High": c * 1.01, "Low": c * 0.99, "Close": c, "Volume": 0}, index=idx)


# 0=1000 → 1=1500 → 2=1250 → 3=2600 → 4=2200 → 5=3000 → A=2000 → B=2500 → C=1800
PATH = [(1000, 1500, 8), (1500, 1250, 4), (1250, 2600, 12), (2600, 2200, 5), (2200, 3000, 8),
        (3000, 2000, 8), (2000, 2500, 6), (2500, 1800, 6)]


class WaveTest(unittest.TestCase):
    def setUp(self):
        self.df = frame(PATH)

    def test_zigzag_alternates(self):
        piv = W.zigzag(self.df, 0.06)
        types = [p["t"] for p in piv]
        self.assertTrue(all(a != b for a, b in zip(types, types[1:])))

    def test_impulse_count(self):
        imp = W.count_impulse(self.df, W.zigzag(self.df, 0.06))
        self.assertTrue(imp["valid"])
        got = [round(imp[k]["p"] / (1.01 if k in "135" else 0.99)) for k in "012345"]
        self.assertEqual(got, [1000, 1500, 1250, 2600, 2200, 3000])

    def test_abc(self):
        imp = W.count_impulse(self.df, W.zigzag(self.df, 0.06))
        abc = W.track_abc(self.df, imp, 0.06)
        self.assertEqual(abc["stage"], "C")
        self.assertAlmostEqual(abc["A"]["p"], 2000 * 0.99, delta=1)
        self.assertAlmostEqual(abc["B"]["p"], 2500 * 1.01, delta=1)

    def test_analyze_and_message(self):
        daily = self.df.tail(5)
        o = W.analyze(self.df, daily)
        self.assertIn(o["phase"], {"C", "C_ZONE", "C_ZONE_SIG", "C_EXT", "C_END"})
        msg = W.build_message(o, {"phase": "B", "phase_label": "B파 반등"})
        self.assertIn("판정 변경", msg)
        self.assertIn("〈트리거〉", msg)

    def test_rules_reject_overlap(self):
        # 4파가 1파 고점 아래로 내려가면 이 조합은 채택되지 않아야 함
        bad = frame([(1000, 1500, 8), (1500, 1250, 4), (1250, 2600, 12), (2600, 1400, 6),
                     (1400, 3000, 10), (3000, 2400, 6)])
        imp = W.count_impulse(bad, W.zigzag(bad, 0.06))
        if imp["valid"]:
            self.assertGreater(imp["4"]["p"], imp["1"]["p"])

    def test_judge_ext_below_ema200(self):
        o = {"abc": {"stage": "C", "B": {"p": 7000}}, "price": 4000, "ema50": 6000, "ema200": 4100,
             "c_ratio": 0.7, "retr_now": 0.7, "rebound": 0, "hist_turn": False, "bull_div": False}
        self.assertEqual(W.judge(o)[0], "C_EXT")

    def test_keep_previous_count(self):
        imp = W.count_impulse(self.df, W.zigzag(self.df, 0.06))
        prev = {"impulse": {**imp, "2": {**imp["2"]}}}
        kept = W.keep_previous_count(self.df, imp, prev)
        self.assertTrue(kept.get("kept"))
        self.assertEqual(kept["3"]["date"], imp["3"]["date"])


    def test_score_range_and_lights(self):
        o = W.analyze(self.df, self.df.tail(5))
        if o["abc"]["stage"] == "C":
            self.assertTrue(0 <= o["score"] <= 100)
            self.assertEqual(sum(x["max"] for x in o["score_items"]), 100)
            for x in o["score_items"]:
                self.assertIn(x["light"], {"🟢", "🟡", "🔴"})
                self.assertLessEqual(x["pts"], x["max"])
            msg = W.build_message(o, {"phase": o["phase"], "score": o["score"] - 5})
            self.assertIn("〈C파 완성도〉", msg)
            self.assertIn("전일 +5", msg)

    def test_lerp_and_grade(self):
        self.assertEqual(W.lerp(40, 45, 30, 0, 10), 10 / 3)
        self.assertEqual(W.lerp(20, 45, 30, 0, 10), 10)
        self.assertEqual(W.grade(51), "완성 접근")
        self.assertEqual(W.grade(85), "완성·반등 확인")


if __name__ == "__main__":
    unittest.main()
