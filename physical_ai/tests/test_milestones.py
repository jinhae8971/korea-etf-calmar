import os, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import milestones as ms

CFG = {
    "milestones": [
        {"id":"narrative","label":"①","rule":"r","threshold":3.0},
        {"id":"revenue","label":"②","rule":"r","z_threshold":1.0,"abs_threshold":0.25},
        {"id":"bottleneck","label":"③","rule":"r","gap_threshold":0.10},
        {"id":"diffusion","label":"④","rule":"r","threshold":2.0},
        {"id":"margin","label":"⑤","rule":"r","threshold":0.03},
        {"id":"rerating","label":"⑥","rule":"r","threshold":0.20},
    ],
    "reference_timeline": {"narrative":"2023Q1"},
    "alerts": {"watch_gap_quarters": 6, "decoupling_corr": 0.35},
}

def narr(base, recent, sic_base=5, sic_recent=12):
    out = {}
    qs = [f"{y}Q{q}" for y in (2023,2024,2025) for q in (1,2,3,4)]
    for i,q in enumerate(qs):
        hi = base if i < 8 else recent
        sn = sic_base if i < 8 else sic_recent
        out[q] = {"hits": hi, "sic_n": sn}
    return out

def rev_series(vals, start="2023Q1"):
    out, q = {}, start
    for v in vals:
        out[q] = v; q = ms.qshift(q, 1)
    return out


class TestHelpers(unittest.TestCase):
    def test_qshift_wraps_year(self):
        self.assertEqual(ms.qshift("2024Q4", 1), "2025Q1")
        self.assertEqual(ms.qshift("2024Q1", -1), "2023Q4")
        self.assertEqual(ms.qshift("2024Q2", -4), "2023Q2")

    def test_yoy_requires_prior_year(self):
        s = rev_series([100,100,100,100,150,150,150,150])
        y = ms.yoy(s)
        self.assertNotIn("2023Q1", y)
        self.assertAlmostEqual(y["2024Q1"], 0.5, places=6)

    def test_zlast_needs_history(self):
        self.assertEqual(ms.zlast({"2024Q1":1.0})[0], None)


class TestMilestones(unittest.TestCase):
    def _real(self, rev_vals, inv_vals=None, oi_vals=None):
        d = {"A": {"revenue": rev_series(rev_vals)}, "B": {"revenue": rev_series(rev_vals)}}
        if inv_vals:
            for k in d: d[k]["inventory"] = rev_series(inv_vals)
        if oi_vals:
            for k in d: d[k]["operating_income"] = rev_series(oi_vals)
        return d

    def test_narrative_pass_and_fail(self):
        r = ms.evaluate(CFG, narr(10, 100), {}, {})
        self.assertEqual(r[0]["status"], ms.PASS)
        r = ms.evaluate(CFG, narr(100, 110), {}, {})
        self.assertEqual(r[0]["status"], ms.FAIL)

    def test_unknown_when_history_short(self):
        r = ms.evaluate(CFG, {"2025Q1": {"hits": 5, "sic_n": 3}}, {}, {})
        self.assertEqual(r[0]["status"], ms.UNKNOWN)
        self.assertEqual(r[1]["status"], ms.UNKNOWN)

    def test_bottleneck_gated_by_revenue(self):
        """②가 미통과면 ③은 PASS 가 아니라 UNKNOWN 이어야 한다 (병목·적체 구분 불가)."""
        flat = [100]*4 + [105]*8          # 매출 +5% → ② 미통과
        surge = [100]*4 + [300]*8         # 재고 +200%
        r = ms.evaluate(CFG, narr(10, 100), self._real(flat, surge), {})
        self.assertEqual(r[1]["status"], ms.FAIL)
        self.assertEqual(r[2]["status"], ms.UNKNOWN)
        self.assertIn("구분 불가", r[2]["note"])

    def test_rerating_threshold(self):
        r = ms.evaluate(CFG, narr(10,100), {}, {"2026Q1": 0.35})
        self.assertEqual(r[5]["status"], ms.PASS)
        r = ms.evaluate(CFG, narr(10,100), {}, {"2026Q1": 0.05})
        self.assertEqual(r[5]["status"], ms.FAIL)

    def test_stage_counts_consecutive_only(self):
        milestones = [{"id":"a","status":ms.PASS,"label":"a"},
                      {"id":"b","status":ms.FAIL,"label":"b"},
                      {"id":"c","status":ms.PASS,"label":"c"}]
        s = ms.stage_summary(milestones)
        self.assertEqual(s["stage"], 1)      # ③이 켜져도 ②에서 끊긴다
        self.assertEqual(s["next"], "b")

    def test_gap_alert_escalates(self):
        milestones = [{"id":"narrative","status":ms.PASS},{"id":"revenue","status":ms.FAIL}]
        hist = [{"stage":1} for _ in range(3)]
        self.assertEqual(ms.gap_alert(milestones, hist, CFG)["level"], "INFO")
        hist = [{"stage":1} for _ in range(9)]
        self.assertEqual(ms.gap_alert(milestones, hist, CFG)["level"], "WARN")

    def test_no_alert_when_revenue_passed(self):
        milestones = [{"id":"narrative","status":ms.PASS},{"id":"revenue","status":ms.PASS}]
        self.assertIsNone(ms.gap_alert(milestones, [{"stage":2}]*9, CFG))


if __name__ == "__main__":
    unittest.main()
