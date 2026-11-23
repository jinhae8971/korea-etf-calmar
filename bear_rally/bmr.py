#!/usr/bin/env python3
"""베어랠리 레이더 — 수집 → 판정(engine) → 차트 → 리포트·텔레그램 본문 → data/latest.json.

발송은 하지 않는다. Brief Hub(@spytltgld_strategy_bot)가 latest.json의 message·photos를 읽어 보낸다.

  python bear_rally/bmr.py            # 새 확정 봉이 없으면 아무것도 쓰지 않고 끝냄(멱등)
  python bear_rally/bmr.py --force    # 강제 재계산
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import breadth  # noqa: E402
import engine  # noqa: E402
import report  # noqa: E402
import sources  # noqa: E402

KST = timezone(timedelta(hours=9))
ROOT = os.path.dirname(HERE)
DATA = os.path.join(HERE, "data")
DOCS = os.path.join(ROOT, "docs", "bear-rally")
LATEST = os.path.join(DATA, "latest.json")
EVENTS = os.path.join(DATA, "events.json")
SHOW_EVENTS = {"bear_enter", "rally", "bear_rally", "shift_fail", "shift", "bull", "new_low", "rearm", "zbt_fire", "zbt_near"}
HITS_STOCK = ["rsi_ceil", "ma50", "ma200", "avwap", "fib"]


def log(m: str) -> None:
    print(m, flush=True)


def load(path: str, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def save(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
        f.write("\n")
    os.replace(tmp, path)


def zbt_state(r: dict) -> str:
    if r.get("zbt_recent"):
        return f"VALID {r['zbt_since']}봉 전"
    if r.get("zbt_active"):
        return "셋업 진행"
    if r.get("zbt_near_rec"):
        return "근접 실패"
    return "대기"


def summarize(a: dict, bars: list, res: dict, prev_last: str | None) -> dict:
    rows, p = res["rows"], res["params"]
    r = rows[-1]
    st = r["state"]
    since = rows[0]["d"]
    for i in range(len(rows) - 1, 0, -1):
        if rows[i - 1]["state"] != st:
            since = rows[i]["d"]
            break
    is_crypto = res["mode"] == "crypto"
    hi63 = max(b["h"] for b in bars[-p["dd_len"]:])
    dd63 = r["c"] / hi63 - 1
    dd_min = (p["dd_crypto"] if is_crypto else p["dd_stock"]) / 100
    gap = (r["c"] / r["ma200"] - 1) if r["ma200"] else None
    ma50_gap = (r["ma50"] / r["ma200"] - 1) if (r["ma50"] and r["ma200"]) else None
    rally_pct = (r["c"] / r["rally_base"] - 1) if r["rally_base"] else None
    out = {
        "key": a["key"], "name": a["name"], "market": a["market"], "mode": res["mode"],
        "state": st, "state_ko": engine.STATE_KO[st], "state_since": since,
        "last_date": r["d"], "close": r["c"], "patched": bool(bars[-1].get("patched")),
        "ma200": r["ma200"], "ma200_gap": gap, "ma50_gap": ma50_gap, "ma_fall": r["ma_fall"], "ma50_up": r["ma50_up"],
        "dd_peak": r["dd_peak"], "dd63": dd63, "dd_len": p["dd_len"], "dd_min_pct": dd_min * 100,
        "rsi": r["rsi"] or 0.0, "avwap": r["avwap"],
        "w_line": r["w_line"], "w_bot": r["w_bot"], "w_close": r["w_close"], "w_up": r["w_up"], "hold_weeks": p["hold_weeks"],
        "rally_base": r["rally_base"], "rally_pct": rally_pct, "rally_min_pct": res["rally_min"] * 100,
        "rally_trigger": r["rally_base"] * (1 + res["rally_min"]) if (st == engine.S_BEAR and r["rally_base"]) else None,
        "fib_ratio": r["fib_ratio"],
        "hits": [k for k, v in r["hits"].items() if v], "bear_hits": r["bear_hits"], "hits_max": res["hits_max"],
        "min_hits": p["min_hits"],
        "hits_all": (HITS_STOCK[:3] + ["band"] + HITS_STOCK[3:]) if is_crypto
        else HITS_STOCK + (["narrow", "zbt_near"] if res["use_breadth"] else []),
        "evid": [k for k, v in r["evid"].items() if v], "shift_score": r["shift_score"],
        "shift_max": res["shift_max"], "shift_min": res["shift_min"],
        "evid_all": (["zbt", "breadth"] if res["use_breadth"] else []) + ["rsi_shift", "weekly", "ma_flat", "reclaim"],
        "breadth_on": res["use_breadth"], "br200": r["br200"], "br50": r["br50"], "zbt": r["zbt"], "zbt_state": zbt_state(r),
        "cond": {"dd": dd63 <= -dd_min, "slope_or_cross": bool(r["ma_fall"] or (ma50_gap is not None and ma50_gap < 0)),
                 "weekly_below_band": bool(r["w_close"] is not None and r["w_bot"] is not None and r["w_close"] < r["w_bot"])},
    }
    if st == engine.S_RALLY and r["rally_high"] and r["atr"]:
        out["turn_level"] = r["rally_high"] - p["rej_atr"] * r["atr"]
        cands = [("200일선", r["ma200"]), ("고점 AVWAP", r["avwap"]), ("50일선", r["ma50"]),
                 ("피보 38.2%", r["fib382"]), ("피보 61.8%", r["fib618"])]
        if is_crypto:
            cands.append(("BMSB 하단", r["w_bot"]))
        above = sorted((v, n) for n, v in cands if v is not None and v > r["c"])
        if above:
            out["res_level"], out["res_name"] = above[0]
    if st == engine.S_FAILED and r["rally_high"] and r["rally_base"]:
        out["rearm_level"] = r["rally_base"] + (r["rally_high"] - r["rally_base"]) * (1 - p["rearm_frac"] / 100)
    evs = [e for e in res["events"] if e["type"] in SHOW_EVENTS]
    out["events_recent"] = [slim(e) for e in evs[-12:]]
    if prev_last:
        ch = [e for e in evs if e["d"] > prev_last]
    else:
        ch = [e for e in evs if e["d"] == r["d"]]
    out["changes"] = [slim(e) for e in ch]
    return out


def slim(e: dict) -> dict:
    return {"d": e["d"], "type": e["type"], "state": e["state"], "c": e["c"], "hits": e["hits"], "evid": e["evid"],
            "shift_score": e["shift_score"], "bear_hits": e["bear_hits"],
            "rally_pct": e["rally_pct"], "fib_ratio": e["fib_ratio"]}


def basis_text(assets: list) -> str:
    def md(s):
        return report.md(s)
    parts = []
    us = [a for a in assets if a.get("market") == "us" and not a.get("error")]
    kr = [a for a in assets if a.get("market") == "kr" and not a.get("error")]
    cr = [a for a in assets if a.get("market") == "crypto" and not a.get("error")]
    if us:
        parts.append(f"미국 {md(max(a['last_date'] for a in us))}")
    if kr:
        parts.append(f"코스피 {md(max(a['last_date'] for a in kr))} 종가")
    if cr:
        parts.append(f"크립토 {md(max(a['last_date'] for a in cr))} 일봉")
    return " · ".join(parts) + " 기준"


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--now", help="KST 시각 (테스트용) 예: 2026-11-24T09:10")
    ap.add_argument("--no-charts", action="store_true")
    args = ap.parse_args(argv)
    now = datetime.fromisoformat(args.now).replace(tzinfo=KST) if args.now else datetime.now(KST)
    cfg = load(os.path.join(HERE, "settings.json"), {})
    prev = load(LATEST, {})
    prev_keys = prev.get("data_key") or {}

    fetched, errors = {}, {}
    for a in cfg["assets"]:
        try:
            bars, src = sources.fetch_asset(a, now)
            fetched[a["key"]] = (bars, src)
            log(f"[fetch] {a['key']:<6} {src:<13} {len(bars)}봉 · 마지막 {bars[-1]['d']}{' (보강)' if bars[-1].get('patched') else ''}")
        except Exception as e:  # noqa: BLE001
            errors[a["key"]] = str(e)[-160:]
            log(f"[fetch] {a['key']} 실패: {e}")
    if not fetched:
        log("::error::모든 자산 수집 실패")
        return 1

    data_key = {k: v[0][-1]["d"] for k, v in fetched.items()}
    if not args.force and prev_keys and data_key == prev_keys and not errors:
        log(f"새 확정 봉 없음 — 쓰기 생략 ({data_key})")
        return 0

    notes = []
    series, bmeta = [], {"universe": len(breadth.UNIVERSE), "last_date": None, "mode": None}
    if any(a.get("breadth") for a in cfg["assets"]):
        try:
            br = breadth.update(now)
            series, bmeta["mode"] = br["rows"], br["mode"]
            if br["received"] < len(breadth.UNIVERSE) * breadth.MIN_COVERAGE:
                notes.append(f"미국 브레드스 수신 {br['received']}/{len(breadth.UNIVERSE)}종 — 일부 종목 결측")
        except Exception as e:  # noqa: BLE001
            log(f"[breadth] 갱신 실패 — 저장된 시계열 사용: {e}")
            series = breadth._load(breadth.SERIES_PATH, {}).get("rows", [])
            notes.append("미국 브레드스 갱신 실패 — 저장된 값 사용")

    assets, results = [], []
    for a in cfg["assets"]:
        if a["key"] in errors:
            assets.append({"key": a["key"], "name": a["name"], "market": a["market"], "error": errors[a["key"]], "changes": []})
            continue
        bars, src = fetched[a["key"]]
        br = None
        if a.get("breadth") and series:
            br = breadth.align(series, [b["d"] for b in bars])
            bmeta["last_date"] = br["last_date"]
            if br["last_date"] is None:
                br = None
        res = engine.run(bars, a.get("mode", "stock"), {**cfg.get("params", {}), **a.get("params", {})}, br)
        s = summarize(a, bars, res, prev_keys.get(a["key"]))
        s["src"] = src
        assets.append(s)
        results.append((a["key"], res))
    if bmeta["last_date"]:
        us_last = max((x["last_date"] for x in assets if x.get("market") == "us" and not x.get("error")), default=None)
        if us_last and bmeta["last_date"] < us_last:
            notes.append(f"미국 브레드스는 {report.md(bmeta['last_date'])} 기준 (원천 하루 지연 — 직전 값 유지)")
    for k, msg in errors.items():
        notes.append(f"{k} 수집 실패 — 판정 불가")

    # ---- 차트
    os.makedirs(os.path.join(DOCS, "img"), exist_ok=True)
    chart_ok = False
    if not args.no_charts:
        try:
            import charts
            for key, res in results:
                charts.asset_chart(key, res, os.path.join(DOCS, "img", f"{key}.png"))
            charts.overview_chart(results, os.path.join(DOCS, "img", "overview.png"))
            chart_ok = True
        except Exception as e:  # noqa: BLE001
            log(f"[chart] 실패 — 차트 없이 진행: {e}")
            traceback.print_exc()
    for s in assets:
        s["chart"] = f"img/{s['key']}.png"

    # 허브 신선도 기준일 — 크립토 일봉(UTC)은 다음날 09:00 KST에 닫히므로 '마지막 크립토 봉 + 1일'.
    # 09시 전에 돈 계산(크립토 봉 미확정)은 어제 날짜가 되어 허브가 오늘 것으로 보내지 않는다.
    cr_last = [s["last_date"] for s in assets if s.get("market") == "crypto" and not s.get("error")]
    hub_date = ((datetime.fromisoformat(min(cr_last)) + timedelta(days=1)).strftime("%Y-%m-%d")
                if cr_last else now.strftime("%Y-%m-%d"))
    snap = {
        "as_of_kst": now.strftime("%Y-%m-%d %H:%M"), "as_of_date": now.strftime("%Y-%m-%d"), "hub_date": hub_date,
        "basis": basis_text(assets), "pages_url": cfg["pages_url"], "data_key": data_key,
        "assets": assets, "overview": "img/overview.png", "breadth": bmeta, "notes": notes,
        "data_status": "OK" if not errors else "DEGRADED", "failed": sorted(errors),
    }
    snap["message"] = report.telegram(snap)
    changed = any(s.get("changes") for s in assets)
    first_days = cfg.get("photo_until") and hub_date <= cfg["photo_until"]      # 도입 초기엔 매일 첨부
    if chart_ok and (changed or now.weekday() == 0 or not prev or first_days):
        snap["photos"] = ["docs/bear-rally/img/overview.png"]
        snap["photo_caption"] = "베어랠리 레이더 · 최근 12개월 상태 (자산별 차트는 리포트)"

    with open(os.path.join(DOCS, "index.html"), "w", encoding="utf-8") as f:
        f.write(report.page(snap))
    save(LATEST, snap)
    save(EVENTS, {"as_of_kst": snap["as_of_kst"],
                  "events": {k: [slim(e) for e in res["events"] if e["type"] in SHOW_EVENTS] for k, res in results}})
    log(f"[done] {snap['as_of_kst']} · {snap['basis']} · 변화 {sum(len(s.get('changes', [])) for s in assets)}건"
        f" · 본문 {len(snap['message'])}자 · 사진 {len(snap.get('photos', []))}장")
    print("-----\n" + snap["message"])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
