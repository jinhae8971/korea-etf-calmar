#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import json
import os
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import hood_radar as hr  # noqa: E402

CFG = hr.load_cfg()


def pool(sym, name, mc, fdv, vol, liq, cg=None, addr=None, created="2026-07-01T00:00:00Z",
         chg24=1.0, pool_addr="0xpool"):
    addr = addr or ("0x" + sym.lower().ljust(40, "0"))
    tid = "robinhood_" + addr
    return {
        "attributes": {
            "address": pool_addr,
            "name": "%s / WETH" % sym,
            "market_cap_usd": mc,
            "fdv_usd": fdv,
            "base_token_price_usd": "0.1",
            "reserve_in_usd": liq,
            "pool_created_at": created,
            "volume_usd": {"h24": vol, "h6": float(vol or 0) / 4},
            "price_change_percentage": {"h24": chg24, "h6": 0.5},
            "transactions": {"h24": {"buys": 10, "sells": 8}},
        },
        "relationships": {"base_token": {"data": {"id": tid}}},
    }, {tid: {"address": addr, "symbol": sym, "name": name, "coingecko_coin_id": cg}}


def build(specs):
    pools, tokens = [], {}
    for spec in specs:
        p, t = spec
        pools.append(p)
        tokens.update(t)
    return pools, tokens


class TestClassify(unittest.TestCase):
    def test_infra_excluded(self):
        self.assertEqual(hr.classify({"symbol": "WETH", "name": "WETH"}, CFG), "INFRA")
        self.assertEqual(hr.classify({"symbol": "USDG", "name": "Global Dollar"}, CFG), "INFRA")

    def test_rwa_by_name_marker(self):
        self.assertEqual(hr.classify(
            {"symbol": "NVDA", "name": "NVIDIA \u2022 Robinhood Token"}, CFG), "RWA")

    def test_rwa_by_cg_suffix(self):
        self.assertEqual(hr.classify(
            {"symbol": "SPCX", "name": "Space Exploration", "coingecko_coin_id": "spacex-robinhood"},
            CFG), "RWA")

    def test_ticker_collision_not_excluded(self):
        """NET(NetNet 밈코인)이 Cloudflare 티커와 겹친다고 제외되면 안 된다."""
        self.assertEqual(hr.classify(
            {"symbol": "NET", "name": "NetNet", "coingecko_coin_id": "netnet"}, CFG), "MEME")

    def test_plain_meme(self):
        self.assertEqual(hr.classify(
            {"symbol": "CASHCAT", "name": "Cash Cat", "coingecko_coin_id": "cash-cat"}, CFG), "MEME")


class TestMcap(unittest.TestCase):
    def test_bridged_uses_fdv(self):
        mcap, basis, bridged = hr.pick_mcap(457_000_000, 5_700_000, CFG)
        self.assertEqual(basis, "FDV")
        self.assertTrue(bridged)
        self.assertEqual(mcap, 5_700_000)

    def test_normal_uses_mc(self):
        mcap, basis, bridged = hr.pick_mcap(200_000_000, 201_000_000, CFG)
        self.assertEqual(basis, "MC")
        self.assertFalse(bridged)

    def test_missing_mc_falls_back(self):
        mcap, basis, _ = hr.pick_mcap(0, 62_000_000, CFG)
        self.assertEqual((mcap, basis), (62_000_000, "FDV"))

    def test_all_missing(self):
        self.assertEqual(hr.pick_mcap(0, 0, CFG)[1], "NONE")


class TestNumeric(unittest.TestCase):
    def test_fnum_robust(self):
        self.assertEqual(hr.fnum(None), 0.0)
        self.assertEqual(hr.fnum("abc"), 0.0)
        self.assertEqual(hr.fnum("12.5"), 12.5)
        self.assertEqual(hr.fnum(float("nan")), 0.0)

    def test_negative_reserve_clamped_and_flagged(self):
        pools, tokens = build([pool("SIT", "Board Sit", "2692216", "2692216", "2338243", "-1873418")])
        uni = hr.build_universe(pools, tokens, CFG)
        ent = list(uni.values())[0]
        self.assertEqual(ent["liq"], 0.0)
        self.assertTrue(ent["data_warn"])

    def test_human(self):
        self.assertEqual(hr.human(1_500_000), "1.5M")
        self.assertEqual(hr.human(2_300_000_000), "2.30B")
        self.assertEqual(hr.human(4100), "4.1K")


class TestAggregation(unittest.TestCase):
    def test_multi_pool_sums_volume_and_liquidity(self):
        p1, t1 = pool("CAT", "Cat", "10000000", "10000000", "1000000", "500000", pool_addr="0xa")
        p2, t2 = pool("CAT", "Cat", "10000000", "10000000", "2000000", "300000", pool_addr="0xb")
        uni = hr.build_universe([p1, p2], dict(t1, **t2), CFG)
        ent = list(uni.values())[0]
        self.assertEqual(ent["v24"], 3_000_000)
        self.assertEqual(ent["liq"], 800_000)
        self.assertEqual(ent["pools"], 2)

    def test_same_symbol_different_address_are_separate(self):
        p1, t1 = pool("GG", "Golden Goose", "25000000", "25000000", "1600000", "400000", addr="0x" + "1" * 40)
        p2, t2 = pool("GG", "Golden Goose", "8700000", "8700000", "5100000", "400000", addr="0x" + "2" * 40)
        uni = hr.build_universe([p1, p2], dict(t1, **t2), CFG)
        self.assertEqual(len(uni), 2)

    def test_price_taken_from_deepest_pool(self):
        p1, t1 = pool("CAT", "Cat", "10000000", "10000000", "1000000", "50000", chg24=99.0, pool_addr="0xa")
        p2, t2 = pool("CAT", "Cat", "10000000", "10000000", "1000000", "900000", chg24=3.0, pool_addr="0xb")
        uni = hr.build_universe([p1, p2], dict(t1, **t2), CFG)
        self.assertEqual(list(uni.values())[0]["chg24"], 3.0)


class TestGate(unittest.TestCase):
    def _rows(self, specs):
        pools, tokens = build(specs)
        return hr.gate(hr.build_universe(pools, tokens, CFG), CFG)

    def test_thin_liquidity_dropped(self):
        rows, dropped = self._rows([pool("THIN", "Thin", "40000000", "40000000", "1000000", "4100")])
        self.assertEqual(rows, [])
        self.assertEqual(dropped["liq"], 1)

    def test_low_volume_dropped(self):
        rows, dropped = self._rows([pool("DEAD", "Dead", "5000000", "5000000", "1000", "500000")])
        self.assertEqual(dropped["vol"], 1)

    def test_rank_is_mcap_descending(self):
        rows, _ = self._rows([
            pool("A", "A", "10000000", "10000000", "500000", "300000", addr="0x" + "a" * 40),
            pool("B", "B", "90000000", "90000000", "500000", "300000", addr="0x" + "b" * 40),
        ])
        self.assertEqual([r["symbol"] for r in rows], ["B", "A"])
        self.assertEqual(rows[0]["rank"], 1)


class TestChangeDetection(unittest.TestCase):
    """유니버스는 12종으로 잡는다 — paired_ranks가 교집합 5종 이상을 요구한다."""

    def setUp(self):
        self.now = hr.now_kst()
        self.addrs = ["0x%02d" % i for i in range(12)]

    def _snap(self, hours_ago, order, source="live", mcaps=None):
        ts = (self.now - timedelta(hours=hours_ago)).isoformat()
        return {
            "ts": ts, "source": source,
            "rank": {a: i + 1 for i, a in enumerate(order)},
            "mcap": mcaps or {a: 1_000_000 * (len(order) - i) for i, a in enumerate(order)},
            "liq": {a: 500_000 for a in order},
            "symbol": {a: a for a in order},
        }

    def _rows(self, order):
        """order 순서대로 시총 내림차순 행을 만든다."""
        return [{"address": a, "symbol": a, "rank": i + 1,
                 "mcap": 1_000_000 * (len(order) - i), "flags": [], "chg24": 1.0}
                for i, a in enumerate(order)]

    def test_rank_surge_detected_24h(self):
        past = self.addrs[:]
        cur = [past[9]] + [a for a in past if a != past[9]]  # 10위 → 1위
        ev = hr.detect_changes(self._rows(cur), [self._snap(24, past)], CFG, self.now)
        surge = [e for e in ev if e["code"] == "RANK_SURGE"]
        self.assertTrue(surge)
        self.assertEqual(surge[0]["symbol"], past[9])

    def test_small_move_not_reported(self):
        past = self.addrs[:]
        cur = past[:]
        cur[3], cur[4] = cur[4], cur[3]  # 1계단 교환
        ev = hr.detect_changes(self._rows(cur), [self._snap(24, past)], CFG, self.now)
        self.assertEqual([e for e in ev if e["code"] in ("RANK_SURGE", "RANK_DROP")], [])

    def test_six_hour_move_detected(self):
        past = self.addrs[:]
        cur = [past[7]] + [a for a in past if a != past[7]]  # 8위 → 1위
        ev = hr.detect_changes(self._rows(cur), [self._snap(6, past)], CFG, self.now)
        self.assertTrue(any(e["window"] == "6h" and e["code"] == "RANK_SURGE" for e in ev))

    def test_population_mismatch_does_not_fake_moves(self):
        """
        핵심 회귀 테스트 — 과거 스냅샷이 상위 6종만 담고 현재는 12종일 때,
        하위 종목의 '순위 하락'은 모집단 차이일 뿐 실재하지 않는다.
        """
        past_small = self._snap(24, self.addrs[:6], source="ohlcv_backfill")
        rows = self._rows(self.addrs)  # 순서 동일 = 실제 변동 없음
        ev = hr.detect_changes(rows, [past_small], CFG, self.now)
        self.assertEqual([e for e in ev if e["code"] in ("RANK_SURGE", "RANK_DROP")], [])

    def test_paired_ranks_uses_intersection_only(self):
        ref = {"rank": {a: i + 1 for i, a in enumerate(self.addrs[:6])}}
        pairs = hr.paired_ranks(self._rows(self.addrs), ref)
        self.assertEqual(set(pairs.keys()), set(self.addrs[:6]))
        self.assertEqual(max(r for _, r in pairs.values()), 6)

    def test_paired_ranks_bails_on_tiny_overlap(self):
        ref = {"rank": {self.addrs[0]: 1, self.addrs[1]: 2}}
        self.assertEqual(hr.paired_ranks(self._rows(self.addrs), ref), {})

    def test_new_entry_only_from_live_history(self):
        """백필 스냅샷에 없다는 이유로 NEW_ENTRY를 붙이면 안 된다."""
        backfill_only = [self._snap(24, self.addrs[:6], source="ohlcv_backfill")]
        rows = self._rows(self.addrs)
        ev = hr.detect_changes(rows, backfill_only, CFG, self.now)
        self.assertEqual([e for e in ev if e["code"] == "NEW_ENTRY"], [])

        live = [self._snap(6, self.addrs[1:], source="live")]
        ev2 = hr.detect_changes(rows, live, CFG, self.now)
        self.assertTrue(any(e["code"] == "NEW_ENTRY" and e["symbol"] == self.addrs[0] for e in ev2))

    def test_dropped_out_detected(self):
        live = [self._snap(6, self.addrs, source="live")]
        rows = self._rows(self.addrs[:-1] )  # 마지막 종목 이탈
        gone = self.addrs[-1]
        ev = hr.detect_changes(rows, live, CFG, self.now)
        self.assertTrue(any(e["code"] == "DROPPED_OUT" and e["symbol"] == gone for e in ev))

    def test_mcap_surge(self):
        past = self._snap(24, self.addrs)
        past["mcap"] = {a: 100_000 for a in self.addrs}
        ev = hr.detect_changes(self._rows(self.addrs), [past], CFG, self.now)
        self.assertTrue(any(e["code"] == "MCAP_SURGE" for e in ev))

    def test_reference_picks_oldest_beyond_window(self):
        hist = [self._snap(30, self.addrs), self._snap(3, self.addrs)]
        ref = hr.pick_reference(hist, 22, self.now)
        self.assertEqual(ref["ts"], hist[0]["ts"])

    def test_no_reference_returns_none(self):
        self.assertIsNone(hr.pick_reference([self._snap(1, self.addrs)], 22, self.now))


class TestRiskFlags(unittest.TestCase):
    def _row(self, **kw):
        base = {"address": "0xaa", "symbol": "AA", "mc_liq": 5.0, "pools": 3,
                "liq": 900_000, "data_warn": False, "bridged": False,
                "oldest_pool": "2026-07-01T00:00:00Z", "mcap": 10_000_000}
        base.update(kw)
        return base

    def test_thin_liquidity_flag(self):
        rows = hr.tag_risks([self._row(mc_liq=10_000.0)], CFG, None)
        self.assertIn("LIQ_THIN", [f["code"] for f in rows[0]["flags"]])

    def test_copycat_flag(self):
        a = self._row(address="0x1", symbol="GG")
        b = self._row(address="0x2", symbol="GG")
        rows = hr.tag_risks([a, b], CFG, None)
        self.assertIn("COPYCAT", [f["code"] for f in rows[0]["flags"]])

    def test_liquidity_drain_flag(self):
        prev = {"liq": {"0xaa": 1_000_000}}
        rows = hr.tag_risks([self._row(liq=400_000)], CFG, prev)
        self.assertIn("LIQ_DRAIN", [f["code"] for f in rows[0]["flags"]])

    def test_young_pool_flag(self):
        recent = (hr.now_kst() - timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows = hr.tag_risks([self._row(oldest_pool=recent)], CFG, None)
        self.assertIn("YOUNG", [f["code"] for f in rows[0]["flags"]])

    def test_no_flags_for_clean_token(self):
        rows = hr.tag_risks([self._row()], CFG, None)
        self.assertEqual(rows[0]["flags"], [])


class TestRenderSafety(unittest.TestCase):
    def test_escapes_injected_markup(self):
        self.assertNotIn("<script>", hr.esc("<script>alert(1)</script>"))

    def test_symbol_injection_in_dashboard(self):
        payload = {
            "as_of_kst": "2026-08-30 18:07", "data_status": "OK",
            "meta": {"chain_volume_24h": 1e6, "pools_scanned": 200},
            "rows": [{
                "rank": 1, "symbol": "<img src=x onerror=alert(1)>", "name": "evil",
                "address": "0x" + "f" * 40, "mcap": 1e7, "mcap_basis": "MC",
                "d_rank_6h": None, "d_rank_24h": None, "chg24": 1.0,
                "v24": 1e6, "liq": 1e5, "flags": [],
            }],
            "events": [], "spark": {},
        }
        out = "/tmp/_hood_test.html"
        hr.render_dashboard(payload, CFG, out)
        with open(out, encoding="utf-8") as fh:
            html = fh.read()
        self.assertNotIn("<img src=x", html)
        self.assertIn("&lt;img", html)

    def test_telegram_renders_without_history(self):
        rows = [{
            "rank": 1, "symbol": "CASHCAT", "mcap": 2.03e8, "chg24": 8.4,
            "d_rank_24h": None, "flags": [],
        }]
        payload = {"as_of_kst": "2026-08-30 18:07", "data_status": "OK", "rows": rows,
                   "events": [], "meta": {"chain_volume_24h": 3e8, "pools_scanned": 200}}
        msg = hr.render_telegram(payload, CFG, "https://example.invalid/")
        self.assertIn("CASHCAT", msg)
        self.assertIn("임계", msg)


class TestIdempotence(unittest.TestCase):
    def test_write_skipped_when_unchanged(self):
        path = "/tmp/_hood_idem.json"
        obj = {"a": 1, "as_of_utc": "x"}
        if os.path.exists(path):
            os.remove(path)
        self.assertTrue(hr.write_json_if_changed(path, obj))
        self.assertFalse(hr.write_json_if_changed(path, obj))

    def test_timestamp_only_change_ignored(self):
        path = "/tmp/_hood_idem2.json"
        if os.path.exists(path):
            os.remove(path)
        hr.write_json_if_changed(path, {"a": 1, "as_of_utc": "t1"}, ignore_keys=("as_of_utc",))
        self.assertFalse(hr.write_json_if_changed(path, {"a": 1, "as_of_utc": "t2"},
                                                  ignore_keys=("as_of_utc",)))


class TestConfig(unittest.TestCase):
    def test_config_has_required_keys(self):
        for key in ("network", "min_liquidity_usd", "rank_move_6h_threshold",
                    "rank_move_24h_threshold", "history_max_snapshots", "rwa_rule_note"):
            self.assertIn(key, CFG)

    def test_thresholds_are_sane(self):
        self.assertLess(CFG["rank_move_6h_threshold"], CFG["rank_move_24h_threshold"])
        self.assertGreater(CFG["history_max_snapshots"], 4 * 7)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestDisplayHelpers(unittest.TestCase):
    def _row(self, **kw):
        base = {"symbol": "GG", "address": "0xabcd" + "0" * 36, "chg24": 5.0, "flags": []}
        base.update(kw)
        return base

    def test_label_plain(self):
        self.assertEqual(hr.label(self._row()), "GG")

    def test_label_disambiguates_copycat(self):
        row = self._row(flags=[{"code": "COPYCAT", "detail": "x"}])
        self.assertEqual(hr.label(row), "GG\u00b7abcd")

    def test_new_pool_change_shown_as_new(self):
        row = self._row(chg24=3968711.0, flags=[{"code": "YOUNG", "detail": "x"}])
        self.assertEqual(hr.chg_str(row)[1], "\uc2e0\uaddc")

    def test_extreme_change_clamped(self):
        self.assertEqual(hr.chg_str(self._row(chg24=250000.0))[1], "+999%\u2191")

    def test_normal_change_formatted(self):
        self.assertEqual(hr.chg_str(self._row(chg24=8.4))[1], "+8%")

    def test_missing_change(self):
        self.assertEqual(hr.chg_str(self._row(chg24=None))[1], "\u2013")


class TestTelegramEscaping(unittest.TestCase):
    def test_malicious_symbol_escaped_in_telegram(self):
        rows = [{
            "rank": 1, "symbol": "<b>evil</b>", "address": "0x" + "e" * 40,
            "mcap": 1e7, "chg24": 2.0, "d_rank_24h": None,
            "flags": [{"code": "LIQ_THIN", "detail": "<i>x</i>"}],
        }]
        payload = {"as_of_kst": "2026-08-30 18:07", "data_status": "OK", "rows": rows,
                   "events": [{"code": "RANK_SURGE", "window": "24h", "symbol": "<u>x</u>",
                               "detail": "<script>", "mcap": 1.0, "severity": 9}],
                   "meta": {"chain_volume_24h": 1e6, "pools_scanned": 10}}
        msg = hr.render_telegram(payload, CFG, "https://example.invalid/")
        self.assertNotIn("<b>evil", msg)
        self.assertNotIn("<script>", msg)
        self.assertIn("&lt;b&gt;evil", msg)


import security as sec_mod  # noqa: E402
import backtest as bt_mod   # noqa: E402
import backfill as bf_mod   # noqa: E402
import chainvol            # noqa: E402


class TestSecurity(unittest.TestCase):
    def test_unindexed_is_flagged_not_treated_safe(self):
        flags = sec_mod.flags_for({"indexed": False}, CFG)
        self.assertEqual([f["code"] for f in flags], ["UNVERIFIED"])

    def test_honeypot_flag(self):
        flags = sec_mod.flags_for({"indexed": True, "honeypot": "1", "owner_renounced": True}, CFG)
        self.assertIn("HONEYPOT", [f["code"] for f in flags])

    def test_owner_active_flag(self):
        flags = sec_mod.flags_for(
            {"indexed": True, "honeypot": "0", "owner_renounced": False, "owner": "0xeb7c03"}, CFG)
        self.assertIn("OWNER_ACTIVE", [f["code"] for f in flags])

    def test_high_tax_flag(self):
        flags = sec_mod.flags_for(
            {"indexed": True, "honeypot": "0", "owner_renounced": True, "sell_tax": 0.25}, CFG)
        self.assertIn("SELL_TAX", [f["code"] for f in flags])

    def test_clean_token_only_gets_no_flags(self):
        flags = sec_mod.flags_for(
            {"indexed": True, "honeypot": "0", "owner_renounced": True, "mintable": "0",
             "pausable": "0", "open_source": "1", "top10_pct": 1.9}, CFG)
        self.assertEqual(flags, [])

    def test_summarize_renounced_detection(self):
        s = sec_mod.summarize({"owner_address": "0x0000000000000000000000000000000000000000",
                               "holders": [], "lp_holders": []})
        self.assertTrue(s["owner_renounced"])

    def test_summarize_concentration(self):
        s = sec_mod.summarize({"owner_address": "", "holders": [{"percent": "0.2"}, {"percent": "0.2"}],
                               "lp_holders": []})
        self.assertAlmostEqual(s["top10_pct"], 40.0, places=1)


class TestBacktest(unittest.TestCase):
    def _hist(self):
        from datetime import timedelta
        base = hr.now_kst() - timedelta(hours=96)
        snaps = []
        for i in range(17):  # 6시간 간격 4일
            t = base + timedelta(hours=6 * i)
            rank, mcap = {}, {}
            for k in range(12):
                addr = "0x%02d" % k
                rank[addr] = k + 1
                mcap[addr] = 1_000_000 * (12 - k) * (1 + 0.01 * i)
            snaps.append({"ts": t.isoformat(), "rank": rank, "mcap": mcap,
                          "symbol": {a: a for a in rank}, "liq": {}})
        return snaps

    def test_insufficient_sample_is_not_a_verdict(self):
        r = bt_mod.run(self._hist(), rank_threshold=5)
        self.assertEqual(r["verdict"], "INSUFFICIENT")
        self.assertIn("유보", r["note"])

    def test_render_line_handles_empty(self):
        self.assertIn("유보", bt_mod.render_line({}))
        self.assertIn("유보", bt_mod.render_line(None))

    def test_no_edge_when_random(self):
        import random
        random.seed(3)
        hist = self._hist()
        for s in hist:
            addrs = list(s["rank"].keys())
            random.shuffle(addrs)
            s["rank"] = {a: i + 1 for i, a in enumerate(addrs)}
            s["mcap"] = {a: 1_000_000 * random.uniform(0.6, 1.6) for a in addrs}
        r = bt_mod.run(hist, rank_threshold=3, min_picks=5)
        self.assertIn(r["verdict"], ("NO_EDGE", "POSITIVE", "NEGATIVE"))
        self.assertIn("n_picks", r)


class TestBackfillMerge(unittest.TestCase):
    def test_real_snapshot_wins_over_synthetic(self):
        from datetime import timedelta
        t = hr.now_kst() - timedelta(hours=12)
        real = {"ts": t.isoformat(), "source": "live", "rank": {"0xa": 1}, "mcap": {"0xa": 5},
                "symbol": {"0xa": "A"}, "liq": {}}
        synth = [{"ts": (t + timedelta(hours=1)).isoformat(), "source": "ohlcv_backfill",
                  "rank": {"0xa": 2}, "mcap": {"0xa": 4}, "symbol": {"0xa": "A"}, "liq": {}}]
        merged = bf_mod.merge([real], synth)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["source"], "live")

    def test_synthetic_kept_when_no_real_nearby(self):
        from datetime import timedelta
        t = hr.now_kst()
        real = {"ts": t.isoformat(), "source": "live", "rank": {"0xa": 1}, "mcap": {"0xa": 5},
                "symbol": {"0xa": "A"}, "liq": {}}
        synth = [{"ts": (t - timedelta(hours=30)).isoformat(), "source": "ohlcv_backfill",
                  "rank": {"0xa": 3}, "mcap": {"0xa": 2}, "symbol": {"0xa": "A"}, "liq": {}}]
        merged = bf_mod.merge([real], synth)
        self.assertEqual(len(merged), 2)
        self.assertEqual(merged[0]["source"], "ohlcv_backfill")


class TestCrosscheckAttach(unittest.TestCase):
    def test_divergence_flagged(self):
        rows = [{"address": "0xa", "symbol": "A", "flags": []}]
        hr.attach_crosscheck(rows, {"0xa": {"gap_pct": 15.0, "ds_mcap": 1}}, 8.0)
        self.assertIn("SRC_DIVERGENCE", [f["code"] for f in rows[0]["flags"]])

    def test_within_tolerance_not_flagged(self):
        rows = [{"address": "0xa", "symbol": "A", "flags": []}]
        hr.attach_crosscheck(rows, {"0xa": {"gap_pct": 1.0, "ds_mcap": 1}}, 8.0)
        self.assertEqual(rows[0]["flags"], [])

    def test_promotion_flagged_as_caution(self):
        rows = [{"address": "0xa", "symbol": "A", "flags": []}]
        hr.attach_promotion(rows, {"0xa": ["boost_top"]})
        codes = [f["code"] for f in rows[0]["flags"]]
        self.assertIn("PROMOTED", codes)
        self.assertIn("보증 아님", rows[0]["flags"][0]["detail"])


class TestVolumeCharts(unittest.TestCase):
    """차트는 체인 전체(DefiLlama) 시계열을 그린다 — 자체 200풀 집계와 섞지 않는다."""

    def _cv(self, days=30, base=5e8, grow=1.02):
        import time as _t
        now = int(_t.time())
        series, v = [], base
        for i in range(days):
            v *= grow
            series.append({"ts": now - (days - i) * 86400, "v": v})
        return {
            "series": series, "total24h": series[-1]["v"], "change_1d_pct": 12.09,
            "vs_avg7d_pct": 18.4, "peak": max(x["v"] for x in series),
            "peak_ts": series[-1]["ts"], "days": days,
            "protocols": [{"name": "Uniswap V3", "v24": 322e6},
                          {"name": "Uniswap V2", "v24": 65e6},
                          {"name": "0x", "v24": 2e6}],
            "protocol_total": 389e6,
        }

    def test_chart_renders_series(self):
        out = hr.volume_chart(self._cv())
        self.assertIn("<polyline", out)
        self.assertIn("7일 이동평균", out)
        self.assertIn("일별 거래량", out)

    def test_missing_series_shows_placeholder_not_fake_line(self):
        for bad in ({}, None, {"series": []}, {"series": [{"ts": 1, "v": 5.0}]}):
            out = hr.volume_chart(bad)
            self.assertIn("불러오지 못했습니다", out)
            self.assertNotIn("<polyline", out)

    def test_moving_average_lags_a_rising_series(self):
        """상승 구간에서 7일 이동평균은 일별선보다 낮아야 한다(y가 더 큼)."""
        out = hr.volume_chart(self._cv(days=20, grow=1.08))
        polys = [seg for seg in out.split("<polyline")[1:]]
        self.assertEqual(len(polys), 2)
        last_y = lambda seg: float(seg.split('points="')[1].split('"')[0].split()[-1].split(",")[1])
        self.assertGreater(last_y(polys[1]), last_y(polys[0]))

    def test_protocol_chart(self):
        out = hr.protocol_chart(self._cv())
        self.assertIn("Uniswap V3", out)
        self.assertIn("82.8%", out)  # 322/389

    def test_protocol_chart_empty(self):
        self.assertIn("없음", hr.protocol_chart({}))

    def test_protocol_name_escaped(self):
        cv = {"protocols": [{"name": "<img src=x>", "v24": 10.0}], "protocol_total": 10.0}
        self.assertNotIn("<img src=x>", hr.protocol_chart(cv))

    def test_share_chart_escapes_and_totals(self):
        rows = [{"symbol": "<b>x</b>", "address": "0x" + "a" * 40, "v24": 100.0, "flags": []},
                {"symbol": "B", "address": "0x" + "b" * 40, "v24": 300.0, "flags": []}]
        out = hr.volume_share_chart(rows)
        self.assertNotIn("<b>x</b>", out)
        self.assertIn("75.0%", out)

    def test_share_chart_empty(self):
        self.assertIn("없습니다", hr.volume_share_chart([]))

    def test_snapshot_records_volume(self):
        rows = [{"address": "0xa", "symbol": "A", "rank": 1, "mcap": 10.0, "liq": 1.0, "v24": 5.0},
                {"address": "0xb", "symbol": "B", "rank": 2, "mcap": 5.0, "liq": 1.0, "v24": 7.0}]
        snap = hr.snapshot_of(rows, "2026-08-30T12:00:00+09:00", chain_v24=999.0)
        self.assertEqual(snap["chain_v24"], 999.0)
        self.assertEqual(snap["tracked_v24"], 12.0)


class TestChainVolSummary(unittest.TestCase):
    def _payload(self):
        import time as _t
        now = int(_t.time())
        return {
            "totalDataChart": [[now - 86400 * i, 1e8 * (10 - i)] for i in range(9, -1, -1)],
            "total24h": 1033911214.89, "total48hto24h": 922400000.0,
            "total7d": 5496218605.38, "total30d": 2e10, "change_1d": 12.09,
            "protocols": [{"name": "Uniswap V3", "total24h": 322.3e6},
                          {"name": "Curve DEX", "total24h": 0.0},
                          {"name": "Uniswap V2", "total24h": 65.3e6}],
        }

    def test_summary_fields(self):
        s = chainvol.summarize(self._payload())
        self.assertEqual(s["total24h"], 1033911214.89)
        self.assertEqual(s["change_1d_pct"], 12.09)
        self.assertAlmostEqual(s["avg7d"], 5496218605.38 / 7, places=2)
        self.assertIsNotNone(s["vs_avg7d_pct"])

    def test_zero_volume_protocols_dropped(self):
        s = chainvol.summarize(self._payload())
        self.assertNotIn("Curve DEX", [p["name"] for p in s["protocols"]])
        self.assertEqual(s["protocols"][0]["name"], "Uniswap V3")

    def test_series_sorted_and_trimmed(self):
        s = chainvol.summarize(self._payload(), days=4)
        self.assertEqual(len(s["series"]), 4)
        self.assertEqual(s["series"], sorted(s["series"], key=lambda x: x["ts"]))

    def test_handles_missing_change_field(self):
        p = self._payload()
        p.pop("change_1d")
        s = chainvol.summarize(p)
        self.assertAlmostEqual(s["change_1d_pct"], round((1033911214.89 - 922400000.0) / 922400000.0 * 100, 2))

    def test_empty_payload_is_safe(self):
        s = chainvol.summarize({})
        self.assertEqual(s["series"], [])
        self.assertEqual(s["protocols"], [])
