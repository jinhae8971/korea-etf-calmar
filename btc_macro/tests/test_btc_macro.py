# -*- coding: utf-8 -*-
"""네트워크 없이 판정·변화탐지·렌더링을 검증한다."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import btc_macro as m  # noqa: E402


def sig(key, level, value, **kw):
    d = {"key": key, "label": key, "level": level, "value": value, "display": str(value),
         "note": "", "deltas": {}}
    d.update(kw)
    return d


class GradeTests(unittest.TestCase):
    def test_real_yield_bands(self):
        self.assertEqual(m.grade_real_yield({"value": 1.2})[0], 0)
        self.assertEqual(m.grade_real_yield({"value": 1.7})[0], 1)
        self.assertEqual(m.grade_real_yield({"value": 2.43})[0], 2)
        self.assertEqual(m.grade_real_yield({"value": 2.0})[0], 2)   # 경계 포함

    def test_real_yield_trend_note(self):
        lv, note = m.grade_real_yield({"value": 1.7, "deltas": {"d30": {"value": -0.2}}})
        self.assertIn("하락", note)

    def test_dxy_bands(self):
        self.assertEqual(m.grade_dxy({"value": 99.2, "kind": "DXY"})[0], 0)
        self.assertEqual(m.grade_dxy({"value": 102.5, "kind": "DXY"})[0], 1)
        self.assertEqual(m.grade_dxy({"value": 106.0, "kind": "DXY"})[0], 2)

    def test_dxy_broad_fallback_uses_trend_only(self):
        self.assertEqual(m.grade_dxy({"value": 118.4, "kind": "BROAD", "deltas": {"d30": {"value": -1.5}}})[0], 0)
        self.assertEqual(m.grade_dxy({"value": 118.4, "kind": "BROAD", "deltas": {"d30": {"value": 1.5}}})[0], 2)
        self.assertEqual(m.grade_dxy({"value": 118.4, "kind": "BROAD"})[0], 1)

    def test_m2_grades(self):
        self.assertEqual(m.grade_m2({"value": 5.1, "momentum": -0.18})[0], 0)   # 확장, 모멘텀 보합
        self.assertEqual(m.grade_m2({"value": 5.1, "momentum": -0.9})[0], 1)    # 높지만 둔화 → 중립
        self.assertEqual(m.grade_m2({"value": 1.5, "momentum": -0.9})[0], 2)    # 둔화
        self.assertEqual(m.grade_m2({"value": -0.5, "momentum": 0.5})[0], 2)    # 수축
        self.assertEqual(m.grade_m2({"value": 1.5, "momentum": 0.8})[0], 0)     # 바닥 반등
        self.assertEqual(m.grade_m2({"value": 1.5, "momentum": 0.0})[0], 1)

    def test_etf_grades(self):
        self.assertEqual(m.grade_etf({"value": 39450, "sum5": 5942, "streak": 4})[0], 0)
        self.assertIn("강함", m.grade_etf({"value": 39450, "sum5": 5942, "streak": 4})[1])
        self.assertEqual(m.grade_etf({"value": -3000, "sum5": -800, "streak": -3})[0], 2)
        self.assertEqual(m.grade_etf({"value": 3000, "sum5": -800, "streak": -2})[0], 1)
        self.assertEqual(m.grade_etf({"value": -3000, "sum5": 800, "streak": 1})[0], 1)

    def test_coupling_grades(self):
        self.assertEqual(m.grade_coupling({"value": 0.2, "ndx_20d": -5})[0], 1)
        self.assertEqual(m.grade_coupling({"value": 0.7, "ndx_20d": -5})[0], 2)
        self.assertEqual(m.grade_coupling({"value": 0.7, "ndx_20d": 4})[0], 0)
        self.assertEqual(m.grade_coupling({"value": 0.7, "ndx_20d": 0.5})[0], 1)
        self.assertEqual(m.grade_coupling({"value": 0.7, "ndx_20d": None})[0], 1)


class ComposeTests(unittest.TestCase):
    def test_phase_bands(self):
        self.assertEqual(m.compose([sig("a", 0, 1)] * 5)["phase"], "우호")
        self.assertEqual(m.compose([sig("a", 0, 1)] * 3 + [sig("b", 1, 1), sig("c", 2, 1)])["phase"], "완만한 우호")
        self.assertEqual(m.compose([sig("a", 1, 1)] * 5)["phase"], "중립·과도기")
        self.assertEqual(m.compose([sig("a", 2, 1)] * 4 + [sig("b", 0, 1)])["phase"], "역풍")

    def test_compose_ignores_failed(self):
        r = m.compose([sig("a", 0, 1), sig("b", None, None)])
        self.assertEqual((r["score"], r["max"]), (2, 2))

    def test_compose_all_failed(self):
        self.assertEqual(m.compose([sig("a", None, None)])["phase"], "판정 불가")


class ChangeTests(unittest.TestCase):
    def _cur(self, dxy=99.0, ry=2.4, etf5=500, phase="우호"):
        return {"phase": {"phase": phase},
                "signals": [sig("dxy", 0, dxy), sig("real_yield", 2, ry),
                            sig("etf", 0, 1000, sum5=etf5)]}

    def test_first_run_empty(self):
        self.assertEqual(m.detect_changes(self._cur(), None), [])

    def test_no_change(self):
        self.assertEqual(m.detect_changes(self._cur(), self._cur()), [])

    def test_dxy_cross_100(self):
        out = m.detect_changes(self._cur(dxy=100.4), self._cur(dxy=99.6))
        self.assertTrue(any("100선" in c for c in out), out)

    def test_real_yield_cross(self):
        out = m.detect_changes(self._cur(ry=1.95), self._cur(ry=2.05))
        self.assertTrue(any("2.0%" in c for c in out), out)

    def test_etf_sign_flip(self):
        out = m.detect_changes(self._cur(etf5=-300), self._cur(etf5=400))
        self.assertTrue(any("순유출 전환" in c for c in out), out)

    def test_level_and_phase_change(self):
        prev = self._cur(phase="중립·과도기")
        prev["signals"][0]["level"] = 2
        out = m.detect_changes(self._cur(), prev)
        self.assertTrue(any("국면" in c for c in out))
        self.assertTrue(any("🔴 → 🟢" in c for c in out))


class SeriesTests(unittest.TestCase):
    def test_delta_pack_uses_calendar_days(self):
        s = [("2026-08-01", 1.0), ("2026-08-05", 1.5), ("2026-09-03", 2.0), ("2026-09-04", 2.2)]
        d = m.delta_pack(s, None, unit="%p")
        self.assertEqual(d["d1"]["text"], "+0.20%p")
        self.assertEqual(d["d30"]["text"], "+0.70%p")   # 8/5 관측(8/5 <= 8/5)
        d2 = m.delta_pack(s, None, pct=True)
        self.assertEqual(d2["d1"]["dir"], "up")

    def test_delta_pack_short(self):
        self.assertEqual(m.delta_pack([("2026-09-04", 1.0)], None), {})

    def test_pearson(self):
        a = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
        self.assertAlmostEqual(m._pearson(a, a), 1.0)
        self.assertAlmostEqual(m._pearson(a, [-x for x in a]), -1.0)
        self.assertIsNone(m._pearson(a[:5], a[:5]))

    def test_m2_deltas(self):
        d = m._m2_deltas([("2026-06-01", 4.7), ("2026-07-01", 5.1)])
        self.assertEqual(d["d30"]["dir"], "up")
        self.assertEqual(m._m2_deltas([("2026-07-01", 5.1)]), {})


class RenderTests(unittest.TestCase):
    def _payload(self, **kw):
        p = {"as_of_kst": "2026-09-06 07:36", "data_status": "OK",
             "phase": {"phase": "완만한 우호", "score": 7, "max": 10},
             "signals": [sig("dxy", 0, 99.2, label="달러 인덱스", note="100 하회",
                             deltas={"d1": {"dir": "up", "text": "+0.16%"}}),
                         sig("etf", None, None, label="현물 ETF 순유입", note="수집 실패")],
             "changes": [], "first_run": True}
        p.update(kw)
        return p

    def test_message_first_run(self):
        msg = m.render_message(self._payload())
        self.assertIn("첫 관측", msg)
        self.assertIn("⚪", msg)
        self.assertIn("1일 ▲+0.16%", msg)

    def test_message_changes_and_degraded(self):
        msg = m.render_message(self._payload(first_run=False, changes=["DXY 100선 통과"],
                                             data_status="DEGRADED"))
        self.assertIn("⚡ <b>변화</b>", msg)
        self.assertIn("DEGRADED", msg)
        self.assertLess(len(msg), 3900)

    def test_dashboard_html(self):
        html = m.render_dashboard(self._payload(signals=[
            sig("dxy", 0, 99.2, label="달러 인덱스", note="x", series=[("2026-09-01", 99.0), ("2026-09-02", 99.5)])]))
        self.assertIn("<svg", html)
        self.assertIn("완만한 우호", html)
        self.assertIn("임계값 고정일", html)


if __name__ == "__main__":
    unittest.main()
