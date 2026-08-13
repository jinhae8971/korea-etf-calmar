# -*- coding: utf-8 -*-
import os
import sys
import unittest

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import income_etf_monitor as m  # noqa: E402


def series(vals, start="2026-01-01"):
    idx = pd.bdate_range(start=start, periods=len(vals))
    return pd.Series(vals, index=idx, dtype=float)


class TestMetrics(unittest.TestCase):
    def test_max_drawdown(self):
        s = series([100, 120, 90, 110])
        self.assertAlmostEqual(m.max_drawdown(s), -0.25, places=6)

    def test_max_drawdown_monotonic(self):
        self.assertAlmostEqual(m.max_drawdown(series([100, 101, 102])), 0.0, places=6)

    def test_total_return_window(self):
        s = series([100] * 20 + [110])
        self.assertIsNotNone(m.total_return(s, 7))

    def test_total_return_insufficient(self):
        self.assertIsNone(m.total_return(series([100]), 7))
        self.assertIsNone(m.total_return(series([100, 105]), 3650))

    def test_ytd_return(self):
        idx = pd.to_datetime(["2025-12-31", "2026-01-02", "2026-08-14"])
        s = pd.Series([100.0, 101.0, 125.0], index=idx)
        self.assertAlmostEqual(m.ytd_return(s), 0.25, places=6)

    def test_ytd_return_no_prior_year(self):
        idx = pd.to_datetime(["2026-01-02", "2026-08-14"])
        s = pd.Series([100.0, 120.0], index=idx)
        self.assertIsNone(m.ytd_return(s))


class TestFormat(unittest.TestCase):
    def test_pct_none(self):
        self.assertEqual(m.pct(None), "–")

    def test_pct_sign(self):
        self.assertEqual(m.pct(0.0123), "+1.2%")
        self.assertEqual(m.pct(-0.0123), "-1.2%")
        self.assertEqual(m.pct(0.0857, 2, sign=False), "8.57%")

    def test_money(self):
        self.assertEqual(m.money(13_400_000_000), "$13.40B")
        self.assertEqual(m.money(359_000_000), "$359M")
        self.assertEqual(m.money(None), "–")

    def test_delta_pp(self):
        self.assertAlmostEqual(m.delta_pp(13.6, 13.2), 0.4, places=6)
        self.assertIsNone(m.delta_pp(13.6, None))


class TestMessage(unittest.TestCase):
    def row(self, **kw):
        base = dict(
            ticker="QQQI", ok=True, name="테스트", date="2026-08-14", price=55.78,
            r_1w=0.012, r_1m=0.05, r_3m=0.08, r_ytd=0.124, r_1y=0.183,
            ttm_div=7.626, ttm_yield=0.1367, last_div=0.636, prev_div=0.62,
            div_chg=0.0258, last_div_date="2026-07-31", aum=13.4e9,
            mdd_1y=-0.082, mdd_all=-0.20, inception="2024-01-30", div_vs_avg=0.01,
            interval_days=30, interval_median=30, prev_vs_avg=0.0, div_rate_dev=0.0,
        )
        base.update(kw)
        return base

    def test_first_run_notice(self):
        msg = m.build_message([self.row()], {}, "2026-08-14")
        self.assertIn("첫 실행", msg)
        self.assertIn("QQQI", msg)

    def test_wow_and_alerts(self):
        prev = {"QQQI": self.row(ttm_yield=0.1300, aum=14.5e9)}
        msg = m.build_message(
            [self.row(div_chg=-0.12, div_vs_avg=-0.22)], prev, "2026-08-14"
        )
        self.assertIn("%p", msg)          # 배당률 증감 표기
        self.assertIn("분배금 추세 이탈", msg)  # 감액 경보
        self.assertIn("AUM 3%", msg)      # 순유출 경보

    def test_failed_row_does_not_crash(self):
        msg = m.build_message([{"ticker": "BALI", "ok": False}], {}, "2026-08-14")
        self.assertIn("데이터 수집 실패", msg)


class TestDiagnosis(unittest.TestCase):
    def base(self, **kw):
        r = dict(
            ticker="BALI", r_1m=0.01, interval_days=30, interval_median=30,
            prev_vs_avg=0.0, div_rate_dev=-0.30, div_vs_avg=-0.21,
        )
        r.update(kw)
        return r

    def test_skipped_payout(self):
        out = m.diagnose_distribution(self.base(interval_days=62), {})
        self.assertTrue(any("스킵" in x for x in out))

    def test_base_effect(self):
        out = m.diagnose_distribution(self.base(prev_vs_avg=0.40), {})
        self.assertTrue(any("기저효과" in x for x in out))

    def test_nav_linked(self):
        out = m.diagnose_distribution(self.base(div_rate_dev=0.01), {})
        self.assertTrue(any("기준가 하락" in x for x in out))

    def test_real_cut_detected(self):
        out = m.diagnose_distribution(self.base(div_rate_dev=-0.28), {})
        self.assertTrue(any("분배 자체의 감액" in x for x in out))

    def test_vol_regime(self):
        ctx = {"^VIX": {"m1": 12.0, "m6": 18.0, "last": 12.0}}
        out = m.diagnose_distribution(self.base(), ctx)
        self.assertTrue(any("프리미엄 수취 환경 축소" in x for x in out))

    def test_vxn_for_qqqi(self):
        ctx = {"^VXN": {"m1": 15.0, "m6": 22.0, "last": 15.0}}
        out = m.diagnose_distribution(self.base(ticker="QQQI"), ctx)
        self.assertTrue(any("^VXN" in x for x in out))

    def test_underlying_rally(self):
        out = m.diagnose_distribution(self.base(r_1m=0.11), {})
        self.assertTrue(any("콜 피인" in x for x in out))

    def test_fallback_with_url(self):
        out = m.diagnose_distribution(self.base(div_rate_dev=-0.07), {})
        self.assertEqual(len(out), 1)
        self.assertIn("자동 판별 불가", out[0])
        self.assertIn("ishares.com", out[0])

    def test_message_includes_reasons(self):
        rows = [dict(
            ticker="BALI", ok=True, name="테스트", date="2026-08-14", price=35.19,
            r_1w=0.01, r_1m=0.02, r_3m=0.03, r_ytd=0.16, r_1y=0.22,
            ttm_div=2.65, ttm_yield=0.0754, last_div=0.189, prev_div=0.267,
            div_chg=-0.29, div_vs_avg=-0.21, last_div_date="2026-08-01",
            aum=1.32e9, mdd_1y=-0.067, mdd_all=-0.166, inception="2023-09-28",
            interval_days=61, interval_median=30, prev_vs_avg=0.0, div_rate_dev=-0.3,
        )]
        msg = m.build_message(rows, {}, "2026-08-14", {})
        self.assertIn("분배금 추세 이탈", msg)
        self.assertIn("스킵", msg)


class TestConfig(unittest.TestCase):
    def test_get_tickers_default(self):
        os.environ.pop("TICKERS", None)
        self.assertEqual(m.get_tickers(), ["QQQI", "BALI", "JEPI", "SCHD"])

    def test_get_tickers_env(self):
        os.environ["TICKERS"] = "qqqi, idvo  bali"
        try:
            self.assertEqual(m.get_tickers(), ["QQQI", "IDVO", "BALI"])
        finally:
            os.environ.pop("TICKERS", None)


if __name__ == "__main__":
    unittest.main()
