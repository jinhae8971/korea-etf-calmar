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


# ---------------------------------------------------------------- 종목 특성별 규칙
import chainctx  # noqa: E402

PCFG = dict(CFG, wl_share_drop_pp_6h=10.0, wl_rival_rise_pp=10.0,
            wl_issuance_drop_pct=-50.0, wl_issuance_surge_pct=100.0,
            wl_index_rev_floor_usd=3000.0, wl_liq_floor_usd=1_500_000.0,
            wl_pf_premium_x=2.5, wl_attn_drop_pct=-40.0,
            wl_meme_turnover_min_pct=5.0, wl_rev_noise_floor_usd=0.0)


def pstate(profile, **over):
    st = state(profile=profile, **over)
    return st


class TestLaunchpadRules(unittest.TestCase):
    def codes(self, st, history=()):
        return [a["code"] for a in wl.evaluate(st, list(history), PCFG, NOW)]

    def test_share_loss_6h_window(self):
        h = [hist(6, lp_share={"PONS": 89.0})]
        self.assertIn("SHARE_LOSS", self.codes(pstate("launchpad", lp_share=70.0), h))
        self.assertNotIn("SHARE_LOSS", self.codes(pstate("launchpad", lp_share=85.0), h))

    def test_leader_lost_to_rival(self):
        st = pstate("launchpad", lp_share=30.0, lp_leader="o1-launchpad", slugs=["pons-v2"])
        self.assertIn("LEAD_LOST", self.codes(st))
        st2 = pstate("launchpad", lp_share=80.0, lp_leader="pons-v2", slugs=["pons-v2"])
        self.assertNotIn("LEAD_LOST", self.codes(st2))

    def test_rival_rise_in_pp(self):
        h = [hist(24, rival_share={"PONS": 7.0})]
        st = pstate("launchpad", rival_share=25.0, rival="o1-launchpad")
        self.assertIn("RIVAL_RISE", self.codes(st, h))

    def test_issuance_rate_is_leading_signal(self):
        h = [hist(24, issuance={"PONS": 20.0})]
        self.assertIn("ISSUANCE_SLOW", self.codes(pstate("launchpad", issuance_rate=6.0), h))
        self.assertIn("ISSUANCE_SURGE", self.codes(pstate("launchpad", issuance_rate=45.0), h))


class TestIndexRules(unittest.TestCase):
    def codes(self, st, history=()):
        return [a["code"] for a in wl.evaluate(st, list(history), PCFG, NOW)]

    def test_absolute_revenue_floor(self):
        self.assertIn("REV_FLOOR", self.codes(pstate("index", rev24=900.0, rev30=50000.0)))
        self.assertNotIn("REV_FLOOR", self.codes(pstate("index", rev24=17000.0, rev30=331000.0)))

    def test_revenue_zero_is_service_risk(self):
        self.assertIn("REV_ZERO", self.codes(pstate("index", rev24=0.0, rev30=331000.0)))

    def test_absolute_liquidity_floor(self):
        self.assertIn("LIQ_FLOOR", self.codes(pstate("index", liq=900_000.0)))
        self.assertNotIn("LIQ_FLOOR", self.codes(pstate("index", liq=3_400_000.0)))

    def test_multiple_premium_vs_peer_median(self):
        st = pstate("index", pf=10.2, pf_peer_median=3.0, pf_premium_x=3.4)
        self.assertIn("PF_PREMIUM", self.codes(st))

    def test_small_revenue_noise_is_suppressed(self):
        """몇 천 달러 진폭이 만든 -70%로는 붕괴 경보를 내지 않는다."""
        st = state(profile="index", rev24=1200.0, burst_pct=-70.0,
                   thresholds={"wl_rev_noise_floor_usd": 5000.0})
        self.assertNotIn("REV_COLLAPSE", self.codes(st))


class TestMemeRules(unittest.TestCase):
    def codes(self, st, history=()):
        return [a["code"] for a in wl.evaluate(st, list(history), PCFG, NOW)]

    def test_attention_share_loss(self):
        h = [hist(24, attn={"PONS": 4.4})]
        self.assertIn("ATTN_LOSS", self.codes(pstate("meme", attn_share_pct=2.0), h))
        self.assertNotIn("ATTN_LOSS", self.codes(pstate("meme", attn_share_pct=4.0), h))

    def test_turnover_dry(self):
        self.assertIn("TURNOVER_DRY", self.codes(pstate("meme", turnover_pct=2.0)))
        self.assertNotIn("TURNOVER_DRY", self.codes(pstate("meme", turnover_pct=27.0)))

    def test_meme_never_gets_revenue_alerts(self):
        codes = self.codes(pstate("meme", turnover_pct=27.0))
        self.assertNotIn("REV_COLLAPSE", codes)
        self.assertNotIn("REV_FLOOR", codes)


class TestCopycat(unittest.TestCase):
    def test_alert_raised_for_all_profiles(self):
        st = state(profile="meme", turnover_pct=27.0,
                   copycats=[{"name": "CASHCATS / WETH", "ratio": 0.93}])
        self.assertIn("COPYCAT", [a["code"] for a in wl.evaluate(st, [], PCFG, NOW)])

    def test_short_symbol_false_positive_rejected(self):
        """PORN vs PONS = 0.75 — 짧은 심볼의 우연한 유사도는 걸러야 한다."""
        pools = [{"name": "PORN / RDDT", "pool_created_at": "2026-09-03T00:00:00Z"},
                 {"name": "PONSY / WETH", "pool_created_at": "2026-09-03T00:01:00Z"}]
        hits = _copycats(pools, ["PONS"])
        self.assertEqual([h["name"] for h in hits], ["PONSY / WETH"])


def _copycats(pools, symbols):
    """chainctx 의 매칭 규칙만 떼어 검증한다(네트워크 없이)."""
    import difflib
    watch = {chainctx._norm(s) for s in symbols}
    out = []
    for a in pools:
        base = chainctx._norm((a.get("name") or "").split("/")[0])
        if not base or len(base) < 4 or base in watch:
            continue
        for w in watch:
            ratio = difflib.SequenceMatcher(None, base, w).ratio()
            contains = (w in base or base in w) and abs(len(base) - len(w)) <= 3
            if contains or ratio >= 0.85:
                out.append({"target": w, "name": a["name"], "ratio": round(ratio, 2)})
                break
    return out


class TestThresholdOverride(unittest.TestCase):
    def test_per_symbol_override_wins(self):
        st = state(profile="launchpad", liq_mcap_pct=2.8,
                   thresholds={"wl_liq_mcap_min_pct": 2.0})
        self.assertNotIn("LIQ_THIN", [a["code"] for a in wl.evaluate(st, [], PCFG, NOW)])
        st2 = state(profile="meme", liq_mcap_pct=2.8, turnover_pct=27.0)
        self.assertIn("LIQ_THIN", [a["code"] for a in wl.evaluate(st2, [], PCFG, NOW)])


class TestDeltas(unittest.TestCase):
    """변화량 주석 — 직전(prev)·전일(day) 기준과 렌더 화살표."""

    def _hist(self):
        return [
            hist(24, px={"PONS": 1.10}, mcap={"PONS": 110.0}, liq={"PONS": 25.0},
                 vol24={"PONS": 11.0}, rev24={"PONS": 10.0}, pf={"PONS": 4.0},
                 lp_share={"PONS": 88.0}, rank={"PONS": 3}),
            hist(1, px={"PONS": 0.98}, mcap={"PONS": 98.0}, liq={"PONS": 20.0},
                 vol24={"PONS": 10.0}, rev24={"PONS": 12.0}, pf={"PONS": 3.6},
                 lp_share={"PONS": 82.0}, rank={"PONS": 2}),
        ]

    def test_no_history_means_no_delta_and_no_crash(self):
        st = wl.annotate_deltas(state(), [], NOW)
        self.assertEqual(st["items"][0]["delta"], {"prev": None, "day": None})
        lines = wl.render_telegram(st, [], CFG)
        self.assertIn("비교 기준 없음", lines[0])
        self.assertFalse(any("▲" in ln or "▼" in ln for ln in lines))

    def test_prev_and_day_are_distinct_references(self):
        st = state(rev24=12.0, pf=3.6, lp_share=82.0, rank=2, turnover_pct=10.0)
        wl.annotate_deltas(st, self._hist(), NOW)
        d = st["items"][0]["delta"]
        self.assertAlmostEqual(d["prev"]["px"], (1.0 - 0.98) / 0.98 * 100, places=6)
        self.assertAlmostEqual(d["day"]["px"], (1.0 - 1.10) / 1.10 * 100, places=6)
        self.assertAlmostEqual(d["day"]["lp_share_pp"], -6.0)
        self.assertEqual(d["day"]["rank_prev"], 3)
        self.assertEqual(d["day"]["pf_prev"], 4.0)
        # 회전율은 스냅샷에 없어도 vol24/mcap 로 복원된다
        self.assertAlmostEqual(d["prev"]["turnover_pp"], 10.0 - 10.0 / 98.0 * 100, places=6)

    def test_same_snapshot_is_not_used_twice(self):
        st = wl.annotate_deltas(state(), [hist(24, px={"PONS": 1.1})], NOW)
        refs = st["delta_refs"]
        # 직전 기준은 12h 이내여야 하므로 24h 전 스냅샷은 '전일'로만 잡힌다
        self.assertIsNone(refs["prev_epoch"])
        self.assertIsNotNone(refs["day_epoch"])

    def test_render_prefers_day_basis_and_shows_both_price_arrows(self):
        st = state(rev24=12.0, pf=3.6, lp_share=82.0, rank=2)
        wl.annotate_deltas(st, self._hist(), NOW)
        text = "\n".join(wl.render_telegram(st, [], CFG))
        self.assertIn("전일", text)
        self.assertIn("직전▲2.0%", text)
        self.assertIn("전일▼9.1%", text)
        self.assertIn("배수 4.0→3.6배", text)
        self.assertIn("점유율 82% ▼6.0pp", text)
        self.assertIn("시총 3위→2위", text)
        self.assertIn("주요 변화", text)

    def test_render_falls_back_to_prev_when_no_day(self):
        st = state()
        wl.annotate_deltas(st, [hist(1, px={"PONS": 0.98}, mcap={"PONS": 98.0},
                                     liq={"PONS": 20.0}, vol24={"PONS": 10.0})], NOW)
        text = "\n".join(wl.render_telegram(st, [], CFG))
        self.assertIn("이력 축적 중", text)
        self.assertIn("직전▲2.0%", text)
        self.assertNotIn("전일▲", text)

    def test_flat_is_a_dash_not_a_number(self):
        self.assertEqual(wl._arrow_pct(0.2), "─")
        self.assertEqual(wl._arrow_pp(0.1), "─")
        self.assertEqual(wl._arrow_pct(None), "")

    def test_evaluate_annotates_in_place(self):
        st = state()
        wl.evaluate(st, [hist(1, px={"PONS": 0.98})], CFG, NOW)
        self.assertIn("delta", st["items"][0])
