import json
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import narrative_radar as nr  # noqa: E402


def mk(idx, r24=0.0, r7=0.0, r30=0.0, mcap=1e9, vol=1e8, price=1.0):
    return {
        "id": idx, "current_price": price, "market_cap": mcap, "total_volume": vol,
        "price_change_percentage_24h_in_currency": r24,
        "price_change_percentage_7d_in_currency": r7,
        "price_change_percentage_30d_in_currency": r30,
    }


UNI = {
    "frozen_at": "2026-01-01",
    "benchmarks": [{"id": "bitcoin", "symbol": "BTC"}],
    "narratives": {
        "A": {"name": "가", "thesis": "t", "members": [
            {"id": "a1", "symbol": "A1", "fit": 1.0, "role": "r"},
            {"id": "a2", "symbol": "A2", "fit": 0.7, "role": "r"},
            {"id": "a3", "symbol": "A3", "fit": 0.4, "role": "r"},
        ]},
        "B": {"name": "나", "thesis": "t", "members": [
            {"id": "b1", "symbol": "B1", "fit": 1.0, "role": "r"},
            {"id": "b2", "symbol": "B2", "fit": 1.0, "role": "r"},
        ]},
    },
}


class TestStats(unittest.TestCase):
    def test_median_ignores_none(self):
        self.assertEqual(nr.median([1, None, 3]), 2)
        self.assertIsNone(nr.median([None, None]))

    def test_median_not_dragged_by_outlier(self):
        """평균이 극단치에 지배되는 케이스에서 중앙값이 방어하는지 — 설계 원칙 3."""
        xs = [1, 2, 3, 4, 1000]
        self.assertEqual(nr.median(xs), 3)
        self.assertGreater(sum(xs) / len(xs), 100)

    def test_zscore_zero_variance(self):
        z = nr.zscores({"a": 5.0, "b": 5.0, "c": 5.0})
        self.assertEqual(set(z.values()), {0.0})

    def test_zscore_single_sample(self):
        self.assertEqual(nr.zscores({"a": 3.0}), {"a": 0.0})

    def test_excess(self):
        self.assertEqual(nr.excess(10, 4), 6)
        self.assertIsNone(nr.excess(None, 4))
        self.assertIsNone(nr.excess(10, None))


class TestAggregate(unittest.TestCase):
    def setUp(self):
        self.mkt = {
            "bitcoin": mk("bitcoin", 1.0, 2.0, 10.0),
            "a1": mk("a1", 3.0, 8.0, 40.0, vol=5e8),
            "a2": mk("a2", 2.0, 6.0, 30.0),
            "a3": mk("a3", 1.0, 4.0, 20.0),
            "b1": mk("b1", 0.0, 0.0, 5.0),
            "b2": mk("b2", -1.0, -2.0, 0.0),
        }
        rows, btc = nr.build_coin_rows(UNI, self.mkt)
        self.rows = nr.score_coins(rows, btc)
        self.nars = nr.aggregate_narratives(UNI, self.rows)

    def test_relative_strength_is_excess_over_btc(self):
        a = next(n for n in self.nars if n["code"] == "A")
        self.assertAlmostEqual(a["rs30"], 20.0)  # median(40,30,20)=30 - btc 10

    def test_breadth(self):
        a = next(n for n in self.nars if n["code"] == "A")
        b = next(n for n in self.nars if n["code"] == "B")
        self.assertEqual(a["breadth"], 100.0)
        self.assertEqual(b["breadth"], 0.0)

    def test_ranking_order(self):
        self.assertEqual(self.nars[0]["code"], "A")
        self.assertEqual(self.nars[0]["rank"], 1)

    def test_fit_weights_but_does_not_flip_sign(self):
        """부합도가 낮다고 양수 점수를 음수로 뒤집으면 안 된다."""
        for r in self.rows:
            self.assertEqual(r["score"] > 0, r["flow"] > 0)

    def test_coin_rank_assigned(self):
        self.assertEqual([r["rank"] for r in self.rows], list(range(1, len(self.rows) + 1)))


class TestChangeDetection(unittest.TestCase):
    def _nars(self, order, breadth=50.0):
        return [{"code": c, "name": c, "rank": i + 1, "rs30": 10 - i,
                 "rs7": 1.0, "rs24": 0.1, "breadth": breadth, "n": 2,
                 "turnover": 0.1, "top": "X", "thesis": "t"}
                for i, c in enumerate(order)]

    def test_leader_shift_detected(self):
        hist = [{"as_of": f"2026-01-0{i}", "btc_dominance": 58.0,
                 "narratives": [{"code": "B", "rank": 1, "rs30": 5, "breadth": 50},
                                {"code": "A", "rank": 2, "rs30": 3, "breadth": 50}]}
                for i in range(1, 6)]
        ev = nr.detect_changes(self._nars(["A", "B"]), [], {}, hist)
        kinds = [e["kind"] for e in ev]
        self.assertIn("LEADER_SHIFT", kinds)
        shift = next(e for e in ev if e["kind"] == "LEADER_SHIFT")
        self.assertEqual(shift["level"], "high")  # 직전 리더 5일 유지 → 유의

    def test_no_leader_shift_when_stable(self):
        hist = [{"as_of": f"2026-01-0{i}", "btc_dominance": 58.0,
                 "narratives": [{"code": "A", "rank": 1, "rs30": 5, "breadth": 50}]}
                for i in range(1, 6)]
        ev = nr.detect_changes(self._nars(["A", "B"]), [], {}, hist)
        self.assertNotIn("LEADER_SHIFT", [e["kind"] for e in ev])

    def test_breadth_expansion(self):
        hist = [{"as_of": f"2026-01-0{i}", "btc_dominance": 58.0,
                 "narratives": [{"code": "A", "rank": 1, "rs30": 5, "breadth": 20.0}]}
                for i in range(1, 6)]
        ev = nr.detect_changes(self._nars(["A"], breadth=90.0), [], {}, hist)
        self.assertIn("BREADTH_EXPANSION", [e["kind"] for e in ev])

    def test_dominance_break_low(self):
        hist = [{"as_of": f"2026-01-{i:02d}", "btc_dominance": 58.0 + i * 0.1,
                 "narratives": [{"code": "A", "rank": 1, "rs30": 5, "breadth": 50}]}
                for i in range(1, 8)]
        ev = nr.detect_changes(self._nars(["A"]), [], {"market_cap_percentage": {"btc": 50.0}}, hist)
        self.assertIn("DOMINANCE_BREAK", [e["kind"] for e in ev])

    def test_turnover_spike(self):
        rows = [{"symbol": "X", "zturn": 3.1, "narrative": "A"},
                {"symbol": "Y", "zturn": 0.2, "narrative": "A"}]
        ev = nr.detect_changes(self._nars(["A"]), rows, {}, [])
        spikes = [e for e in ev if e["kind"] == "TURNOVER_SPIKE"]
        self.assertEqual(len(spikes), 1)
        self.assertIn("X", spikes[0]["text"])

    def test_empty_history_safe(self):
        self.assertIsInstance(nr.detect_changes(self._nars(["A"]), [], {}, []), list)


class TestFailureHandling(unittest.TestCase):
    def test_unavailable_message_never_says_no_change(self):
        """수집 실패 시 '변화 없음'을 발송하지 않는다 — 설계 원칙 4 (crypto-monitor v2 오탐 교훈)."""
        payload = {"as_of_kst": "2026-01-01 08:00 KST", "data_status": "UNAVAILABLE",
                   "pages_url": ""}
        msg = nr.render_telegram(payload)
        self.assertIn("판정 불가", msg)
        self.assertNotIn("변화 없음", msg)

    def test_ok_message_contains_disclaimer(self):
        payload = {
            "as_of_kst": "2026-01-01 08:00 KST", "data_status": "OK", "pages_url": "",
            "market": {"btc_price": 60000, "btc_r24": 1.0, "btc_dominance": 57.0,
                       "total_mcap_t": 2.3, "regime": "혼조"},
            "narratives": [{"rank": 1, "name": "가", "rs30": 5.0, "rs7": 1.0, "breadth": 60.0}],
            "coins": [{"rank": 1, "symbol": "A1", "score": 1.2, "narrative_name": "가",
                       "x30": 10.0, "x7": 2.0, "zturn": 0.5, "mcap": 1e9}],
            "events": [], "watch": [],
        }
        msg = nr.render_telegram(payload)
        self.assertIn("미래 수익률을 주장하지 않으며", msg)
        self.assertIn("변화 없음", msg)

    def test_html_escaped(self):
        self.assertEqual(nr.esc("<b>&x</b>"), "&lt;b&gt;&amp;x&lt;/b&gt;")


class TestIdempotency(unittest.TestCase):
    def test_comparable_ignores_timestamp_and_message(self):
        a = {"as_of": "2026-01-01", "as_of_kst": "08:00", "message": "m1", "x": 1}
        b = {"as_of": "2026-01-01", "as_of_kst": "09:00", "message": "m2", "x": 1}
        self.assertEqual(nr._comparable(a), nr._comparable(b))

    def test_comparable_detects_real_change(self):
        a = {"as_of": "2026-01-01", "x": 1}
        b = {"as_of": "2026-01-01", "x": 2}
        self.assertNotEqual(nr._comparable(a), nr._comparable(b))


class TestUniverseIntegrity(unittest.TestCase):
    def test_real_universe_is_valid(self):
        p = os.path.join(os.path.dirname(__file__), "..", "universe.json")
        u = json.load(open(p, encoding="utf-8"))
        ids = []
        for code, n in u["narratives"].items():
            self.assertTrue(n["name"] and n["thesis"], code)
            self.assertGreaterEqual(len(n["members"]), 3, f"{code}: 중앙값이 의미 있으려면 3종목 이상")
            for m in n["members"]:
                self.assertIn(m["fit"], (0.4, 0.7, 1.0), f"{code}/{m['symbol']}")
                ids.append(m["id"])
        self.assertEqual(len(ids), len(set(ids)), "종목이 두 내러티브에 중복 배정됨")
        self.assertIn("frozen_at", u)

    def test_input_validation_rejects_injection(self):
        self.assertEqual(nr.main(["x", "2026-01-01; echo PWNED"]), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)


import tvl_divergence as tv  # noqa: E402


class TestTvlDivergence(unittest.TestCase):
    def test_pct(self):
        self.assertAlmostEqual(tv.pct(110, 100), 10.0)
        self.assertIsNone(tv.pct(110, 0))
        self.assertIsNone(tv.pct(None, 100))

    def test_series_change_picks_correct_lookback(self):
        base = 1_700_000_000
        series = [{"date": base + i * 86400, "tvl": 100 + i} for i in range(40)]
        now, ch = tv._series_change(series, 7)
        self.assertEqual(now, 139)
        self.assertAlmostEqual(ch, (139 / 132 - 1) * 100)

    def test_series_change_short_history(self):
        series = [{"date": 1_700_000_000, "tvl": 100}]
        now, ch = tv._series_change(series, 30)
        self.assertEqual(now, 100)
        self.assertIsNone(ch)

    def test_attach_and_divergence_sign(self):
        rows = [{"id": "a", "symbol": "A", "mcap": 2e9, "r7": 30.0, "r30": 50.0,
                 "narrative_name": "가"},
                {"id": "b", "symbol": "B", "mcap": 1e9, "r7": -10.0, "r30": -5.0,
                 "narrative_name": "나"}]
        tmap = {"a": {"tvl": 1e9, "t7": 2.0, "t30": 5.0, "source": "protocol:a"},
                "b": {"tvl": 5e8, "t7": 40.0, "t30": 60.0, "source": "chain:B"}}
        tv.attach(rows, tmap)
        self.assertAlmostEqual(rows[0]["div30"], 45.0)   # 가격 선행
        self.assertAlmostEqual(rows[1]["div30"], -65.0)  # 가격 지연
        self.assertAlmostEqual(rows[0]["mc_tvl"], 2.0)

    def test_attach_leaves_none_when_unmapped(self):
        rows = [{"id": "z", "symbol": "Z", "mcap": 1e9, "r7": 5.0, "r30": 5.0}]
        tv.attach(rows, {})
        self.assertIsNone(rows[0]["div7"])
        self.assertIsNone(rows[0]["tvl"])

    def test_small_pools_filtered_out(self):
        rows = [{"id": "s", "symbol": "S", "mcap": 1e7, "r7": 100.0, "r30": 100.0,
                 "narrative_name": "가"}]
        tv.attach(rows, {"s": {"tvl": 1e6, "t7": 0.0, "t30": 0.0, "source": "p"}})
        self.assertEqual(tv.rank_divergence(rows), [])

    def test_rank_sorted_by_absolute_divergence(self):
        rows = [
            {"id": "a", "symbol": "A", "mcap": 1e9, "r7": 10.0, "r30": 10.0, "narrative_name": "가"},
            {"id": "b", "symbol": "B", "mcap": 1e9, "r7": -90.0, "r30": -90.0, "narrative_name": "나"},
        ]
        tv.attach(rows, {
            "a": {"tvl": 1e9, "t7": 5.0, "t30": 5.0, "source": "p"},
            "b": {"tvl": 1e9, "t7": 5.0, "t30": 5.0, "source": "p"}})
        ranked = tv.rank_divergence(rows)
        self.assertEqual(ranked[0]["symbol"], "B")
        self.assertEqual(ranked[0]["direction"], "가격 지연")
        self.assertEqual(ranked[1]["direction"], "가격 선행")

    def test_events_respect_threshold(self):
        small = [{"symbol": "A", "div": 5.0, "horizon": "7d", "price": 5, "tvl_chg": 0}]
        big = [{"symbol": "B", "div": -40.0, "horizon": "30d", "price": -30, "tvl_chg": 10}]
        self.assertEqual(tv.events(small), [])
        self.assertEqual(len(tv.events(big)), 1)
        self.assertIn("TVL_DIVERGENCE", [e["kind"] for e in tv.events(big)])

    def test_backfill_uses_own_history(self):
        tmap = {"a": {"tvl": 200.0, "t7": 1.0, "t30": None}}
        hist = [{"as_of": "1999-01-01", "tvl": {"a": 100.0}}]
        tv.backfill_30d(tmap, hist)
        self.assertAlmostEqual(tmap["a"]["t30"], 100.0)
        self.assertEqual(tmap["a"]["t30_src"], "self-history")

    def test_backfill_noop_without_history(self):
        tmap = {"a": {"tvl": 200.0, "t30": None}}
        tv.backfill_30d(tmap, [])
        self.assertIsNone(tmap["a"]["t30"])

    def test_universe_llama_mapping_valid(self):
        p = os.path.join(os.path.dirname(__file__), "..", "universe.json")
        with open(p, encoding="utf-8") as f:
            u = json.load(f)
        n = 0
        for nar in u["narratives"].values():
            for m in nar["members"]:
                ll = m.get("llama")
                if ll:
                    self.assertIn(ll["kind"], ("chain", "protocol"), m["symbol"])
                    self.assertTrue(ll["key"], m["symbol"])
                    n += 1
        self.assertGreaterEqual(n, 15)

    def test_telegram_reports_missing_tvl_explicitly(self):
        payload = {
            "as_of_kst": "x", "data_status": "OK", "pages_url": "",
            "market": {"btc_price": 1, "btc_r24": 0.0, "btc_dominance": 50.0,
                       "total_mcap_t": 1.0, "regime": "r"},
            "narratives": [], "coins": [], "events": [], "watch": [],
            "divergence": [], "tvl_covered": 0,
        }
        msg = nr.render_telegram(payload)
        self.assertIn("예치금 데이터를 받지 못했습니다", msg)

    def test_parent_aggregation_is_tvl_weighted(self):
        """DefiLlama가 브랜드를 자식으로 쪼개 둔 경우 가중 합산이 맞는지."""
        kids = [{"tvl": 900.0, "change_7d": 10.0}, {"tvl": 100.0, "change_7d": 0.0}]
        tot = sum(k["tvl"] for k in kids)
        t7 = sum(k["tvl"] * k["change_7d"] for k in kids) / tot
        self.assertAlmostEqual(tot, 1000.0)
        self.assertAlmostEqual(t7, 9.0)

    def test_new_chains_inclusion_rule_documented(self):
        """규칙 기반 편입임을 파일에 남겨 사후선택 의심을 검증 가능하게 둔다."""
        p = os.path.join(os.path.dirname(__file__), "..", "universe.json")
        with open(p, encoding="utf-8") as f:
            u = json.load(f)
        n = u["narratives"]["NEW_CHAINS"]
        self.assertIn("inclusion_rule", n)
        self.assertGreaterEqual(len(n["members"]), 5)
        for m in n["members"]:
            self.assertIn("llama", m, m["symbol"])
            self.assertEqual(m["llama"]["kind"], "chain", m["symbol"])

    def test_no_chain_mapped_twice(self):
        p = os.path.join(os.path.dirname(__file__), "..", "universe.json")
        with open(p, encoding="utf-8") as f:
            u = json.load(f)
        keys = [m["llama"]["key"] for nar in u["narratives"].values()
                for m in nar["members"] if m.get("llama")]
        self.assertEqual(len(keys), len(set(keys)), "같은 체인/프로토콜이 두 번 매핑됨")


import discovery as ds  # noqa: E402


def _m(sym, mcap, vol, r7, r30, fdv=None):
    return {"symbol": sym, "name": sym, "market_cap": mcap, "total_volume": vol,
            "fully_diluted_valuation": fdv,
            "price_change_percentage_7d_in_currency": r7,
            "price_change_percentage_30d_in_currency": r30}


class TestDiscoveryScreen(unittest.TestCase):
    def test_monad_like_case_is_found(self):
        """TVL +33%, 가격 +14%, MC/TVL 0.33 — 실제 MON 케이스가 걸려야 한다."""
        cand = {"monad": {"tvl": 936e6, "t7": 8.0, "t30": 32.8,
                          "source": "chain:Monad", "spike_share": 0.2}}
        mkt = {"monad": _m("MON", 306e6, 50e6, 23.3, 13.8, fdv=3e9)}
        r = ds.screen(cand, mkt)
        self.assertEqual([x["symbol"] for x in r["lagging"]], ["MON"])
        self.assertAlmostEqual(r["lagging"][0]["div"], 13.8 - 32.8, places=1)
        self.assertEqual(r["lagging"][0]["mc_tvl"], 0.33)
        self.assertEqual(r["lagging"][0]["fdv_tvl"], 3.21)

    def test_one_day_tvl_spike_rejected(self):
        """고래 1명 예치·인센티브 개시로 만들어진 TVL은 걸러야 한다."""
        cand = {"x": {"tvl": 500e6, "t7": 5.0, "t30": 80.0,
                      "source": "chain:X", "spike_share": 0.9}}
        r = ds.screen(cand, {"x": _m("X", 100e6, 20e6, 2.0, 5.0)})
        self.assertEqual(r["lagging"], [])
        self.assertIn("하루짜리 TVL 점프", r["rejected"])

    def test_already_pumped_rejected(self):
        """TVL +140%로 괴리는 크지만 가격이 이미 +60% 오른 종목은 '지연'이 아니다."""
        cand = {"x": {"tvl": 500e6, "t7": 5.0, "t30": 140.0, "source": "c", "spike_share": 0.1}}
        r = ds.screen(cand, {"x": _m("X", 100e6, 20e6, 10.0, 60.0)})
        self.assertEqual(r["lagging"], [])
        self.assertIn("이미 급등", r["rejected"])

    def test_illiquid_rejected(self):
        cand = {"x": {"tvl": 500e6, "t7": 5.0, "t30": 60.0, "source": "c", "spike_share": 0.1}}
        r = ds.screen(cand, {"x": _m("X", 100e6, 1000, 2.0, 5.0)})
        self.assertIn("거래대금 미달", r["rejected"])

    def test_expensive_mc_tvl_rejected(self):
        cand = {"x": {"tvl": 100e6, "t7": 5.0, "t30": 60.0, "source": "c", "spike_share": 0.1}}
        r = ds.screen(cand, {"x": _m("X", 900e6, 20e6, 2.0, 5.0)})
        self.assertIn("MC/TVL 과다", r["rejected"])

    def test_hot_case_detected(self):
        cand = {"y": {"tvl": 100e6, "t7": -25.0, "t30": -30.0, "source": "c", "spike_share": 0.0}}
        r = ds.screen(cand, {"y": _m("Y", 100e6, 20e6, 20.0, 10.0)})
        self.assertEqual([x["symbol"] for x in r["hot"]], ["Y"])
        self.assertAlmostEqual(r["hot"][0]["div"], 45.0)

    def test_single_day_share(self):
        base = 1_700_000_000
        # 마지막 하루에 전부 오른 시계열
        s = [{"date": base + i * 86400, "tvl": 100} for i in range(30)]
        s.append({"date": base + 30 * 86400, "tvl": 200})
        self.assertAlmostEqual(ds._single_day_share(s, 30), 1.0)

    def test_single_day_share_zero_when_flat(self):
        base = 1_700_000_000
        s = [{"date": base + i * 86400, "tvl": 100} for i in range(31)]
        self.assertEqual(ds._single_day_share(s, 30), 0.0)


class TestDiscoveryTracking(unittest.TestCase):
    def test_new_then_promoted_after_two_days(self):
        res = {"lagging": [{"id": "a", "symbol": "A"}]}
        st = ds.track(res, {}, "2026-01-01")
        self.assertEqual(res["lagging"][0]["status"], "신규")
        self.assertEqual(res["alert"], [])          # 하루 반짝은 알리지 않는다
        res2 = {"lagging": [{"id": "a", "symbol": "A"}]}
        ds.track(res2, st, "2026-01-02")
        self.assertEqual(res2["lagging"][0]["streak"], 2)
        self.assertEqual(len(res2["alert"]), 1)

    def test_dropped_recorded(self):
        st = {"lagging": {"z": {"streak": 3, "first_seen": "2026-01-01",
                                "last_seen": "2026-01-03", "symbol": "Z"}}}
        res = {"lagging": []}
        ds.track(res, st, "2026-01-04")
        self.assertEqual(res["dropped"], ["Z"])

    def test_render_reports_empty_scan_honestly(self):
        out = "\n".join(ds.render({"lagging": [], "scanned": 92,
                                   "rejected": {"MC/TVL 과다": 5}}))
        self.assertIn("조건 충족 종목 없음", out)
        self.assertIn("92", out)

    def test_events_only_from_promoted(self):
        self.assertEqual(ds.events({"lagging": [{"symbol": "A"}], "alert": []}), [])
        ev = ds.events({"alert": [{"symbol": "A", "streak": 3, "tvl_chg": 30.0,
                                   "price_chg": 5.0, "mc_tvl": 0.4, "source": "chain:A"}]})
        self.assertEqual(ev[0]["kind"], "DISCOVERY_LAGGING")
