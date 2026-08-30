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
    def setUp(self):
        self.now = hr.now_kst()

    def _snap(self, hours_ago, ranks, mcaps=None, liqs=None):
        ts = (self.now - timedelta(hours=hours_ago)).isoformat()
        return {
            "ts": ts,
            "rank": ranks,
            "mcap": mcaps or {a: 1_000_000 for a in ranks},
            "liq": liqs or {a: 500_000 for a in ranks},
            "symbol": {a: a[:4].upper() for a in ranks},
        }

    def _rows(self, ranks):
        out = []
        for addr, rank in ranks.items():
            out.append({
                "address": addr, "symbol": addr[:4].upper(), "rank": rank,
                "mcap": 1_000_000, "flags": [], "chg24": 1.0,
            })
        return sorted(out, key=lambda r: r["rank"])

    def test_rank_surge_detected_24h(self):
        hist = [self._snap(24, {"0xaa": 9, "0xbb": 1})]
        rows = self._rows({"0xaa": 2, "0xbb": 1})
        ev = hr.detect_changes(rows, hist, CFG, self.now)
        codes = [e["code"] for e in ev]
        self.assertIn("RANK_SURGE", codes)

    def test_small_move_not_reported(self):
        hist = [self._snap(24, {"0xaa": 3, "0xbb": 1})]
        rows = self._rows({"0xaa": 2, "0xbb": 1})
        ev = hr.detect_changes(rows, hist, CFG, self.now)
        self.assertEqual([e for e in ev if e["code"] in ("RANK_SURGE", "RANK_DROP")], [])

    def test_six_hour_move_detected(self):
        hist = [self._snap(6, {"0xaa": 8, "0xbb": 1})]
        rows = self._rows({"0xaa": 4, "0xbb": 1})
        ev = hr.detect_changes(rows, hist, CFG, self.now)
        self.assertTrue(any(e["window"] == "6h" and e["code"] == "RANK_SURGE" for e in ev))

    def test_new_entry_only_when_history_exists(self):
        rows = self._rows({"0xnew": 3})
        self.assertEqual([e for e in hr.detect_changes(rows, [], CFG, self.now)
                          if e["code"] == "NEW_ENTRY"], [])
        hist = [self._snap(6, {"0xold": 3})]
        ev = hr.detect_changes(rows, hist, CFG, self.now)
        self.assertTrue(any(e["code"] == "NEW_ENTRY" for e in ev))

    def test_dropped_out_detected(self):
        hist = [self._snap(6, {"0xold": 4, "0xaa": 1})]
        rows = self._rows({"0xaa": 1})
        ev = hr.detect_changes(rows, hist, CFG, self.now)
        self.assertTrue(any(e["code"] == "DROPPED_OUT" for e in ev))

    def test_mcap_surge(self):
        hist = [self._snap(24, {"0xaa": 1}, mcaps={"0xaa": 500_000})]
        rows = self._rows({"0xaa": 1})
        ev = hr.detect_changes(rows, hist, CFG, self.now)
        self.assertTrue(any(e["code"] == "MCAP_SURGE" for e in ev))

    def test_reference_picks_oldest_beyond_window(self):
        hist = [self._snap(30, {"0xaa": 1}), self._snap(3, {"0xaa": 1})]
        ref = hr.pick_reference(hist, 22, self.now)
        self.assertEqual(ref["ts"], hist[0]["ts"])

    def test_no_reference_returns_none(self):
        self.assertIsNone(hr.pick_reference([self._snap(1, {"0xaa": 1})], 22, self.now))


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
