# -*- coding: utf-8 -*-
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import xrs  # noqa: E402


def mk(key, **kw):
    d = {"key": key, "label": key, "kind": "asset", "chain": None, "notes": [], "coverage": "1/1"}
    d.update(kw)
    return d


class TestNumeric(unittest.TestCase):
    def test_pct(self):
        self.assertAlmostEqual(xrs.pct(110, 100), 10.0)
        self.assertIsNone(xrs.pct(110, 0))
        self.assertIsNone(xrs.pct(None, 100))

    def test_med_ignores_none(self):
        self.assertEqual(xrs.med([1, None, 3]), 2)
        self.assertIsNone(xrs.med([None, None]))

    def test_fmt_usd(self):
        self.assertEqual(xrs.fmt_usd(1_500_000_000), "$1.50B")
        self.assertEqual(xrs.fmt_usd(None), "—")

    def test_color_thresholds(self):
        self.assertEqual(xrs.color_dot(20), "🟩")
        self.assertEqual(xrs.color_dot(3), "🟢")
        self.assertEqual(xrs.color_dot(0.2), "⚪")
        self.assertEqual(xrs.color_dot(-3), "🔴")
        self.assertEqual(xrs.color_dot(-20), "🟥")
        self.assertEqual(xrs.color_dot(None), "⬜")


class TestWidth(unittest.TestCase):
    def test_hangul_counts_two(self):
        self.assertEqual(xrs.dwidth("가격"), 4)
        self.assertEqual(xrs.dwidth("abc"), 3)

    def test_matrix_rows_align(self):
        tracks = [mk(k) for k in ("PRIV", "DEPIN", "HOOD", "HYPE", "BTC")]
        for i, t in enumerate(tracks):
            t["lens_rank"] = {L["key"]: (i + 1) for L in xrs.LENSES}
            t["lens_n"] = {L["key"]: 5 for L in xrs.LENSES}
            t["overall_rank"] = i + 1
        widths = {xrs.dwidth(ln) for ln in xrs.lens_matrix(tracks).split("\n")}
        self.assertEqual(len(widths), 1, f"행 표시폭 불일치: {widths}")


class TestScoring(unittest.TestCase):
    def _tracks(self):
        t1 = mk("A", px30=10, px7=1, mcap_chg24=1, tvl30=5, rev_chg30=50, turn=0.3)
        t2 = mk("B", px30=5, px7=2, mcap_chg24=2, tvl30=10, rev_chg30=10, turn=0.2)
        t3 = mk("C", px30=1, px7=3, mcap_chg24=3)  # TVL·매출 없음
        return [t1, t2, t3]

    def test_percentile_and_renormalization(self):
        ts = self._tracks()
        xrs.rank_and_score(ts)
        self.assertEqual(ts[0]["lens_rank"]["px30"], 1)
        self.assertEqual(ts[2]["lens_rank"]["tvl30"], None)
        # 결측 관점은 제외되고 가중치가 재정규화되므로 점수는 0~100 범위를 지킨다
        for t in ts:
            self.assertTrue(0 <= t["score"] <= 100)
        self.assertLess(ts[2]["lens_weight_covered"], ts[0]["lens_weight_covered"])

    def test_overall_rank_is_dense_and_ordered(self):
        ts = self._tracks()
        xrs.rank_and_score(ts)
        ranks = sorted(t["overall_rank"] for t in ts)
        self.assertEqual(ranks, [1, 2, 3])
        best = min(ts, key=lambda x: x["overall_rank"])
        self.assertEqual(best["score"], max(t["score"] for t in ts))

    def test_single_available_lens_does_not_crash(self):
        ts = [mk("A", px30=1), mk("B", px30=2)]
        xrs.rank_and_score(ts)
        self.assertEqual(ts[1]["score"], 100.0)


class TestDelta(unittest.TestCase):
    def test_rank_improvement_is_positive(self):
        ts = [mk("A", score=80, overall_rank=1, px30=10)]
        hist = [{"as_of_kst": "2026-09-04 07:18",
                 "tracks": {"A": {"score": 60, "overall_rank": 3, "px30": 4}}}]
        xrs.attach_delta(ts, hist)
        self.assertEqual(ts[0]["delta"]["rank"], 2)
        self.assertAlmostEqual(ts[0]["delta"]["score"], 20)
        self.assertEqual(ts[0]["delta"]["_base"], "2026-09-04 07:18")

    def test_first_run_has_no_delta(self):
        ts = [mk("A", score=80, overall_rank=1)]
        xrs.attach_delta(ts, [])
        self.assertEqual(ts[0]["delta"], {})


class TestBuildTrack(unittest.TestCase):
    def _mkt(self, **over):
        base = {"symbol": "X", "price": 1, "mcap": 100.0, "fdv": 100.0, "vol": 10.0,
                "p24": 1.0, "p7": 2.0, "p30": 3.0}
        base.update(over)
        return base

    def test_basket_uses_median_and_sum(self):
        t = {"key": "PRIV", "code": "PRIVACY_PQ", "label": "P", "kind": "basket", "chain": None}
        mkt = {"a": self._mkt(p30=10), "b": self._mkt(p30=20), "c": self._mkt(p30=90)}
        r = xrs.build_track(t, {"PRIVACY_PQ": ["a", "b", "c"]}, mkt, None, {}, {}, None)
        self.assertEqual(r["px30"], 20)          # 평균(40)이 아니라 중앙값
        self.assertEqual(r["mcap"], 300.0)
        self.assertEqual(r["coverage"], "3/3")

    def test_ecosystem_age_filter_blocks_new_listings(self):
        t = {"key": "HOOD", "code": None, "label": "H", "kind": "ecosystem", "chain": None}
        rows = [{"cg_id": "old1", "age_hours": 900}, {"cg_id": "new1", "age_hours": 20},
                {"cg_id": "new2", "age_hours": 30}]
        mkt = {"old1": self._mkt(p30=50), "new1": self._mkt(p30=5000), "new2": self._mkt(p30=8000)}
        r = xrs.build_track(t, {}, mkt, rows, {}, {}, None)
        self.assertIsNone(r["px30"], "표본 3종 미만이면 30일 수익률을 내면 안 된다")
        self.assertEqual(r["px_sample"]["30d"], 1)

    def test_missing_market_data_flags_not_silently_zero(self):
        t = {"key": "HYPE", "code": None, "label": "H", "kind": "asset", "chain": None}
        r = xrs.build_track(t, {}, {}, None, {}, {}, None)
        self.assertIn("시세 수집 실패", r["notes"])
        self.assertIsNone(r.get("px30"))

    def test_btc_revenue_uses_miner_fees(self):
        t = {"key": "BTC", "code": None, "label": "B", "kind": "asset", "chain": "Bitcoin"}
        mkt = {"bitcoin": self._mkt(mcap=1e12)}
        fee = {"Bitcoin": {"rev": {"d30": 1000.0, "chg_1m": 1.0}}}
        btc = {"d30": 7_000_000.0, "chg_1m": 12.0}
        r = xrs.build_track(t, {}, mkt, None, {}, fee, btc)
        self.assertEqual(r["rev30"], 7_000_000.0)
        self.assertEqual(r["rev_defi30"], 1000.0)
        self.assertIn("채굴 수수료", r["rev_basis"])


class TestRender(unittest.TestCase):
    def _payload(self):
        ts = [mk("A", label="가나<script>", px30=10, px7=1, mcap=1e9, mcap_chg24=1,
                 turn=0.1, tvl=1e8, tvl30=5, rev30=1e6, rev_chg30=50),
              mk("B", label="B", px30=1, px7=2, mcap=2e9, mcap_chg24=-2, turn=0.2)]
        xrs.rank_and_score(ts)
        xrs.attach_delta(ts, [])
        p = {"as_of_kst": "2026-09-05 07:18", "status": "OK", "tracks": ts, "version": "t"}
        p["highlights"] = xrs.build_highlights(ts)
        return p

    def test_telegram_escapes_html(self):
        msg = xrs.render_telegram(self._payload())
        self.assertNotIn("<script>", msg)
        self.assertIn("&lt;script&gt;", msg)

    def test_telegram_has_limits_disclaimer(self):
        msg = xrs.render_telegram(self._payload())
        self.assertIn("예측이 아닙니다", msg)
        self.assertIn("첫 관측", msg)

    def test_dashboard_renders(self):
        html = xrs.render_dashboard(self._payload())
        self.assertIn("<table>", html)
        self.assertNotIn("<script>가", html)
        self.assertGreater(len(html), 1500)

    def test_highlights_capped_at_three(self):
        self.assertLessEqual(len(xrs.build_highlights(self._payload()["tracks"])), 3)


class TestIO(unittest.TestCase):
    def test_save_json_atomic(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "sub", "x.json")
            xrs.save_json(p, {"a": 1})
            self.assertEqual(json.load(open(p, encoding="utf-8")), {"a": 1})
            self.assertFalse(os.path.exists(p + ".tmp"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
