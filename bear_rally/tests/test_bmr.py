"""베어랠리 레이더 테스트 — 네트워크 없이 돈다 (python -m unittest discover -s bear_rally/tests)."""
import math
import os
import re
import sys
import unittest
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import breadth  # noqa: E402
import engine  # noqa: E402
import report  # noqa: E402
import sources  # noqa: E402
from engine import S_BEAR, S_BULL, S_FAILED, S_RALLY, S_SHIFT  # noqa: E402

KST = timezone(timedelta(hours=9))
ALLOWED = {  # 한 봉 안에서 가능한 상태 전이 (Pine 블록 1·2 구조상 봉당 최대 1회)
    (S_BULL, S_BEAR), (S_BEAR, S_RALLY), (S_RALLY, S_SHIFT), (S_RALLY, S_FAILED), (S_RALLY, S_BEAR),
    (S_FAILED, S_SHIFT), (S_FAILED, S_BEAR), (S_SHIFT, S_BULL), (S_SHIFT, S_FAILED), (S_SHIFT, S_BEAR),
}


def mkbars(closes, start="2020-01-06", step_days=1, weekdays_only=True, vol=1000.0):
    out, d = [], date.fromisoformat(start)
    for c in closes:
        while weekdays_only and d.weekday() >= 5:
            d += timedelta(days=1)
        out.append({"d": d.isoformat(), "o": c, "h": c * 1.01, "l": c * 0.99, "c": c, "v": vol})
        d += timedelta(days=step_days)
    return out


def regime_path(seed=7):
    """상승 → 급락(베어) → 반등 실패 → 신저점 → 큰 반등 → 회복 — 결정적 의사난수 경로."""
    import random
    rnd = random.Random(seed)
    px, out = 100.0, []
    plan = [(320, 0.0012), (70, -0.0065), (25, 0.0075), (30, -0.0060), (40, -0.0030), (60, 0.0060), (160, 0.0025)]
    for n, mu in plan:
        for _ in range(n):
            px *= math.exp(mu + rnd.gauss(0, 0.009))
            out.append(px)
    return out


class Indicators(unittest.TestCase):
    def test_sma_ema_rma(self):
        xs = [1, 2, 3, 4, 5, 6]
        self.assertEqual(engine.sma(xs, 3)[:3], [None, None, 2.0])
        self.assertAlmostEqual(engine.sma(xs, 3)[-1], 5.0)
        e = engine.ema(xs, 3)
        self.assertIsNone(e[1])
        self.assertAlmostEqual(e[2], 2.0)                    # SMA 시드
        self.assertAlmostEqual(e[3], 0.5 * 4 + 0.5 * 2.0)
        r = engine.rma(xs, 3)
        self.assertAlmostEqual(r[3], (4 + 2 * 2.0) / 3)

    def test_rsi_bounds(self):
        up = [100 + i for i in range(40)]
        self.assertEqual(engine.rsi(up, 14)[-1], 100.0)
        dn = [100 - i for i in range(40)]
        self.assertAlmostEqual(engine.rsi(dn, 14)[-1], 0.0)
        mix = [100 + (1 if i % 2 else -1) for i in range(60)]
        self.assertTrue(30 < engine.rsi(mix, 14)[-1] < 70)

    def test_atr_positive(self):
        b = mkbars([100 + i for i in range(30)])
        a = engine.atr([x["h"] for x in b], [x["l"] for x in b], [x["c"] for x in b], 14)
        self.assertIsNone(a[12])
        self.assertGreater(a[-1], 0)


class Weekly(unittest.TestCase):
    def test_previous_completed_week_only(self):
        b = mkbars([float(i) for i in range(1, 16)], start="2026-01-05")   # 월요일 시작 3주
        w = engine.weekly_frame([x["d"] for x in b], [x["c"] for x in b])
        self.assertIsNone(w["close"][0])                    # 첫 주에는 직전 주가 없음
        self.assertEqual(w["close"][5], 5.0)                # 둘째 주 월요일 → 첫 주 금요일 종가
        self.assertEqual(w["close"][14], 10.0)              # 셋째 주 금요일 → 둘째 주 종가 (진행 중인 주는 안 씀)

    def test_week_key_monday(self):
        self.assertEqual(engine.week_key("2026-11-29"), "2026-11-23")   # 일요일 → 그 주 월요일
        self.assertEqual(engine.week_key("2026-11-23"), "2026-11-23")


class StateMachine(unittest.TestCase):
    def _check(self, res):
        prev = S_BULL
        for r in res["rows"]:
            st = r["state"]
            self.assertIn(st, (S_BULL, S_BEAR, S_RALLY, S_FAILED, S_SHIFT))
            if st != prev:
                self.assertIn((prev, st), ALLOWED, f"{r['d']}: {prev}->{st}")
            prev = st

    def test_transitions_stock_and_crypto(self):
        for mode in ("stock", "crypto"):
            for seed in range(1, 9):
                b = mkbars(regime_path(seed), weekdays_only=(mode == "stock"))
                res = engine.run(b, mode)
                self._check(res)

    def test_bear_then_rally_detected(self):
        b = mkbars(regime_path(3))
        res = engine.run(b, "stock")
        types = [e["type"] for e in res["events"]]
        self.assertIn("bear_enter", types)
        self.assertIn("rally", types)
        self.assertLess(types.index("bear_enter"), types.index("rally"))

    def test_rally_requires_threshold(self):
        b = mkbars(regime_path(3))
        res = engine.run(b, "stock")
        for e in res["events"]:
            if e["type"] == "rally":
                self.assertGreaterEqual(e["rally_pct"], 0.10 - 1e-9)

    def test_no_breadth_scoring_base(self):
        res = engine.run(mkbars(regime_path(2)), "stock")
        self.assertEqual(res["shift_max"], 4)
        self.assertEqual(res["hits_max"], 5)
        res = engine.run(mkbars(regime_path(2), weekdays_only=False), "crypto")
        self.assertEqual(res["hits_max"], 6)

    def test_breadth_inputs_change_scoring_base(self):
        b = mkbars(regime_path(2))
        n = len(b)
        br = {"pct200": [55.0] * n, "pct50": [60.0] * n, "adv_ratio": [0.5] * n}
        res = engine.run(b, "stock", None, br)
        self.assertTrue(res["use_breadth"])
        self.assertEqual(res["shift_max"], 6)
        self.assertTrue(all(r["evid"]["breadth"] for r in res["rows"]))

    def test_deterministic(self):
        b = mkbars(regime_path(5))
        self.assertEqual(engine.run(b, "stock")["events"], engine.run(b, "stock")["events"])


class ZBT(unittest.TestCase):
    def test_fire_and_near(self):
        ratio = [0.5] * 20 + [0.2] * 12 + [0.9] * 10 + [0.5] * 30
        z = engine.zbt_track(ratio, 0.40, 0.615, 10, 0.58)
        self.assertTrue(any(z["fire"]))
        ratio2 = [0.5] * 20 + [0.2] * 12 + [0.75] * 4 + [0.45] * 40      # EMA 최고 ≈0.52: 0.50 근접, 0.615 미달
        z2 = engine.zbt_track(ratio2, 0.40, 0.615, 10, 0.50)
        self.assertFalse(any(z2["fire"]))
        self.assertTrue(any(z2["near"]))


class Sources(unittest.TestCase):
    def test_complete_utc_drops_open_bar(self):
        now = datetime(2026, 11, 24, 8, 0, tzinfo=KST)          # = 11/23 23:00 UTC → 11/23 봉은 미확정
        bars = [{"d": "2026-11-22", "h": 2, "l": 1, "c": 1.5, "o": 1, "v": 1},
                {"d": "2026-11-23", "h": 2, "l": 1, "c": 1.5, "o": 1, "v": 1}]
        out = sources._complete_utc([dict(b) for b in bars], now)
        self.assertEqual([b["d"] for b in out], ["2026-11-22"])
        later = datetime(2026, 11, 24, 9, 5, tzinfo=KST)
        self.assertEqual(len(sources._complete_utc([dict(b) for b in bars], later)), 2)

    def test_kr_unconfirmed_today(self):
        bars = [{"d": "2026-11-23"}, {"d": "2026-11-24"}]
        self.assertEqual(len(sources.drop_unconfirmed_kr(bars, datetime(2026, 11, 24, 9, 10, tzinfo=KST))), 1)
        self.assertEqual(len(sources.drop_unconfirmed_kr(bars, datetime(2026, 11, 24, 15, 45, tzinfo=KST))), 2)

    def test_us_session_closed(self):
        self.assertFalse(sources.us_session_closed("2026-11-23", datetime(2026, 11, 24, 5, 0, tzinfo=KST)))
        self.assertTrue(sources.us_session_closed("2026-11-23", datetime(2026, 11, 24, 6, 20, tzinfo=KST)))

    def test_closed_regex(self):
        m = sources._CLOSED_RE.search("Closed at Nov 23, 2026 4:00 PM ET")
        self.assertEqual(datetime.strptime(m.group(1), "%b %d, %Y").date(), date(2026, 11, 23))
        self.assertIsNone(sources._CLOSED_RE.search("Nov 23, 2026 5:34 PM ET"))

    def test_merge_patched_rules(self):
        real = {"d": "2026-11-23", "c": 10.0, "h": 11, "l": 9, "o": 10, "v": 1}
        patched = dict(real, c=10.5, patched=True)
        self.assertEqual(sources.merge([real], [patched])[0]["c"], 10.0)     # 보강 봉은 실제 봉을 못 덮음
        self.assertEqual(sources.merge([patched], [real])[0]["c"], 10.0)     # 실제 봉이 오면 교체


class Breadth(unittest.TestCase):
    def test_compute_and_align(self):
        days = [(date(2025, 1, 1) + timedelta(days=i)).isoformat() for i in range(260)]
        closes = {
            "UP": [[d, 100 + i] for i, d in enumerate(days)],
            "DN": [[d, 400 - i] for i, d in enumerate(days)],
        }
        out = breadth.compute(closes)
        p200, p50, adv, dec, n200, n50 = out[days[-1]]
        self.assertEqual((p200, p50, adv, dec, n200, n50), (50.0, 50.0, 1, 1, 2, 2))
        rows = [[d] + out[d] for d in days]
        old_min = breadth.MIN_COVERAGE
        try:
            breadth.MIN_COVERAGE = 0.0
            al = breadth.align(rows, days[-3:] + ["2099-01-01"])
        finally:
            breadth.MIN_COVERAGE = old_min
        self.assertEqual(al["pct200"][-1], 50.0)                       # 없는 날짜는 직전 값 유지
        self.assertEqual(al["adv_ratio"][-1], 0.5)

    def test_pack_roundtrip(self):
        t = {"A": [["2026-01-01", 1.0], ["2026-01-02", 2.0]], "B": [["2026-01-02", 3.0]]}
        self.assertEqual(breadth._unpack_tails(breadth._pack_tails(t)), t)


class Report(unittest.TestCase):
    def _snap(self):
        assets = []
        for key, mode, path_seed in (("SPY", "stock", 3), ("BTC", "crypto", 4)):
            b = mkbars(regime_path(path_seed), weekdays_only=(mode == "stock"))
            res = engine.run(b, mode)
            sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            import bmr
            s = bmr.summarize({"key": key, "name": key, "market": "us" if mode == "stock" else "crypto"}, b, res, None)
            s["src"], s["chart"] = "test", f"img/{key}.png"
            assets.append(s)
        return {"as_of_kst": "2026-11-24 09:10", "as_of_date": "2026-11-24", "basis": "테스트 기준",
                "pages_url": "https://example.invalid/", "assets": assets, "overview": "img/overview.png",
                "breadth": {"universe": 97, "last_date": None}, "notes": ["노트 <테스트>"]}

    def test_telegram_text(self):
        t = report.telegram(self._snap())
        self.assertIn("<b>SPY</b>", t)
        self.assertIn("<b>BTC</b>", t)
        self.assertLess(len(t), 1500)
        tags = set(re.findall(r"</?([a-z]+)", t))
        self.assertTrue(tags <= {"b", "i"}, tags)
        self.assertIn("&lt;테스트&gt;", t)                               # 노트는 이스케이프

    def test_page_html(self):
        h = report.page(self._snap())
        self.assertTrue(h.startswith("<!doctype html>"))
        self.assertIn("img/SPY.png", h)
        self.assertEqual(h.count("<table"), h.count("</table>"))


if __name__ == "__main__":
    unittest.main()
