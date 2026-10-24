import copy
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
import sources as S  # noqa: E402


def read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)


CFG = read_json(os.path.join(ROOT, "radar_config.json"))
VENUES = {v["id"]: v for v in CFG["venues"]}
LRH = VENUES["lighter-rh"]
OC = LRH["onchain"]
NAMES = LRH["docs"]["names"]
KST = J.KST


def ts(y, m, d, h, mi=0):
    return int(dt.datetime(y, m, d, h, mi, tzinfo=KST).timestamp())


NOW = ts(2026, 10, 12, 9, 17)
A, B, C = "0x" + "a" * 40, "0x" + "b" * 40, "0x" + "c" * 40


def tr(frm, to, amt, t=NOW, tx="0x1", li=0):
    return {"from": frm, "to": to, "amount": amt, "ts": t, "tx": tx, "log_index": li, "block": 1}


class Config(unittest.TestCase):
    def test_seven_venues(self):
        self.assertEqual([v["id"] for v in CFG["venues"]],
                         ["arcus", "lighter-rh", "qfex", "extended", "standx", "variational", "pacifica"])
        for v in CFG["venues"]:
            self.assertTrue(v.get("status_note"), v["id"])
            for k in ("news", "polymarket"):
                if v.get(k):
                    __import__("re").compile(v[k]["match"])
        self.assertTrue(CFG["dashboard_url"].endswith("/perp-dex-radar/"))


class Keywords(unittest.TestCase):
    """오탐 이력: 10/22 'recommended' 속 'ended'·'integration' 속 'ratio', 10/24 토큰 언급 없는 'distributed'."""

    def test_partial_words_do_not_hit(self):
        for s in ["### Option 1 — Ask a question (recommended)",
                  "- [Choose your integration](https://docs.lighter.xyz/developers/choose-your-integration.md)",
                  "Unclaimed reward history is in the portfolio tab", "We are concerned about latency"]:
            self.assertEqual(J.signal_words(s), [], s)

    def test_points_description_is_not_a_signal(self):
        for s in ["The live points are distributed in real time based on factors like volume, positions held.",
                  "Weekly points are distributed every Friday, and are fine-tuned depending on the week’s activity.",
                  "Manipulating snapshots, scoring methodologies, or eligibility calculations.",
                  "Rotate your API token to unlock higher rate limits", "Stock tokens used as collateral earn rewards",
                  "The trading competition ended with 3,000 participants"]:
            self.assertEqual(J.signal_words(s, ["LIT"]), [], s)

    def test_real_statements_hit(self):
        for s, tk in [("Points will convert to LIT at a ratio of 4.2 LIT per point.", ["LIT"]),
                      ("Season 1 Points Program ended with the final distribution", []),
                      ("The airdrop snapshot was taken on Friday", []), ("Claim opens on Nov 3", []),
                      ("Points conversion begins", []), ("LIT will be distributed to points holders", ["LIT"]),
                      ("$VAR will be launched in Q4 2026", ["VAR"]), ("Launch of the $VAR token", ["VAR"])]:
            self.assertTrue(J.signal_words(s, tk), s)

    def test_venue_extra_rule(self):
        qfex = VENUES["qfex"]["extra_kw"]
        self.assertTrue(J.signal_words("Introducing the QFEX Points Program", [], qfex))
        self.assertFalse(J.signal_words("Introducing the QFEX Points Program"))

    def test_legacy_explanation(self):
        self.assertEqual(J.legacy_why(["### Option 1 — Ask a question (recommended)"]), "'recommended' 속 'ended'")
        self.assertEqual(J.legacy_why(["- [Choose your integration](u)"]), "'integration' 속 'ratio'")
        self.assertEqual(J.legacy_why(["The live points are distributed in real time"]), "")  # 통째 단어 — 문맥 문제

    def test_dates(self):
        self.assertTrue(J.has_date("TGE is set for Nov 12"))
        self.assertTrue(J.has_date("Claims open 2026-11-01"))
        self.assertFalse(J.has_date("24/7 trading on 100+ markets"))
        self.assertFalse(J.has_date("TGE in Q4 2026"))


class Docs(unittest.TestCase):
    GITBOOK = ("\n\n---\n\n# Agent Instructions\nThis documentation is published with GitBook.\n\n"
               "## Querying This Documentation\n### Option 1 — Ask a question (recommended)\n")
    MINTLIFY_HEAD = ("> ## Documentation Index\n> Fetch the complete documentation index at: https://x/llms.txt\n"
                     "> Use this file to discover all available pages before exploring further.\n\n")

    def test_boilerplate_stripped(self):
        body = "# Points\n\nPoints are calculated in real time."
        self.assertEqual(S.normalize_doc(body + self.GITBOOK), S.normalize_doc(body))
        self.assertEqual(S.normalize_doc(self.MINTLIFY_HEAD + body + "\n\nThis documentation is built and hosted on "
                                         "[Mintlify](https://mintlify.com)."), S.normalize_doc(body))
        self.assertTrue(S.is_not_found("# Page Not Found\n\nThe URL `points-program` does not exist."))

    def test_html_page_to_text(self):
        raw = "<!doctype html><html><head><script>x()</script></head><body><nav>menu</nav><h1>Points</h1><p>a &amp; b</p></body></html>"
        self.assertEqual(S.html_text(raw), "# Points\na & b")

    def test_minor_edit_is_watch(self):
        old = "# T\n\nNo airdrop snapshot has been taken yet.\n"
        new = "# T\n\nNo airdrop snapshot has been taken yet!\n"
        self.assertEqual(J.detect_doc("https://x/terms.md", old, new, NOW)[0]["grade"], "WATCH")

    def test_checkbox_and_date_are_not_minor(self):
        old = "# Roadmap\n\n- [ ] Launch of the $VAR token\n"
        new = "# Roadmap\n\n- [x] Launch of the $VAR token\n"
        self.assertEqual(J.detect_doc("https://x/roadmap.md", old, new, NOW, tickers=["VAR"])[0]["grade"], "ALERT")
        old2 = "# Token\n\n$VAR will be launched in Q4 2026.\n"
        new2 = "# Token\n\n$VAR will be launched on Nov 12, 2026.\n"
        self.assertEqual(J.detect_doc("https://x/token.md", old2, new2, NOW, tickers=["VAR"])[0]["grade"], "IMMINENT")

    def test_any_change_page(self):
        old = "# Does Arcus have a token?\n\nNo, Arcus does not currently have a token.\n"
        new = "# Does Arcus have a token?\n\nYes. ARC launches with the Season 1 rewards.\n"
        ev = J.detect_doc("https://help.arcus.xyz/faq/token.md", old, new, NOW, "토큰 FAQ", watch_any=True)
        self.assertEqual((ev[0]["grade"], ev[0]["title"]), ("ALERT", "공식 문서 '토큰 FAQ' 답변이 바뀜"))
        self.assertEqual(J.rejudge(ev, {}, NOW + 60), 0)  # 다시 판정해도 풀리지 않는다

    def test_doc_names_and_links(self):
        self.assertEqual(J.doc_key("https://docs.lighter.xyz/programs/readme.md"), "programs/readme")
        self.assertEqual(J.doc_name("https://docs.lighter.xyz/programs/lighter-on-robinhood-chain-points.md", NAMES),
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

    def test_index_new_token_page_is_alert(self):
        new = self.IDX_OLD + "- [XTEN Tokenomics](https://d/token/xten.md): Supply and airdrop\n"
        ev = J.detect_doc("https://d/llms.txt", self.IDX_OLD, new, NOW, tickers=["XTEN"])
        self.assertEqual((ev[0]["signal"], ev[0]["grade"]), ("index_keyword", "ALERT"))

    def test_resolve_move(self):
        idx = J.parse_index(self.IDX_OLD.replace("points-program/lighter", "programs/lighter"))
        self.assertEqual(J.resolve_move("https://d/points-program/lighter-on-robinhood-chain-points.md", idx),
                         "https://d/programs/lighter-on-robinhood-chain-points.md")
        idx2 = J.parse_index("- [RH Points](https://d/programs/rh.md)\n")
        self.assertEqual(J.resolve_move("https://d/old/rh-points.md", idx2, "# RH Points\n\nbody"), "https://d/programs/rh.md")
        self.assertIsNone(J.resolve_move("https://d/points-program.md", idx2, "# Points Program"))

    def test_rejudge_lifts_old_false_positives(self):
        old_rh = "https://docs.lighter.xyz/points-program/lighter-on-robinhood-chain-points.md"
        moved = J.event("S2", "doc_keyword", "ALERT", "문서 변경: rh", ts=NOW,
                        ref={"url": old_rh, "added": ["### Option 1 — Ask a question (recommended)"]}, key="a")
        index = J.event("S2", "doc_keyword", "ALERT", "문서 변경: llms.txt", ts=NOW,
                        ref={"url": "https://docs.lighter.xyz/llms.txt", "added": ["- [Choose your integration](u)"]}, key="b")
        desc = J.event("S2", "doc_keyword", "ALERT", "rh", ts=NOW,
                       ref={"url": "https://docs.lighter.xyz/programs/lighter-on-robinhood-chain-points.md",
                            "added": ["The live points are distributed in real time based on volume."],
                            "words": ["distributed"]}, key="d")
        real = J.event("S2", "doc_keyword", "ALERT", "x", ts=NOW,
                       ref={"url": "u", "added": ["Points convert to LIT at 4.2 LIT per point."]}, key="c")
        evs = [moved, index, desc, real]
        self.assertEqual(J.rejudge(evs, {old_rh: {"to": "new", "same": True}}, NOW + 60, ["LIT"]), 3)
        self.assertFalse(real["refuted"])
        self.assertIn("문서 주소를 옮겼어요(내용은 그대로)", moved["refute_reason"])
        self.assertIn("'integration' 속 'ratio'", index["refute_reason"])
        self.assertIn("'distributed'가 토큰·에어드랍 언급 없이", desc["refute_reason"])


class Posts(unittest.TestCase):
    def items(self, *titles):
        return [{"title": t, "link": f"https://t.me/demo/{i}", "ts": NOW} for i, t in enumerate(titles)]

    def test_grades(self):
        ev = J.detect_posts(self.items(
            "StandX airdrop checker is live now!",
            "XTEN TGE is set for Nov 12. Claims open the same day.",
            "RT by @x: TGE tomorrow",
            "R to @someone: TGE is coming soon",
            "Season 2 points are distributed every Friday",
            "New listing: $TSM perps with 10x leverage",
        ), [], NOW, "telegram", "Demo", ["XTEN"])
        got = {e["ref"]["text"].split(".")[0][:20]: e["grade"] for e in ev}
        self.assertEqual(got, {"StandX airdrop check": "ALERT", "XTEN TGE is set for ": "IMMINENT",
                               "R to @someone: TGE i": "WATCH"})
        self.assertTrue(all(e["signal"] == "telegram_post" for e in ev))

    def test_season_end_post_is_alert(self):
        ev = J.detect_posts(self.items("Season 1 has ended — thank you, traders"), [], NOW, "x", "Demo")
        self.assertEqual(ev[0]["grade"], "ALERT")

    def test_token_live_venue_ignores_listing_and_tge_words(self):
        ev = J.detect_posts(self.items("LIT listing on Upbit is live", "Happy TGE anniversary!",
                                       "RH points airdrop claim opens Nov 3"), [], NOW, "x", "Lighter RH",
                            ["LIT"], token_live=True)
        self.assertEqual([e["grade"] for e in ev], ["WATCH", "WATCH", "IMMINENT"])  # 상장·TGE 언급은 관찰까지만

    def test_announcement_json_keys_match_old_records(self):
        rows = {"announcements": [{"title": "Points Program Update ", "content": "Final weekly drop on Friday",
                                   "created_at": 1792779495}]}
        with mock.patch.object(S, "get_json", return_value=rows):
            items = S.json_posts(LRH["ann"])
        self.assertEqual(items[0]["key"], "1792779495:Points Program Update")
        self.assertEqual(J.detect_posts(items, [items[0]["key"]], NOW, "ann", "Lighter RH"), [])
        self.assertEqual(J.detect_posts(items, [], NOW, "ann", "Lighter RH")[0]["grade"], "ALERT")


class ApiAndApp(unittest.TestCase):
    META = {"season": {"name": "Season 1", "ends_at": None, "points_visible": True},
            "seasons": [{"name": "Season 0", "ends_at": "2026-09-30", "points_visible": False},
                        {"name": "Season 1", "ends_at": None, "points_visible": True}],
            "latest_week": {"week_start": "2026-10-21"}, "next_drop_at": "2026-10-28T19:00:00.000Z",
            "season_total_points": 750000}
    IGN = frozenset(VENUES["arcus"]["api"]["ignore"])

    def flat(self, d):
        return S.flatten(d, ignore=self.IGN)

    def test_volatile_ignored(self):
        new = copy.deepcopy(self.META)
        new["season_total_points"] = 1_000_000
        new["next_drop_at"] = "2026-11-04T20:00:00.000Z"
        self.assertEqual(J.detect_api(self.flat(self.META), self.flat(new), NOW, VENUES["arcus"]["api"], "Arcus"), [])

    def test_season_end_and_reveal_are_alerts(self):
        new = copy.deepcopy(self.META)
        new["season"]["ends_at"] = "2026-12-31T16:00:00.000Z"
        ev = J.detect_api(self.flat(self.META), self.flat(new), NOW, VENUES["arcus"]["api"], "Arcus")
        self.assertEqual(ev[0]["grade"], "ALERT")
        self.assertIn("season.ends_at", ev[0]["title"])
        new2 = copy.deepcopy(self.META)
        new2["seasons"][0]["points_visible"] = True
        self.assertEqual(J.detect_api(self.flat(self.META), self.flat(new2), NOW, VENUES["arcus"]["api"], "Arcus")[0]["grade"],
                         "ALERT")

    def test_new_season_entry_is_watch(self):
        new = copy.deepcopy(self.META)
        new["seasons"].append({"name": "Season 2", "ends_at": None, "points_visible": False})
        self.assertEqual(J.detect_api(self.flat(self.META), self.flat(new), NOW, VENUES["arcus"]["api"], "Arcus")[0]["grade"],
                         "WATCH")

    def test_drop_delay(self):
        prev = {"next": NOW - 30 * 3600, "week": "2026-10-21"}
        self.assertEqual(J.detect_drop_delay(prev, {"next": NOW + 6 * 86400, "week": "2026-10-21"}, NOW,
                                             {"grace_hours": 24}, "Arcus")[0]["signal"], "drop_late")
        self.assertEqual(J.detect_drop_delay(prev, {"week": "2026-10-28"}, NOW, {"grace_hours": 24}, "Arcus"), [])
        self.assertEqual(J.detect_drop_delay({"next": NOW - 3600, "week": "w"}, {"week": "w"}, NOW, {}, "Arcus"), [])

    def test_js_claim_path(self):
        cfg = VENUES["standx"]["js"]
        ev = J.detect_jsscan({"claimable": ["404"]}, {"claimable": ["/v1/airdrop/claimable"]}, NOW, cfg, "StandX")
        self.assertEqual((ev[0]["grade"], ev[0]["title"]), ("ALERT", "StandX 앱 코드: 에어드랍 확인 API 경로 바뀜"))
        self.assertEqual(J.detect_jsscan({"claimable": ["404"]}, {"claimable": []}, NOW, cfg, "StandX")[0]["grade"], "WATCH")
        self.assertEqual(J.detect_jsscan({"claimable": ["404"]}, {"claimable": ["404"]}, NOW, cfg, "StandX"), [])

    def test_js_scan_follows_chunks(self):
        pages = {"https://app.test/": b'<script src="/_next/static/chunks/main-1.js"></script>',
                 "https://app.test/_next/static/chunks/main-1.js": b'import("/_next/static/chunks/air-2.js")',
                 "https://app.test/_next/static/chunks/air-2.js": b'getAirdropClaimable(){return this.post("404",{})}'}
        with mock.patch.object(S, "http", side_effect=lambda u, **k: pages[u]):
            got = S.js_scan("https://app.test/", VENUES["standx"]["js"]["patterns"])
        self.assertEqual(got, {"claimable": ["404"]})


class Odds(unittest.TestCase):
    TITLES = {"a": "Will Variational launch a token by November 30, 2026?",
              "b": "Will Variational launch a token by December 31, 2026?",
              "c": "Variational FDV above $1B one day after launch?"}

    def test_line_and_jump(self):
        odds = {"a": [[NOW - 86400, 33.0], [NOW, 61.0]], "b": [[NOW, 78.1]], "c": [[NOW, 58.5]]}
        self.assertEqual(J.odds_line(odds, self.TITLES, now=NOW), "토큰 출시 확률 11/30까지 61% · 12/31까지 78%")
        ev = J.detect_odds(odds, self.TITLES, NOW, "Variational")
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["grade"], "WATCH")
        self.assertIn("33% → 61%", ev[0]["title"])

    def test_next_year_label(self):
        t = {"q": "Will QFEX launch a token by March 31, 2027?"}
        self.assertEqual(J.odds_line({"q": [[NOW, 21.0]]}, t, now=NOW), "토큰 출시 확률 27년 3/31까지 21%")


class S1(unittest.TestCase):
    def test_pool_band_single(self):
        ev = J.detect_eth([tr(A, B, 10_800_000)], {}, OC, NOW, [])
        self.assertEqual([(e["signal"], e["grade"]) for e in ev], [("eth_pool_band", "ALERT")])

    def test_fresh_contract(self):
        metas = {B: {"is_contract": True, "created_ts": NOW - 3 * 86400}}
        ev = J.detect_eth([tr(A, B, 2_000_000)], metas, OC, NOW, [])
        self.assertEqual((ev[0]["signal"], ev[0]["grade"]), ("eth_fresh_contract", "ALERT"))

    def test_old_contract_is_watch(self):
        metas = {B: {"is_contract": True, "created_ts": NOW - 90 * 86400}}
        self.assertEqual(J.detect_eth([tr(A, B, 2_000_000)], metas, OC, NOW, [])[0]["grade"], "WATCH")

    def test_excluded(self):
        self.assertEqual(J.detect_eth([tr(A, B, 10_500_000)], {B: {"is_contract": False, "name": "Binance 14"}}, OC, NOW, []), [])
        self.assertEqual(J.detect_eth([tr("0x3b4d794a66304f130a4db8f2551b0070dfcf5ca7", A, 5e6)], {}, OC, NOW, []), [])
        self.assertEqual(J.detect_eth([tr(A, "0x000000000000000000000000000000000000dead", 11e6)], {}, OC, NOW, []), [])

    def test_cumulative_band(self):
        hist = [tr(A, B, 4e6, NOW - 86400, "0x1"), tr(A, C, 4e6, NOW - 3600, "0x2"), tr(A, B, 3e6, NOW, "0x3")]
        self.assertIn("eth_pool_cumulative", [e["signal"] for e in J.detect_eth([hist[-1]], {}, OC, NOW, hist)])

    def test_refute_reverse(self):
        ev = J.detect_eth([tr(A, B, 10_800_000)], {}, OC, NOW, [])
        self.assertEqual(J.apply_refutes(ev, [tr(B, A, 10_000_000, NOW + 3600, "0x9")], OC, NOW + 3600), 1)
        self.assertEqual(ev[0]["refute_kind"], "data")
        self.assertEqual(J.level_of(J.active(ev, NOW + 3600, 7))[0], "NONE")
        ev2 = J.detect_eth([tr(A, B, 10_800_000)], {}, OC, NOW, [])
        self.assertEqual(J.apply_refutes(ev2, [tr(B, A, 11e6, NOW + 80 * 3600, "0x9")], OC), 0)

    def test_rh_supply(self):
        ev = J.detect_rh_supply([[NOW - 86400 * 2, 396_441], [NOW, 396_441 + 1_200_000]], OC, NOW)
        self.assertEqual(ev[0]["grade"], "ALERT")
        self.assertIn("대량", ev[0]["title"])
        self.assertIn("풀 규모", J.detect_rh_supply([[NOW - 86400, 396_441], [NOW, 11_500_000]], OC, NOW)[0]["title"])
        self.assertEqual(J.detect_rh_supply([[NOW - 1, 1e5], [NOW, 5e5]], OC, NOW), [])
        self.assertEqual(J.detect_rh_supply([[NOW - 10 * 86400, 0], [NOW - 86400, 5e6], [NOW, 5.5e6]], OC, NOW), [])


class Board(unittest.TestCase):
    B = {"top": 10, "lb_drop_pct": 3, "lb_freeze_hours": 48}

    def test_slash_frozen_slowdown(self):
        self.assertIn("lb_slash", [e["signal"] for e in J.detect_leaderboard(
            [[NOW - 7200, 132_900, 1], [NOW, 120_000, 1]], self.B, NOW)])
        h = [[NOW - 60 * 3600 + i * 7200, 150_000, 1] for i in range(31)]
        h[-1][0] = NOW
        self.assertIn("lb_frozen", [e["signal"] for e in J.detect_leaderboard(h, self.B, NOW)])
        g = [[NOW - 60 * 3600 + i * 7200, 100_000 + i * 10, 1] for i in range(31)]
        self.assertNotIn("lb_frozen", [e["signal"] for e in J.detect_leaderboard(g, self.B, NOW)])
        d0 = NOW - 28 * 86400
        s = [[d0 + i * 86400, 100_000 + (i * 1000 if i <= 21 else 21_000 + (i - 21) * 300), 1] for i in range(29)]
        s[-1][0] = NOW
        self.assertIn("lb_slowdown", [e["signal"] for e in J.detect_leaderboard(s, self.B, NOW)])

    def test_frontend_titles(self):
        old = {"chunks": ["PointsLeaderboard"], "api_paths": ["/api/v1/airdrop"]}
        new = {"chunks": ["PointsLeaderboard", "ClaimRewards", "StockNews"], "api_paths": ["/api/v1/airdrop", "/api/v1/rhClaim"]}
        ev = {e["title"]: e["grade"] for e in J.detect_frontend(old, new, NOW)}
        self.assertEqual(ev, {"앱에 새 화면 모듈: ClaimRewards": "ALERT", "앱에 새 화면 모듈: StockNews": "WATCH",
                              "앱에 새 API 경로: /api/v1/rhClaim": "ALERT"})


class Level(unittest.TestCase):
    def e(self, sig, g, fam="S2"):
        return J.event(fam, sig, g, f"{sig}{g}", ts=NOW, key=f"{sig}{g}")

    def test_groups(self):
        self.assertEqual(J.level_of([])[0], "NONE")
        self.assertEqual(J.level_of([self.e("doc_change", "WATCH")])[0], "WATCH")
        self.assertEqual(J.level_of([self.e("doc_keyword", "ALERT")]), ("ALERT", ["docs"]))
        self.assertEqual(J.level_of([self.e("doc_keyword", "ALERT"), self.e("index_keyword", "ALERT")])[0], "ALERT")
        self.assertEqual(J.level_of([self.e("doc_keyword", "ALERT"), self.e("telegram_post", "ALERT")]),
                         ("IMMINENT", ["docs", "post"]))
        self.assertEqual(J.level_of([self.e("rh_supply", "ALERT", "S1"), self.e("news", "ALERT", "S4")])[0], "ALERT")
        self.assertEqual(J.level_of([self.e("x_post", "IMMINENT")])[0], "IMMINENT")

    def test_window(self):
        self.assertEqual(J.active([J.event("S1", "x", "ALERT", "t", ts=NOW - 8 * 86400)], NOW, 7), [])


class Render(unittest.TestCase):
    URL = "https://docs.lighter.xyz/programs/lighter-on-robinhood-chain-points.md"

    def doc_alert(self):
        old = "# Lighter on Robinhood Chain Points\n\nPoints are calculated in real time.\n"
        new = old + "\nPoints will convert to LIT at a ratio of 4.2 LIT per point after the final weekly drop.\n"
        return J.detect_doc(self.URL, old, new, NOW, J.doc_name(self.URL, NAMES), ["LIT"])

    def test_alert_reads_on_its_own(self):
        txt = J.render_venue_alert(LRH, "ALERT", ["docs"], self.doc_alert(), NOW, CFG)
        self.assertTrue(txt.startswith("🟡 <b>퍼프덱스 레이더 · Lighter RH 경보</b>"))
        self.assertIn("10/12(월) 09:17 감지 · 공식 문서", txt)
        self.assertIn("알려진 상태: LIT 출시됨", txt)
        self.assertIn("공식 문서 '로빈후드 체인 포인트 안내'에 전환·TGE 관련 문구 추가", txt)
        self.assertIn("after the final weekly drop.", txt)
        for part in ("<b>무슨 일</b>", "<b>의미</b>", "<b>해제 조건</b>", "'임박'으로 올려요", "잡힌 단어: "):
            self.assertIn(part, txt)
        self.assertIn('<a href="https://docs.lighter.xyz/programs/lighter-on-robinhood-chain-points">원문</a>', txt)
        self.assertNotIn("…", txt)

    def test_post_alert_quotes_the_post(self):
        ev = J.detect_posts([{"title": "XTEN TGE is set for Nov 12 <b>", "link": "https://t.me/extended_updates/400"}],
                            [], NOW, "telegram", "Extended", ["XTEN"])
        txt = J.render_venue_alert(VENUES["extended"], "IMMINENT", ["post"], ev, NOW, CFG)
        self.assertTrue(txt.startswith("🔴 <b>퍼프덱스 레이더 · Extended 임박</b>"))
        self.assertIn("Extended 공식 텔레그램에 새 글", txt)
        self.assertIn("“XTEN TGE is set for Nov 12 &lt;b&gt;.”", txt)
        self.assertIn("TGE·청구 일정(날짜)이 함께", txt)
        self.assertIn('<a href="https://t.me/extended_updates/400">원문</a>', txt)

    def test_multi_venue_message(self):
        a = J.render_venue_alert(LRH, "ALERT", ["docs"], self.doc_alert(), NOW, CFG, head=False)
        ev = J.detect_posts([{"title": "StandX airdrop checker is live", "link": "https://x.com/s/1"}], [], NOW, "x", "StandX")
        b = J.render_venue_alert(VENUES["standx"], "ALERT", ["post"], ev, NOW, CFG, head=False)
        txt = J.render_alert([("ALERT", a), ("ALERT", b)], CFG)
        self.assertTrue(txt.startswith("🟡 <b>퍼프덱스 레이더 · 경보 2곳</b>"))
        self.assertEqual(txt.count("대시보드</a>"), 1)
        self.assertIn("<b>━ StandX · 경보</b>", txt)

    def test_release_sections(self):
        e = J.event("S2", "doc_keyword", "ALERT", "x", ts=NOW,
                    ref={"url": "https://docs.lighter.xyz/llms.txt", "added": ["- [Choose your integration](u)"]})
        J.rejudge([e], {}, NOW + 3600)
        sec = J.render_release_section(LRH, [e], "WATCH", "ALERT", NOW)
        self.assertTrue(sec.startswith("<b>Lighter RH · 정정</b>"))
        self.assertIn("10/12(월) 09:17 '경보' 알림은 오탐이었어요.", sec)
        self.assertIn("· 문서 전체 목록 — 'integration' 속 'ratio'처럼", sec)
        self.assertTrue(J.render_release([sec], CFG, True).startswith("<b>퍼프덱스 레이더 · 정정</b>"))
        ev = J.detect_eth([tr(A, B, 10_800_000)], {}, OC, NOW, [])
        J.apply_refutes(ev, [tr(B, A, 10_000_000, NOW + 3600, "0x9")], OC, NOW + 3600)
        self.assertIn("· 해제</b>", J.render_release_section(LRH, ev, "NONE", "ALERT", NOW))

    def test_kickoff_lists_every_venue(self):
        sums = [{"id": v["id"], "name": v["name"], "level": "NONE", "status_note": v["status_note"], "sources_ok": 2,
                 "sources_all": 2, "odds": ""} for v in CFG["venues"]]
        txt = J.render_kickoff(sums, NOW, CFG, ["<b>Lighter RH · 정정</b>\n…"], renamed=True)
        self.assertIn("라이터RH 전환감시를 '퍼프덱스 레이더'로 바꾸고 7곳의", txt)
        for v in CFG["venues"]:
            self.assertIn(f"· <b>{v['name']}</b> — 조용함 · 수집 2/2 정상", txt)
        self.assertIn("청구 11/1 09:00 ~ 12/31 17:00", txt)
        self.assertIn("<b>Lighter RH · 정정</b>", txt)

    def test_html_escaped(self):
        e = J.event("S2", "announcement", "ALERT", "앱 공지: <Season 3> & claims", "x < y", "r", NOW)
        txt = J.render_venue_alert(LRH, "ALERT", ["post"], [e], NOW, CFG)
        self.assertIn("&lt;Season 3&gt; &amp; claims", txt)
        self.assertNotIn("<Season", txt)


D = "https://docs.lighter.xyz/"
DEMO = "https://docs.demo.test/"


def test_cfg():
    lrh = copy.deepcopy(LRH)
    lrh.update({"ann": {"url": "https://api.test/ann"}, "board": {"url": "https://api.test/lb", "top": 10},
                "frontend": "https://app.test", "x": ["Lighter_xyz"], "news": {"q": "Lighter", "match": "lighter"}})
    lrh.pop("polymarket", None)
    demo = {"id": "demo", "name": "Demo", "tickers": ["DMO"], "token_live": False, "status_note": "토큰 없음",
            "docs": {"index": DEMO + "llms.txt", "pages": [{"url": DEMO + "faq/token.md", "name": "토큰 FAQ", "any": True}],
                     "names": {"llms.txt": "문서 전체 목록"}},
            "telegram": "demo",
            "api": {"url": "https://api.demo.test/meta", "ignore": ["latest_week", "next_drop_at"],
                    "drop": {"next": "next_drop_at", "week": "latest_week", "grace_hours": 24}},
            "js": {"url": "https://demo.test/", "patterns": {"claimable": "x"}, "names": {"claimable": "에어드랍 확인 API 경로"}},
            "polymarket": {"q": "demo", "match": "demo"}}
    out = {k: v for k, v in CFG.items() if k != "venues"}
    out["venues"] = [lrh, demo]
    return out


class Integration(unittest.TestCase):
    """sources를 가짜로 바꿔 radar.main 전체 흐름(시딩 → 범위 안내 → 경보 → 야간 보류 → 정정)을 검증."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        shutil.copytree(ROOT, os.path.join(self.tmp, "perp_dex_radar"),
                        ignore=shutil.ignore_patterns("data", "__pycache__", "account_probe"))
        self.root = os.path.join(self.tmp, "perp_dex_radar")
        write_json(os.path.join(self.root, "radar_config.json"), test_cfg())
        sys.modules.pop("radar", None)
        sys.path.insert(0, self.root)
        import radar  # noqa
        self.R = radar
        self.supply = 396_441.0
        self.docs = "# Points\n\nreal time\n"
        self.demo_docs = "# Token\n\nNo token yet.\n"
        self.pages = None
        self.fetched: list[str] = []
        self.lb_sum = 100_000.0
        self.ann: list[dict] = []
        self.tg = [{"title": "Week 1 points are out", "link": "https://t.me/demo/1", "ts": NOW}]
        self.api = {"season": {"ends_at": None}, "latest_week": "w1", "next_drop_at": "2027-01-01T00:00:00Z"}
        self.js = {"claimable": ["404"]}
        self.pm = [{"slug": "will-demo-launch-a-token-by-december-31-2026", "yes_pct": 20.0, "volume": 1000.0,
                    "title": "Will Demo launch a token by December 31, 2026?"}]
        self.fail_all = False

    def tearDown(self):
        sys.path.remove(self.root)
        sys.modules.pop("radar", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def fetch(self, url):
        self.fetched.append(url)
        if self.fail_all:
            raise RuntimeError("down")
        if self.pages is not None and url.startswith(D):
            return self.pages.get(url, "# Page Not Found\n\nThe URL does not exist.\n\n### Option 1 — Ask a question (recommended)\n")
        return self.demo_docs if url.startswith(DEMO) else self.docs

    def run_at(self, now, expect_rc=0):
        S_ = self.R.S

        def boom(*a, **k):
            raise RuntimeError("down")
        f = (lambda fn: boom if self.fail_all else fn)
        patches = [
            mock.patch.object(S_, "block_number", side_effect=f(lambda u: 1000 + (now // 12) % 10_000_000)),
            mock.patch.object(S_, "transfer_logs", return_value=[]),
            mock.patch.object(S_, "total_supply", side_effect=f(lambda u, t: self.supply)),
            mock.patch.object(S_, "fetch_doc", side_effect=self.fetch),
            mock.patch.object(S_, "json_posts", side_effect=f(lambda spec: list(self.ann))),
            mock.patch.object(S_, "leaderboard_json", side_effect=f(lambda *a, **k: [self.lb_sum])),
            mock.patch.object(S_, "frontend_surface", side_effect=f(lambda u: {
                "chunks": [f"m{i}" for i in range(20)], "api_paths": ["/api/v1/airdrop"]})),
            mock.patch.object(S_, "x_feed", side_effect=RuntimeError("x")),
            mock.patch.object(S_, "telegram_posts", side_effect=f(lambda ch: list(self.tg))),
            mock.patch.object(S_, "get_json", side_effect=f(lambda url, **k: json.loads(json.dumps(self.api)))),
            mock.patch.object(S_, "js_scan", side_effect=f(lambda url, pats, mx=60: {k: list(v) for k, v in self.js.items()})),
            mock.patch.object(S_, "google_news", return_value=[]),
            mock.patch.object(S_, "polymarket", side_effect=f(lambda q: copy.deepcopy(self.pm))),
        ]
        for p in patches:
            p.start()
        try:
            with mock.patch.dict(os.environ, {"RADAR_NOW": str(now)}):
                rc = self.R.main([])
        finally:
            for p in patches:
                p.stop()
        self.assertEqual(rc, expect_rc)
        return read_json(os.path.join(self.root, "data", "latest.json"))

    def state(self):
        return read_json(os.path.join(self.root, "data", "state.json"))

    def test_flow(self):
        t0 = ts(2026, 10, 12, 9, 17)
        l0 = self.run_at(t0)  # 처음 — 기준선 + 감시 범위 안내 한 번
        self.assertEqual(l0["outbox"]["kind"], "scope")
        self.assertIn("· <b>Demo</b> — 조용함", l0["outbox"]["text"])
        self.assertIn("토큰 출시 확률 12/31까지 20%", l0["outbox"]["text"])
        self.lb_sum += 500
        l1 = self.run_at(t0 + 7200)
        self.assertEqual(l1["outbox"]["id"], l0["outbox"]["id"])  # 같은 안내를 다시 만들지 않는다
        self.assertEqual(l1["levels"], {"lighter-rh": "NONE", "demo": "NONE"})
        # 밤 23:17 — 공급 급증 → 경보지만 outbox 없음
        self.supply += 10_900_000
        self.lb_sum += 500
        l2 = self.run_at(ts(2026, 10, 12, 23, 17))
        self.assertEqual(l2["levels"]["lighter-rh"], "ALERT")
        self.assertEqual(l2["outbox"]["kind"], "scope")
        # 다음날 07:17 — 밤에 잡은 것을 보낸다(감지 시각 그대로)
        self.lb_sum += 500
        l3 = self.run_at(ts(2026, 10, 13, 7, 17))
        self.assertEqual(l3["outbox"]["kind"], "alert")
        self.assertIn("Lighter RH 경보", l3["outbox"]["text"])
        self.assertIn("로빈후드 체인 LIT 공급", l3["outbox"]["text"])
        self.assertIn("10/12(월) 23:17 감지", l3["outbox"]["text"])
        first_id = l3["outbox"]["id"]
        self.lb_sum += 500
        l4 = self.run_at(ts(2026, 10, 13, 9, 17))
        self.assertEqual(l4["outbox"]["id"], first_id)  # 같은 등급 — 새 메시지 없음
        # 문서에 전환 비율 → 근거 2가지(온체인·문서) → 임박
        self.docs += "\nPoints convert to LIT at 4.2 LIT per point.\n"
        self.lb_sum += 500
        l5 = self.run_at(ts(2026, 10, 13, 11, 17))
        self.assertEqual(l5["levels"]["lighter-rh"], "IMMINENT")
        self.assertNotEqual(l5["outbox"]["id"], first_id)
        self.assertTrue(l5["outbox"]["text"].startswith("🔴"))

    def test_other_venue_alerts(self):
        t0 = ts(2026, 10, 12, 9, 17)
        self.run_at(t0)
        self.tg.append({"title": "Demo TGE is set for Nov 12. Airdrop checker is live.", "link": "https://t.me/demo/2", "ts": t0})
        self.js = {"claimable": ["/v1/airdrop/claimable"]}
        self.lb_sum += 1
        l1 = self.run_at(t0 + 7200)
        self.assertEqual(l1["levels"]["demo"], "IMMINENT")
        txt = l1["outbox"]["text"]
        self.assertTrue(txt.startswith("🔴 <b>퍼프덱스 레이더 · Demo 임박</b>"))
        self.assertIn("Demo 공식 텔레그램에 새 글", txt)
        self.assertIn("Demo 앱 코드: 에어드랍 확인 API 경로 바뀜", txt)
        self.assertIn("알려진 상태: 토큰 없음", txt)

    def test_api_drop_late_and_resume(self):
        t0 = ts(2026, 10, 12, 9, 17)
        self.api["next_drop_at"] = "2026-10-12T19:00:00Z"
        self.run_at(t0)
        self.lb_sum += 1
        l1 = self.run_at(ts(2026, 10, 14, 9, 17))  # 예정 + 38시간
        self.assertEqual(l1["levels"]["demo"], "ALERT")
        self.assertIn("주간 포인트 배분이", l1["outbox"]["text"])
        self.api["latest_week"] = "w2"
        self.lb_sum += 1
        l2 = self.run_at(ts(2026, 10, 14, 11, 17))
        self.assertEqual(l2["outbox"]["kind"], "release")
        self.assertIn("Demo · 해제", l2["outbox"]["text"])
        self.assertIn("다음 주 포인트 배분이 들어왔어요", l2["outbox"]["text"])

    def test_migration_from_lit_rh_watch(self):
        """옛 라이터RH 전환감시 상태 — 기준선·공지 기록을 잇고, 오탐 경보는 정정과 함께 범위 안내로."""
        t0 = ts(2026, 10, 24, 8, 10)
        self.ann = [{"title": "Network Upgrade", "content": "Points Program maintenance window", "ts": 1,
                     "key": "1791310722:Network Upgrade"}]
        e = J.event("S2", "doc_keyword", "ALERT", "공식 문서 '로빈후드 체인 포인트 안내'에 전환 관련 문구 추가", ts=t0 - 4 * 3600,
                    ref={"url": D + "programs/lighter-on-robinhood-chain-points.md", "name": "로빈후드 체인 포인트 안내",
                         "added": ["The live points are distributed in real time based on volume."], "words": ["distributed"]},
                    key="old")
        v1 = {"version": 1, "seeded": True, "cursors": {"eth": 900, "rh": 900}, "rh_supply_hist": [[t0 - 3600, self.supply]],
              "lb_hist": [[t0 - 3600, self.lb_sum, 1]], "docs": {}, "doc_moves": {}, "ann_seen": ["1791310722:Network Upgrade"],
              "frontend": {"chunks": [f"m{i}" for i in range(20)], "api_paths": ["/api/v1/airdrop"]},
              "news_seen": [], "pm_seen": ["lighter-airdrop"], "large_hist": [], "addr_cache": {}, "events": [e],
              "level": "ALERT", "fams": ["S2"], "notified": "ALERT", "notified_ids": [e["id"]], "notified_at": t0,
              "calm_since": None, "outbox": {"id": "old", "text": "x", "created": t0, "kind": "alert", "level": "ALERT"},
              "heartbeat": "2026-10", "fail_streak": 0, "status": {}}
        os.makedirs(os.path.join(self.root, "data"), exist_ok=True)
        write_json(os.path.join(self.root, "data", "state.json"), v1)
        self.lb_sum += 10
        l = self.run_at(ts(2026, 10, 24, 10, 0))
        self.assertEqual(l["outbox"]["kind"], "scope")
        txt = l["outbox"]["text"]
        self.assertIn("라이터RH 전환감시를 '퍼프덱스 레이더'로 바꾸고 2곳의", txt)
        self.assertIn("<b>Lighter RH · 정정</b>", txt)
        self.assertIn("10/24(토) 08:10 '경보' 알림은 오탐이었어요", txt)
        self.assertIn("'distributed'가 토큰·에어드랍 언급 없이", txt)
        st = self.state()
        self.assertEqual(st["version"], 2)
        self.assertEqual(st["venues"]["lighter-rh"]["notified"], "NONE")
        self.assertNotIn("announcement", [x["signal"] for x in st["venues"]["lighter-rh"]["events"]])
        self.assertNotIn("ann_post", [x["signal"] for x in st["venues"]["lighter-rh"]["events"]])  # 옛 공지는 다시 안 알림
        self.assertEqual(st["venues"]["lighter-rh"]["cursors"]["eth"], 1000 + (ts(2026, 10, 24, 10, 0) // 12) % 10_000_000 - 3)

    def test_doc_move_is_followed_quietly(self):
        rh_path = "programs/lighter-on-robinhood-chain-points"

        def site(path):
            return {D + "llms.txt": f"# Lighter Docs\n- [Lighter on Robinhood Chain Points]({D}{path}.md)\n- [Programs]({D}programs/readme.md)\n",
                    D + path + ".md": "# Lighter on Robinhood Chain Points\n\nWeekly drop every Friday.\n",
                    D + "programs/readme.md": "# Programs\n\nSeveral programs.\n",
                    D + "about-lighter/lit-utility.md": "# LIT Utility\n\nBuybacks.\n"}
        self.pages = site(rh_path)
        t0 = ts(2026, 10, 12, 9, 17)
        self.run_at(t0)
        self.pages = site("rewards/lighter-on-robinhood-chain-points")
        self.lb_sum += 1
        l1 = self.run_at(t0 + 7200)
        self.assertEqual(l1["outbox"]["kind"], "scope")  # 경보 없음 — 처음 안내 그대로
        vs = self.state()["venues"]["lighter-rh"]
        old, new = D + rh_path + ".md", D + "rewards/lighter-on-robinhood-chain-points.md"
        self.assertEqual(vs["doc_moves"][old]["to"], new)
        self.assertTrue(vs["doc_moves"][old]["same"])
        self.assertEqual(vs["docs"][old]["url"], new)
        sig = {x["signal"]: x for x in vs["events"]}
        self.assertEqual(sig["doc_moved"]["grade"], "WATCH")
        self.assertNotIn("doc_keyword", sig)
        self.fetched.clear()
        self.pages[new] += "\nPoints will convert to LIT at 4.2 LIT per point.\n"
        self.lb_sum += 1
        l2 = self.run_at(t0 + 14400)
        self.assertNotIn(old, self.fetched)
        self.assertEqual(l2["outbox"]["kind"], "alert")
        self.assertIn("rewards/lighter-on-robinhood-chain-points", l2["outbox"]["text"])

    def test_release_after_false_positive(self):
        t0 = ts(2026, 10, 12, 9, 17)
        self.run_at(t0)
        p = os.path.join(self.root, "data", "state.json")
        st = read_json(p)
        e = J.event("S2", "doc_keyword", "ALERT", "문서 변경: llms.txt", ts=t0 + 600,
                    ref={"url": D + "llms.txt", "added": ["- [Choose your integration](u)"]})
        st["venues"]["lighter-rh"]["events"].append(e)
        st["venues"]["lighter-rh"].update({"notified": "ALERT", "notified_ids": [e["id"]], "notified_at": t0 + 900})
        write_json(p, st)
        self.lb_sum += 1
        l = self.run_at(t0 + 3600)
        self.assertEqual(l["outbox"]["kind"], "release")
        self.assertTrue(l["outbox"]["text"].startswith("<b>퍼프덱스 레이더 · 정정</b>"))
        self.assertIn("'integration' 속 'ratio'", l["outbox"]["text"])
        self.lb_sum += 1
        l2 = self.run_at(t0 + 7200)
        self.assertEqual(l2["outbox"]["id"], l["outbox"]["id"])
        self.assertEqual(self.state()["venues"]["lighter-rh"]["notified"], "NONE")

    def test_expired_alert_does_not_block_the_next(self):
        t0 = ts(2026, 10, 12, 9, 17)
        self.run_at(t0)
        self.docs += "\nPoints convert to LIT at 4.2 LIT per point.\n"
        self.lb_sum += 1
        l1 = self.run_at(t0 + 7200)
        self.assertEqual(l1["outbox"]["kind"], "alert")
        for d in range(2, 10, 2):
            self.docs += f"\nPlain note {d}.\n"
            self.lb_sum += 1
            lw = self.run_at(t0 + d * 86400)
        self.assertEqual(lw["levels"]["lighter-rh"], "WATCH")
        self.docs += "\nClaim opens on Nov 3.\n"
        self.lb_sum += 1
        l2 = self.run_at(t0 + 10 * 86400)
        self.assertEqual(l2["outbox"]["kind"], "alert")
        self.assertIn("Lighter RH 임박", l2["outbox"]["text"])  # 청구 날짜가 나오면 바로 임박
        self.assertIn("Claim opens on Nov 3", l2["outbox"]["text"])
        self.assertNotEqual(l2["outbox"]["id"], l1["outbox"]["id"])

    def test_new_venue_is_seeded_and_announced(self):
        t0 = ts(2026, 10, 12, 9, 17)
        cfg = test_cfg()
        only = {**cfg, "venues": cfg["venues"][:1]}
        write_json(os.path.join(self.root, "radar_config.json"), only)
        self.run_at(t0)
        write_json(os.path.join(self.root, "radar_config.json"), cfg)
        self.tg.append({"title": "Demo TGE is set for Nov 12", "link": "https://t.me/demo/9", "ts": t0})  # 첫 수집 — 기준선
        self.lb_sum += 1
        l = self.run_at(t0 + 2 * 86400)
        self.assertEqual(l["outbox"]["kind"], "scope")
        self.assertIn("추가: Demo", l["outbox"]["text"])
        self.assertEqual(l["levels"]["demo"], "NONE")

    def test_undecidable_exit(self):
        t0 = ts(2026, 10, 12, 9, 17)
        self.run_at(t0)
        self.fail_all = True
        self.run_at(t0 + 7200, expect_rc=0)   # 1회는 경고만
        self.run_at(t0 + 14400, expect_rc=1)  # 2회 연속부터 실패

    def test_heartbeat_first_week(self):
        self.run_at(ts(2026, 10, 30, 9, 17))  # 시딩 + 범위 안내
        self.lb_sum += 1
        l = self.run_at(ts(2026, 11, 2, 9, 17))
        self.assertEqual(l["outbox"]["kind"], "heartbeat")
        self.assertIn("퍼프덱스 레이더 · 월간 점검", l["outbox"]["text"])
        self.lb_sum += 1
        l2 = self.run_at(ts(2026, 11, 2, 11, 17))
        self.assertEqual(l2["outbox"]["id"], l["outbox"]["id"])


if __name__ == "__main__":
    unittest.main()
