#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
protocol.py 단위 테스트 — 네트워크를 타지 않는다.
테스트가 외부 API에 의존하면 API가 흔들릴 때 수집 잡이 영구 차단된다.
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import protocol as pr  # noqa: E402
import hood_radar as hr  # noqa: E402

CFG = hr.load_cfg()


def proto(slug, chains, fee24=1000.0, fee7=7000.0, fee30=30000.0, cat="Launchpad"):
    return {"slug": slug, "name": slug, "displayName": slug, "category": cat,
            "chains": chains, "total24h": fee24, "total7d": fee7, "total30d": fee30}


class TestRhShare(unittest.TestCase):
    def test_uses_chain_breakdown_not_chains_list(self):
        """실측: NOXA의 chains에는 5개 체인이 더 있지만 수수료는 전부 0이었다.
        목록에 있다는 것과 거기서 번다는 것은 다르다."""
        sm = {"total30d": 1000.0, "chains": ["Stable", "Monad", "Robinhood Chain"],
              "chainBreakdown": {"Robinhood Chain": {"total30d": 1000.0},
                                 "Stable": {"total30d": 0.0}, "Monad": {"total30d": 0.0}}}
        self.assertEqual(pr.rh_share(sm), 100.0)

    def test_partial_share(self):
        sm = {"total30d": 2623371.0,
              "chainBreakdown": {"Robinhood Chain": {"total30d": 2564433.0},
                                 "Base": {"total30d": 58941.0}}}
        self.assertAlmostEqual(pr.rh_share(sm), 97.8, places=1)

    def test_zero_total_returns_none(self):
        self.assertIsNone(pr.rh_share({"total30d": 0.0, "chainBreakdown": {}}))


class TestInferToken(unittest.TestCase):
    """DefiLlama의 symbol 필드는 불완전하다 — 실측에서 NOXA Fun과 StonkBrokers가
    모두 symbol='-'이지만 두 토큰 다 이 체인에 실재한다."""

    def _rows(self, *syms):
        return {s: {"symbol": s, "address": "0x" + s.lower().ljust(40, "0")} for s in syms}

    def test_links_on_normalized_name(self):
        hit = pr.infer_token({"name": "NOXA Fun", "slug": "noxa-fun"}, self._rows("NOXA", "CASHCAT"))
        self.assertEqual(hit["symbol"], "NOXA")

    def test_tolerates_plural(self):
        hit = pr.infer_token({"name": "StonkBrokers", "slug": "stonkbrokers"},
                             self._rows("STONKBROKER"))
        self.assertEqual(hit["symbol"], "STONKBROKER")

    def test_refuses_ambiguous_match(self):
        """후보가 둘이면 연결하지 않는다 — 잘못된 연결은 없는 밸류에이션을 만든다."""
        rows = {"PONS": {"symbol": "PONS", "address": "0xa" + "0" * 39},
                "PONSS": {"symbol": "PONSS", "address": "0xb" + "0" * 39}}
        self.assertIsNone(pr.infer_token({"name": "Pons", "slug": "pons"}, rows))

    def test_refuses_partial_match(self):
        """부분일치를 허용하면 'Pools'가 'POOL' 밈코인에 붙는 식의 오연결이 난다."""
        self.assertIsNone(pr.infer_token({"name": "Pons Family", "slug": "pons-family"},
                                         self._rows("PON")))

    def test_no_match_returns_none(self):
        self.assertIsNone(pr.infer_token({"name": "LetsCash", "slug": "letscash"},
                                         self._rows("CASHCAT")))


class TestAddress(unittest.TestCase):
    def test_strips_chain_prefix(self):
        a = pr.token_address({"address": "robinhood:0x39dBED3a2bd333467115dE45665cC57F813C4571"})
        self.assertEqual(a, "0x39dbed3a2bd333467115de45665cc57f813c4571")

    def test_rejects_other_chain(self):
        self.assertIsNone(pr.token_address({"address": "ethereum:0x" + "a" * 40}))

    def test_rejects_malformed(self):
        for bad in ({"address": ""}, {"address": "0xdead"}, {}, {"address": "robinhood:xyz"}):
            self.assertIsNone(pr.token_address(bad), bad)


class TestMetrics(unittest.TestCase):
    def test_pf_uses_revenue_when_available(self):
        it = pr.compute_metrics({"fdv": 100e6, "rev30": 2.4e6, "fee30": 13.6e6,
                                 "fee7": 10e6, "fee24": 3.9e6})
        self.assertEqual(it["basis"], "REV")
        # 100M / (2.4M*365/30 = 29.2M) = 3.42
        self.assertAlmostEqual(it["pf"], 3.42, places=1)

    def test_falls_back_to_fees_and_marks_basis(self):
        it = pr.compute_metrics({"fdv": 10e6, "rev30": 0.0, "fee30": 1e6, "fee7": 2e5, "fee24": 3e4})
        self.assertEqual(it["basis"], "FEES")
        self.assertIsNotNone(it["pf"])

    def test_no_divide_by_zero(self):
        it = pr.compute_metrics({"fdv": 10e6, "rev30": 0.0, "fee30": 0.0, "fee7": 0.0, "fee24": 0.0})
        self.assertIsNone(it["pf"])
        self.assertIsNone(it["momentum_pct"])
        self.assertIsNone(it["burst_pct"])

    def test_no_pf_without_fdv(self):
        it = pr.compute_metrics({"fdv": 0.0, "rev30": 1e6, "fee30": 1e6, "fee7": 1e5, "fee24": 1e4})
        self.assertIsNone(it["pf"])

    def test_burst_measures_24h_against_7d_average(self):
        # 7일 700 → 일평균 100. 24시간 300 → +200%
        it = pr.compute_metrics({"fdv": 1e6, "rev30": 1e5, "fee30": 3000.0, "fee7": 700.0, "fee24": 300.0})
        self.assertAlmostEqual(it["burst_pct"], 200.0, places=0)


class TestShares(unittest.TestCase):
    def test_intra_category_sums_to_100(self):
        items = [{"slug": "a", "category": "Launchpad", "fee24": 60.0},
                 {"slug": "b", "category": "Launchpad", "fee24": 40.0},
                 {"slug": "c", "category": "Dexs", "fee24": 999.0}]
        sh = pr.intra_category_shares(items, "Launchpad", "fee24")
        self.assertEqual(sorted(sh.keys()), ["a", "b"])
        self.assertAlmostEqual(sum(sh.values()), 100.0, places=1)

    def test_empty_category_returns_empty(self):
        self.assertEqual(pr.intra_category_shares([], "Launchpad", "fee24"), {})


class TestDetect(unittest.TestCase):
    def _payload(self, native, lp=None):
        return {"native": native, "shares": {"launchpad_24h": lp or {}},
                "tokenless": [], "external": [],
                "summary": {"chain_fee_24h": 1.0, "chain_fee_30d": 1.0, "native_n": len(native),
                            "rankable_n": 0, "tokenless_n": 0}}

    def test_collapse_fires_without_history(self):
        """이력이 없어도 7일 평균 대비로 붕괴를 잡아야 한다 — NOXA형 사건은 하루 안에 온다."""
        it = pr.compute_metrics({"slug": "noxa-fun", "symbol": "NOXA", "fdv": 1e5,
                                 "rev30": 0.0, "fee30": 3.9e6, "fee7": 7e5, "fee24": 1e3})
        ev = pr.detect(self._payload([it]), [], CFG, 1000000)
        self.assertIn("REV_COLLAPSE", [e["code"] for e in ev])

    def test_zero_revenue_is_highest_severity(self):
        it = pr.compute_metrics({"slug": "dead", "symbol": "DEAD", "fdv": 1e5,
                                 "rev30": 0.0, "fee30": 1e5, "fee7": 5e4, "fee24": 0.0})
        ev = pr.detect(self._payload([it]), [], CFG, 1000000)
        codes = [e["code"] for e in ev]
        self.assertIn("REV_ZERO", codes)
        self.assertEqual(ev[0]["code"], "REV_ZERO")  # severity 정렬 확인

    def test_no_events_on_quiet_data(self):
        it = pr.compute_metrics({"slug": "calm", "symbol": "CALM", "fdv": 1e7,
                                 "rev30": 3e5, "fee30": 3e5, "fee7": 7e4, "fee24": 1e4})
        self.assertEqual(pr.detect(self._payload([it]), [], CFG, 1000000), [])

    def test_pf_move_needs_history(self):
        it = pr.compute_metrics({"slug": "x", "symbol": "X", "fdv": 5e6,
                                 "rev30": 1e6, "fee30": 1e6, "fee7": 233333.0, "fee24": 33333.0})
        now = 1000000
        # 실제 배수 = 5e6 / (1e6*365/30) = 0.41배. 직전 1.0배 → 싸짐
        hist = [{"epoch": now - 24 * 3600, "pf": {"x": 1.0}, "fee24": {"x": 1.0},
                 "rev24": {}, "fdv": {}, "lp_share": {}}]
        codes = [e["code"] for e in pr.detect(self._payload([it]), hist, CFG, now)]
        self.assertIn("PF_CHEAP", codes)

        # 반대 방향도 잡히는지 — 직전이 0.1배였다면 재평가
        hist2 = [{"epoch": now - 24 * 3600, "pf": {"x": 0.1}, "fee24": {"x": 1.0},
                  "rev24": {}, "fdv": {}, "lp_share": {}}]
        codes2 = [e["code"] for e in pr.detect(self._payload([it]), hist2, CFG, now)]
        self.assertIn("PF_RERATE", codes2)

    def test_stale_history_is_ignored(self):
        """24시간을 크게 벗어난 스냅샷을 24h 비교에 쓰면 허위 변동이 생긴다."""
        it = pr.compute_metrics({"slug": "x", "symbol": "X", "fdv": 5e6,
                                 "rev30": 1e6, "fee30": 1e6, "fee7": 233333.0, "fee24": 33333.0})
        now = 1000000
        hist = [{"epoch": now - 20 * 24 * 3600, "pf": {"x": 1.0}, "fee24": {"x": 1.0},
                 "rev24": {}, "fdv": {}, "lp_share": {}}]
        codes = [e["code"] for e in pr.detect(self._payload([it]), hist, CFG, now)]
        self.assertNotIn("PF_CHEAP", codes)
        self.assertNotIn("PF_RERATE", codes)

    def test_share_shift(self):
        it = pr.compute_metrics({"slug": "pons-v2", "symbol": "PONS", "fdv": 2e8,
                                 "rev30": 2.4e6, "fee30": 1.3e7, "fee7": 3e6, "fee24": 4.3e5})
        now = 1000000
        hist = [{"epoch": now - 24 * 3600, "pf": {}, "fee24": {"pons-v2": 1.0},
                 "rev24": {}, "fdv": {}, "lp_share": {"pons-v2": 40.0}}]
        ev = pr.detect(self._payload([it], lp={"pons-v2": 75.0}), hist, CFG, now)
        self.assertIn("SHARE_SHIFT", [e["code"] for e in ev])


class TestRender(unittest.TestCase):
    def test_render_survives_empty_payload(self):
        self.assertEqual(pr.render_telegram(None, [], CFG), [])
        self.assertIn("불러오지 못했습니다", pr.render_html(None, [], CFG))

    def test_html_is_format_safe(self):
        """대시보드 템플릿은 str.format을 쓴다 — 삽입되는 HTML에 미이스케이프 중괄호가 있으면 안 된다."""
        it = pr.compute_metrics({"slug": "p", "symbol": "P", "name": "P", "category": "Launchpad",
                                 "fdv": 1e7, "rev30": 1e6, "fee30": 1e6, "fee7": 2e5, "fee24": 3e4,
                                 "liq": 1e6, "flags": []})
        it["value_rank"] = 1
        html = pr.render_html({"native": [it], "external": [], "tokenless": [],
                               "shares": {"launchpad_24h": {"p": 100.0}},
                               "summary": {"chain_fee_24h": 1e6, "chain_fee_30d": 1e7,
                                           "native_n": 1, "rankable_n": 1, "tokenless_n": 0}},
                              [], CFG)
        self.assertNotIn("{", html)
        self.assertNotIn("}", html)

    def test_telegram_escapes_html(self):
        it = pr.compute_metrics({"slug": "x", "symbol": "<b>evil</b>", "fdv": 1e7,
                                 "rev30": 1e6, "fee30": 1e6, "fee7": 2e5, "fee24": 3e4, "liq": 1e6})
        it["value_rank"] = 1
        out = "\n".join(pr.render_telegram(
            {"native": [it], "tokenless": [], "external": [],
             "shares": {"launchpad_24h": {}},
             "summary": {"chain_fee_24h": 1.0, "native_n": 1, "rankable_n": 1, "tokenless_n": 0}},
            [], CFG))
        self.assertIn("&lt;b&gt;evil", out)


class TestConfig(unittest.TestCase):
    def test_required_keys_present(self):
        for k in ("protocol_enabled", "protocol_top_n", "protocol_min_fee30_usd",
                  "protocol_min_liq_usd", "protocol_burst_dn_pct", "protocol_history_max"):
            self.assertIn(k, CFG, k)


if __name__ == "__main__":
    unittest.main(verbosity=2)
