#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""top_signal 유닛테스트 — 네트워크 없이 판정·렌더·변화탐지 로직만 검증."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import top_signal as ts  # noqa: E402


class TestGrade(unittest.TestCase):
    def test_kimchi_bands(self):
        self.assertEqual(ts.grade("kimchi", 6.0)[0], 2)
        self.assertEqual(ts.grade("kimchi", 3.0)[0], 1)
        self.assertEqual(ts.grade("kimchi", 0.5)[0], 0)
        self.assertEqual(ts.grade("kimchi", -3.0)[0], 0)
        self.assertIn("역프", ts.grade("kimchi", -3.0)[1])

    def test_altseason_bands(self):
        self.assertEqual(ts.grade("altseason", 80)[0], 2)
        self.assertEqual(ts.grade("altseason", 60)[0], 1)
        self.assertEqual(ts.grade("altseason", 20)[0], 0)

    def test_fng_bands(self):
        self.assertEqual(ts.grade("fng", 85)[0], 2)
        self.assertEqual(ts.grade("fng", 76)[0], 1)
        self.assertEqual(ts.grade("fng", 40)[0], 0)

    def test_mvrv_bands(self):
        self.assertEqual(ts.grade("mvrv", 4.0)[0], 2)
        self.assertEqual(ts.grade("mvrv", 3.1)[0], 1)
        self.assertEqual(ts.grade("mvrv", 1.5)[0], 0)
        self.assertEqual(ts.grade("mvrv_z", 6.0)[0], 2)
        self.assertEqual(ts.grade("mvrv_z", 0.9)[0], 0)

    def test_boundary_is_inclusive(self):
        self.assertEqual(ts.grade("kimchi", ts.TH["kimchi"]["hot"])[0], 2)
        self.assertEqual(ts.grade("fng", ts.TH["fng"]["warn"])[0], 1)


class TestCompose(unittest.TestCase):
    def _sig(self, levels):
        return [{"key": "k%d" % i, "label": "x", "level": l} for i, l in enumerate(levels)]

    def test_phases(self):
        self.assertEqual(ts.compose(self._sig([0, 0, 0, 0]))["phase"], "평온")
        self.assertEqual(ts.compose(self._sig([1, 1, 0, 0]))["phase"], "주의")
        self.assertEqual(ts.compose(self._sig([2, 2, 0, 0]))["phase"], "과열 확산")
        self.assertEqual(ts.compose(self._sig([2, 2, 2, 0]))["phase"], "고점 경계")

    def test_max_scales_with_available_signals(self):
        r = ts.compose(self._sig([2, 2]))
        self.assertEqual(r["max"], 4)

    def test_all_failed(self):
        r = ts.compose([{"key": "a", "label": "x", "level": None}])
        self.assertEqual(r["phase"], "판정 불가")
        self.assertIsNone(r["score"])


class TestChanges(unittest.TestCase):
    def test_no_prev_means_no_changes(self):
        cur = {"phase": {"phase": "평온"}, "signals": []}
        self.assertEqual(ts.detect_changes(cur, None), [])

    def test_phase_and_level_moves(self):
        prev = {"phase": {"phase": "평온"},
                "signals": [{"key": "fng", "level": 0, "value": 50}]}
        cur = {"phase": {"phase": "주의"},
               "signals": [{"key": "fng", "label": "공포탐욕지수", "level": 1, "value": 76}]}
        out = ts.detect_changes(cur, prev)
        self.assertTrue(any("국면" in c for c in out))
        self.assertTrue(any("단계 상승" in c for c in out))

    def test_kimchi_spike(self):
        prev = {"phase": {"phase": "평온"},
                "signals": [{"key": "kimchi", "level": 0, "value": 0.2}]}
        cur = {"phase": {"phase": "평온"},
               "signals": [{"key": "kimchi", "label": "김치프리미엄",
                            "level": 0, "value": 1.5}]}
        self.assertTrue(any("급변" in c for c in ts.detect_changes(cur, prev)))

    def test_small_kimchi_move_is_silent(self):
        prev = {"phase": {"phase": "평온"},
                "signals": [{"key": "kimchi", "level": 0, "value": 0.2}]}
        cur = {"phase": {"phase": "평온"},
               "signals": [{"key": "kimchi", "label": "김치프리미엄",
                            "level": 0, "value": 0.5}]}
        self.assertEqual(ts.detect_changes(cur, prev), [])


class TestRender(unittest.TestCase):
    def _payload(self, status="OK"):
        sig = [{"key": "kimchi", "label": "김치프리미엄", "level": 1, "value": 3.2,
                "display": "+3.20%", "note": "국내 프리미엄 확대"},
               {"key": "fng", "label": "공포탐욕지수", "level": None, "value": None,
                "display": "—", "note": "수집 실패"}]
        return {"as_of_kst": "2026-09-05 07:48", "data_status": status,
                "signals": sig, "phase": ts.compose(sig), "changes": ["국면 평온 → 주의"]}

    def test_message_contains_core_parts(self):
        m = ts.render_message(self._payload())
        self.assertIn("크립토 고점신호", m)
        self.assertIn("김치프리미엄", m)
        self.assertIn("수집 실패", m)
        self.assertIn("관측 리포트", m)

    def test_degraded_is_disclosed(self):
        self.assertIn("DEGRADED", ts.render_message(self._payload("DEGRADED")))

    def test_ok_has_no_degraded_banner(self):
        self.assertNotIn("데이터 상태", ts.render_message(self._payload("OK")))

    def test_dashboard_renders(self):
        html = ts.render_dashboard(self._payload())
        self.assertIn("<table>", html)
        self.assertIn("김치프리미엄", html)
        self.assertIn(ts.THRESHOLDS_FROZEN_AT, html)


class TestGuards(unittest.TestCase):
    def test_stablecoins_excluded_from_altseason(self):
        for cid in ("tether", "usd-coin", "wrapped-bitcoin", "staked-ether"):
            self.assertIn(cid, ts.STABLE_OR_WRAPPED)

    def test_thresholds_frozen_marker_present(self):
        self.assertRegex(ts.THRESHOLDS_FROZEN_AT, r"^\d{4}-\d{2}-\d{2}$")




class TestDeltas(unittest.TestCase):
    from datetime import date as _d
    TODAY = _d(2026, 9, 5)

    def test_own_history_takes_priority(self):
        hist = [{"as_of": "2026-09-04", "signals": [{"key": "fng", "value": 60}]}]
        out = ts.build_deltas("fng", 73, {"d1": 99}, hist, self.TODAY)
        self.assertEqual(out["d1"]["delta"], 13)
        self.assertEqual(out["d1"]["dir"], "up")

    def test_external_fallback_when_no_history(self):
        out = ts.build_deltas("kimchi", 1.34, {"d1": 2.0, "d30": 0.1}, [], self.TODAY)
        self.assertEqual(out["d1"]["dir"], "down")
        self.assertEqual(out["d30"]["dir"], "up")
        self.assertIn("%p", out["d1"]["text"])

    def test_missing_base_yields_none(self):
        out = ts.build_deltas("mvrv", 1.51, {}, [], self.TODAY)
        self.assertIsNone(out["d1"])
        self.assertIsNone(out["d30"])

    def test_flat_when_below_epsilon(self):
        out = ts.build_deltas("mvrv", 1.51, {"d1": 1.505}, [], self.TODAY)
        self.assertEqual(out["d1"]["dir"], "flat")

    def test_delta_line_shows_dash_for_missing(self):
        line = ts.render_delta_line(ts.build_deltas("mvrv", 1.5, {}, [], self.TODAY))
        self.assertIn("1일 —", line)
        self.assertIn("30일 —", line)

    def test_dashboard_has_delta_columns(self):
        sig = [{"key": "fng", "label": "공포탐욕지수", "level": 1, "value": 76,
                "display": "76", "note": "탐욕 구간",
                "deltas": ts.build_deltas("fng", 76, {"d1": 60, "d30": 80},
                                          [], self.TODAY)}]
        html = ts.render_dashboard({"as_of_kst": "2026-09-05 07:48",
                                    "data_status": "OK", "signals": sig,
                                    "phase": ts.compose(sig), "changes": []})
        self.assertIn("1일", html)
        self.assertIn("30일", html)
        self.assertIn("#c62828", html)   # 상승 = 적색
        self.assertIn("#1565c0", html)   # 하락 = 청색

if __name__ == "__main__":
    unittest.main(verbosity=2)
