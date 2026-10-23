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


if __name__ == "__main__":
    unittest.main(verbosity=2)
