import datetime as dt
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import judge as J  # noqa: E402

CFG = json.load(open(os.path.join(ROOT, "watch_config.json"), encoding="utf-8"))
KST = J.KST


def ts(y, m, d, h, mi=0):
    return int(dt.datetime(y, m, d, h, mi, tzinfo=KST).timestamp())


NOW = ts(2026, 10, 12, 9, 17)
A, B, C = "0x" + "a" * 40, "0x" + "b" * 40, "0x" + "c" * 40


def tr(frm, to, amt, t=NOW, tx="0x1", li=0):
    return {"from": frm, "to": to, "amount": amt, "ts": t, "tx": tx, "log_index": li, "block": 1}


class S1(unittest.TestCase):
    def test_pool_band_single(self):
        ev = J.detect_eth([tr(A, B, 10_800_000)], {}, CFG, NOW, [])
        self.assertEqual([e["grade"] for e in ev], ["ALERT"])
        self.assertEqual(ev[0]["signal"], "eth_pool_band")

    def test_fresh_contract(self):
        metas = {B: {"is_contract": True, "created_ts": NOW - 3 * 86400}}
        ev = J.detect_eth([tr(A, B, 2_000_000)], metas, CFG, NOW, [])
        self.assertEqual(ev[0]["signal"], "eth_fresh_contract")
        self.assertEqual(ev[0]["grade"], "ALERT")

    def test_old_contract_is_watch(self):
        metas = {B: {"is_contract": True, "created_ts": NOW - 90 * 86400}}
        ev = J.detect_eth([tr(A, B, 2_000_000)], metas, CFG, NOW, [])
        self.assertEqual(ev[0]["grade"], "WATCH")

    def test_cex_excluded(self):
        metas = {B: {"is_contract": False, "name": "Binance 14"}}
        self.assertEqual(J.detect_eth([tr(A, B, 10_500_000)], metas, CFG, NOW, []), [])

    def test_core_withdrawal_skipped(self):
        core = "0x3b4d794a66304f130a4db8f2551b0070dfcf5ca7"
        self.assertEqual(J.detect_eth([tr(core, A, 5_000_000)], {}, CFG, NOW, []), [])

    def test_burn_skipped(self):
        self.assertEqual(J.detect_eth([tr(A, "0x000000000000000000000000000000000000dead", 11e6)], {}, CFG, NOW, []),
                         [])

    def test_cumulative_band(self):
        hist = [tr(A, B, 4e6, NOW - 86400, "0x1"), tr(A, C, 4e6, NOW - 3600, "0x2"), tr(A, B, 3e6, NOW, "0x3")]
        ev = J.detect_eth([hist[-1]], {}, CFG, NOW, hist)
        self.assertIn("eth_pool_cumulative", [e["signal"] for e in ev])

    def test_refute_reverse(self):
        ev = J.detect_eth([tr(A, B, 10_800_000)], {}, CFG, NOW, [])
        n = J.apply_refutes(ev, [tr(B, A, 10_000_000, NOW + 3600, "0x9")], CFG)
        self.assertEqual(n, 1)
        self.assertTrue(ev[0]["refuted"])
        self.assertEqual(J.level_of(J.active(ev, NOW + 3600, 7))[0], "NONE")

    def test_refute_window_expired(self):
        ev = J.detect_eth([tr(A, B, 10_800_000)], {}, CFG, NOW, [])
        self.assertEqual(J.apply_refutes(ev, [tr(B, A, 11e6, NOW + 80 * 3600, "0x9")], CFG), 0)

    def test_rh_supply(self):
        h = [[NOW - 86400 * 2, 396_441], [NOW, 396_441 + 1_200_000]]
        ev = J.detect_rh_supply(h, CFG, NOW)
        self.assertEqual(ev[0]["grade"], "ALERT")
        self.assertIn("대량", ev[0]["title"])
        h2 = [[NOW - 86400, 396_441], [NOW, 11_500_000]]
        self.assertIn("풀 규모", J.detect_rh_supply(h2, CFG, NOW)[0]["title"])
        self.assertEqual(J.detect_rh_supply([[NOW - 1, 1e5], [NOW, 5e5]], CFG, NOW), [])

    def test_rh_supply_uses_7d_min(self):
        h = [[NOW - 10 * 86400, 0], [NOW - 86400, 5e6], [NOW, 5.5e6]]
        self.assertEqual(J.detect_rh_supply(h, CFG, NOW), [])


class S2(unittest.TestCase):
    OLD = "# Points\n\nPoints are calculated in real time.\n"

    def test_doc_keyword(self):
        new = self.OLD + "\nPoints will convert to LIT at a ratio of 4.2 LIT per point.\n"
        ev = J.detect_doc("https://x/lighter-on-robinhood-chain-points.md", self.OLD, new, NOW)
        self.assertEqual(ev[0]["grade"], "ALERT")

    def test_doc_plain_change(self):
        new = self.OLD + "\nPlease open a ticket in Discord.\n"
        self.assertEqual(J.detect_doc("u", self.OLD, new, NOW)[0]["grade"], "WATCH")

    def test_doc_first_seen_or_same(self):
        self.assertEqual(J.detect_doc("u", None, self.OLD, NOW), [])
        self.assertEqual(J.detect_doc("u", self.OLD, self.OLD, NOW), [])

    def test_announcements(self):
        items = [{"title": "New Perp Listing", "content": "$TSM at 10x", "created_at": 1},
                 {"title": "Points Program Update", "content": "Final weekly drop on Friday", "created_at": 2}]
        ev = J.detect_announcements(items, [], NOW)
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["grade"], "ALERT")
        self.assertEqual(J.detect_announcements(items, [J.ann_key(i) for i in items], NOW), [])

    def test_lb_slash(self):
        h = [[NOW - 7200, 132_900, 32_000], [NOW, 120_000, 30_000]]
        self.assertIn("lb_slash", [e["signal"] for e in J.detect_leaderboard(h, CFG, NOW)])

    def test_lb_frozen(self):
        h = [[NOW - 60 * 3600 + i * 7200, 150_000, 1] for i in range(31)]
        h[-1][0] = NOW
        sig = [e["signal"] for e in J.detect_leaderboard(h, CFG, NOW)]
        self.assertIn("lb_frozen", sig)

    def test_lb_growing_not_frozen(self):
        h = [[NOW - 60 * 3600 + i * 7200, 100_000 + i * 10, 1] for i in range(31)]
        self.assertNotIn("lb_frozen", [e["signal"] for e in J.detect_leaderboard(h, CFG, NOW)])

    def test_lb_slowdown(self):
        d0 = NOW - 28 * 86400
        h = [[d0 + i * 86400, 100_000 + (i * 1000 if i <= 21 else 21_000 + (i - 21) * 300), 1] for i in range(29)]
        h[-1][0] = NOW
        self.assertIn("lb_slowdown", [e["signal"] for e in J.detect_leaderboard(h, CFG, NOW)])


class S3(unittest.TestCase):
    def test_frontend(self):
        old = {"chunks": ["PointsLeaderboard", "referrals"], "api_paths": ["/api/v1/airdrop"]}
        new = {"chunks": ["PointsLeaderboard", "referrals", "ClaimRewards", "StockNews"],
               "api_paths": ["/api/v1/airdrop", "/api/v1/rhClaim"]}
        ev = {e["title"]: e["grade"] for e in J.detect_frontend(old, new, NOW)}
        self.assertEqual(ev["화면 모듈 추가: ClaimRewards"], "ALERT")
        self.assertEqual(ev["화면 모듈 추가: StockNews"], "WATCH")
        self.assertEqual(ev["API 경로 추가: /api/v1/rhClaim"], "ALERT")
        self.assertEqual(J.detect_frontend(None, new, NOW), [])


class Level(unittest.TestCase):
    def e(self, fam, g):
        return J.event(fam, "x", g, f"{fam}{g}", ts=NOW, key=f"{fam}{g}")

    def test_levels(self):
        self.assertEqual(J.level_of([])[0], "NONE")
        self.assertEqual(J.level_of([self.e("S1", "WATCH")])[0], "WATCH")
        self.assertEqual(J.level_of([self.e("S1", "ALERT")])[0], "ALERT")
        self.assertEqual(J.level_of([self.e("S1", "ALERT"), self.e("S2", "ALERT")])[0], "IMMINENT")
        self.assertEqual(J.level_of([self.e("S1", "ALERT"), self.e("S4", "ALERT")])[0], "ALERT")
        self.assertEqual(J.level_of([self.e("S3", "IMMINENT")])[0], "IMMINENT")

    def test_window(self):
        old = J.event("S1", "x", "ALERT", "t", ts=NOW - 8 * 86400)
        self.assertEqual(J.active([old], NOW, 7), [])

    def test_render_width(self):
        evs = [J.event("S1", "rh_supply", "ALERT", "RH체인 LIT 공급 +10.80M (풀 규모) 아주 긴 제목 테스트입니다",
                       "7일 최저 396K → 11.20M", "공급이 7일 최저 수준으로 복귀 시 해제", NOW),
               J.event("S2", "doc_keyword", "ALERT", "문서 변경: lighter-on-robinhood-chain-points",
                       "Points will convert to LIT at a ratio", "x", NOW)]
        txt = J.render("IMMINENT", ["S1", "S2"], evs, NOW, CFG)
        for ln in txt.split("\n"):
            if ln.startswith("http"):
                continue
            self.assertLessEqual(J.cell_width(ln), 40, ln)
        self.assertTrue(txt.startswith("🔴"))
        self.assertIn("반증:", txt)


class Integration(unittest.TestCase):
    """sources를 가짜로 바꿔 watch.main 전체 흐름(시딩→경보→야간 보류→중복 방지)을 검증."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        shutil.copytree(ROOT, os.path.join(self.tmp, "lit_rh_watch"),
                        ignore=shutil.ignore_patterns("data", "__pycache__", "account_probe"))
        self.root = os.path.join(self.tmp, "lit_rh_watch")
        sys.modules.pop("watch", None)
        sys.path.insert(0, self.root)
        import watch  # noqa
        self.W = watch
        self.supply = 396_441.0
        self.docs = "# Points\n\nreal time\n"
        self.lb_sum = 100_000.0

    def tearDown(self):
        sys.path.remove(self.root)
        sys.modules.pop("watch", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_at(self, now):
        S = self.W.S
        patches = [
            mock.patch.object(S, "block_number", side_effect=lambda u: 1000 + (now // 12) % 10_000_000),
            mock.patch.object(S, "transfer_logs", return_value=[]),
            mock.patch.object(S, "total_supply", side_effect=lambda u, t: self.supply),
            mock.patch.object(S, "fetch_doc", side_effect=lambda u: self.docs),
            mock.patch.object(S, "announcements", return_value=[]),
            mock.patch.object(S, "leaderboard", side_effect=lambda a: [
                {"addr": "0x", "points": self.lb_sum, "rank": 1}]),
            mock.patch.object(S, "frontend_surface", return_value={
                "chunks": [f"m{i}" for i in range(20)], "api_paths": ["/api/v1/airdrop"]}),
            mock.patch.object(S, "google_news", return_value=[]),
            mock.patch.object(S, "x_feed", side_effect=RuntimeError("x")),
            mock.patch.object(S, "polymarket", return_value=[]),
        ]
        for p in patches:
            p.start()
        try:
            with mock.patch.dict(os.environ, {"LITRH_NOW": str(now)}):
                rc = self.W.main([])
        finally:
            for p in patches:
                p.stop()
        latest = json.load(open(os.path.join(self.root, "data", "latest.json")))
        return rc, latest

    def test_flow(self):
        t0 = ts(2026, 10, 12, 9, 17)
        rc, l0 = self.run_at(t0)
        self.assertEqual(rc, 0)
        self.assertIsNone(l0["outbox"])  # 시딩: 발송 없음
        self.lb_sum += 500
        rc, l1 = self.run_at(t0 + 7200)
        self.assertIsNone(l1["outbox"])
        self.assertEqual(l1["level"], "NONE")
        # 밤 23:17 — 공급 급증 → ALERT지만 outbox 없음
        self.supply += 10_900_000
        self.lb_sum += 500
        rc, l2 = self.run_at(ts(2026, 10, 12, 23, 17))
        self.assertEqual(l2["level"], "ALERT")
        self.assertIsNone(l2["outbox"])
        # 다음날 07:17 — 보류분 발송
        self.lb_sum += 500
        rc, l3 = self.run_at(ts(2026, 10, 13, 7, 17))
        self.assertEqual(l3["outbox"]["level"], "ALERT")
        self.assertIn("RH체인 LIT 공급", l3["outbox"]["text"])
        first_id = l3["outbox"]["id"]
        # 같은 등급 유지 — 새 outbox 없음(기존 것 TTL 안에서 유지)
        self.lb_sum += 500
        rc, l4 = self.run_at(ts(2026, 10, 13, 9, 17))
        self.assertEqual(l4["outbox"]["id"], first_id)
        # 문서에 전환 비율 추가 → 서로 다른 신호군 2종 → IMMINENT, 새 outbox
        self.docs += "\nPoints convert to LIT at 4.2 LIT per point.\n"
        self.lb_sum += 500
        rc, l5 = self.run_at(ts(2026, 10, 13, 11, 17))
        self.assertEqual(l5["level"], "IMMINENT")
        self.assertNotEqual(l5["outbox"]["id"], first_id)
        self.assertTrue(l5["outbox"]["text"].startswith("🔴"))

    def test_undecidable_exit(self):
        t0 = ts(2026, 10, 12, 9, 17)
        self.run_at(t0)
        S = self.W.S
        with mock.patch.object(S, "total_supply", side_effect=RuntimeError("x")), \
             mock.patch.object(S, "fetch_doc", side_effect=RuntimeError("x")), \
             mock.patch.object(S, "announcements", side_effect=RuntimeError("x")), \
             mock.patch.object(S, "leaderboard", side_effect=RuntimeError("x")), \
             mock.patch.object(S, "block_number", side_effect=RuntimeError("x")), \
             mock.patch.object(S, "frontend_surface", side_effect=RuntimeError("x")), \
             mock.patch.object(S, "google_news", return_value=[]), \
             mock.patch.object(S, "x_feed", side_effect=RuntimeError("x")), \
             mock.patch.object(S, "polymarket", return_value=[]):
            with mock.patch.dict(os.environ, {"LITRH_NOW": str(t0 + 7200)}):
                self.assertEqual(self.W.main([]), 0)  # 1회는 경고만
            with mock.patch.dict(os.environ, {"LITRH_NOW": str(t0 + 14400)}):
                self.assertEqual(self.W.main([]), 1)  # 2회 연속부터 실패

    def test_heartbeat_first_week(self):
        t0 = ts(2026, 10, 30, 9, 17)
        self.run_at(t0)  # 시딩 — 10월 생존신고 생략
        self.lb_sum += 1
        rc, l = self.run_at(ts(2026, 11, 2, 9, 17))
        self.assertEqual(l["outbox"]["kind"], "heartbeat")
        self.lb_sum += 1
        rc, l2 = self.run_at(ts(2026, 11, 2, 11, 17))
        self.assertEqual(l2["outbox"]["id"], l["outbox"]["id"])  # 같은 달 재발행 없음


if __name__ == "__main__":
    unittest.main()
