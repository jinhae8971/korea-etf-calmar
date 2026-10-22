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

def read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)


CFG = read_json(os.path.join(ROOT, "watch_config.json"))
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


class Keywords(unittest.TestCase):
    """2026-10-22 오탐: 'recommended' 속 'ended', 'integration' 속 'ratio' — 단어 경계로 막는다."""

    def test_partial_words_do_not_hit(self):
        for s in ["### Option 1 — Ask a question (recommended)",
                  "- [Choose your integration](https://docs.lighter.xyz/developers/choose-your-integration.md)",
                  "Unclaimed reward history is in the portfolio tab maybe", "We are concerned about latency"]:
            self.assertFalse(J.KW.search(s), s)

    def test_real_statements_hit(self):
        for s in ["Points will convert to LIT at a ratio of 4.2 LIT per point.",
                  "Season 1 Points Program ended with the final distribution",
                  "The airdrop snapshot was taken on Friday", "Claim opens on Nov 3", "Points conversion begins"]:
            self.assertTrue(J.KW.search(s), s)

    def test_legacy_explanation(self):
        self.assertEqual(J.legacy_why(["### Option 1 — Ask a question (recommended)"]), "'recommended' 속 'ended'")
        self.assertEqual(J.legacy_why(["- [Choose your integration](u)"]), "'integration' 속 'ratio'")

    def test_announcement_words(self):
        self.assertFalse(J.KW_ANN.search("Scheduled maintenance appointment window"))
        self.assertTrue(J.KW_ANN.search("Weekly points drop moved to Saturday"))


class Docs(unittest.TestCase):
    AGENT = ("\n\n---\n\n# Agent Instructions\nThis documentation is published with GitBook.\n\n"
             "## Querying This Documentation\n### Option 1 — Ask a question (recommended)\n")

    def test_agent_block_stripped(self):
        import sources as S
        body = "# Points\n\nPoints are calculated in real time."
        self.assertEqual(S.normalize_doc(body + self.AGENT), S.normalize_doc(body))
        self.assertTrue(S.is_not_found("# Page Not Found\n\nThe URL `points-program` does not exist."))
        self.assertFalse(S.is_not_found(body))

    def test_minor_edit_is_watch(self):
        old = "# T\n\nManipulating snapshots, scoring methodologies, or eligibility.\n"
        new = "# T\n\nManipulating snapshots, scoring methodologies or eligibility calculations.\n"
        self.assertEqual(J.detect_doc("https://x/terms.md", old, new, NOW)[0]["grade"], "WATCH")

    def test_doc_names_and_links(self):
        self.assertEqual(J.doc_key("https://docs.lighter.xyz/programs/readme.md"), "programs/readme")
        self.assertEqual(J.doc_name("https://docs.lighter.xyz/programs/lighter-on-robinhood-chain-points.md", CFG),
                         "로빈후드 체인 포인트 안내")
        self.assertEqual(J.web_url("https://docs.lighter.xyz/programs/readme.md"), "https://docs.lighter.xyz/programs/readme")

    IDX_OLD = ("# Lighter Docs\n- [Points Program](https://d/points-program.md)\n"
               "- [Lighter on Robinhood Chain Points](https://d/points-program/lighter-on-robinhood-chain-points.md)\n"
               "- [Trading Fees](https://d/trading/trading-fees.md)\n")

    def test_index_move_is_not_a_signal(self):
        new = self.IDX_OLD.replace("points-program/lighter", "programs/lighter") + \
            "- [Choose your integration](https://d/developers/choose-your-integration.md)\n"
        ev = J.detect_doc("https://d/llms.txt", self.IDX_OLD, new, NOW, "문서 전체 목록")
        self.assertEqual([e["grade"] for e in ev], ["WATCH"])
        self.assertIn("주소 이전 1개", ev[0]["title"])
        self.assertIn("새 문서 1개", ev[0]["title"])

    def test_index_new_conversion_page_is_alert(self):
        new = self.IDX_OLD + "- [LIT Conversion](https://d/programs/lit-conversion.md): How points convert to LIT\n"
        ev = J.detect_doc("https://d/llms.txt", self.IDX_OLD, new, NOW)
        self.assertEqual(ev[0]["signal"], "index_keyword")
        self.assertEqual(ev[0]["grade"], "ALERT")

    def test_resolve_move(self):
        idx = J.parse_index(self.IDX_OLD.replace("points-program/lighter", "programs/lighter"))
        self.assertEqual(J.resolve_move("https://d/points-program/lighter-on-robinhood-chain-points.md", idx),
                         "https://d/programs/lighter-on-robinhood-chain-points.md")
        idx2 = J.parse_index("- [RH Points](https://d/programs/rh.md)\n")
        self.assertEqual(J.resolve_move("https://d/old/rh-points.md", idx2, "# RH Points\n\nbody"),
                         "https://d/programs/rh.md")
        self.assertIsNone(J.resolve_move("https://d/points-program.md", idx2, "# Points Program"))

    def test_rejudge_lifts_old_false_positives(self):
        old_rh = "https://docs.lighter.xyz/points-program/lighter-on-robinhood-chain-points.md"
        moved = J.event("S2", "doc_keyword", "ALERT", "문서 변경: lighter-on-robinhood-chain-points",
                        ts=NOW, ref={"url": old_rh, "added": ["### Option 1 — Ask a question (recommended)"]}, key="a")
        index = J.event("S2", "doc_keyword", "ALERT", "문서 변경: llms.txt", ts=NOW,
                        ref={"url": "https://docs.lighter.xyz/llms.txt",
                             "added": ["- [Choose your integration](https://d/choose-your-integration.md)"]}, key="b")
        real = J.event("S2", "doc_keyword", "ALERT", "문서 변경: x", ts=NOW,
                       ref={"url": "u", "added": ["Points convert to LIT at 4.2 LIT per point."]}, key="c")
        evs = [moved, index, real]
        n = J.rejudge(evs, {old_rh: {"to": "new", "same": True}}, NOW + 60)
        self.assertEqual(n, 2)
        self.assertTrue(moved["refuted"] and index["refuted"])
        self.assertFalse(real["refuted"])
        self.assertIn("문서 주소를 옮겼어요(내용은 그대로)", moved["refute_reason"])
        self.assertIn("'integration' 속 'ratio'", index["refute_reason"])
        self.assertEqual(moved["refute_kind"], "false_positive")


class S3(unittest.TestCase):
    def test_frontend(self):
        old = {"chunks": ["PointsLeaderboard", "referrals"], "api_paths": ["/api/v1/airdrop"]}
        new = {"chunks": ["PointsLeaderboard", "referrals", "ClaimRewards", "StockNews"],
               "api_paths": ["/api/v1/airdrop", "/api/v1/rhClaim"]}
        ev = {e["title"]: e["grade"] for e in J.detect_frontend(old, new, NOW)}
        self.assertEqual(ev["앱에 새 화면 모듈: ClaimRewards"], "ALERT")
        self.assertEqual(ev["앱에 새 화면 모듈: StockNews"], "WATCH")
        self.assertEqual(ev["앱에 새 API 경로: /api/v1/rhClaim"], "ALERT")
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


class Render(unittest.TestCase):
    """메시지만 보고 무슨 일인지 알 수 있어야 한다 — 무슨 일 → 의미 → 해제 조건 → 링크, 잘림 없음."""

    URL = "https://docs.lighter.xyz/programs/lighter-on-robinhood-chain-points.md"

    def doc_alert(self):
        old = "# Lighter on Robinhood Chain Points\n\nPoints are calculated in real time.\n"
        new = old + "\nPoints will convert to LIT at a ratio of 4.2 LIT per point after the final weekly drop on Nov 6.\n"
        return J.detect_doc(self.URL, old, new, NOW, J.doc_name(self.URL, CFG))

    def test_alert_reads_on_its_own(self):
        txt = J.render("ALERT", ["S2"], self.doc_alert(), NOW, CFG)
        self.assertTrue(txt.startswith("🟡 <b>라이터RH 전환 감시 · 경보</b>"))
        self.assertIn("10/12(월) 09:17 감지 · 공식 문서·공지", txt)
        self.assertIn("공식 문서 '로빈후드 체인 포인트 안내'에 전환 관련 문구 추가", txt)
        self.assertIn("after the final weekly drop on Nov 6.", txt)          # 문장이 잘리지 않는다
        for part in ("<b>무슨 일</b>", "<b>의미</b>", "<b>해제 조건</b>", "'임박'으로 올려요"):
            self.assertIn(part, txt)
        self.assertIn('<a href="https://docs.lighter.xyz/programs/lighter-on-robinhood-chain-points">문서 원문</a>', txt)
        self.assertNotIn("…", txt)
        self.assertNotIn("반증", txt)

    def test_imminent_lists_both(self):
        evs = self.doc_alert() + J.detect_rh_supply([[NOW - 86400, 396_441], [NOW, 11_300_000]], CFG, NOW)
        txt = J.render("IMMINENT", ["S1", "S2"], evs, NOW, CFG)
        self.assertTrue(txt.startswith("🔴"))
        self.assertIn("· 로빈후드 체인 LIT 공급 +10.90M (포인트 풀 규모)", txt)
        self.assertIn("서로 다른 종류의 신호 2개", txt)
        self.assertIn("공급이 7일 최저 수준으로 돌아오면 해제", txt)

    def test_html_escaped(self):
        e = J.event("S2", "announcement", "ALERT", "라이터 RH 앱 공지: <Season 3> & claims", "x < y", "r", NOW)
        txt = J.render("ALERT", ["S2"], [e], NOW, CFG)
        self.assertIn("&lt;Season 3&gt; &amp; claims", txt)
        self.assertNotIn("<Season", txt)

    def test_release_correction(self):
        e = J.event("S2", "doc_keyword", "ALERT", "문서 변경: llms.txt", ts=NOW,
                    ref={"url": "https://docs.lighter.xyz/llms.txt", "added": ["- [Choose your integration](u)"]})
        J.rejudge([e], {}, NOW + 3600)
        txt = J.render_release([e], "WATCH", "ALERT", NOW, NOW + 3600, CFG)
        self.assertTrue(txt.startswith("<b>라이터RH 전환 감시 · 정정</b>"))
        self.assertIn("10/12(월) 09:17 '경보' 알림은 오탐이었어요.", txt)
        self.assertIn("· 문서 전체 목록 — 'integration' 속 'ratio'처럼", txt)
        self.assertIn("관찰 — 경보 신호는 없고", txt)

    def test_release_by_data_is_haeje(self):
        ev = J.detect_eth([tr(A, B, 10_800_000)], {}, CFG, NOW, [])
        J.apply_refutes(ev, [tr(B, A, 10_000_000, NOW + 3600, "0x9")], CFG, NOW + 3600)
        txt = J.render_release(ev, "NONE", "ALERT", NOW, NOW + 3600, CFG)
        self.assertIn("· 해제</b>", txt)
        self.assertIn("원래 지갑으로 돌아왔어요", txt)


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
        self.pages = None          # {주소: 본문} — 주어지면 주소별로 다른 문서를 돌려준다
        self.fetched: list[str] = []
        self.lb_sum = 100_000.0

    def tearDown(self):
        sys.path.remove(self.root)
        sys.modules.pop("watch", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def fetch(self, url):
        self.fetched.append(url)
        if self.pages is None:
            return self.docs
        return self.pages.get(url, "# Page Not Found\n\nThe URL does not exist.\n\n### Option 1 — Ask a question (recommended)\n")

    def run_at(self, now):
        S = self.W.S
        patches = [
            mock.patch.object(S, "block_number", side_effect=lambda u: 1000 + (now // 12) % 10_000_000),
            mock.patch.object(S, "transfer_logs", return_value=[]),
            mock.patch.object(S, "total_supply", side_effect=lambda u, t: self.supply),
            mock.patch.object(S, "fetch_doc", side_effect=self.fetch),
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
        latest = read_json(os.path.join(self.root, "data", "latest.json"))
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
        self.assertIn("로빈후드 체인 LIT 공급", l3["outbox"]["text"])
        self.assertIn("10/12(월) 23:17 감지", l3["outbox"]["text"])     # 밤에 감지한 시각 그대로
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

    D = "https://docs.lighter.xyz/"

    def site(self, rh_path):
        rh = "# Lighter on Robinhood Chain Points\n\nWeekly drop every Friday.\n"
        return {
            self.D + "llms.txt": (f"# Lighter Docs\n- [Lighter on Robinhood Chain Points]({self.D}{rh_path}.md)\n"
                                  f"- [Programs]({self.D}programs/readme.md)\n"
                                  f"- [LIT Utility]({self.D}about-lighter/lit-utility.md)\n"),
            self.D + rh_path + ".md": rh,
            self.D + "programs/readme.md": "# Programs\n\nSeveral programs.\n",
            self.D + "about-lighter/lit-utility.md": "# LIT Utility\n\nBuybacks.\n",
        }

    def test_doc_move_is_followed_quietly(self):
        """2026-10-22처럼 문서 주소가 바뀌면 경보 없이 새 주소로 따라가고, 옛 내용과 그대로 비교한다."""
        self.pages = self.site("programs/lighter-on-robinhood-chain-points")
        t0 = ts(2026, 10, 12, 9, 17)
        self.run_at(t0)                                                   # 시딩
        self.pages = self.site("rewards/lighter-on-robinhood-chain-points")  # 주소만 옮김
        self.lb_sum += 1
        rc, l1 = self.run_at(t0 + 7200)
        self.assertEqual(rc, 0)
        self.assertIsNone(l1["outbox"])                                   # 경보·메시지 없음
        st = read_json(os.path.join(self.root, "data", "state.json"))
        old = self.D + "programs/lighter-on-robinhood-chain-points.md"
        new = self.D + "rewards/lighter-on-robinhood-chain-points.md"
        self.assertEqual(st["doc_moves"][old]["to"], new)
        self.assertTrue(st["doc_moves"][old]["same"])
        self.assertEqual(st["docs"][old]["url"], new)
        sig = {e["signal"]: e for e in st["events"]}
        self.assertEqual(sig["doc_moved"]["grade"], "WATCH")
        self.assertIn("(내용 그대로)", sig["doc_moved"]["title"])
        self.assertNotIn("doc_keyword", sig)
        # 다음 실행은 새 주소를 바로 읽고, 내용이 바뀌면 옛 기준선과 비교해 판정한다
        self.fetched.clear()
        self.pages[new] += "\nPoints will convert to LIT at 4.2 LIT per point.\n"
        self.lb_sum += 1
        rc, l2 = self.run_at(t0 + 14400)
        self.assertNotIn(old, self.fetched)
        self.assertEqual(l2["outbox"]["level"], "ALERT")
        self.assertIn("rewards/lighter-on-robinhood-chain-points", l2["outbox"]["text"])  # 원문 링크는 새 주소

    def test_missing_doc_keeps_baseline(self):
        self.pages = self.site("programs/lighter-on-robinhood-chain-points")
        t0 = ts(2026, 10, 12, 9, 17)
        self.run_at(t0)
        url = self.D + "programs/lighter-on-robinhood-chain-points.md"
        before = read_json(os.path.join(self.root, "data", "state.json"))["docs"][url]["text"]
        del self.pages[url]                                               # 목록에서도 사라짐
        self.pages[self.D + "llms.txt"] = "# Lighter Docs\n"
        self.lb_sum += 1
        rc, l = self.run_at(t0 + 7200)
        st = read_json(os.path.join(self.root, "data", "state.json"))
        self.assertEqual(st["docs"][url]["text"], before)                 # '페이지 없음'으로 덮지 않는다
        self.assertIn("doc_missing", [e["signal"] for e in st["events"]])
        self.assertIsNone(l["outbox"])

    def test_release_after_false_positive(self):
        """보낸 경보가 오탐으로 풀리면 낮에 '정정' 한 번 — 다음 실행에서 되풀이하지 않는다."""
        t0 = ts(2026, 10, 12, 9, 17)
        self.run_at(t0)
        p = os.path.join(self.root, "data", "state.json")
        st = read_json(p)
        e = J.event("S2", "doc_keyword", "ALERT", "문서 변경: llms.txt", ts=t0 + 600,
                    ref={"url": "https://docs.lighter.xyz/llms.txt", "added": ["- [Choose your integration](u)"]})
        st["events"].append(e)
        st.update({"notified": "ALERT", "notified_ids": [e["id"]], "notified_at": t0 + 900})
        write_json(p, st)
        self.lb_sum += 1
        rc, l = self.run_at(t0 + 3600)
        self.assertEqual(l["outbox"]["kind"], "release")
        self.assertIn("· 정정</b>", l["outbox"]["text"])
        self.assertIn("'integration' 속 'ratio'", l["outbox"]["text"])
        self.lb_sum += 1
        rc, l2 = self.run_at(t0 + 7200)
        self.assertEqual(l2["outbox"]["id"], l["outbox"]["id"])
        self.assertEqual(read_json(p)["notified"], "NONE")

    def test_expired_alert_does_not_block_the_next(self):
        """알린 신호가 7일 창을 지나 빠지면, 그 뒤 새 경보는 다시 보낸다."""
        t0 = ts(2026, 10, 12, 9, 17)
        self.run_at(t0)
        self.docs += "\nPoints convert to LIT at 4.2 LIT per point.\n"
        self.lb_sum += 1
        rc, l1 = self.run_at(t0 + 7200)
        self.assertEqual(l1["outbox"]["kind"], "alert")
        for d in range(2, 10, 2):                                         # 약신호(일반 문구 변경)만 이어지는 9일
            self.docs += f"\nPlain note {d}.\n"
            self.lb_sum += 1
            rc, lw = self.run_at(t0 + d * 86400)
        self.assertEqual(lw["level"], "WATCH")                            # 조용함(NONE)은 아니다
        self.docs += "\nClaim opens on Nov 3.\n"
        self.lb_sum += 1
        rc, l2 = self.run_at(t0 + 10 * 86400)
        self.assertEqual(l2["outbox"]["kind"], "alert")
        self.assertNotEqual(l2["outbox"]["id"], l1["outbox"]["id"])

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
