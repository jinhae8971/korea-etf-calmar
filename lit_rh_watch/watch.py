#!/usr/bin/env python3
"""lit-rh-watch — 라이터 로빈후드 포인트 → LIT 전환 신호 감시기 (수집·판정·대시보드 데이터).

실행: python watch.py [--seed] [--dry]
  --seed : 기준선만 갱신하고 이벤트를 만들지 않는다(최초 1회 자동 적용).
  --dry  : 파일을 쓰지 않는다.

출력
  data/state.json            내부 상태(커서·기준선·이벤트)
  data/latest.json           릴레이용 스냅샷(outbox 포함)
  ../docs/lit-rh-watch/data.json  대시보드용
발송은 하지 않는다 — hynix-correction-monitor의 lit-rh-relay가 outbox를 읽어 트레이드채널로 보낸다.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import judge as J  # noqa: E402
import sources as S  # noqa: E402

DATA = os.path.join(HERE, "data")
DOCS = os.path.join(os.path.dirname(HERE), "docs", "lit-rh-watch")
CORE_SOURCES = ["eth_transfers", "rh_supply", "docs", "announcements", "leaderboard", "frontend"]


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


def save_if_changed(path, obj, volatile=("generated_at",)):
    """휘발 필드만 달라졌으면 쓰지 않는다 — 빈 커밋 방지."""
    old = load_json(path, None)
    if old is not None:
        a = {k: v for k, v in old.items() if k not in volatile}
        b = {k: v for k, v in obj.items() if k not in volatile}
        if a == b:
            return False
    save_json(path, obj)
    return True


def new_state():
    return {"version": 1, "seeded": False, "cursors": {}, "rh_supply_hist": [], "lb_hist": [],
            "docs": {}, "ann_seen": [], "frontend": None, "news_seen": [], "pm_seen": [],
            "large_hist": [], "addr_cache": {}, "events": [], "level": "NONE", "fams": [],
            "notified": "NONE", "calm_since": None, "outbox": None, "heartbeat": "",
            "fail_streak": 0, "status": {}}


class Run:
    def __init__(self, cfg, state, now):
        self.cfg, self.st, self.now = cfg, state, now
        self.new_events: list[dict] = []
        self.status: dict[str, dict] = {}

    def ok(self, name, note=""):
        self.status[name] = {"ok": True, "at": self.now, "note": note}

    def fail(self, name, err):
        self.status[name] = {"ok": False, "at": self.now, "err": str(err)[:200]}
        print(f"[{name}] 실패: {err}")

    # ── S1 ETH ──
    def eth(self):
        c = self.cfg
        try:
            head = S.block_number(c["eth_rpcs"]) - 3  # 재조직 여유
            cur = self.st["cursors"].get("eth")
            if cur is None:
                self.st["cursors"]["eth"] = head
                self.ok("eth_transfers", "기준 블록 설정")
                return []
            start = max(cur + 1, head - c["eth_max_backfill_blocks"])
            gap = start > cur + 1
            if start > head:
                self.ok("eth_transfers", "신규 블록 없음")
                return []
            tr = S.transfer_logs(c["eth_rpcs"], c["lit_eth"], start, head, c["eth_chunk_blocks"],
                                 c["eth_large_lit"])
            ts_cache = {}
            for t in tr:
                if t["block"] not in ts_cache:
                    try:
                        ts_cache[t["block"]] = S.block_time(c["eth_rpcs"], t["block"])
                    except Exception:  # noqa: BLE001
                        ts_cache[t["block"]] = self.now
                t["ts"] = ts_cache[t["block"]]
            self._meta(tr, c["eth_blockscout"])
            self.st["cursors"]["eth"] = head
            self.ok("eth_transfers", f"{head - start + 1}블록 · 대량 {len(tr)}건" + (" · 일부 구간 건너뜀" if gap else ""))
            return tr
        except Exception as e:  # noqa: BLE001
            self.fail("eth_transfers", e)
            return []

    def _meta(self, tr, blockscout):
        cache = self.st["addr_cache"]
        for t in tr:
            for a in (t["from"], t["to"]):
                if a in self.cfg["known"]:
                    continue
                m = cache.get(a)
                if m and not m.get("error") and self.now - m.get("_at", 0) < 7 * 86400:
                    continue
                m = S.address_meta(blockscout, a)
                m["_at"] = self.now
                cache[a] = m
        # 캐시 상한
        if len(cache) > 400:
            for k in sorted(cache, key=lambda k: cache[k].get("_at", 0))[:len(cache) - 400]:
                cache.pop(k, None)

    # ── S1 RH ──
    def rh(self):
        c = self.cfg
        tr = []
        try:
            sup = S.total_supply(c["rh_rpcs"], c["lit_rh"])
            h = self.st["rh_supply_hist"]
            if not h or abs(h[-1][1] - sup) > 1e-6 or self.now - h[-1][0] > 6 * 3600:
                h.append([self.now, round(sup, 2)])
            self.st["rh_supply_hist"] = [x for x in h if x[0] >= self.now - 14 * 86400]
            self.ok("rh_supply", f"{sup:,.0f} LIT")
        except Exception as e:  # noqa: BLE001
            self.fail("rh_supply", e)
        try:
            head = S.block_number(c["rh_rpcs"]) - 3
            cur = self.st["cursors"].get("rh")
            if cur is None:
                self.st["cursors"]["rh"] = head
            else:
                start = max(cur + 1, head - c["rh_max_backfill_blocks"])
                if start <= head:
                    tr = S.transfer_logs(c["rh_rpcs"], c["lit_rh"], start, head, c["rh_chunk_blocks"],
                                         c["rh_large_lit"])
                    for t in tr:
                        t["ts"] = self.now
                self.st["cursors"]["rh"] = head
            self.ok("rh_transfers", f"대량 {len(tr)}건")
        except Exception as e:  # noqa: BLE001
            self.fail("rh_transfers", e)
        return tr

    # ── S2 문서 ──
    def docs(self):
        evs, nok = [], 0
        for url in self.cfg["docs_pages"]:
            try:
                txt = S.fetch_doc(url)
                old = (self.st["docs"].get(url) or {}).get("text")
                evs += J.detect_doc(url, old, txt, self.now)
                if old != txt:
                    self.st["docs"][url] = {"text": txt, "hash": S.sha(txt), "changed": self.now}
                nok += 1
            except Exception as e:  # noqa: BLE001
                print(f"[docs] {url}: {e}")
        if nok:
            self.ok("docs", f"{nok}/{len(self.cfg['docs_pages'])} 페이지")
        else:
            self.fail("docs", "전 페이지 실패")
        return evs

    def announcements(self):
        try:
            items = S.announcements(self.cfg["rh_api"])
            evs = J.detect_announcements(items, self.st["ann_seen"], self.now)
            self.st["ann_seen"] = (self.st["ann_seen"] + [J.ann_key(a) for a in items
                                                          if J.ann_key(a) not in self.st["ann_seen"]])[-300:]
            self.ok("announcements", f"{len(items)}건")
            return evs
        except Exception as e:  # noqa: BLE001
            self.fail("announcements", e)
            return []

    def leaderboard(self):
        try:
            lb = S.leaderboard(self.cfg["rh_api"])
            if not lb:
                raise RuntimeError("빈 리더보드")
            tot = round(sum(x["points"] for x in lb), 3)
            h = self.st["lb_hist"]
            h.append([self.now, tot, lb[0]["points"]])
            self.st["lb_hist"] = [x for x in h if x[0] >= self.now - 60 * 86400][-800:]
            self.ok("leaderboard", f"상위10 합계 {tot:,.0f}")
            return J.detect_leaderboard(self.st["lb_hist"], self.cfg, self.now)
        except Exception as e:  # noqa: BLE001
            self.fail("leaderboard", e)
            return []

    # ── S3 프론트 ──
    def frontend(self):
        try:
            surf = S.frontend_surface(self.cfg["rh_frontend"])
            if len(surf["chunks"]) < 10:
                raise RuntimeError(f"번들 파싱 이상(모듈 {len(surf['chunks'])}개)")
            evs = J.detect_frontend(self.st.get("frontend"), surf, self.now)
            old = self.st.get("frontend") or {"chunks": [], "api_paths": []}
            # 사라진 것은 기준선에 누적 유지 — 코드 분할 흔들림으로 재등장 시 오탐 방지
            self.st["frontend"] = {"chunks": sorted(set(old["chunks"]) | set(surf["chunks"])),
                                   "api_paths": sorted(set(old["api_paths"]) | set(surf["api_paths"]))}
            self.ok("frontend", f"모듈 {len(surf['chunks'])} · API {len(surf['api_paths'])}")
            return evs
        except Exception as e:  # noqa: BLE001
            self.fail("frontend", e)
            return []

    # ── S4 ──
    def aux(self):
        evs = []
        news = []
        for q in self.cfg["news_queries"]:
            try:
                news += S.google_news(q)
            except Exception as e:  # noqa: BLE001
                print(f"[news] {q}: {e}")
        if news:
            evs += J.detect_news(news, self.st["news_seen"], self.now, "news")
            self.ok("news", f"{len(news)}건")
        else:
            self.fail("news", "수집 0건")
        xs = []
        for acc in self.cfg["x_accounts"]:
            try:
                xs += S.x_feed(self.cfg["x_hosts"], acc)
            except Exception as e:  # noqa: BLE001
                print(f"[x] {acc}: {e}")
        if xs:
            evs += J.detect_news(xs, self.st["news_seen"], self.now, "x")
            self.ok("x", f"{len(xs)}건")
        else:
            self.fail("x", "수집 불가(보조 원천)")
        seen = set(self.st["news_seen"])
        for it in news + xs:
            seen.add(J.hash_text(it.get("link") or it.get("title", "")))
        self.st["news_seen"] = sorted(seen)[-1500:]
        try:
            pm = S.polymarket(self.cfg["polymarket_query"])
            evs += J.detect_polymarket(pm, self.st["pm_seen"], self.now)
            self.st["pm_seen"] = sorted(set(self.st["pm_seen"]) | {m["slug"] for m in pm})[-300:]
            self.ok("polymarket", f"진행 중 {len(pm)}건")
        except Exception as e:  # noqa: BLE001
            self.fail("polymarket", e)
        return evs


def is_day(now: int, cfg: dict) -> bool:
    h = dt.datetime.fromtimestamp(now, J.KST).hour
    return cfg["day_start_kst"] <= h <= cfg["day_end_kst"]


def main(argv):
    seed_flag, dry = "--seed" in argv, "--dry" in argv
    cfg = load_json(os.path.join(HERE, "watch_config.json"), None)
    if cfg is None:
        print("watch_config.json 없음")
        return 2
    now = int(os.environ.get("LITRH_NOW") or time.time())
    st_path = os.path.join(DATA, "state.json")
    st = load_json(st_path, None) or new_state()
    seeding = seed_flag or not st.get("seeded")

    r = Run(cfg, st, now)
    eth_tr = r.eth()
    rh_tr = r.rh()
    evs = []
    # 이동 이력(7일) — 누적 판정용
    st["large_hist"] = [t for t in st["large_hist"] if t.get("ts", now) >= now - 7 * 86400] + \
        [{k: t[k] for k in ("tx", "from", "to", "amount", "ts", "block")} for t in eth_tr]
    evs += J.detect_eth(eth_tr, st["addr_cache"], cfg, now, st["large_hist"])
    evs += J.detect_rh_supply(st["rh_supply_hist"], cfg, now)
    evs += J.detect_rh_transfers(rh_tr, cfg, now)
    evs += r.docs()
    evs += r.announcements()
    evs += r.leaderboard()
    evs += r.frontend()
    evs += r.aux()

    refuted = J.apply_refutes(st["events"], eth_tr + rh_tr, cfg)
    # 공급 복귀 반증
    if st["rh_supply_hist"]:
        cur = st["rh_supply_hist"][-1][1]
        for e in st["events"]:
            if e["signal"] == "rh_supply" and not e["refuted"] and cur <= e["ref"].get("base", 0) + 1e5:
                e["refuted"] = True
                refuted += 1
    # 리더보드 재증가 → 정지 반증
    if len(st["lb_hist"]) >= 2 and st["lb_hist"][-1][1] > st["lb_hist"][-2][1]:
        for e in st["events"]:
            if e["signal"] == "lb_frozen" and not e["refuted"]:
                e["refuted"] = True
                refuted += 1

    known = {e["id"] for e in st["events"]}
    fresh = [] if seeding else [e for e in evs if e["id"] not in known]
    st["events"] = [e for e in st["events"] if e["ts"] >= now - 30 * 86400] + fresh
    st["events"] = st["events"][-500:]

    # 원천 상태 · 판정 불가
    st["status"].update(r.status)
    core_ok = sum(1 for s in CORE_SOURCES if r.status.get(s, {}).get("ok"))
    undecidable = core_ok < 3
    note = ""
    if undecidable:
        st["fail_streak"] = st.get("fail_streak", 0) + 1
        note = f"판정 불가: 핵심 원천 {core_ok}/{len(CORE_SOURCES)}만 수신"
    else:
        st["fail_streak"] = 0

    act = J.active(st["events"], now, cfg["event_window_days"])
    level, fams = J.level_of(act)
    st["level"], st["fams"] = level, fams
    # 에피소드 리셋: NONE이 7일 지속되면 알림 기준을 초기화
    if level == "NONE":
        st["calm_since"] = st.get("calm_since") or now
        if now - st["calm_since"] >= 7 * 86400:
            st["notified"] = "NONE"
    else:
        st["calm_since"] = None

    # outbox — 낮 시간에만 새로 만든다(밤 발생분은 아침 첫 실행에서 자연 발송)
    ob = st.get("outbox")
    if ob and now - ob["created"] > cfg["outbox_ttl_hours"] * 3600:
        st["outbox"] = ob = None
    day = is_day(now, cfg)
    if not seeding and day and J.RANK[level] >= 2 and J.RANK[level] > J.RANK[st.get("notified", "NONE")]:
        text = J.render(level, fams, act, now, cfg, note)
        st["outbox"] = {"id": J.eid("alert", level, now), "text": text, "created": now, "kind": "alert",
                        "level": level}
        st["notified"] = level
    month = dt.datetime.fromtimestamp(now, J.KST).strftime("%Y-%m")
    if (not seeding and day and st.get("heartbeat") != month and not st.get("outbox")
            and dt.datetime.fromtimestamp(now, J.KST).day <= 7):
        n_ok = sum(1 for s in CORE_SOURCES if st["status"].get(s, {}).get("ok"))
        st["outbox"] = {"id": J.eid("hb", month), "text": J.render_heartbeat(now, cfg, level, n_ok,
                                                                               len(CORE_SOURCES)),
                        "created": now, "kind": "heartbeat", "level": level}
        st["heartbeat"] = month
    if seeding:
        st["seeded"] = True
        st["heartbeat"] = st.get("heartbeat") or month  # 첫 달 생존신고 생략

    latest = {
        "generated_at": now,
        "level": level, "families": fams, "undecidable": undecidable, "note": note,
        "outbox": st.get("outbox"),
        "status": {k: v.get("ok") for k, v in st["status"].items()},
    }
    dash = {
        "generated_at": now, "level": level, "families": fams, "note": note,
        "events": sorted(act, key=lambda e: -e["ts"])[:60],
        "recent_all": sorted(st["events"], key=lambda e: -e["ts"])[:120],
        "rh_supply_hist": st["rh_supply_hist"], "lb_hist": st["lb_hist"][-400:],
        "status": st["status"], "cursors": st["cursors"],
        "docs": {u: {"hash": v.get("hash"), "changed": v.get("changed")} for u, v in st["docs"].items()},
    }
    print(f"[판정] {level} {fams} · 신규 이벤트 {len(fresh)} · 반증 {refuted} · 핵심원천 {core_ok}/6"
          + (" · 시딩" if seeding else "") + (" · 낮" if day else " · 밤"))
    if st.get("outbox"):
        print("[outbox]\n" + st["outbox"]["text"])
    if not dry:
        save_json(st_path, st)
        # 매 실행 기록한다 — 릴레이가 generated_at으로 수집 정지를 감지한다
        save_json(os.path.join(DATA, "latest.json"), latest)
        save_json(os.path.join(DOCS, "data.json"), dash)
    if st["fail_streak"] >= 2:
        print(f"::error::핵심 원천 연속 {st['fail_streak']}회 판정 불가")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
