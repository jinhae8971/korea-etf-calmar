# -*- coding: utf-8 -*-
"""watchlist 트랙 회귀 테스트 — 네트워크 호출 없이 판정 로직만 검증한다."""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import watchlist as wl  # noqa: E402

CFG = {
    "watchlist": [
        {"symbol": "INDEX", "address": "0xAAA", "slugs": ["the-index"]},
        {"symbol": "PONS", "address": "0xBBB", "slugs": ["pons-v1", "pons-v2"]},
        {"symbol": "CASHCAT", "address": "0xCCC"},
    ],
    "wl_price_drop_6h_pct": -15.0, "wl_price_drop_24h_pct": -25.0,
    "wl_liq_drain_pct": -25.0, "wl_liq_mcap_min_pct": 3.0,
    "wl_rev_collapse_pct": -50.0, "wl_rev_surge_pct": 100.0,
    "wl_pf_move_pct": 25.0, "wl_share_drop_pp": 8.0, "wl_rank_move": 5,
    "wl_cooldown_hours": 6.0,
}
NOW = 1_700_000_000


def state(**over):
    item = {"symbol": "PONS", "address": "0xbbb", "track": "protocol", "resolved": True,
            "price": 1.0, "fdv": 100.0, "mcap": 100.0, "liq": 20.0, "vol24": 10.0,
            "liq_mcap_pct": 20.0, "flags": []}
    item.update(over)
    return {"as_of_epoch": NOW, "as_of_kst": "2026-01-01 00:00", "items": [item],
            "unresolved": []}


def hist(hours_ago, **fields):
    snap = {"epoch": NOW - hours_ago * 3600}
    snap.update(fields)
    return snap


class TestEntries(unittest.TestCase):
    def test_address_pinning_normalizes_and_rejects_junk(self):
        cfg = {"watchlist": [{"symbol": "a", "address": "0xAbC"},
                             {"symbol": "b", "address": "not-an-address"}]}
        out = wl.entries(cfg)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["address"], "0xabc")
        self.assertEqual(out[0]["symbol"], "A")

    def test_track_inferred_from_slugs(self):
        self.assertEqual(wl.entries(CFG)[0]["track"], "protocol")
        self.assertEqual(wl.entries(CFG)[2]["track"], "meme")


class TestEvaluate(unittest.TestCase):
    def codes(self, st, history):
        return [a["code"] for a in wl.evaluate(st, history, CFG, NOW)]

    def test_no_history_produces_no_delta_alerts(self):
        """기준 스냅샷이 없으면 없는 신호를 만들지 않는다."""
        self.assertEqual(self.codes(state(), []), [])

    def test_unresolved_token_is_loud_not_silent(self):
        st = state(resolved=False, reason="응답 없음")
        self.assertIn("UNRESOLVED", self.codes(st, []))

    def test_liquidity_drain(self):
        h = [hist(6, liq={"PONS": 100.0})]
        self.assertIn("LIQ_DRAIN", self.codes(state(liq=50.0), h))
        self.assertNotIn("LIQ_DRAIN", self.codes(state(liq=95.0), h))

    def test_thin_liquidity_ratio(self):
        self.assertIn("LIQ_THIN", self.codes(state(liq_mcap_pct=1.2), []))
        self.assertNotIn("LIQ_THIN", self.codes(state(liq_mcap_pct=9.0), []))

    def test_revenue_collapse_and_surge(self):
        self.assertIn("REV_COLLAPSE", self.codes(state(rev24=1.0, burst_pct=-73.0), []))
        self.assertIn("REV_SURGE", self.codes(state(rev24=1.0, burst_pct=196.0), []))

    def test_price_drop_uses_matching_window(self):
        h = [hist(24, px={"PONS": 2.0})]
        self.assertIn("PRICE_DROP", self.codes(state(price=1.0), h))

    def test_reference_too_far_is_ignored(self):
        """6h 기준을 요구했는데 90h 전 스냅샷뿐이면 비교하지 않는다."""
        h = [hist(90, liq={"PONS": 1000.0})]
        self.assertNotIn("LIQ_DRAIN", self.codes(state(liq=10.0), h))

    def test_share_loss_in_percentage_points(self):
        h = [hist(24, lp_share={"PONS": 89.0})]
        self.assertIn("SHARE_LOSS", self.codes(state(lp_share=70.0), h))
        self.assertNotIn("SHARE_LOSS", self.codes(state(lp_share=86.0), h))

    def test_security_flag_outranks_everything(self):
        alerts = wl.evaluate(state(flags=["HONEYPOT"], liq_mcap_pct=1.0), [], CFG, NOW)
        self.assertEqual(alerts[0]["code"], "SECURITY")

    def test_zero_denominator_never_raises(self):
        h = [hist(6, liq={"PONS": 0.0}), hist(24, px={"PONS": 0.0})]
        wl.evaluate(state(mcap=0.0, liq=0.0, price=0.0, liq_mcap_pct=None), h, CFG, NOW)


class TestCooldown(unittest.TestCase):
    def test_same_alert_suppressed_then_escalation_passes(self):
        a = [{"code": "LIQ_DRAIN", "symbol": "PONS", "detail": "d", "severity": 10.0}]
        fresh, sent = wl.gate_alerts(a, {}, CFG, NOW)
        self.assertEqual(len(fresh), 1)
        again, sent = wl.gate_alerts(a, sent, CFG, NOW + 3600)
        self.assertEqual(len(again), 0)
        worse = [dict(a[0], severity=12.0)]
        esc, _ = wl.gate_alerts(worse, sent, CFG, NOW + 3600)
        self.assertEqual(len(esc), 1)

    def test_cooldown_expires(self):
        a = [{"code": "LIQ_DRAIN", "symbol": "PONS", "detail": "d", "severity": 10.0}]
        _, sent = wl.gate_alerts(a, {}, CFG, NOW)
        later, _ = wl.gate_alerts(a, sent, CFG, NOW + 7 * 3600)
        self.assertEqual(len(later), 1)


class TestRender(unittest.TestCase):
    def test_section_renders_without_alerts(self):
        lines = wl.render_telegram(state(), [], CFG)
        self.assertTrue(any("보유 종목 정밀 감시" in ln for ln in lines))

    def test_html_is_escaped(self):
        lines = wl.render_telegram(state(symbol="<b>X"), [], CFG)
        self.assertFalse(any("<b>X" in ln.replace("<b>", "", 1) for ln in lines[1:]))

    def test_empty_state_is_silent(self):
        self.assertEqual(wl.render_telegram({"items": []}, [], CFG), [])


class TestShares(unittest.TestCase):
    def test_slug_match_is_exact_and_summed(self):
        payload = {"shares": {"launchpad_24h": {"pons-v1": 5.0, "pons-v2": 84.0,
                                                "the-other-index": 9.0}}}
        self.assertEqual(wl._shares_from_payload(payload, ["pons-v1", "pons-v2"]), 89.0)
        self.assertIsNone(wl._shares_from_payload(payload, ["the-index"]))


if __name__ == "__main__":
    unittest.main()
