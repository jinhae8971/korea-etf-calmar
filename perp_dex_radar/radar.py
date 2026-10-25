#!/usr/bin/env python3
"""perp-dex-radar — 퍼프 DEX 여러 곳의 포인트 → 토큰 전환(TGE·에어드랍) 신호 감시기 (수집·판정·대시보드 데이터).

2026-10-24 '라이터RH 전환감시'(lit-rh-watch)를 이름을 바꾸고 거래소 7곳으로 넓혔다. 같은 날 TxFlow·HelloTrade·TitanX를 더해 10곳.

실행: python radar.py [--seed] [--dry] [--summary]
  --seed    : 기준선만 갱신하고 이벤트를 만들지 않는다(새 거래소·새 원천은 첫 실행에 자동 적용).
  --dry     : 파일을 쓰지 않는다.
  --summary : 다른 메시지가 없으면 '현황' 메시지를 만든다(낮에만). 환경변수 RADAR_SUMMARY=1, 첫 줄 문구 RADAR_SUMMARY_NOTE.
현황 요청 파일 request.json {"summary": {"id", "at": "YYYY-MM-DD HH:MM"(KST), "note"}} — push하면 그 실행(못 하면 다음 낮
실행)에서 한 번 만든다. 24시간 지난 요청은 버린다. (Actions 수동 실행 권한이 없는 곳에서도 요청할 수 있게)

출력
  data/state.json                  내부 상태(거래소별 커서·기준선·이벤트)
  data/latest.json                 발송용 스냅샷(outbox 포함)
  ../docs/perp-dex-radar/data.json 대시보드용
발송은 하지 않는다 — PC의 airdrop-radar(swing-rebalancer/airdrop-radar, radar/relay.py)가 30분마다(낮) data/latest.json을 읽어
새 outbox를 @spytltgld_airdrop_bot(아커스·라이터 채굴단가 채널)으로 보낸다(2026-10-24부터 — 그 전에는 Brief Hub·전략 봇).
outbox 종류: alert(경보·임박) · release(보낸 경보가 풀림 — 오탐이면 정정) · scope(감시 범위 변경) · summary(요청한 현황) ·
heartbeat(월초 점검). latest.json의 outboxes = 24시간 안에 만든 것 전부(오래된 순) — 실행이 몰려도 중계가 놓치지 않게.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import judge as J  # noqa: E402
import sources as S  # noqa: E402

DATA = os.path.join(HERE, "data")
DOCS = os.path.join(os.path.dirname(HERE), "docs", "perp-dex-radar")
AUX = ("news", "polymarket")   # 보조 원천 — '원천 정상' 계산에서 뺀다
FLAKY = ("x:",)                # 공식 X는 미러가 자주 막혀 핵심 원천에서 뺀다


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


# ───────────────────────── 상태 ─────────────────────────
VENUE_KEYS = ("seeded", "cursors", "rh_supply_hist", "lb_hist", "docs", "doc_moves", "frontend", "news_seen",
              "large_hist", "addr_cache", "events", "level", "notified", "notified_ids", "notified_at",
              "calm_since")


def new_venue_state():
    return {"seeded": False, "seeded_sources": [], "cursors": {}, "rh_supply_hist": [], "lb_hist": [], "docs": {},
            "doc_moves": {}, "frontend": None, "news_seen": [], "pm_seen": [], "seen": {}, "large_hist": [],
            "addr_cache": {}, "events": [], "level": "NONE", "groups": [], "notified": "NONE", "notified_ids": [],
            "notified_at": None, "calm_since": None, "status": {}, "api_flat": None, "api_drop": None, "js": None,
            "odds": {}, "odds_titles": {}}


def new_state():
    return {"version": 2, "venues": {}, "outbox": None, "heartbeat": "", "fail_streak": 0, "scope": []}


def migrate(st: dict) -> dict:
    """v1(라이터RH 전환감시 하나) → v2(거래소별). 기준선·이벤트·알림 기록은 lighter-rh로 옮긴다."""
    if st.get("version", 1) >= 2 and "venues" in st:
        return st
    vs = new_venue_state()
    for k in VENUE_KEYS:
        if k in st:
            vs[k] = st[k]
    vs["seen"]["ann"] = list(st.get("ann_seen") or [])   # 예전 앱 공지 기록 — 같은 공지를 다시 알리지 않게
    # 기준선이 이미 있는 원천. 공식 X·Polymarket(시장 단위로 바뀜)은 새로 기준선을 잡는다.
    vs["seeded_sources"] = ["docs", "ann", "board", "eth_transfers", "rh_supply", "rh_transfers", "frontend", "news"]
    for e in vs["events"]:
        e.setdefault("venue", "lighter-rh")
    out = new_state()
    out["venues"]["lighter-rh"] = vs
    out["heartbeat"] = st.get("heartbeat", "")
    out["fail_streak"] = st.get("fail_streak", 0)
    out["scope"] = ["lighter-rh"]          # 이전 감시 범위 — 설정과 다르면 '감시 범위 변경'을 한 번 보낸다
    out["renamed_from"] = "lit-rh-watch"   # 그 메시지에 이름 바꿈을 함께 알린다
    return out


def follow(moves: dict, url: str) -> str:
    """옮겨진 문서의 지금 주소 — 여러 번 옮겨졌으면 끝까지 따라간다."""
    seen = set()
    while url in moves and url not in seen and len(seen) < 5:
        seen.add(url)
        url = moves[url]["to"]
    return url


def is_day(now: int, cfg: dict) -> bool:
    h = dt.datetime.fromtimestamp(now, J.KST).hour
    return cfg["day_start_kst"] <= h <= cfg["day_end_kst"]


# ───────────────────────── 거래소 한 곳 수집 ─────────────────────────
class VenueRun:
    def __init__(self, cfg, v, vs, now):
        self.cfg, self.v, self.vs, self.now = cfg, v, vs, now
        self.status: dict[str, dict] = {}
        self.tickers = v.get("tickers") or []
        self.extra = v.get("extra_kw")
        self.eth_tr: list[dict] = []
        self.rh_tr: list[dict] = []

    def ok(self, k, note=""):
        self.status[k] = {"ok": True, "at": self.now, "note": note}

    def fail(self, k, err):
        self.status[k] = {"ok": False, "at": self.now, "err": str(err)[:200]}
        print(f"[{self.v['id']}:{k}] 실패: {err}")

    def first(self, k) -> bool:
        return k not in self.vs["seeded_sources"]

    def seeded(self, k):
        if k not in self.vs["seeded_sources"]:
            self.vs["seeded_sources"].append(k)

    def collect(self) -> list[dict]:
        evs = []
        v = self.v
        if v.get("onchain"):
            evs += self.onchain()
        if v.get("docs"):
            evs += self.docs()
        evs += self.posts()
        if v.get("board"):
            evs += self.board()
        if v.get("api"):
            evs += self.api()
        if v.get("frontend"):
            evs += self.frontend()
        if v.get("js"):
            evs += self.js()
        evs += self.aux()
        for e in evs:
            e["venue"] = v["id"]
        return evs

    # ── S1 온체인 (라이터 LIT) ──
    def onchain(self):
        oc, vs, now = self.v["onchain"], self.vs, self.now
        self.eth_tr = self._eth(oc)
        self.rh_tr = self._rh(oc)
        vs["large_hist"] = [t for t in vs["large_hist"] if t.get("ts", now) >= now - 7 * 86400] + \
            [{k: t[k] for k in ("tx", "from", "to", "amount", "ts", "block")} for t in self.eth_tr]
        return (J.detect_eth(self.eth_tr, vs["addr_cache"], oc, now, vs["large_hist"])
                + J.detect_rh_supply(vs["rh_supply_hist"], oc, now)
                + J.detect_rh_transfers(self.rh_tr, oc, now))

    def _eth(self, c):
        try:
            head = S.block_number(c["eth_rpcs"]) - 3  # 재조직 여유
            cur = self.vs["cursors"].get("eth")
            if cur is None:
                self.vs["cursors"]["eth"] = head
                self.ok("eth_transfers", "기준 블록 설정")
                return []
            start = max(cur + 1, head - c["eth_max_backfill_blocks"])
            gap = start > cur + 1
            if start > head:
                self.ok("eth_transfers", "신규 블록 없음")
                return []
            tr = S.transfer_logs(c["eth_rpcs"], c["lit_eth"], start, head, c["eth_chunk_blocks"], c["eth_large_lit"])
            ts_cache = {}
            for t in tr:
                if t["block"] not in ts_cache:
                    try:
                        ts_cache[t["block"]] = S.block_time(c["eth_rpcs"], t["block"])
                    except Exception:  # noqa: BLE001
                        ts_cache[t["block"]] = self.now
                t["ts"] = ts_cache[t["block"]]
            self._meta(tr, c)
            self.vs["cursors"]["eth"] = head
            self.ok("eth_transfers", f"{head - start + 1}블록 · 대량 {len(tr)}건" + (" · 일부 구간 건너뜀" if gap else ""))
            return tr
        except Exception as e:  # noqa: BLE001
            self.fail("eth_transfers", e)
            return []

    def _meta(self, tr, c):
        cache = self.vs["addr_cache"]
        for t in tr:
            for a in (t["from"], t["to"]):
                if a in c["known"]:
                    continue
                m = cache.get(a)
                if m and not m.get("error") and self.now - m.get("_at", 0) < 7 * 86400:
                    continue
                m = S.address_meta(c["eth_blockscout"], a)
                m["_at"] = self.now
                cache[a] = m
        if len(cache) > 400:
            for k in sorted(cache, key=lambda k: cache[k].get("_at", 0))[:len(cache) - 400]:
                cache.pop(k, None)

    def _rh(self, c):
        tr = []
        try:
            sup = S.total_supply(c["rh_rpcs"], c["lit_rh"])
            h = self.vs["rh_supply_hist"]
            if not h or abs(h[-1][1] - sup) > 1e-6 or self.now - h[-1][0] > 6 * 3600:
                h.append([self.now, round(sup, 2)])
            self.vs["rh_supply_hist"] = [x for x in h if x[0] >= self.now - 14 * 86400]
            self.ok("rh_supply", f"{sup:,.0f} LIT")
        except Exception as e:  # noqa: BLE001
            self.fail("rh_supply", e)
        try:
            head = S.block_number(c["rh_rpcs"]) - 3
            cur = self.vs["cursors"].get("rh")
            if cur is None:
                self.vs["cursors"]["rh"] = head
            else:
                start = max(cur + 1, head - c["rh_max_backfill_blocks"])
                if start <= head:
                    tr = S.transfer_logs(c["rh_rpcs"], c["lit_rh"], start, head, c["rh_chunk_blocks"], c["rh_large_lit"])
                    for t in tr:
                        t["ts"] = self.now
                self.vs["cursors"]["rh"] = head
            self.ok("rh_transfers", f"대량 {len(tr)}건")
        except Exception as e:  # noqa: BLE001
            self.fail("rh_transfers", e)
        return tr

    # ── S2 공식 문서 ──
    def docs(self):
        """문서 전체 목록(llms.txt)을 먼저 읽고, '페이지 없음'이 된 문서는 목록에서 새 주소를 찾아 따라간다.
        기준선은 설정의 주소로 묶어 두므로 주소가 바뀌어도 옛 내용과 그대로 비교한다.
        '페이지 없음' 안내문은 기준선에 넣지 않는다 — 2026-10-22 오탐(문서 이전을 내용 변경으로 읽음)."""
        d, st, now = self.v["docs"], self.vs, self.now
        moves = st.setdefault("doc_moves", {})
        names = d.get("names") or {}
        pages = [{"url": p} if isinstance(p, str) else dict(p) for p in d.get("pages", [])]
        order = ([{"url": d["index"], "index": True}] if d.get("index") else []) + pages
        index: dict = {}
        evs, nok, notes = [], 0, []
        for p in order:
            url = p["url"]
            name = p.get("name") or J.doc_name(url, names)
            eff = follow(moves, url)
            prev = st["docs"].get(url) or {}
            try:
                txt = S.fetch_doc(eff)
                if S.is_not_found(txt):
                    new = J.resolve_move(eff, index, prev.get("text"))
                    txt2 = S.fetch_doc(new) if new else None
                    if not txt2 or S.is_not_found(txt2):
                        evs.append(J.doc_missing_event(eff, name, now))
                        notes.append(f"{name} 없음")
                        continue
                    same = prev.get("text") == txt2
                    moves[eff] = {"to": new, "at": now, "same": same, "name": name}
                    evs.append(J.doc_moved_event(eff, new, name, same, now))
                    notes.append(f"{name} 주소 이전")
                    txt, eff = txt2, new
                old = prev.get("text")
                evs += J.detect_doc(eff, old, txt, now, name, self.tickers, bool(p.get("any")), self.extra)
                if old != txt or prev.get("url") != eff:
                    st["docs"][url] = {"text": txt, "hash": S.sha(txt), "url": eff,
                                       "changed": now if old != txt else prev.get("changed")}
                if p.get("index"):
                    index = J.parse_index(txt, eff)
                nok += 1
            except Exception as e:  # noqa: BLE001
                print(f"[{self.v['id']}:docs] {eff}: {e}")
        if nok:
            self.ok("docs", f"{nok}/{len(order)} 페이지" + (" · " + ", ".join(notes) if notes else ""))
        else:
            self.fail("docs", "전 페이지 실패")
        self.seeded("docs")
        return evs

    # ── S2 공식 발표 (X · 텔레그램 · 블로그 · 피드 · 앱 공지) ──
    def posts(self):
        v, evs = self.v, []
        srcs = [("x", f"x:{h}", (lambda h=h: S.x_feed(self.cfg["x_hosts"], h))) for h in v.get("x") or []]
        if v.get("telegram"):
            srcs.append(("telegram", "telegram", lambda: S.telegram_posts(v["telegram"])))
        if v.get("blog"):
            srcs.append(("blog", "blog", lambda: S.link_items(v["blog"]["url"], v["blog"]["pattern"])))
        if v.get("feed"):
            srcs.append(("feed", "feed", lambda: S.feed_items(v["feed"])))
        if v.get("ann"):
            srcs.append(("ann", "ann", lambda: S.json_posts(v["ann"])))
        for kind, key, fetch in srcs:
            try:
                items = fetch()
            except Exception as e:  # noqa: BLE001
                self.fail(key, e)
                continue
            seen = self.vs["seen"].setdefault(key, [])
            first = self.first(key)
            if not first:
                evs += J.detect_posts(items, seen, self.now, kind, v["name"], self.tickers,
                                      bool(v.get("token_live")), self.extra)
            keys = [J.post_key(it) for it in items]
            self.vs["seen"][key] = (seen + [k for k in keys if k not in seen])[-600:]
            self.seeded(key)
            self.ok(key, f"{len(items)}건" + (" · 기준선" if first else ""))
        return evs

    # ── S2 리더보드 ──
    def board(self):
        b = self.v["board"]
        try:
            pts = S.leaderboard_json(b["url"], b.get("list", ""), b.get("field", "points"), b.get("top", 10))
        except Exception as e:  # noqa: BLE001
            self.fail("board", e)
            return []
        tot = round(sum(pts), 3)
        h = self.vs["lb_hist"]
        h.append([self.now, tot, pts[0] if pts else 0])
        self.vs["lb_hist"] = [x for x in h if x[0] >= self.now - 60 * 86400][-800:]
        self.ok("board", f"상위 {len(pts)}명 합계 {tot:,.0f}")
        first = self.first("board")
        self.seeded("board")
        if first:
            return []
        return J.detect_leaderboard(self.vs["lb_hist"], {"top": len(pts), "lb_drop_pct": b.get("drop_pct", 3),
                                                         "lb_freeze_hours": b.get("freeze_hours", 48)}, self.now)

    # ── S2 포인트 API 값 ──
    def api(self):
        a, vs = self.v["api"], self.vs
        try:
            data = S.get_json(a["url"])
        except Exception as e:  # noqa: BLE001
            self.fail("api", e)
            return []
        flat = S.flatten(data, ignore=frozenset(a.get("ignore", [])))
        evs = J.detect_api(vs.get("api_flat"), flat, self.now, a, self.v["name"])
        vs["api_flat"] = flat
        drop = a.get("drop")
        if drop:
            try:
                nxt = S.json_path(data, drop["next"])
                cur = {"next": S.parse_time(nxt) if isinstance(nxt, str) else nxt,
                       "week": json.dumps(S.json_path(data, drop["week"]), sort_keys=True)}
            except (KeyError, IndexError, TypeError):
                cur = None
            prev = vs.get("api_drop")
            if cur and (not prev or cur["week"] != prev.get("week")):
                if prev:  # 새 주 배분이 들어옴 — 지연 신호 해제
                    for e in vs["events"]:
                        if e["signal"] == "drop_late" and not e["refuted"]:
                            J.lift(e, "다음 주 포인트 배분이 들어왔어요", "data", self.now)
                vs["api_drop"] = cur
            elif cur:
                evs += J.detect_drop_delay(prev, cur, self.now, drop, self.v["name"])
        self.ok("api", f"항목 {len(flat)}개")
        return evs

    # ── S3 앱 화면·코드 ──
    def frontend(self):
        try:
            surf = S.frontend_surface(self.v["frontend"])
            if len(surf["chunks"]) < 10:
                raise RuntimeError(f"번들 파싱 이상(모듈 {len(surf['chunks'])}개)")
            evs = J.detect_frontend(self.vs.get("frontend"), surf, self.now)
            old = self.vs.get("frontend") or {"chunks": [], "api_paths": []}
            # 사라진 것은 기준선에 누적 유지 — 코드 분할 흔들림으로 재등장 시 오탐 방지
            self.vs["frontend"] = {"chunks": sorted(set(old["chunks"]) | set(surf["chunks"])),
                                   "api_paths": sorted(set(old["api_paths"]) | set(surf["api_paths"]))}
            self.ok("frontend", f"모듈 {len(surf['chunks'])} · API {len(surf['api_paths'])}")
            self.seeded("frontend")
            return evs
        except Exception as e:  # noqa: BLE001
            self.fail("frontend", e)
            return []

    def js(self):
        j = self.v["js"]
        try:
            found = S.js_scan(j["url"], j["patterns"], j.get("max_files", 60))
        except Exception as e:  # noqa: BLE001
            self.fail("js", e)
            return []
        evs = J.detect_jsscan(self.vs.get("js"), found, self.now, j, self.v["name"])
        base = dict(self.vs.get("js") or {})
        for k, vals in found.items():
            if vals:  # 못 찾은 값은 기준선을 지우지 않는다(번들 구조 변경일 수 있음)
                base[k] = vals
        self.vs["js"] = base
        names = j.get("names") or {}
        self.ok("js", " · ".join(f"{names.get(k, k)}={','.join(v) or '없음'}" for k, v in found.items())[:120])
        self.seeded("js")
        return evs

    # ── S4 보조 (뉴스 · Polymarket) ──
    def aux(self):
        v, vs, now, evs = self.v, self.vs, self.now, []
        n = v.get("news")
        if n:
            items = []
            for i, q in enumerate(n["q"] if isinstance(n["q"], list) else [n["q"]]):
                if i:
                    time.sleep(1.0)
                try:
                    items += S.google_news(q)
                except Exception as e:  # noqa: BLE001
                    print(f"[{v['id']}:news] {q}: {e}")
            if items:
                first = self.first("news")
                if not first:
                    evs += J.detect_news(items, vs["news_seen"], now, "news", n.get("match") or re.escape(v["name"]),
                                         self.tickers)
                seen = set(vs["news_seen"]) | {J.hash_text(it.get("link") or it.get("title", "")) for it in items}
                vs["news_seen"] = sorted(seen)[-2000:]
                self.seeded("news")
                self.ok("news", f"{len(items)}건")
            else:
                self.fail("news", "수집 0건")
        p = v.get("polymarket")
        if p:
            try:
                match = p.get("match") or re.escape(v["name"])
                rx = re.compile(match, re.I)
                mk = [m for m in S.polymarket(p["q"]) if rx.search(m["title"] + " " + m["slug"])]
                first = self.first("polymarket")
                if not first:
                    evs += J.detect_polymarket(mk, vs["pm_seen"], now, match)
                vs["pm_seen"] = sorted(set(vs["pm_seen"]) | {m["slug"] for m in mk})[-600:]
                odds, titles = vs.setdefault("odds", {}), vs.setdefault("odds_titles", {})
                live = set()
                for m in mk:
                    if not m.get("volume"):  # 거래가 한 번도 없는 시장(기본가 50%)은 확률로 보지 않는다
                        continue
                    live.add(m["slug"])
                    titles[m["slug"]] = m["title"]
                    h = odds.setdefault(m["slug"], [])
                    if not h or h[-1][1] != m["yes_pct"] or now - h[-1][0] > 6 * 3600:
                        h.append([now, m["yes_pct"]])
                    odds[m["slug"]] = [x for x in h if x[0] >= now - 14 * 86400][-200:]
                for s in [s for s in odds if s not in live]:  # 닫힌 시장 정리
                    odds.pop(s, None)
                    titles.pop(s, None)
                if not first:
                    evs += J.detect_odds(odds, titles, now, v["name"])
                self.seeded("polymarket")
                self.ok("polymarket", f"열린 시장 {len(mk)}개")
            except Exception as e:  # noqa: BLE001
                self.fail("polymarket", e)
        return evs


def core_keys(status: dict) -> list[str]:
    return [k for k in status if k not in AUX and not k.startswith(FLAKY)]


def summary(v: dict, vs: dict, now: int) -> dict:
    core = core_keys(vs["status"])
    odds = J.odds_line(vs.get("odds", {}), vs.get("odds_titles", {}), now=now) if v.get("show_odds", True) else ""
    return {"id": v["id"], "name": v["name"], "level": vs["level"], "groups": vs.get("groups", []),
            "status_note": v.get("status_note", ""), "sources_ok": sum(1 for k in core if vs["status"][k]["ok"]),
            "sources_all": len(core), "odds": odds, "site": v.get("site", ""), "upcoming": J.upcoming(v, now)}


def push_outbox(st: dict, ob: dict, now: int, ttl: int) -> None:
    """새 메시지 — outbox(최신 하나)와 outbox_log(24시간 안 것, 중계가 놓치지 않게)에 넣는다."""
    st["outbox"] = ob
    log = [o for o in st.get("outbox_log") or [] if now - o["created"] <= ttl and o["id"] != ob["id"]]
    st["outbox_log"] = (log + [ob])[-10:]


RUN_SRC = {"schedule": "s", "workflow_dispatch": "d", "push": "p"}


def record_run(st: dict, now: int, event: str | None = None) -> list:
    """실행 기록 — [시각, 출처(s 예약 · d 수동/PC 시작 · p push · l 로컬)], 48시간 안 것만 (2026-10-26 운영자
    "어제 메시지가 안 온 것 같은데 주기 확인해봐" — GitHub 예약이 자주 빠져 실제 주기를 보이게)."""
    runs = [r for r in st.get("runs") or [] if isinstance(r, list) and now - r[0] <= 48 * 3600]
    st["runs"] = runs + [[now, RUN_SRC.get(event or "", "l")]]
    return st["runs"]


# ───────────────────────── 실행 ─────────────────────────
def main(argv):
    seed_flag, dry = "--seed" in argv, "--dry" in argv
    want_summary = "--summary" in argv or os.environ.get("RADAR_SUMMARY", "").strip() in ("1", "true")
    summary_note = (os.environ.get("RADAR_SUMMARY_NOTE") or "").strip()[:200]
    cfg = load_json(os.path.join(HERE, "radar_config.json"), None)
    if cfg is None:
        print("radar_config.json 없음")
        return 2
    now = int(os.environ.get("RADAR_NOW") or time.time())
    st_path = os.path.join(DATA, "state.json")
    st = migrate(load_json(st_path, None) or new_state())
    day = is_day(now, cfg)
    req = (load_json(os.path.join(HERE, "request.json"), {}) or {}).get("summary") or {}
    req_id = str(req.get("id") or "")
    if req_id and req_id != st.get("summary_done"):
        try:
            req_at = J.kst_time(req.get("at") or "")[0]
        except ValueError:
            req_at = now
        if now - req_at <= 24 * 3600:
            want_summary = True
            summary_note = summary_note or str(req.get("note") or "").strip()[:200]
        else:
            st["summary_done"] = req_id   # 하루 지난 요청은 버린다
    window = cfg["event_window_days"]
    alerts, releases, release_fp, summaries = [], [], [], []
    core_ok = core_all = 0

    for v in cfg["venues"]:
        vs = st["venues"].setdefault(v["id"], new_venue_state())
        for k, val in new_venue_state().items():
            vs.setdefault(k, val)
        seeding = seed_flag or not vs["seeded"]
        r = VenueRun(cfg, v, vs, now)
        evs = r.collect()

        refuted = 0
        if v.get("onchain"):
            refuted += J.apply_refutes(vs["events"], r.eth_tr + r.rh_tr, v["onchain"], now)
            if vs["rh_supply_hist"]:  # 공급 복귀
                cur = vs["rh_supply_hist"][-1][1]
                for e in vs["events"]:
                    if e["signal"] == "rh_supply" and not e["refuted"] and cur <= e["ref"].get("base", 0) + 1e5:
                        J.lift(e, "로빈후드 체인 LIT 공급이 7일 최저 수준으로 돌아왔어요", "data", now)
                        refuted += 1
        if len(vs["lb_hist"]) >= 2 and vs["lb_hist"][-1][1] > vs["lb_hist"][-2][1]:  # 적립 재개
            for e in vs["events"]:
                if e["signal"] == "lb_frozen" and not e["refuted"]:
                    J.lift(e, "포인트 적립이 다시 늘기 시작했어요", "data", now)
                    refuted += 1
        # 지금 규칙으로 지난 문서 경보를 다시 본다 — 오탐이면 풀고 아래에서 정정 메시지를 만든다
        refuted += J.rejudge(vs["events"], vs.get("doc_moves", {}), now, r.tickers, r.extra)

        known = {e["id"] for e in vs["events"]}
        fresh = [] if seeding else [e for e in evs if e["id"] not in known]
        vs["events"] = ([e for e in vs["events"] if e["ts"] >= now - 30 * 86400] + fresh)[-500:]
        vs["status"] = r.status
        core = core_keys(r.status)
        ok = [k for k in core if r.status[k]["ok"]]
        core_ok, core_all = core_ok + len(ok), core_all + len(core)
        note = "수집이 모두 실패해 이번에는 판정할 수 없었어요" if core and not ok else ""

        act = J.active(vs["events"], now, window)
        level, groups = J.level_of(act)
        vs["level"], vs["groups"] = level, groups
        if level == "NONE":  # 조용함이 7일 이어지면 알림 기준 초기화
            vs["calm_since"] = vs.get("calm_since") or now
            if now - vs["calm_since"] >= 7 * 86400:
                vs["notified"] = "NONE"
        else:
            vs["calm_since"] = None

        notified = vs.get("notified", "NONE")
        strong_ids = [e["id"] for e in act if J.RANK[e["grade"]] >= 2]
        nids = set(vs.get("notified_ids") or [])
        if not nids and J.RANK[notified] >= 2:  # 예전 상태 — 알림 당시 강신호를 7일 창에서 추정
            nids = {e["id"] for e in vs["events"] if J.RANK[e["grade"]] >= 2 and e["ts"] >= now - window * 86400}
        lifted = [e for e in vs["events"] if e["id"] in nids and e["refuted"]]
        if J.RANK[level] < J.RANK[notified]:
            if lifted:
                if day and not seed_flag:  # 보낸 경보가 풀림 — 오탐이면 정정, 해제 조건이면 해제 (낮에 한 번)
                    moves = {}
                    for e in lifted:
                        u = (e.get("ref") or {}).get("url")
                        if u in vs.get("doc_moves", {}) and u not in moves:
                            moves[u] = vs["doc_moves"][u]
                    releases.append(J.render_release_section(v, lifted, level, notified, vs.get("notified_at"), moves))
                    release_fp.append(all(e.get("refute_kind") == "false_positive" for e in lifted))
                    vs["notified"], vs["notified_ids"], vs["notified_at"] = level, strong_ids, now
            elif not (nids & {e["id"] for e in act}):
                # 알린 신호가 7일 창을 지나 빠짐 — 다음 경보를 다시 보낼 수 있게 기준만 낮춘다(메시지 없음)
                vs["notified"], vs["notified_ids"] = level, strong_ids
        elif not seeding and day and J.RANK[level] >= 2 and J.RANK[level] > J.RANK[notified]:
            alerts.append((level, v, groups, act, note))
            vs["notified"], vs["notified_ids"], vs["notified_at"] = level, strong_ids, now
        if seeding and not seed_flag:
            vs["seeded"] = True
        summaries.append(summary(v, vs, now))
        print(f"[{v['id']}] {level} {groups} · 신규 {len(fresh)} · 반증 {refuted} · 원천 {len(ok)}/{len(core)}"
              + (" · 시딩" if seeding else ""))

    # ── 메시지(outbox) — 실행마다 많아야 하나. 중계(PC airdrop-radar)는 outbox_log의 24시간 안 것을 순서대로 읽는다 ──
    ttl = cfg["outbox_ttl_hours"] * 3600
    if st.get("outbox_log") is None:  # 이전 상태: 마지막 outbox만 있음
        st["outbox_log"] = [st["outbox"]] if st.get("outbox") else []
    st["outbox_log"] = [o for o in st["outbox_log"] if now - o["created"] <= ttl]
    ob = st.get("outbox")
    if ob and now - ob["created"] > ttl:
        st["outbox"] = None
    scope = [v["id"] for v in cfg["venues"]]
    names = {v["id"]: v["name"] for v in cfg["venues"]}
    text = kind = None
    top = max((lv for lv, *_ in alerts), key=lambda lv: J.RANK[lv], default="NONE")
    if alerts:
        one = len(alerts) == 1
        secs = [(lv, J.render_venue_alert(v, lv, g, a, now, cfg, n, head=one)) for lv, v, g, a, n in alerts]
        text = J.render_alert(secs, cfg)
        if releases:
            text += "\n\n" + "\n\n".join(releases)
        kind = "alert"
    elif day and not seed_flag and st.get("scope") != scope:
        old = st.get("scope") or []
        text = J.render_kickoff(summaries, now, cfg, releases or None,
                                [names[i] for i in scope if i not in old], [i for i in old if i not in scope],
                                renamed=bool(st.get("renamed_from")))
        st["scope"] = scope
        st.pop("renamed_from", None)
        kind = "scope"
    elif releases:
        text = J.render_release(releases, cfg, all(release_fp))
        kind = "release"
    elif want_summary and day and not seed_flag:  # 요청한 현황(수동 실행·request.json) — 다른 메시지가 없을 때만
        text = J.render_summary(summaries, now, cfg, "현황", summary_note)
        kind = "summary"
        if req_id:
            st["summary_done"] = req_id
    if want_summary and kind != "summary":
        why = f"'{kind}' 메시지가 있어" if kind else ("밤이라" if not day else "기준선만 잡는 실행이라")
        print(f"[현황] 이번 실행은 {why} 현황을 만들지 않음")
    if text:
        push_outbox(st, {"id": J.eid(kind, now, text[:300]), "text": text, "created": now, "kind": kind, "level": top},
                    now, ttl)
    month = dt.datetime.fromtimestamp(now, J.KST).strftime("%Y-%m")
    if (not st.get("outbox") and day and not seed_flag and st.get("heartbeat") != month
            and dt.datetime.fromtimestamp(now, J.KST).day <= 7):
        push_outbox(st, {"id": J.eid("hb", month), "text": J.render_heartbeat(summaries, now, cfg),
                         "created": now, "kind": "heartbeat", "level": "NONE"}, now, ttl)
        st["heartbeat"] = month

    undecidable = core_all > 0 and core_ok < core_all / 2
    st["fail_streak"] = st.get("fail_streak", 0) + 1 if undecidable else 0
    runs = record_run(st, now, os.environ.get("GITHUB_EVENT_NAME"))
    runs24 = [r for r in runs if now - r[0] <= 24 * 3600]
    latest = {
        "generated_at": now, "levels": {v["id"]: st["venues"][v["id"]]["level"] for v in cfg["venues"]},
        "undecidable": undecidable, "outbox": st.get("outbox"),
        "outboxes": sorted(st.get("outbox_log") or [], key=lambda o: o["created"]),
        "status": {v["id"]: {k: s.get("ok") for k, s in st["venues"][v["id"]]["status"].items()} for v in cfg["venues"]},
        "runs": runs24,
    }
    dash = {"generated_at": now, "name": J.NAME, "venues": [], "recent_all": [], "runs": runs24}
    for v, s in zip(cfg["venues"], summaries):
        vs = st["venues"][v["id"]]
        act = J.active(vs["events"], now, window)
        dash["venues"].append({
            **s, "sources": vs["status"], "events": sorted(act, key=lambda e: -e["ts"])[:40],
            "lb_hist": vs["lb_hist"][-400:] if v.get("board") else None,
            "rh_supply_hist": vs["rh_supply_hist"] if v.get("onchain") else None,
            "links": v.get("links", {}),
        })
        dash["recent_all"] += [{**e, "venue_name": v["name"]} for e in vs["events"]]
    dash["recent_all"] = sorted(dash["recent_all"], key=lambda e: -e["ts"])[:250]
    print(f"[판정] 핵심 원천 {core_ok}/{core_all}" + (" · 판정 불가" if undecidable else "") + (" · 낮" if day else " · 밤"))
    if st.get("outbox") and st["outbox"]["created"] == now:
        print("[outbox]\n" + st["outbox"]["text"])
    if not dry:
        save_json(st_path, st)
        save_json(os.path.join(DATA, "latest.json"), latest)
        save_json(os.path.join(DOCS, "data.json"), dash)
    if st["fail_streak"] >= 2:
        print(f"::error::핵심 원천 연속 {st['fail_streak']}회 판정 불가")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
