# -*- coding: utf-8 -*-
"""오프라인 테스트 — 네트워크 없이 합성 시세로 계산·본문·멱등·허브 호환을 검증한다."""
import json
import os
import re
import sys
import tempfile
import unittest
from datetime import datetime

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import kospi_levels as kl  # noqa: E402


def synth(seed: int = 7, days: int = 780) -> tuple[pd.DataFrame, pd.DataFrame]:
    """상승 → 급락 → 반등 구간을 가진 합성 일봉과, 그걸 월요일 기준 주봉으로 묶은 데이터."""
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2023-10-02", periods=days)
    drift = np.r_[np.full(days - 120, 0.0012), np.full(60, -0.004), np.full(60, 0.002)]
    close = 2400 * np.exp(np.cumsum(drift + rng.normal(0, 0.011, days)))
    open_ = close * (1 + rng.normal(0, 0.003, days))
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.006, days)))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.006, days)))
    daily = pd.DataFrame({"Open": open_, "High": high, "Low": low, "Close": close,
                          "Volume": rng.integers(3e8, 9e8, days)}, index=idx)
    weekly = daily.resample("W-MON", label="left", closed="left").agg(
        {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}).dropna()
    return weekly, daily.tail(40)


class ComputeTest(unittest.TestCase):
    def setUp(self):
        self.weekly, self.daily = synth()
        self.d = kl.compute_reference_points(self.weekly, self.daily)

    def test_core_values_finite(self):
        for k in ("price", "chg", "st", "ma", "rsi", "rsi_sma", "macd", "macd_sig", "atr",
                  "swing_high", "swing_low", "base_low"):
            self.assertTrue(np.isfinite(self.d[k]), k)
        self.assertGreater(self.d["price"], 0)
        self.assertIn(self.d["regime"], ("상승추세 유지", "추세 훼손/전환 경계", "변동성 조정(중립)"))

    def test_levels_sides_and_order(self):
        p = self.d["price"]
        res, sup = self.d["resistances"], self.d["supports"]
        self.assertLessEqual(len(res), 4)
        self.assertLessEqual(len(sup), 4)
        self.assertTrue(all(lv > p for _, lv in res))
        self.assertTrue(all(lv < p for _, lv in sup))
        self.assertEqual([lv for _, lv in res], sorted(lv for _, lv in res))
        self.assertEqual([lv for _, lv in sup], sorted((lv for _, lv in sup), reverse=True))

    def test_dates(self):
        self.assertEqual(self.d["close_date"], self.daily.index[-1].strftime("%Y-%m-%d"))
        self.assertEqual(self.d["week_bar"], self.weekly.index[-1].strftime("%Y-%m-%d"))

    def test_rejects_nan_price(self):
        bad = self.daily.copy()
        bad.iloc[-1, bad.columns.get_loc("Close")] = np.nan
        with self.assertRaises(ValueError):
            kl.compute_reference_points(self.weekly, bad)


def synth_crash(seed: int = 21) -> tuple[pd.DataFrame, pd.DataFrame]:
    """긴 상승 뒤 급락 → 슈퍼트렌드가 가격 위로 올라간 하락 국면 (2026-10 실제 국면과 같은 모양)."""
    rng = np.random.default_rng(seed)
    days = 780
    idx = pd.bdate_range("2023-10-02", periods=days)
    drift = np.r_[np.full(days - 140, 0.0016), np.full(110, -0.0052), np.full(30, 0.001)]
    close = 2400 * np.exp(np.cumsum(drift + rng.normal(0, 0.009, days)))
    high, low = close * 1.006, close * 0.994
    daily = pd.DataFrame({"Open": close, "High": high, "Low": low, "Close": close,
                          "Volume": np.full(days, 5e8)}, index=idx)
    weekly = daily.resample("W-MON", label="left", closed="left").agg(
        {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}).dropna()
    return weekly, daily.tail(40)


class ScenarioDirectionTest(unittest.TestCase):
    """약세 시나리오의 하방 목표는 항상 직전 저점 아래여야 한다 (2026-10-24 하락 국면 버그)."""

    def test_downtrend_bear_targets_below(self):
        w, d = synth_crash()
        r = kl.compute_reference_points(w, d)
        self.assertFalse(r["st_up"])
        self.assertGreater(r["st"], r["swing_low"])
        self.assertLess(r["deep_target"], r["swing_low"])
        self.assertLess(r["deep_target"], r["price"])
        act = r["scenarios"]["bear"]["action"]
        self.assertNotIn("테스트", act)
        self.assertIn("위쪽 저항", act)
        self.assertLess(r["ext_pct"], -6)
        self.assertIn("하방", kl.build_message(r))

    def test_bear_target_below_swing_low_for_all_shapes(self):
        for seed in range(30):
            for fn in (synth, synth_crash):
                w, d = fn(seed=seed)
                r = kl.compute_reference_points(w, d)
                self.assertLess(r["deep_target"], r["swing_low"], (fn.__name__, seed))

    def test_uptrend_wording_unchanged(self):
        for seed in range(30):
            w, d = synth(seed=seed)
            r = kl.compute_reference_points(w, d)
            if r["st"] < r["swing_low"]:
                self.assertTrue(r["scenarios"]["bear"]["action"].startswith(f"슈퍼트렌드 {kl.fmt(r['st'])} 테스트"))
                return
        self.skipTest("상승 구조 표본 없음")


class MessageTest(unittest.TestCase):
    def setUp(self):
        weekly, daily = synth(seed=11)
        self.d = kl.compute_reference_points(weekly, daily)
        self.msg = kl.build_message(self.d)

    def test_shape(self):
        self.assertTrue(self.msg.startswith("<b>KOSPI 매매기준점</b>"))
        for part in ("〈지표〉", "〈저항 ▲〉", "〈지지 ▼〉", "〈시나리오 &amp; 트리거〉", "종가 기준"):
            self.assertIn(part, self.msg)
        self.assertLess(len(self.msg), 3900)          # 허브 분할 기준 안쪽

    def test_html_balanced_and_safe(self):
        for t in ("b", "i"):
            self.assertEqual(self.msg.count(f"<{t}>"), self.msg.count(f"</{t}>"), t)
        stripped = re.sub(r"</?(b|i)>", "", self.msg)
        self.assertNotIn("<", stripped)                 # 텔레그램 HTML 파싱 오류 방지
        self.assertNotIn(">", stripped)
        self.assertNotIn("&", re.sub(r"&(amp|lt|gt|quot);", "", stripped))   # 맨 & 금지

    def test_calm_style(self):
        self.assertNotIn("📊", self.msg)
        self.assertEqual(sum(self.msg.count(c) for c in "🟢🟡🔴"), 1)   # 색 표시는 레짐 한 곳만


class IdempotencyTest(unittest.TestCase):
    def test_skip_when_today_exists(self):
        now = datetime.now(kl.KST)
        with tempfile.TemporaryDirectory() as td:
            out = os.path.join(td, "latest.json")
            kl.write_json(out, {"as_of_kst": now.strftime("%Y-%m-%d %H:%M"), "message": "x"})
            self.assertTrue(kl.already_collected(out, now))
            orig = kl.fetch_ohlc
            kl.fetch_ohlc = lambda: (_ for _ in ()).throw(AssertionError("네트워크 호출 금지"))
            try:
                self.assertEqual(kl.main(["--out", out]), 0)
            finally:
                kl.fetch_ohlc = orig

    def test_not_skipped_for_yesterday_or_empty(self):
        now = datetime(2026, 10, 26, 7, 5, tzinfo=kl.KST)
        with tempfile.TemporaryDirectory() as td:
            out = os.path.join(td, "latest.json")
            self.assertFalse(kl.already_collected(out, now))
            kl.write_json(out, {"as_of_kst": "2026-10-23 07:05", "message": "x"})
            self.assertFalse(kl.already_collected(out, now))
            kl.write_json(out, {"as_of_kst": "2026-10-26 07:05", "message": ""})
            self.assertFalse(kl.already_collected(out, now))


class HubContractTest(unittest.TestCase):
    """Brief Hub(schedule.json: fresh_key=as_of_kst, body=message)와의 계약."""

    def test_payload_contract(self):
        weekly, daily = synth(seed=3)
        d = kl.compute_reference_points(weekly, daily)
        now = datetime(2026, 10, 26, 7, 5, tzinfo=kl.KST)
        p = kl.build_payload(d, now)
        self.assertEqual(p["as_of_kst"], "2026-10-26 07:05")
        self.assertRegex(p["as_of_kst"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")
        self.assertTrue(p["message"].strip())
        json.dumps(p, ensure_ascii=False)               # 직렬화 가능
        with tempfile.TemporaryDirectory() as td:
            out = os.path.join(td, "data", "latest.json")
            kl.write_json(out, p)
            with open(out, encoding="utf-8") as f:
                self.assertEqual(json.load(f)["message"], p["message"])


class TestDailySource(unittest.TestCase):
    """2026-10-30: Yahoo 일봉 누락(10/29) 재발 방지 — 미확정 봉 제외 · 주봉 마지막 봉 보정."""

    def _daily(self, dates):
        idx = pd.to_datetime(dates)
        n = len(idx)
        return pd.DataFrame({"Open": np.arange(n) + 100.0, "High": np.arange(n) + 110.0,
                             "Low": np.arange(n) + 90.0, "Close": np.arange(n) + 105.0,
                             "Volume": np.ones(n)}, index=idx)

    def test_drop_today_before_close(self):
        d = self._daily(["2026-10-28", "2026-10-29", "2026-10-30"])
        out = kl.drop_unconfirmed(d, datetime(2026, 10, 30, 7, 42, tzinfo=kl.KST))
        self.assertEqual(out.index[-1].strftime("%Y-%m-%d"), "2026-10-29")

    def test_keep_today_after_close(self):
        d = self._daily(["2026-10-29", "2026-10-30"])
        out = kl.drop_unconfirmed(d, datetime(2026, 10, 30, 16, 0, tzinfo=kl.KST))
        self.assertEqual(out.index[-1].strftime("%Y-%m-%d"), "2026-10-30")

    def test_weekly_last_close_follows_daily(self):
        w = self._daily(["2026-10-12", "2026-10-19", "2026-10-26"])
        d = self._daily(["2026-10-26", "2026-10-27", "2026-10-28", "2026-10-29"])
        out = kl.patch_weekly_last(w, d)
        self.assertEqual(len(out), 3)
        self.assertEqual(out["Close"].iloc[-1], d["Close"].iloc[-1])
        self.assertEqual(out["High"].iloc[-1], d["High"].max())

    def test_zero_price_bar_removed(self):
        d = self._daily(["2026-10-19", "2026-10-26"])
        d.loc[d.index[0], "Low"] = 0.0
        out = kl.sanitize_ohlc(d)
        self.assertEqual(len(out), 1)
        self.assertTrue((out[["Open", "High", "Low", "Close"]] > 0).all().all())

    def test_weekly_missing_week_is_added(self):
        w = self._daily(["2026-10-12", "2026-10-19"])
        d = self._daily(["2026-10-26", "2026-10-27"])
        out = kl.patch_weekly_last(w, d)
        self.assertEqual(out.index[-1].strftime("%Y-%m-%d"), "2026-10-26")
        self.assertEqual(out["Close"].iloc[-1], d["Close"].iloc[-1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
