#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
칼마비율 통합 허브 (Calmar Hub)
================================
4개 유니버스(한국ETF·코스피200·S&P500·미국ETF)의 산출을 오케스트레이션하여
  1) 텔레그램 메시지 '1건'으로 압축 발송 (유니버스별 TOP 3만)
  2) GitHub Pages 대시보드용 데이터(docs/data/*.json) 생성
  3) 기존 snapshot_history*.json 갱신 (기존 계약 그대로 유지)

설계 원칙
---------
- 계산 로직은 각 모듈의 compute_top10() 을 그대로 재사용한다. (중복 구현 금지)
- 각 모듈은 여전히 단독 실행 가능하다. (`python calmar_etf.py` → 개별 메시지)
  허브는 모듈의 main() 이 아니라 compute_top10() 만 호출하므로 발송이 중복되지 않는다.
- 한 유니버스가 실패해도 나머지는 계속 진행한다. (기존 continue-on-error 동작 계승)

사용법
------
  python calmar_hub.py              # 산출 → 텔레그램 1건 발송 → docs 데이터 생성
  python calmar_hub.py --dry-run    # 발송 없이 메시지/데이터만 생성 (콘솔 출력)
"""
import os
import sys
import json
import html
import importlib
import datetime
import traceback
import urllib.parse
import urllib.request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOCS_DATA_DIR = os.path.join(BASE_DIR, "docs", "data")
DASHBOARD_URL = "https://jinhae8971.github.io/korea-etf-calmar/"
TELEGRAM_LIMIT = 4096
KST = datetime.timezone(datetime.timedelta(hours=9))

# ── 유니버스 레지스트리 ────────────────────────────────────────────────────────
# module    : compute_top10 / load_history / weekly_diff / save_history 를 제공하는 모듈
# display   : 텔레그램 한 줄 표기 방식
#   name        → 종목명만                (코스피200)
#   name_family → 종목명 (운용사)          (한국 ETF)
#   name_code   → 종목명 (티커)            (S&P500)
#   code_name   → 티커 종목명              (미국 ETF)
UNIVERSES = [
    {"id": "kr_etf",   "label": "한국 ETF",  "icon": "🇰🇷", "module": "calmar_etf",
     "currency": "KRW", "display": "name_family"},
    {"id": "kospi200", "label": "코스피200", "icon": "🏛",  "module": "kospi200_calmar",
     "currency": "KRW", "display": "name"},
    {"id": "sp500",    "label": "S&P500",   "icon": "🇺🇸", "module": "sp500_calmar",
     "currency": "USD", "display": "name_code"},
    {"id": "us_etf",   "label": "미국 ETF",  "icon": "🗽",  "module": "us_etf_calmar",
     "currency": "USD", "display": "code_name"},
]

MEDAL = {1: "🥇", 2: "🥈", 3: "🥉"}
TOP_N_MESSAGE = 3          # 텔레그램에 노출할 상위 개수
TOP_N_DASHBOARD = 10       # 대시보드에 노출할 상위 개수


# ── config / telegram ─────────────────────────────────────────────────────────
def load_config() -> dict:
    cfg = {
        "telegram_token": os.environ.get("TELEGRAM_TOKEN", ""),
        "telegram_chat_id": os.environ.get("TELEGRAM_CHAT_ID", ""),
    }
    path = os.path.join(BASE_DIR, "config.json")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            for k, v in json.load(f).items():
                if not cfg.get(k):
                    cfg[k] = v
    return cfg


def send_telegram(msg: str, token: str, chat_id: str) -> None:
    data = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": msg,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage", data=data)
    with urllib.request.urlopen(req, timeout=20) as r:
        r.read()


# ── 산출 ──────────────────────────────────────────────────────────────────────
def run_universe(spec: dict) -> dict:
    """단일 유니버스 산출. 실패해도 예외를 삼키고 status=error 로 반환한다."""
    out = {
        "id": spec["id"], "label": spec["label"], "icon": spec["icon"],
        "currency": spec["currency"], "display": spec["display"],
        "status": "error", "error": None, "rows": [], "diff": None,
        "base_date": None, "last_date": None,
    }
    try:
        mod = importlib.import_module(spec["module"])
        top10, today_str = mod.compute_top10()
        if not top10:
            raise RuntimeError("산출된 종목이 없습니다 (데이터 수집 확인 필요)")

        history = mod.load_history()
        diff = mod.weekly_diff(history, today_str, top10)
        mod.save_history(history, today_str, top10)   # 기존 스냅샷 계약 유지

        out.update({
            "status": "ok",
            "today": today_str,
            "base_date": top10[0].get("base_date"),
            "last_date": top10[0].get("last_date"),
            "diff": diff,
            "rows": [{
                "rank": i,
                "code": r.get("code", ""),
                "name": r.get("name", ""),
                "family": r.get("family", ""),
                "calmar": round(r["calmar"], 2),
                "ret": round(r["ret"], 4),
                "mdd": round(r["mdd"], 4),
            } for i, r in enumerate(top10[:TOP_N_DASHBOARD], 1)],
        })
        print(f"  [OK] {spec['label']}: {len(out['rows'])}종목 "
              f"(1위 {out['rows'][0]['name']} {out['rows'][0]['calmar']})")
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
        print(f"  [FAIL] {spec['label']}: {out['error']}")
        traceback.print_exc()
    return out


# ── 텔레그램 메시지 ────────────────────────────────────────────────────────────
def fmt_date(s):
    return f"{s[:4]}-{s[4:6]}-{s[6:]}" if s and len(s) == 8 else (s or "-")


def label_of(row: dict, display: str) -> str:
    name = html.escape(row["name"])
    code = html.escape(row["code"])
    family = html.escape(row.get("family") or "")
    if display == "name_family" and family and family != "-":
        return f"{name} <i>({family})</i>"
    if display == "name_code":
        return f"{name} <i>({code})</i>"
    if display == "code_name":
        return f"<b>{code}</b> {name}"
    return name


def compact_diff(diff: dict, name_cap: int = 14) -> str:
    """TOP10 주간 진입/이탈을 한 줄로 압축.
    대표 1건(=최상위 진입/이탈 종목)만 노출하고 나머지는 '외 N'. 전체는 대시보드에서 확인."""
    if not diff:
        return ""
    def squash(names):
        head = names[0]
        if len(head) > name_cap:
            head = head[:name_cap] + "…"
        head = html.escape(head)
        rest = len(names) - 1
        return head + (f" 외 {rest}" if rest > 0 else "")
    parts = []
    if diff.get("entered"):
        parts.append("🆕 " + squash(diff["entered"]))
    if diff.get("exited"):
        parts.append("🔻 " + squash(diff["exited"]))
    return "  ·  ".join(parts)


def build_message(results: list) -> str:
    ok = [u for u in results if u["status"] == "ok"]
    ref = ok[0] if ok else None
    lines = ["<b>📊 칼마비율 TOP 3 · 4대 유니버스</b>"]
    if ref:
        lines.append(f"<i>롤링 6M · {fmt_date(ref['base_date'])} → "
                     f"{fmt_date(ref['last_date'])}</i>")
    lines.append("")

    for u in results:
        cur = " <i>(USD)</i>" if u["currency"] == "USD" else ""
        lines.append(f"{u['icon']} <b>{html.escape(u['label'])}</b>{cur}")
        if u["status"] != "ok":
            lines.append("   ⚠️ 산출 실패 — 대시보드에서 상세 확인")
            lines.append("")
            continue
        for r in u["rows"][:TOP_N_MESSAGE]:
            lines.append(f"{MEDAL.get(r['rank'], str(r['rank']) + '.')} "
                         f"{label_of(r, u['display'])}  ·  <b>{r['calmar']:.2f}</b>")
        d = compact_diff(u["diff"])
        if d:
            lines.append(f"   <i>{d}</i>")
        lines.append("")

    lines.append(f'🔗 <a href="{DASHBOARD_URL}">TOP 10 전체 · 수익률 · MDD · 순위추이 →</a>')
    msg = "\n".join(lines)

    if len(msg) > TELEGRAM_LIMIT:   # 방어: 초과 시 diff 줄 제거 후 재구성
        msg = "\n".join(l for l in lines if not l.strip().startswith("<i>🆕")) 
        msg = msg[:TELEGRAM_LIMIT - 60].rsplit("\n", 1)[0] + \
            f'\n\n🔗 <a href="{DASHBOARD_URL}">전체 보기 →</a>'
    return msg


# ── 대시보드 데이터 ────────────────────────────────────────────────────────────
def write_dashboard_data(results: list) -> None:
    os.makedirs(DOCS_DATA_DIR, exist_ok=True)
    now = datetime.datetime.now(KST)

    latest = {
        "generated_at": now.isoformat(timespec="seconds"),
        "window": "롤링 6M (182일)",
        "metric": "칼마비율 = 연율화 수익률 ÷ |MDD|",
        "universes": [{
            "id": u["id"], "label": u["label"], "icon": u["icon"],
            "currency": u["currency"], "display": u["display"],
            "status": u["status"], "error": u["error"],
            "base_date": u["base_date"], "last_date": u["last_date"],
            "diff": u["diff"], "rows": u["rows"],
        } for u in results],
    }
    with open(os.path.join(DOCS_DATA_DIR, "latest.json"), "w", encoding="utf-8") as f:
        json.dump(latest, f, ensure_ascii=False, indent=1)

    # 순위 추이용 히스토리 병합 (각 모듈의 snapshot_history*.json 재사용)
    history = {"generated_at": latest["generated_at"], "universes": {}}
    for spec in UNIVERSES:
        try:
            mod = importlib.import_module(spec["module"])
            history["universes"][spec["id"]] = mod.load_history()
        except Exception as e:
            print(f"  [history] {spec['label']}: {e}")
            history["universes"][spec["id"]] = []
    with open(os.path.join(DOCS_DATA_DIR, "history.json"), "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=1)

    print(f"  [docs] latest.json / history.json 갱신 완료")


# ── main ──────────────────────────────────────────────────────────────────────
def main():
    dry = "--dry-run" in sys.argv
    cfg = load_config()

    print("■ 칼마 통합 허브 시작")
    results = [run_universe(spec) for spec in UNIVERSES]

    msg = build_message(results)
    print("\n" + "─" * 60 + "\n" + msg + "\n" + "─" * 60)
    print(f"[len] {len(msg)} chars\n")

    write_dashboard_data(results)

    ok_count = sum(1 for u in results if u["status"] == "ok")
    if ok_count == 0:
        raise RuntimeError("4개 유니버스 전부 산출 실패 — 발송하지 않습니다.")

    if dry:
        print("[dry-run] 텔레그램 발송 생략.")
    else:
        if not (cfg["telegram_token"] and cfg["telegram_chat_id"]):
            raise RuntimeError("TELEGRAM_TOKEN / TELEGRAM_CHAT_ID 가 설정되지 않았습니다.")
        send_telegram(msg, cfg["telegram_token"], cfg["telegram_chat_id"])
        print("[telegram] 통합 메시지 1건 발송 완료.")

    # 부분 실패는 exit 0 을 유지한다.
    #   이유: 통합 메시지 본문에 이미 "⚠️ 산출 실패" 가 인라인으로 표시되므로,
    #   여기서 exit 1 을 내면 워크플로우의 실패 알림이 '두 번째' 텔레그램을 쏜다.
    #   → 메시지 1건 원칙이 깨진다. 대신 Actions 로그에 경고 애노테이션을 남긴다.
    failed = [u["label"] for u in results if u["status"] != "ok"]
    if failed:
        print(f"::warning title=칼마 부분 실패::{', '.join(failed)} 산출 실패 "
              f"({ok_count}/{len(results)} 성공) — 메시지는 정상 발송됨")
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with open(summary, "a", encoding="utf-8") as f:
                f.write(f"### ⚠️ 부분 실패 {ok_count}/{len(results)}\n"
                        f"- 실패: {', '.join(failed)}\n")
        print(f"[warn] 부분 실패: {', '.join(failed)} — 통합 메시지는 발송 완료")
        return
    print(f"[done] {ok_count}/{len(results)} 성공")


if __name__ == "__main__":
    main()
