import datetime as dt
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import catalyst_radar as cr          # noqa: E402
import sensors                        # noqa: E402
import universe as uni                # noqa: E402
import verify                         # noqa: E402

TODAY = dt.date(2026, 9, 7)


class TestUniverseRule(unittest.TestCase):
    def test_stable_and_exchange_tokens_excluded(self):
        for sym in ("USDT", "USDC", "USDE", "OKB", "KCS", "XAUT", "PAXG", "GT"):
            self.assertIsNotNone(uni._is_asset_token(sym, "Dexs"), sym)

    def test_real_protocols_survive_narrow_exclusion(self):
        # RWA·Bridge·Basis Trading 은 거버넌스가 살아있어 남겨야 한다
        for sym, cat in (("LINK", "Bridge"), ("ONDO", "RWA"), ("ENA", "Basis Trading"),
                         ("AAVE", "Lending"), ("UNI", "Dexs")):
            self.assertIsNone(uni._is_asset_token(sym, cat), sym)

    def test_stablecoin_category_excluded(self):
        self.assertIsNotNone(uni._is_asset_token("XYZ", "Stablecoin"))

    def test_norm(self):
        self.assertEqual(uni._norm("PancakeSwap "), "pancakeswap")
        self.assertEqual(uni._norm("Ethereum-Classic"), "ethereumclassic")

    def test_parent_protocol_inheritance(self):
        protos = [
            {"name": "Aave V2", "slug": "aave-v2", "tvl": 100, "parentProtocol": "parent#aave"},
            {"name": "Aave V3", "slug": "aave-v3", "tvl": 300, "parentProtocol": "parent#aave"},
        ]
        by_gecko, _, tvl, inherited = uni._index_protocols(protos)
        self.assertEqual(inherited, 1)
        self.assertIn("aave", by_gecko)
        self.assertEqual(tvl["aave"], 400)

    def test_inclusion_rule_is_frozen_and_documented(self):
        self.assertIn("frozen_at", uni.INCLUSION_RULE)
        self.assertGreaterEqual(len(uni.INCLUSION_RULE["rule"]), 4)


class TestClassification(unittest.TestCase):
    def test_supply_events(self):
        for t in ("SIMD-0550: double the disinflation rate",
                  "Increase daily SOL burn", "Token unlock schedule change"):
            self.assertEqual(sensors.classify(t), "SUPPLY", t)

    def test_infra_events(self):
        for t in ("Transaction V1 mainnet activation",
                  "Alpenglow consensus upgrade", "reduce rent by 90%"):
            self.assertEqual(sensors.classify(t), "INFRA", t)

    def test_generic_falls_back(self):
        self.assertEqual(sensors.classify("Update README typo"), "FEATURE")


class TestScoring(unittest.TestCase):
    def test_proximity_curve_is_monotone_near_dday(self):
        p = lambda d: cr.proximity((TODAY + dt.timedelta(days=d)).isoformat(), TODAY)
        self.assertEqual(p(3), 1.0)
        self.assertGreater(p(3), p(20))
        self.assertGreater(p(20), p(60))
        self.assertGreater(p(60), p(200))

    def test_past_events_decay(self):
        self.assertLess(cr.proximity((TODAY - dt.timedelta(days=40)).isoformat(), TODAY), 0.2)

    def test_unknown_date_is_neutral_not_zero(self):
        self.assertEqual(cr.proximity(None, TODAY), 0.55)
        self.assertEqual(cr.proximity("not-a-date", TODAY), 0.55)

    def test_supply_beats_feature_at_same_stage(self):
        base = {"stage": "PASSED", "when": (TODAY + dt.timedelta(days=5)).isoformat()}
        s = cr.score_event(dict(base, impact="SUPPLY"), TODAY)
        f = cr.score_event(dict(base, impact="FEATURE"), TODAY)
        self.assertGreater(s, f)

    def test_certainty_is_monotone_across_stages(self):
        vals = [sensors.CERTAINTY[s] for s in sensors.STAGE_ORDER]
        self.assertEqual(vals, sorted(vals))

    def test_grade_boundaries(self):
        self.assertEqual(cr.grade(cr.GRADE_A), "A")
        self.assertEqual(cr.grade(cr.GRADE_A - 0.1), "B")
        self.assertEqual(cr.grade(cr.GRADE_B - 0.1), "C")

    def test_sol_case_would_have_graded_A(self):
        """SIMD-0553 가결(8/27) → 렌트 게이트 활성화(9/3) 구간의 재현."""
        ev = {"impact": "SUPPLY", "stage": "PASSED", "when": "2026-08-27",
              "event_date": "2026-09-03", "trust": 1.0}
        self.assertGreaterEqual(cr.score_event(ev, dt.date(2026, 8, 28)), cr.GRADE_A)


class TestStateTransition(unittest.TestCase):
    def _ev(self, key="k1", stage="PROPOSED", when=None):
        return {"key": key, "symbol": "SOL", "source": "proposal", "stage": stage,
                "title": "burn proposal", "url": "u", "impact": "SUPPLY", "when": when}

    def test_first_sight_is_new_and_registers_price(self):
        kept, tr = cr.merge_state({"events": []}, [self._ev()], TODAY, {"SOL": 100.0})
        self.assertEqual(len(tr), 1)
        self.assertEqual(tr[0]["transition"], "NEW")
        self.assertEqual(kept[0]["registry"]["price_at_detect"], 100.0)

    def test_same_stage_next_day_is_not_a_transition(self):
        prev = {"events": [dict(self._ev(), first_seen="2026-09-01",
                                stage_history=[], registry={})]}
        _, tr = cr.merge_state(prev, [self._ev()], TODAY, {})
        self.assertEqual(tr, [])

    def test_stage_up_is_detected_and_logged(self):
        prev = {"events": [dict(self._ev(stage="VOTING"), first_seen="2026-09-01",
                                stage_history=[], registry={})]}
        kept, tr = cr.merge_state(prev, [self._ev(stage="PASSED")], TODAY, {})
        self.assertEqual(tr[0]["transition"], "STAGE_UP")
        self.assertEqual(tr[0]["prev_stage"], "VOTING")
        self.assertEqual(kept[0]["stage_history"][-1]["stage"], "PASSED")

    def test_stage_downgrade_never_fires_an_alert(self):
        prev = {"events": [dict(self._ev(stage="ACTIVATED"), first_seen="2026-09-01",
                                stage_history=[], registry={})]}
        _, tr = cr.merge_state(prev, [self._ev(stage="VOTING")], TODAY, {})
        self.assertEqual(tr, [])

    def test_first_seen_is_preserved_across_runs(self):
        prev = {"events": [dict(self._ev(), first_seen="2026-08-01",
                                stage_history=[], registry={"detected_at": "2026-08-01"})]}
        kept, _ = cr.merge_state(prev, [self._ev(stage="VOTING")], TODAY, {"SOL": 999})
        self.assertEqual(kept[0]["first_seen"], "2026-08-01")
        self.assertEqual(kept[0]["registry"]["detected_at"], "2026-08-01")


class TestRendering(unittest.TestCase):
    def _payload(self, **kw):
        p = {"as_of_kst": "2026-09-07 06:52", "universe": {"total": 44, "chain": 34, "protocol": 10},
             "coverage": 1.0, "data_status": "OK", "event_count": 3, "transitions": [],
             "calendar": [], "all_events": [], "unmatched_all": [], "unmatched_top": [],
             "frozen_at": "2026-09-07", "dashboard_url": "https://x/"}
        p.update(kw)
        return p

    def test_html_injection_in_title_is_escaped(self):
        ev = {"symbol": "SOL", "grade": "A", "impact": "SUPPLY", "stage": "PASSED",
              "transition": "NEW", "title": "<script>alert(1)</script>", "when": None,
              "score": 90.0, "url": "#"}
        msg = cr.render_message(self._payload(transitions=[ev]))
        self.assertNotIn("<script>", msg)
        self.assertIn("&lt;script&gt;", msg)

    def test_silence_is_labelled_not_implied(self):
        msg = cr.render_message(self._payload())
        self.assertIn("신규 승격 없음", msg)

    def test_degraded_status_is_surfaced(self):
        msg = cr.render_message(self._payload(data_status="DEGRADED", coverage=0.4))
        self.assertIn("DEGRADED", msg)
        self.assertIn("부분 관측", msg)

    def test_dashboard_always_lists_unmatched_audit(self):
        html = cr.render_dashboard(self._payload(
            unmatched_all=[{"symbol": "KAS", "rank": 40}]))
        self.assertIn("미매칭 감사", html)
        self.assertIn("KAS", html)

    def test_dday_formatting(self):
        self.assertEqual(cr.dday("2026-09-10", TODAY), "D-3")
        self.assertEqual(cr.dday("2026-09-07", TODAY), "D-DAY")
        self.assertEqual(cr.dday("2026-09-01", TODAY), "D+6")
        self.assertEqual(cr.dday(None, TODAY), "일정미정")


class TestNetworkGuards(unittest.TestCase):
    def test_circuit_breaker_blocks_after_threshold(self):
        import net
        net.BREAKER.clear()
        net.BREAKER["api.github.com"] = "rate limit 소진"
        with self.assertRaises(net.FetchError):
            net.fetch("https://api.github.com/repos/x/y")
        net.BREAKER.clear()

    def test_news_sensor_requires_a_date_and_a_keyword(self):
        self.assertTrue(sensors.DATE_RE.search("Transaction V1 goes live Sept 9"))
        self.assertFalse(sensors.DATE_RE.search("SOL price rallies on optimism"))


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestHardening(unittest.TestCase):
    """실측에서 잡힌 오탐에 대한 회귀 테스트."""

    def test_ci_tags_are_not_releases(self):
        for tag in ("build-00388", "sdlt-pass-00387", "nightly-42", ""):
            self.assertFalse(sensors.is_real_release(tag), tag)
        for tag in ("v4.3.0-rc.0", "v0.77.0", "1.17.5"):
            self.assertTrue(sensors.is_real_release(tag), tag)

    def test_past_month_titles_are_dropped(self):
        sep = dt.date(2026, 9, 7)
        self.assertTrue(sensors._is_past_month("July 12", sep))
        self.assertFalse(sensors._is_past_month("Oct 5", sep))
        self.assertFalse(sensors._is_past_month("Sep 9", sep))

    def test_event_date_only_future_within_horizon(self):
        today = dt.date(2026, 9, 7)
        self.assertEqual(
            sensors.extract_event_date("mainnet on September 9", today), "2026-09-09")
        self.assertEqual(
            sensors.extract_event_date("unlock 2026-10-05", today), "2026-10-05")
        self.assertIsNone(sensors.extract_event_date("shipped July 12", today))
        self.assertIsNone(sensors.extract_event_date("no date here", today))

    def test_editorial_commits_filtered(self):
        self.assertFalse(sensors.STATUS_CHANGE_RE.search(
            "Update EIP-8037: correct the general state-gas charge timing"))
        self.assertTrue(sensors.STATUS_CHANGE_RE.search(
            "Add EIP-8246: L2 burn usage"))
        self.assertTrue(sensors.STATUS_CHANGE_RE.search("Move to Last Call"))

    def test_release_dedupe_caps_per_repo(self):
        evs = [{"source": "release", "key": "r/x@v%d" % i} for i in range(6)]
        evs.append({"source": "proposal", "key": "p1"})
        out = sensors.dedupe_releases(evs, per_repo=2)
        self.assertEqual(len([e for e in out if e["source"] == "release"]), 2)
        self.assertEqual(len([e for e in out if e["source"] == "proposal"]), 1)

    def test_news_trust_discounted_but_dated_recovers(self):
        base = {"impact": "INFRA", "stage": "SCHEDULED", "trust": 0.35}
        self.assertAlmostEqual(cr.effective_trust(base), 0.35)
        dated = dict(base, event_date="2026-09-09")
        self.assertGreater(cr.effective_trust(dated), 0.5)
        self.assertLessEqual(cr.effective_trust(dict(dated, trust=0.95)), 1.0)

    def test_unseen_events_expire(self):
        today = dt.date(2026, 9, 7)
        old = today - dt.timedelta(days=cr.STALE_DAYS + 2)
        prev = {"events": [{"key": "gone", "stage": "SCHEDULED", "grade": "B",
                            "first_seen": old.isoformat(), "last_seen": old.isoformat(),
                            "symbol": "X", "impact": "INFRA", "when": None}]}
        kept, _ = cr.merge_state(prev, [], today, {})
        self.assertEqual([k["key"] for k in kept], [])


class TestVerification(unittest.TestCase):
    """사전등록 채점 — 임계 조정 대신 표본 외 누적으로만 검증한다."""

    def _ev(self, sym="SOL", px0=100.0, btc0=50000.0, days_ago=31, impact="SUPPLY"):
        d0 = (dt.date(2026, 9, 7) - dt.timedelta(days=days_ago)).isoformat()
        return {"symbol": sym, "impact": impact, "source": "proposal",
                "registry": {"detected_at": d0, "price_at_detect": px0,
                             "btc_at_detect": btc0, "score_at_detect": 50.0}}

    def test_excess_return_is_relative_to_btc(self):
        ev = self._ev()
        # 종목 +20%, BTC +20% → 초과수익 0 (시장 상승을 신호 성과로 오인하지 않는다)
        verify.accrue([ev], {"SOL": 120.0, "BTC": 60000.0}, dt.date(2026, 9, 7))
        self.assertAlmostEqual(ev["registry"]["t30"], 0.0, places=1)

    def test_horizon_recorded_once_only(self):
        ev = self._ev()
        today = dt.date(2026, 9, 7)
        verify.accrue([ev], {"SOL": 120.0, "BTC": 50000.0}, today)
        first = ev["registry"]["t30"]
        verify.accrue([ev], {"SOL": 999.0, "BTC": 50000.0}, today)
        self.assertEqual(ev["registry"]["t30"], first)   # 재기록 금지 = look-ahead 차단

    def test_not_yet_matured_is_skipped(self):
        ev = self._ev(days_ago=3)
        verify.accrue([ev], {"SOL": 200.0, "BTC": 50000.0}, dt.date(2026, 9, 7))
        self.assertNotIn("t7", ev["registry"])
        self.assertNotIn("t30", ev["registry"])

    def test_small_sample_reports_no_number(self):
        evs = [self._ev(sym="S%d" % i) for i in range(3)]
        for e in evs:
            e["registry"]["t30"] = 10.0
        rows = verify.summarize(evs)
        allrow = [r for r in rows if r["bucket"] == "ALL" and r["horizon"] == 30][0]
        self.assertEqual(allrow["status"], "표본 부족")
        self.assertNotIn("median", allrow)
        self.assertIn("표본 부족", verify.render_line(rows))

    def test_median_resists_single_outlier(self):
        evs = [self._ev(sym="S%d" % i) for i in range(10)]
        for e in evs:
            e["registry"]["t30"] = 1.0
        evs[0]["registry"]["t30"] = 900.0        # 극단치 1건
        rows = verify.summarize(evs)
        allrow = [r for r in rows if r["bucket"] == "ALL" and r["horizon"] == 30][0]
        self.assertEqual(allrow["status"], "OK")
        self.assertLess(allrow["median"], 5.0)   # 평균이면 90 을 넘었을 것
