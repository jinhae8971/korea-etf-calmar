# -*- coding: utf-8 -*-
"""
watchlist 트랙 설치 패치 — 몇 번 실행해도 결과가 같다(멱등).
레포 루트에서: python hood_radar/apply_watchlist.py
"""
import io
import json
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
CFG = os.path.join(BASE, "config.json")
MAIN = os.path.join(BASE, "hood_radar.py")

WATCHLIST_CFG = {
    "watchlist_enabled": True,
    "watchlist": [
        {"symbol": "INDEX", "address": "0x56910d4409f3a0c78c64dd8d0545ff0705389870",
         "slugs": ["the-index"], "track": "protocol", "profile": "index",
         "thresholds": {"wl_rev_noise_floor_usd": 5000.0},
         "note": "The Index — 매출 소액, 절대금액 기준으로 본다"},
        {"symbol": "PONS", "address": "0x39dbed3a2bd333467115de45665cc57f813c4571",
         "slugs": ["pons-v1", "pons-v2"], "track": "protocol", "profile": "launchpad",
         "thresholds": {"wl_liq_mcap_min_pct": 2.0},
         "note": "Pons V1+V2 합산 — 회전율이 높아 유동성 비율 임계를 낮춘다"},
        {"symbol": "CASHCAT", "address": "0x020bfc650a365f8bb26819deaabf3e21291018b4",
         "slugs": [], "track": "meme", "profile": "meme",
         "thresholds": {"wl_liq_mcap_min_pct": 2.5},
         "note": "Cash Cat — 매출 없음, 관심 점유율로 본다"},
    ],
    "wl_price_drop_6h_pct": -15.0,
    "wl_price_drop_24h_pct": -25.0,
    "wl_liq_drain_pct": -25.0,
    "wl_liq_mcap_min_pct": 3.0,
    "wl_rev_collapse_pct": -50.0,
    "wl_rev_surge_pct": 100.0,
    "wl_pf_move_pct": 25.0,
    "wl_share_drop_pp": 8.0,
    "wl_rank_move": 5,
    "wl_history_max": 400,
    "wl_cooldown_hours": 6.0,
    # 런치패드(PONS)
    "wl_share_drop_pp_6h": 10.0,
    "wl_rival_rise_pp": 10.0,
    "wl_issuance_drop_pct": -50.0,
    "wl_issuance_surge_pct": 100.0,
    # 소형 프로토콜(INDEX)
    "wl_index_rev_floor_usd": 3000.0,
    "wl_liq_floor_usd": 1500000.0,
    "wl_pf_premium_x": 2.5,
    "wl_rev_noise_floor_usd": 0.0,
    # 밈(CASHCAT)
    "wl_attn_drop_pct": -40.0,
    "wl_meme_turnover_min_pct": 5.0,
}

IMPORT_OLD = "import security          # noqa: E402"
IMPORT_NEW = "import security          # noqa: E402\nimport watchlist         # noqa: E402"

BUILD_ANCHOR = "    spark = {}\n"
BUILD_BLOCK = '''    # ---- 보유 종목 정밀 감시 (밈·프로토콜 두 트랙을 가로지른다) ----
    wl_hist_path = os.path.join(DATA_DIR, "watchlist_history.json")
    if cfg.get("watchlist_enabled", True):
        try:
            wl_hist = read_json(wl_hist_path, [])
            wstate = watchlist.build(cfg, rows=rows, protocol_payload=payload.get("protocol"))
            walerts = watchlist.evaluate(wstate, wl_hist, cfg, int(now.timestamp()))
            wl_hist.append(watchlist.snapshot(wstate, int(now.timestamp())))
            wl_hist.sort(key=lambda s: s.get("epoch") or 0)
            wl_hist = wl_hist[-int(cfg.get("wl_history_max", 400)):]
            write_json_if_changed(wl_hist_path, wl_hist)
            payload["watchlist"] = wstate
            payload["watchlist_alerts"] = walerts
        except Exception as exc:
            # 보유 섹션이 본 브리프를 인질로 잡지 않는다
            print("[watchlist] 실패(본 브리프는 정상 진행): %s" % exc)
            payload["watchlist"] = None
            payload["watchlist_alerts"] = []

'''

RENDER_ANCHOR = '    lines.append("<b>시총 TOP %d</b>" % min(cfg["top_n_telegram"], len(rows)))'
RENDER_BLOCK = '''    wl_lines = watchlist.render_telegram(
        payload.get("watchlist"), payload.get("watchlist_alerts") or [], cfg)
    if wl_lines:
        # 보유 섹션은 맨 위 — 길이 초과 시 잘려나가는 쪽은 순위표여야 한다
        lines.extend(wl_lines)

'''


def patch_config():
    with io.open(CFG, "r", encoding="utf-8") as fh:
        cfg = json.load(fh)
    changed = False
    for k, v in WATCHLIST_CFG.items():
        if k not in cfg:
            cfg[k] = v
            changed = True
    if cfg.get("version") != "2.3":
        cfg["version"] = "2.3"
        changed = True
    if changed:
        with io.open(CFG, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, ensure_ascii=False, indent=1)
    print("config.json: %s" % ("갱신" if changed else "변경 없음"))


def patch_main():
    with io.open(MAIN, "r", encoding="utf-8") as fh:
        src = fh.read()
    orig = src
    if "import watchlist" not in src:
        src = src.replace(IMPORT_OLD, IMPORT_NEW, 1)
    if 'payload["watchlist"] = wstate' not in src:
        if BUILD_ANCHOR not in src:
            print("!! 앵커(spark) 없음 — 수동 확인 필요", file=sys.stderr)
            return 1
        src = src.replace(BUILD_ANCHOR, BUILD_BLOCK + BUILD_ANCHOR, 1)
    if "watchlist.render_telegram" not in src:
        if RENDER_ANCHOR not in src:
            print("!! 앵커(시총 TOP) 없음 — 수동 확인 필요", file=sys.stderr)
            return 1
        src = src.replace(RENDER_ANCHOR, RENDER_BLOCK + RENDER_ANCHOR, 1)
    if src != orig:
        with io.open(MAIN, "w", encoding="utf-8") as fh:
            fh.write(src)
        print("hood_radar.py: 갱신")
    else:
        print("hood_radar.py: 변경 없음")
    return 0


if __name__ == "__main__":
    patch_config()
    sys.exit(patch_main())
