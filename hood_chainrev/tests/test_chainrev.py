# -*- coding: utf-8 -*-
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import chainrev as cr  # noqa: E402


def proto(name, cat, bd30, bd24=None, chains=None):
    return {
        "displayName": name, "name": name, "category": cat,
        "chains": chains or [], "breakdown30d": bd30,
        "breakdown24h": bd24 if bd24 is not None else bd30,
    }


class TestAggregate(unittest.TestCase):
    def test_sums_nested_breakdown(self):
        ps = [
            proto("A", "Dexs", {"robinhood": {"a": 10, "b": 5}}),
            proto("B", "Chain", {"robinhood": {"gas": 85}}),
        ]
        agg = cr.aggregate_by_chain(ps)
        self.assertAlmostEqual(agg["robinhood"]["v30"], 100.0)
        # 'Chain' 카테고리는 앱레이어에서 빠진다
        self.assertAlmostEqual(agg["robinhood"]["app30"], 15.0)

    def test_ignores_non_numeric_and_none(self):
        ps = [proto("A", "Dexs", {"solana": {"x": None, "y": "bad", "z": 7}})]
        agg = cr.aggregate_by_chain(ps)
        self.assertAlmostEqual(agg["solana"]["v30"], 7.0)

    def test_multichain_protocol_splits(self):
        ps = [proto("Uni", "Dexs", {"ethereum": {"v3": 100}, "base": {"v3": 40}})]
        agg = cr.aggregate_by_chain(ps)
        self.assertAlmostEqual(agg["ethereum"]["v30"], 100.0)
        self.assertAlmostEqual(agg["base"]["v30"], 40.0)


class TestSlugMap(unittest.TestCase):
    def test_infers_from_single_chain_protocols(self):
        ps = [
            proto("P1", "Dexs", {"era": {"x": 1}}, chains=["ZKsync Era"]),
            proto("P2", "Dexs", {"era": {"x": 1}}, chains=["ZKsync Era"]),
            proto("P3", "Dexs", {"avax": {"x": 1}}, chains=["Avalanche"]),
        ]
        m = cr.build_slug_map(ps)
        self.assertEqual(m["era"], "ZKsync Era")
        self.assertEqual(m["avax"], "Avalanche")

    def test_multichain_protocol_does_not_vote(self):
        ps = [proto("P", "Dexs", {"aaa": {"x": 1}, "bbb": {"x": 1}}, chains=["A", "B"])]
        m = cr.build_slug_map(ps)
        self.assertNotIn("aaa", m)

    def test_fallback_applies(self):
        m = cr.build_slug_map([])
        self.assertEqual(m["robinhood"], "Robinhood Chain")

    def test_display_name_titlecases_unknown(self):
        self.assertEqual(cr.display_name("some_chain", {}), "Some Chain")


class TestRows(unittest.TestCase):
    def setUp(self):
        self.rev = {
            "robinhood": {"v30": 35e6, "v24": 5e6, "app30": 33e6, "app24": 4e6,
                          "items": [("GMGN", "Telegram Bot", 20e6, 3e6),
                                    ("Pons", "Launchpad", 15e6, 2e6)]},
            "solana": {"v30": 150e6, "v24": 5.7e6, "app30": 150e6, "app24": 5.7e6, "items": []},
            "off_chain": {"v30": 700e6, "v24": 24e6, "app30": 0, "app24": 0, "items": []},
            "dust": {"v30": 100.0, "v24": 0.0, "app30": 0, "app24": 0, "items": []},
        }
        self.fee = {"robinhood": {"v30": 140e6, "v24": 20e6},
                    "solana": {"v30": 300e6, "v24": 11e6}}
        self.tvl = {"Robinhood Chain": 733e6, "Solana": 5.8e9}
        self.smap = {"robinhood": "Robinhood Chain", "solana": "Solana"}

    def test_excludes_off_chain_bucket(self):
        rows = cr.build_rows(self.rev, self.fee, self.tvl, self.smap)
        self.assertNotIn("off_chain", [r["slug"] for r in rows])

    def test_excludes_dust_below_threshold(self):
        rows = cr.build_rows(self.rev, self.fee, self.tvl, self.smap)
        self.assertNotIn("dust", [r["slug"] for r in rows])

    def test_rank_and_metrics(self):
        rows = cr.build_rows(self.rev, self.fee, self.tvl, self.smap)
        self.assertEqual(rows[0]["slug"], "solana")
        hood = next(r for r in rows if r["slug"] == "robinhood")
        self.assertEqual(hood["rank30"], 2)
        self.assertAlmostEqual(hood["take"], 35e6 / 140e6)
        self.assertAlmostEqual(hood["momentum"], 5e6 / (35e6 / 30))
        self.assertIsNotNone(hood["rpt"])

    def test_rpt_skipped_for_tiny_tvl(self):
        rows = cr.build_rows(self.rev, self.fee, {"Solana": 1e6}, self.smap)
        sol = next(r for r in rows if r["slug"] == "solana")
        self.assertIsNone(sol["rpt"])


class TestRankDelta(unittest.TestCase):
    def test_intersection_recomputes_ranks(self):
        # 오늘 유니버스에 'new'가 2위로 끼어들었다. 그래도 a·b의 상대순위는 그대로여야 한다.
        # (원시 순위를 그대로 빼면 b가 2위→3위로 '하락'한 것처럼 보이는 허위 급변)
        rows = [{"slug": "a", "rev30": 300}, {"slug": "new", "rev30": 250},
                {"slug": "b", "rev30": 200}, {"slug": "c", "rev30": 150},
                {"slug": "d", "rev30": 120}, {"slug": "e", "rev30": 110}]
        prev = {"chains": {"a": {"rev30": 300}, "b": {"rev30": 200}, "c": {"rev30": 150},
                           "d": {"rev30": 120}, "e": {"rev30": 110}}}
        deltas, newcomers = cr.intersect_rank_delta(rows, prev)
        self.assertEqual(deltas["a"], 0)
        self.assertEqual(deltas["b"], 0)
        self.assertEqual(deltas["c"], 0)
        self.assertIn("new", newcomers)
        self.assertNotIn("new", deltas)

    def test_real_move_detected(self):
        rows = [{"slug": "b", "rev30": 300}, {"slug": "a", "rev30": 200},
                {"slug": "c", "rev30": 150}, {"slug": "d", "rev30": 120},
                {"slug": "e", "rev30": 110}]
        prev = {"chains": {"a": {"rev30": 300}, "b": {"rev30": 200}, "c": {"rev30": 150},
                           "d": {"rev30": 120}, "e": {"rev30": 110}}}
        deltas, _ = cr.intersect_rank_delta(rows, prev)
        self.assertEqual(deltas["b"], 1)
        self.assertEqual(deltas["a"], -1)
        self.assertEqual(deltas["c"], 0)

    def test_too_little_overlap_returns_empty(self):
        rows = [{"slug": "a", "rev30": 1}, {"slug": "b", "rev30": 2}]
        prev = {"chains": {"a": {"rev30": 1}}}
        deltas, _ = cr.intersect_rank_delta(rows, prev)
        self.assertEqual(deltas, {})

    def test_no_prev_returns_empty(self):
        deltas, newcomers = cr.intersect_rank_delta([{"slug": "a", "rev30": 1}], None)
        self.assertEqual(deltas, {})
        self.assertEqual(newcomers, set())


class TestEvents(unittest.TestCase):
    def _row(self, slug, name, rev30, rev24, rank=1):
        return {"slug": slug, "name": name, "rev30": rev30, "rev24": rev24,
                "rank30": rank, "rank24": rank,
                "momentum": (rev24 / (rev30 / 30)) if rev30 else None,
                "top_items": [{"name": "GMGN", "cat": "Bot", "rev30": rev30, "rev24": rev24}]}

    def test_surge_detected(self):
        hood = self._row("robinhood", "Robinhood Chain", 30e6, 5e6)
        ev = cr.detect_events([hood], {}, set(), None, hood, 30e6)
        self.assertTrue(any(e["code"] == "REV_SURGE" for e in ev))

    def test_small_chain_excluded_from_surge(self):
        tiny = self._row("tiny", "Tiny", 100_000, 90_000)
        ev = cr.detect_events([tiny], {}, set(), None, None, 100_000)
        self.assertFalse(any(e["code"] == "REV_SURGE" for e in ev))

    def test_collapse_requires_two_consecutive_days(self):
        r = self._row("x", "X", 30e6, 100_000)   # momentum 0.1
        # 어제는 정상이었다면 하루 반짝이므로 승격 금지
        prev_ok = {"chains": {"x": {"mom": 1.0}}}
        ev = cr.detect_events([r], {}, set(), prev_ok, None, 30e6)
        self.assertFalse(any(e["code"] == "REV_COLLAPSE" for e in ev))
        # 어제도 위축이었다면 승격
        prev_bad = {"chains": {"x": {"mom": 0.12}}}
        ev = cr.detect_events([r], {}, set(), prev_bad, None, 30e6)
        self.assertTrue(any(e["code"] == "REV_COLLAPSE" for e in ev))

    def test_surge_needs_absolute_floor(self):
        # 비율은 크지만 24h 절대금액이 미미하면 승격 금지
        r = self._row("x", "X", 1.2e6, 50_000)
        ev = cr.detect_events([r], {}, set(), None, None, 1.2e6)
        self.assertFalse(any(e["code"] == "REV_SURGE" for e in ev))

    def test_share_shift(self):
        hood = self._row("robinhood", "Robinhood Chain", 30e6, 1e6)
        prev = {"hood_share_30d": 0.05, "chains": {}}
        ev = cr.detect_events([hood], {}, set(), prev, hood, 100e6)
        self.assertTrue(any(e["code"] == "SHARE_SHIFT" for e in ev))

    def test_leader_shift(self):
        hood = self._row("robinhood", "Robinhood Chain", 30e6, 1e6)
        prev = {"hood_top_protocol": "Pons V2", "chains": {}}
        ev = cr.detect_events([hood], {}, set(), prev, hood, 30e6)
        self.assertTrue(any(e["code"] == "LEADER_SHIFT" for e in ev))

    def test_rank_move_threshold(self):
        r = self._row("x", "X", 10e6, 0.3e6, rank=5)
        ev = cr.detect_events([r], {"x": 1}, set(), {"chains": {}}, None, 10e6)
        self.assertFalse(any(e["code"] == "RANK_UP" for e in ev))
        ev = cr.detect_events([r], {"x": 3}, set(), {"chains": {}}, None, 10e6)
        self.assertTrue(any(e["code"] == "RANK_UP" for e in ev))


class TestHistory(unittest.TestCase):
    def test_same_date_upsert_is_idempotent(self):
        h = [{"as_of_date": "2026-09-01", "chains": {}}]
        h2 = cr.upsert_history(h, {"as_of_date": "2026-09-01", "chains": {"a": 1}})
        self.assertEqual(len(h2), 1)
        self.assertEqual(h2[0]["chains"], {"a": 1})

    def test_new_date_appends_and_caps(self):
        h = [{"as_of_date": "2026-01-%02d" % i} for i in range(1, 10)]
        h2 = cr.upsert_history(h, {"as_of_date": "2026-01-10"})
        self.assertEqual(len(h2), 10)
        self.assertEqual(h2[-1]["as_of_date"], "2026-01-10")


class TestFormat(unittest.TestCase):
    def test_usd(self):
        self.assertEqual(cr.fmt_usd(1_500_000), "$1.5M")
        self.assertEqual(cr.fmt_usd(2_300_000_000), "$2.30B")
        self.assertEqual(cr.fmt_usd(4200), "$4K")

    def test_delta(self):
        self.assertEqual(cr.fmt_delta_rank(2), "▲2")
        self.assertEqual(cr.fmt_delta_rank(-3), "▼3")
        self.assertEqual(cr.fmt_delta_rank(0), "—")
        self.assertEqual(cr.fmt_delta_rank(None), "NEW")

    def test_escape_blocks_html_injection(self):
        self.assertEqual(cr.esc("<b>x</b>&"), "&lt;b&gt;x&lt;/b&gt;&amp;")

    def test_hhi(self):
        self.assertAlmostEqual(cr.hhi([100]), 1.0)
        self.assertAlmostEqual(cr.hhi([50, 50]), 0.5)
        self.assertIsNone(cr.hhi([0, 0]))


class TestRender(unittest.TestCase):
    def _payload(self):
        row = {"slug": "robinhood", "name": "Robinhood Chain", "rev30": 35e6, "rev24": 5e6,
               "app30": 33e6, "app24": 4e6, "fee30": 140e6, "fee24": 20e6,
               "take": 0.25, "ann": 425e6, "momentum": 4.3, "tvl": 733e6, "rpt": 0.58,
               "hhi": 0.4, "rank30": 1, "rank24": 1,
               "top_items": [{"name": "GMGN", "cat": "Telegram Bot", "rev30": 20e6, "rev24": 3e6}]}
        return {"as_of_kst": "2026-09-01 08:40", "as_of_date": "2026-09-01",
                "data_status": "OK", "universe_size": 60, "total_on_chain_30d": 350e6,
                "rows": [row], "anchor": row, "deltas": {"robinhood": 2}, "events": []}

    def test_telegram_within_limit_and_has_anchor(self):
        msg = cr.render_telegram(self._payload())
        self.assertLess(len(msg), 4096)
        self.assertIn("Robinhood Chain", msg)
        self.assertIn("TOP", msg)

    def test_dashboard_is_html(self):
        html = cr.render_dashboard(self._payload(), [])
        self.assertTrue(html.startswith("<!DOCTYPE html>"))
        self.assertIn("Robinhood Chain", html)
        self.assertIn("</html>", html)

    def test_degraded_status_is_surfaced(self):
        p = self._payload()
        p["data_status"] = "DEGRADED"
        self.assertIn("DEGRADED", cr.render_telegram(p))

    def test_spark_handles_sparse_history(self):
        self.assertIn("이력 축적 중", cr._spark([None, None]))
        self.assertIn("polyline", cr._spark([1, 2, 3]))


class TestPayloadGuard(unittest.TestCase):
    def test_rejects_truncated_upstream(self):
        with self.assertRaises(RuntimeError):
            cr.build_payload({"protocols": [proto("A", "Dexs", {"x": {"y": 1}})]},
                             {"protocols": []}, [], None)


if __name__ == "__main__":
    unittest.main(verbosity=2)
