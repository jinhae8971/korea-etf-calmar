import os, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import milestones as ms

CFG = {
  "milestones": [
    {"id":"narrative","axis":"narrative","label":"①","rule":"r","threshold":3.0},
    {"id":"diffusion","axis":"narrative","label":"②","rule":"r","threshold":2.0},
    {"id":"revenue","axis":"realization","label":"③","rule":"r",
     "abs_threshold":0.25,"persistence":3,"window":4},
    {"id":"bottleneck","axis":"realization","label":"④","rule":"r","gap_threshold":0.10},
    {"id":"margin","axis":"realization","label":"⑤","rule":"r","threshold":0.03},
    {"id":"rerating","axis":"price","label":"⑥","rule":"r","threshold":0.20},
  ],
  "axes":{"narrative":{"label":"서사","total":2},
          "realization":{"label":"실현","total":3},
          "price":{"label":"가격","total":1}},
  "reference_timeline":{"narrative":"2023Q1"},
  "alerts":{"watch_gap_quarters":6,"decoupling_corr":0.35},
}

def narr(base, recent, sb=5, sr=12):
    qs=[f"{y}Q{q}" for y in (2023,2024,2025) for q in (1,2,3,4)]
    return {q:{"hits": base if i<8 else recent, "sic_n": sb if i<8 else sr}
            for i,q in enumerate(qs)}

def series(vals, start="2023Q1"):
    o,q={},start
    for v in vals: o[q]=v; q=ms.qshift(q,1)
    return o

def real(rev, inv=None, oi=None):
    d={"A":{"revenue":series(rev)},"B":{"revenue":series(rev)}}
    if inv:
        for k in d: d[k]["inventory"]=series(inv)
    if oi:
        for k in d: d[k]["operating_income"]=series(oi)
    return d


class TestHelpers(unittest.TestCase):
    def test_qshift_wraps(self):
        self.assertEqual(ms.qshift("2024Q4",1),"2025Q1")
        self.assertEqual(ms.qshift("2024Q1",-4),"2023Q1")
    def test_yoy_needs_prior_year(self):
        y=ms.yoy(series([100]*4+[150]*4))
        self.assertNotIn("2023Q1",y); self.assertAlmostEqual(y["2024Q1"],0.5)


class TestPersistence(unittest.TestCase):
    """메타버스는 단일 분기로는 통과했으나 지속성으로는 통과 못했다."""
    def test_spiky_growth_fails(self):
        # 매출 패턴: 급등 → 정체 반복 (메타버스형)
        vals=[100]*4+[200,110,110,205]      # YoY: +100%,+10%,+10%,+105% → 4중 2회
        r=ms.evaluate(CFG, narr(10,100), real(vals), {})
        rev=[m for m in r if m["id"]=="revenue"][0]
        self.assertEqual(rev["status"], ms.FAIL)
        self.assertIn("2회", rev["note"])

    def test_sustained_growth_passes(self):
        vals=[100]*4+[140,140,140,140]      # 4중 4회
        r=ms.evaluate(CFG, narr(10,100), real(vals), {})
        self.assertEqual([m for m in r if m["id"]=="revenue"][0]["status"], ms.PASS)

    def test_bottleneck_gated(self):
        flat=[100]*4+[105]*4; surge=[100]*4+[300]*4
        r=ms.evaluate(CFG, narr(10,100), real(flat,surge), {})
        b=[m for m in r if m["id"]=="bottleneck"][0]
        self.assertEqual(b["status"], ms.UNKNOWN)
        self.assertIn("구분 불가", b["note"])


class TestAxes(unittest.TestCase):
    def test_narrative_led_regime(self):
        r=ms.evaluate(CFG, narr(10,100), real([100]*8), {"2025Q1":-0.1})
        s=ms.axis_summary(r, CFG)
        self.assertEqual(s["narrative"]["passed"], 2)
        self.assertEqual(s["realization"]["passed"], 0)
        self.assertEqual(s["gap"], 1.0)
        self.assertEqual(s["regime"], "NARRATIVE_LED")

    def test_realization_led_regime(self):
        """서사는 잠잠한데 실적이 먼저 나오는 구간 — 가장 좋은 자리."""
        r=ms.evaluate(CFG, narr(100,110,10,11), real([100]*4+[140]*4), {"2025Q1":0.0})
        s=ms.axis_summary(r, CFG)
        self.assertEqual(s["narrative"]["passed"], 0)
        self.assertEqual(s["realization"]["passed"], 1)
        self.assertEqual(s["regime"], "REALIZATION_LED")

    def test_balanced_regime(self):
        # 서사 1/2(0.50) vs 실현 1/3(0.33) → 격차 +0.17 → 균형
        r=ms.evaluate(CFG, narr(10,100,10,11), real([100]*4+[140]*4), {"2025Q1":0.0})
        s=ms.axis_summary(r, CFG)
        self.assertEqual(s["narrative"]["passed"], 1)
        self.assertEqual(s["realization"]["passed"], 1)
        self.assertEqual(s["regime"], "BALANCED")

    def test_axis_totals_fixed(self):
        r=ms.evaluate(CFG, narr(10,100), {}, {})
        s=ms.axis_summary(r, CFG)
        self.assertEqual(s["realization"]["total"], 3)
        self.assertEqual(s["price"]["total"], 1)


class TestAlert(unittest.TestCase):
    def test_escalates_with_streak(self):
        s={"regime":"NARRATIVE_LED","gap":1.0}
        self.assertEqual(ms.gap_alert(s,[{"regime":"NARRATIVE_LED"}]*3,CFG)["level"],"INFO")
        a=ms.gap_alert(s,[{"regime":"NARRATIVE_LED"}]*8,CFG)
        self.assertEqual(a["level"],"WARN"); self.assertIn("메타버스", a["text"])

    def test_no_alert_when_balanced(self):
        self.assertIsNone(ms.gap_alert({"regime":"BALANCED"},[],CFG))


if __name__ == "__main__":
    unittest.main()
