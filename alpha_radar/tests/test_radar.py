# -*- coding: utf-8 -*-
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import alpha_radar as ar  # noqa: E402


def mk_bars(closes, start=1_700_000_000_000, step=86_400_000, vol=100000.0):
    bars = []
    for i, c in enumerate(closes):
        prev = closes[i - 1] if i else c
        bars.append({"t": start + i * step, "o": prev, "h": max(c, prev) * 1.02,
                     "l": min(c, prev) * 0.98, "c": c, "v": vol,
                     "ct": start + (i + 1) * step - 1, "qv": vol, "n": 500})
    return bars


class TestStats(unittest.TestCase):
    def test_median(self):
        self.assertEqual(ar.median([3, 1, 2]), 2)
        self.assertEqual(ar.median([4, 1, 2, 3]), 2.5)
        self.assertEqual(ar.median([]), 0.0)

    def test_robust_z_center(self):
        z = ar.robust_z([1, 2, 3, 4, 5])
        self.assertAlmostEqual(z[2], 0.0, places=6)
        self.assertGreater(z[4], 0)
        self.assertLess(z[0], 0)

    def test_robust_z_outlier_clipped(self):
        z = ar.robust_z([1, 1, 1, 1, 1000])
        self.assertLessEqual(max(z), 3.0)

    def test_robust_z_all_equal(self):
        self.assertEqual(ar.robust_z([5, 5, 5]), [0.0, 0.0, 0.0])

    def test_fnum_guards(self):
        self.assertEqual(ar.fnum("abc"), 0.0)
        self.assertEqual(ar.fnum(None, 7), 7)
        self.assertEqual(ar.fnum("nan"), 0.0)

    def test_linreg_r2_uptrend(self):
        r2, slope = ar.linreg_r2([1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
        self.assertGreater(r2, 0.99)
        self.assertGreater(slope, 0)


class TestChart(unittest.TestCase):
    def test_closed_bars_drops_inprogress(self):
        bars = mk_bars([1, 2, 3])
        cut = bars[1]["ct"]
        self.assertEqual(len(ar.closed_bars(bars, cut)), 2)

    def test_uptrend_features(self):
        f = ar.chart_features(mk_bars([1.0 * (1.02 ** i) for i in range(60)]))
        self.assertTrue(f["above_ema20"])
        self.assertTrue(f["ema_stack"])
        self.assertTrue(f["new_high20"])
        self.assertGreater(f["r2"], 0.9)
        self.assertGreater(f["near_high"], 0.95)

    def test_downtrend_features(self):
        f = ar.chart_features(mk_bars([1.0 * (0.98 ** i) for i in range(60)]))
        self.assertFalse(f["above_ema20"])
        self.assertFalse(f["new_high20"])
        self.assertEqual(f["r2"], 0.0)          # 하락 기울기는 R² 를 0 처리
        self.assertLess(f["dd30"], 0)

    def test_volume_expansion(self):
        bars = mk_bars([1.0] * 25)
        for b in bars[-5:]:
            b["qv"] = 300000.0
        f = ar.chart_features(bars)
        self.assertGreater(f["volx"], 1.5)

    def test_squeeze_ratio_present(self):
        f = ar.chart_features(mk_bars([1.0 + 0.001 * i for i in range(60)]))
        self.assertIsNotNone(f["squeeze"])


class TestThemes(unittest.TestCase):
    def setUp(self):
        self.cfg = json.load(open(os.path.join(ar.BASE_DIR, "themes.json"), encoding="utf-8"))
        self.themes = self.cfg["themes"]

    def test_config_integrity(self):
        keys = [t["key"] for t in self.themes]
        self.assertEqual(len(keys), len(set(keys)))
        for need in ("min_liquidity_usd", "min_volume24h_usd", "min_marketcap_usd", "min_daily_bars"):
            self.assertIn(need, self.cfg["screen_rule"])
        self.assertIn("frozen_at", self.cfg)

    def test_tagging(self):
        self.assertEqual(ar.tag_theme("Super AI Agent", "SAI", self.themes), "AI_AGENT")
        self.assertEqual(ar.tag_theme("Doge Killer", "DOGE2", self.themes), "MEME_CULT")
        self.assertEqual(ar.tag_theme("qqzzxx", "QQZ", self.themes), "UNTAGGED")


class TestScoring(unittest.TestCase):
    def _row(self, sym, ret30, turnover, liq=1e6, mc=1e7, churn=1.0, float_ratio=1.0, theme="DEFI"):
        bars = mk_bars([1.0 * (1 + ret30 / 60.0) ** i for i in range(60)])
        return {"alpha_id": "A_" + sym, "symbol": sym, "name": sym, "chain": "BSC",
                "theme": theme, "age_days": 100, "f": ar.chart_features(bars),
                "s": {"price": 1.0, "mc": mc, "liq": liq, "vol24": turnover * mc, "fdv": mc,
                      "holders": 1000, "turnover": turnover, "liq_ratio": liq / mc,
                      "churn": churn, "float_ratio": float_ratio, "mc_fdv": 1.0,
                      "mul_point": 1.0, "listing_ms": 0, "chain": "BSC", "cex": False}}

    def test_ranking_orders_by_score(self):
        rows = [self._row("A", 1.0, 0.5), self._row("B", 0.1, 0.05), self._row("C", 0.5, 0.2)]
        rows, _ = ar.compute_scores(rows, {})
        self.assertEqual(rows[0]["symbol"], "A")
        self.assertEqual([r["rank"] for r in rows], [1, 2, 3])

    def test_wash_penalty_applied(self):
        rows = [self._row("A", 1.0, 0.5, churn=250.0), self._row("B", 0.9, 0.45), self._row("C", 0.2, 0.1)]
        rows, _ = ar.compute_scores(rows, {})
        a = next(r for r in rows if r["symbol"] == "A")
        self.assertIn("WASH_SUSPECT", a["flags"])
        self.assertAlmostEqual(a["penalty"], 0.70, places=6)

    def test_thin_liquidity_and_low_float_flags(self):
        rows = [self._row("A", 0.5, 0.2, liq=100000, float_ratio=0.1),
                self._row("B", 0.4, 0.2), self._row("C", 0.3, 0.2)]
        rows, _ = ar.compute_scores(rows, {})
        a = next(r for r in rows if r["symbol"] == "A")
        self.assertIn("THIN_LIQ", a["flags"])
        self.assertIn("LOW_FLOAT", a["flags"])
        self.assertLess(a["penalty"], 0.7)

    def test_theme_fit_never_flips_sign(self):
        rows = [self._row("A", 1.0, 0.5, theme="DEFI"), self._row("B", 0.8, 0.4, theme="DEFI"),
                self._row("C", -0.5, 0.02, theme="MEME_CULT"), self._row("D", -0.4, 0.02, theme="MEME_CULT")]
        rows, stat = ar.compute_scores(rows, {})
        for r in rows:
            self.assertGreaterEqual(r["theme_fit"], 0.55)
            self.assertLessEqual(r["theme_fit"], 1.0)
            self.assertEqual(r["score"] >= 0, r["base"] >= 0)

    def test_untagged_gets_neutral_fit(self):
        rows = [self._row(str(i), 0.3, 0.2, theme="UNTAGGED") for i in range(4)]
        rows, stat = ar.compute_scores(rows, {})
        self.assertAlmostEqual(stat["UNTAGGED"]["fit"], 0.775, places=6)

    def test_history_component_used_when_available(self):
        rows = [self._row("A", 0.3, 0.2), self._row("B", 0.3, 0.2), self._row("C", 0.3, 0.2)]
        rows[0]["s"]["holders"] = 3000
        hist = {"A_A": {"hol": 1000, "liq": 500000}, "A_B": {"hol": 1000, "liq": 1e6},
                "A_C": {"hol": 1000, "liq": 1e6}}
        rows, _ = ar.compute_scores(rows, hist)
        a = next(r for r in rows if r["symbol"] == "A")
        self.assertGreater(a["hol_chg"], 1.0)
        self.assertEqual(a["rank"], 1)


class TestEvents(unittest.TestCase):
    def _rows(self, n=12):
        rows = []
        for i in range(n):
            rows.append({"alpha_id": "A%d" % i, "symbol": "S%d" % i, "rank": i + 1,
                         "f": ar.chart_features(mk_bars([1.0] * 30)), "score": 1.0 - i * 0.1})
        return rows

    def test_streak_requires_two_days(self):
        rows = self._rows()
        ev1, st1 = ar.detect_events(rows, {}, "2026-08-22")
        self.assertTrue(all(v["streak"] <= 1 for v in st1.values()))
        ev2, st2 = ar.detect_events(rows, st1, "2026-08-23")
        self.assertEqual(st2["A0"]["streak"], 2)

    def test_exit_event_after_sustained_stay(self):
        rows = self._rows()
        _, st1 = ar.detect_events(rows, {}, "d1")
        _, st2 = ar.detect_events(rows, st1, "d2")
        rows2 = self._rows()
        dropped = rows2.pop(0)               # A0 를 목록 맨 뒤(상위 10 밖)로 밀어냄
        for i, r in enumerate(rows2):
            r["rank"] = i + 1
        dropped["rank"] = len(rows2) + 1
        rows2.append(dropped)
        ev3, _ = ar.detect_events(rows2, st2, "d3")
        self.assertTrue(any(e["type"] == "TOP10_EXIT" for e in ev3))

    def test_breakout_event(self):
        bars = mk_bars([1.0] * 25 + [1.5])
        for b in bars[-5:]:
            b["qv"] = 400000.0
        rows = [{"alpha_id": "A", "symbol": "S", "rank": 1, "score": 1.0,
                 "f": ar.chart_features(bars)}]
        ev, _ = ar.detect_events(rows, {}, "d1")
        self.assertTrue(any(e["type"] == "BREAKOUT" for e in ev))


class TestRender(unittest.TestCase):
    def _payload(self, status="OK", candidates=None):
        return {
            "as_of_kst": "2026-08-22 09:17", "data_status": status, "status_note": "테스트",
            "universe_size": 100, "coverage": 0.95,
            "market": {"median_ret7": 0.02, "breadth": 0.5, "bnb_ret7": 0.01, "regime": "선별 강세"},
            "candidates": candidates or [], "themes": [], "events": [],
        }

    def test_failed_status_says_unknown(self):
        msg = ar.render_telegram(self._payload("FAILED"))
        self.assertIn("판정 불가", msg)
        self.assertNotIn("추세 후보 (2일", msg)

    def test_zero_promotion_is_explicit(self):
        cand = [{"rank": 1, "symbol": "S", "chain": "BSC", "score": 1.0, "streak": 1,
                 "flags": [], "ret7": 0.1, "ret30": 0.2, "near_high": 0.9, "volx": 1.2,
                 "mc": 1e7, "liq": 1e6, "turnover": 0.5, "holders": 100}]
        msg = ar.render_telegram(self._payload("OK", cand))
        self.assertIn("충족 0건", msg)

    def test_promoted_rendering(self):
        cand = [{"rank": 1, "symbol": "S", "chain": "BSC", "score": 1.0, "streak": 3,
                 "flags": ["THIN_LIQ"], "ret7": 0.1, "ret30": 0.2, "near_high": 0.9,
                 "volx": 1.2, "mc": 1e7, "liq": 1e6, "turnover": 0.5, "holders": 100,
                 "c4h": {"above_ema20": True, "high25": False, "ret24h": 0.01}}]
        msg = ar.render_telegram(self._payload("OK", cand))
        self.assertIn("추세 후보", msg)
        self.assertIn("유동성얕음", msg)
        self.assertIn("4h구조", msg)

    def test_safe_label_strips_injection(self):
        self.assertNotIn("<", ar.safe_label("<script>alert(1)</script>"))
        self.assertNotIn(";", ar.safe_label("BTC; rm -rf /"))
        self.assertEqual(ar.safe_label(""), "?")

    def test_esc_html(self):
        self.assertEqual(ar.esc_html("<b>&"), "&lt;b&gt;&amp;")

    def test_dashboard_renders_and_escapes(self):
        p = self._payload("OK", [{"rank": 1, "symbol": "S<x", "chain": "BSC", "score": 1.0,
                                  "streak": 2, "flags": [], "ret7": 0.1, "ret30": 0.2,
                                  "near_high": 0.9, "volx": 1.2, "mc": 1e7, "liq": 1e6,
                                  "turnover": 0.5, "holders": 100}])
        html = ar.render_dashboard(p)
        self.assertIn("Alpha Trend Radar", html)
        self.assertNotIn("S<x", html)

    def test_formatters(self):
        self.assertEqual(ar.fmt_usd(2.5e6), "$2.5M")
        self.assertEqual(ar.fmt_pct(None), "n/a")
        self.assertEqual(ar.fmt_pct(0.1234, 1), "+12.3%")


class TestRegime(unittest.TestCase):
    def test_regime_labels(self):
        self.assertEqual(ar.regime_label(None, 0.5, None), "판정 불가")
        self.assertIn("강세", ar.regime_label(0.10, 0.6, 0.01))
        self.assertIn("약세", ar.regime_label(-0.20, 0.2, 0.01))
        self.assertIn("열위", ar.regime_label(0.01, 0.5, 0.10))


if __name__ == "__main__":
    unittest.main(verbosity=2)
