#!/usr/bin/env python3
"""catalyst-radar — 시총 100위 체인·프로토콜의 '예고된 미래 이벤트'를 D-day 로 관리한다.

기존 13종 자동화(가격·TVL 후행 관측기)를 수정하지 않는 독립 트랙.
예측하지 않는다. 이미 날짜가 확정된 이벤트를 남보다 먼저 아는 것만 한다.
"""
import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from net import STATS                                    # noqa: E402
from sensors import CERTAINTY, IMPACT, STAGE_ORDER, collect, dedupe_releases  # noqa: E402
from universe import build_universe                      # noqa: E402
import verify                                            # noqa: E402

KST = dt.timezone(dt.timedelta(hours=9))
HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
DOCS = os.path.abspath(os.path.join(HERE, "..", "docs", "catalyst-radar"))

GRADE_A, GRADE_B = 45.0, 25.0
STALE_DAYS = 5          # 재관측 끊긴 이벤트 만료 기한
COVERAGE_MIN = 0.70


# ── 저장 ───────────────────────────────────────────────────────────────
def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


# ── 점수 ───────────────────────────────────────────────────────────────
def proximity(when, today):
    """D-day 근접도. 날짜 미상은 중립 0.55, 지난 이벤트는 급감."""
    if not when:
        return 0.55
    try:
        d = dt.date.fromisoformat(str(when)[:10])
    except ValueError:
        return 0.55
    days = (d - today).days
    if days < -14:
        return 0.10
    if days < 0:
        return 0.45                      # 막 지난 이벤트 — 사후 확인 가치
    if days <= 7:
        return 1.00
    if days <= 30:
        return 0.85
    if days <= 90:
        return 0.60
    return 0.35


def score_event(ev, today):
    """임팩트 × 확실성 × 근접도 × 출처신뢰.

    출처신뢰를 넣기 전 실측에서는 2차 보도(뉴스)가 점수 상위를 전부 덮었다.
    1차 원천(레포·투표)과 기사를 같은 무게로 두지 않는다.
    """
    return round(100.0 * IMPACT.get(ev["impact"], 0.25)
                 * CERTAINTY.get(ev["stage"], 0.3)
                 * proximity(ev.get("event_date") or ev.get("when"), today)
                 * effective_trust(ev), 1)


def effective_trust(ev):
    """확정 일자가 붙은 보도는 논평 기사보다 신뢰도가 높다.

    2차 보도를 일괄 할인하면(trust 0.35) SOL 의 9/9 Transaction V1 처럼
    날짜가 못박힌 공지까지 C 등급으로 묻힌다 — 실측에서 확인.
    """
    t = float(ev.get("trust", 0.5))
    if ev.get("event_date"):
        t = min(1.0, t * 1.6)
    return t


def grade(score):
    return "A" if score >= GRADE_A else ("B" if score >= GRADE_B else "C")


# ── 상태 전이 ──────────────────────────────────────────────────────────
def merge_state(prev, events, today, price_by_symbol):
    """이전 상태와 대조해 NEW / STAGE_UP 을 판정하고 사전등록을 남긴다."""
    state = {e["key"]: dict(e) for e in prev.get("events", [])}
    transitions = []
    now = today.isoformat()

    for ev in events:
        key = ev["key"]
        ev["score"] = score_event(ev, today)
        ev["grade"] = grade(ev["score"])
        old = state.get(key)
        if old is None:
            ev["first_seen"] = now
            ev["stage_history"] = [{"stage": ev["stage"], "at": now}]
            # 사전등록: 탐지 시점 가격을 박아둔다. 사후에 임계를 만질 수 없게 한다.
            ev["registry"] = {"detected_at": now,
                              "price_at_detect": price_by_symbol.get(ev["symbol"]),
                              "btc_at_detect": price_by_symbol.get(verify.BENCH),
                              "score_at_detect": ev["score"]}
            transitions.append(dict(ev, transition="NEW"))
        else:
            ev["first_seen"] = old.get("first_seen", now)
            ev["stage_history"] = old.get("stage_history", [])
            ev["registry"] = old.get("registry")
            oi, ni = _stage_idx(old.get("stage")), _stage_idx(ev["stage"])
            if ni > oi:
                ev["stage_history"] = ev["stage_history"] + [{"stage": ev["stage"], "at": now}]
                transitions.append(dict(ev, transition="STAGE_UP",
                                        prev_stage=old.get("stage")))
        state[key] = ev

    # 만료 규칙 2종
    #  ① 이번 수집에서 재관측되지 않은 이벤트는 STALE_DAYS 후 제거
    #     (뉴스 필터를 강화해도 과거에 저장된 오탐이 남는 문제를 실측에서 확인)
    #  ② 등급 C 이면서 90일 이상 갱신 없는 항목 정리(파일 비대 방지)
    seen_now = {e["key"] for e in events}
    stale_cut = (today - dt.timedelta(days=STALE_DAYS)).isoformat()
    old_cut = (today - dt.timedelta(days=90)).isoformat()
    kept = []
    for v in state.values():
        if v["key"] in seen_now:
            v["last_seen"] = now
            kept.append(v)
            continue
        if v.get("last_seen", v.get("first_seen", now)) < stale_cut:
            continue                       # 재관측 끊김 → 만료
        if v.get("grade") == "C" and v.get("first_seen", now) < old_cut:
            continue
        kept.append(v)
    return kept, transitions


def _stage_idx(stage):
    return STAGE_ORDER.index(stage) if stage in STAGE_ORDER else -1


# ── 렌더 ───────────────────────────────────────────────────────────────
STAGE_KR = {"PROPOSED": "제안", "REVIEW": "검토", "VOTING": "투표중",
            "PASSED": "가결", "SCHEDULED": "일정확정", "ACTIVATED": "활성화"}
IMPACT_KR = {"SUPPLY": "공급", "INFRA": "인프라", "FEATURE": "기능", "OTHER": "기타"}
DOT = {"A": "🔴", "B": "🟠", "C": "⚪"}


def dday(when, today):
    if not when:
        return "일정미정"
    try:
        d = dt.date.fromisoformat(str(when)[:10])
    except ValueError:
        return "일정미정"
    n = (d - today).days
    return "D-%d" % n if n > 0 else ("D-DAY" if n == 0 else "D+%d" % -n)


def render_message(payload):
    t = payload["as_of_kst"]
    lines = ["<b>⚡ 카탈리스트 레이더</b>  %s" % t[:16],
             "감시 %d종목(체인 %d·프로토콜 %d) · 이벤트 %d건"
             % (payload["universe"]["total"], payload["universe"]["chain"],
                payload["universe"]["protocol"], payload["event_count"])]

    if payload["data_status"] != "OK":
        lines.append("⚠️ <b>%s</b> — 수집 %d%%. 아래는 부분 관측입니다."
                     % (payload["data_status"], round(payload["coverage"] * 100)))

    tr = payload["transitions"]
    if tr:
        lines.append("\n<b>■ 오늘의 상태 전이</b>")
        for e in tr[:6]:
            arrow = ("신규" if e["transition"] == "NEW"
                     else "%s→%s" % (STAGE_KR.get(e.get("prev_stage"), "?"),
                                     STAGE_KR.get(e["stage"], "?")))
            lines.append("%s <b>%s</b> [%s·%s] %s"
                         % (DOT[e["grade"]], e["symbol"], IMPACT_KR[e["impact"]], arrow,
                            _esc(e["title"][:78])))
    else:
        lines.append("\n<b>■ 오늘의 상태 전이</b>\n· 신규 승격 없음")

    cal = payload["calendar"]
    if cal:
        lines.append("\n<b>■ D-45 캘린더</b>")
        for e in cal[:8]:
            lines.append("%s <code>%-6s</code> %s <b>%s</b> · %s"
                         % (DOT[e["grade"]], dday(e.get("event_date"), dt.date.fromisoformat(t[:10])),
                            e["symbol"], STAGE_KR.get(e["stage"], e["stage"]),
                            _esc(e["title"][:62])))

    if payload["unmatched_top"]:
        lines.append("\n<b>■ 미매칭 감사</b> — 시총 상위인데 원천 미연결")
        lines.append("· " + ", ".join(payload["unmatched_top"]))

    lines.append("\n<i>%s</i>" % _esc(payload.get("verification_line", "")))
    lines.append("<i>예고된 일정의 D-day 관리이며 수익 예측이 아닙니다.</i>")
    lines.append(payload["dashboard_url"])
    return "\n".join(lines)


def _esc(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render_dashboard(payload):
    rows = []
    today = dt.date.fromisoformat(payload["as_of_kst"][:10])
    for e in payload["all_events"][:200]:
        rows.append(
            "<tr class='g%s'><td>%s</td><td><b>%s</b></td><td>%s</td><td>%s</td>"
            "<td>%s</td><td class='r'>%.1f</td><td><a href='%s' target='_blank'>%s</a></td></tr>"
            % (e["grade"], dday(e.get("when"), today), e["symbol"],
               IMPACT_KR[e["impact"]], STAGE_KR.get(e["stage"], e["stage"]),
               e.get("first_seen", ""), e["score"], e.get("url") or "#",
               _esc(e["title"][:90])))
    vrows = "".join(
        "<tr><td>%s</td><td>T+%d</td><td>%d</td><td>%s</td><td>%s</td></tr>" % (
            r["bucket"], r["horizon"], r["n"],
            ("%+.2f%%p" % r["median"]) if r.get("median") is not None else "&mdash;",
            ("%.0f%%" % r["hit_rate"]) if r.get("hit_rate") is not None else "표본 부족")
        for r in payload.get("verification", [])) or (
        "<tr><td colspan=5 class='muted'>아직 지평(T+7·T+30)에 도달한 이벤트가 없습니다.</td></tr>")

    unmatched = "".join("<li>%s (시총 %d위)</li>" % (u["symbol"], u["rank"])
                        for u in payload["unmatched_all"][:30])
    tpl = """<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Catalyst Radar</title><style>
body{font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
margin:0;padding:16px;background:#0d1117;color:#c9d1d9}
h1{font-size:18px;margin:0 0 4px}h2{font-size:15px;margin:22px 0 8px;color:#8b949e}
.meta{color:#8b949e;font-size:12px;margin-bottom:14px}
table{width:100%;border-collapse:collapse;font-size:12px}
th,td{padding:6px 5px;border-bottom:1px solid #21262d;text-align:left;vertical-align:top}
th{color:#8b949e;font-weight:600;position:sticky;top:0;background:#0d1117}
td.r{text-align:right;font-variant-numeric:tabular-nums}
a{color:#58a6ff;text-decoration:none}
.gA td:first-child{border-left:3px solid #f85149}
.gB td:first-child{border-left:3px solid #d29922}
.gC td:first-child{border-left:3px solid #30363d}
.note{color:#8b949e;font-size:12px;margin-top:18px;border-top:1px solid #21262d;padding-top:10px}
ul{margin:6px 0;padding-left:18px;color:#8b949e;font-size:12px;columns:2}
</style></head><body>
<h1>&#9889; Catalyst Radar</h1>
<div class="meta">@AS_OF@ KST &middot; 감시 @TOTAL@종목(체인 @CHAIN@&middot;프로토콜 @PROTO@)
&middot; 이벤트 @EVENTS@건 &middot; 수집 @COV@% &middot; <b>@STATUS@</b></div>
<h2>이벤트</h2>
<table><thead><tr><th>D-day</th><th>종목</th><th>임팩트</th><th>단계</th>
<th>최초탐지</th><th>점수</th><th>내용</th></tr></thead><tbody>@ROWS@</tbody></table>
<h2>사전등록 검증 &mdash; 탐지 시점을 박아두고 사후 채점한 결과</h2>
<p class="muted">임계값을 사후에 조정하지 않기 위한 장치입니다. 초과수익은 BTC 대비이며,
표본 @MINSAMPLE@건 미만인 구간은 수치를 제시하지 않습니다. 극단치 지배를 피해 평균이 아닌 중앙값을 씁니다.</p>
<table class="tbl"><thead><tr><th>구간</th><th>지평</th><th>표본</th><th>초과수익 중앙값</th><th>적중률</th></tr></thead>
<tbody>@VERIFY@</tbody></table>

<h2>미매칭 감사 &mdash; 시총 100위인데 원천이 연결되지 않은 종목</h2>
<ul>@UNMATCHED@</ul>
<div class="note">이 대시보드는 <b>이미 공개&middot;예고된 일정</b>의 D-day 관리이며 가격 예측이 아닙니다.
탐지 시점 가격을 events.json 에 사전등록해 두고, 이벤트 경과 후 T+7/T+30 성과를 자동 기록합니다.
편입 규칙은 기계적으로 고정되어 있으며(frozen_at @FROZEN@) 성과를 보고 종목을 바꾸지 않습니다.</div>
</body></html>"""
    subs = {"@AS_OF@": payload["as_of_kst"],
            "@TOTAL@": str(payload["universe"]["total"]),
            "@CHAIN@": str(payload["universe"]["chain"]),
            "@PROTO@": str(payload["universe"]["protocol"]),
            "@EVENTS@": str(payload["event_count"]),
            "@COV@": str(round(payload["coverage"] * 100)),
            "@STATUS@": payload["data_status"],
            "@ROWS@": "".join(rows),
            "@VERIFY@": vrows,
            "@MINSAMPLE@": str(verify.MIN_SAMPLE),
            "@UNMATCHED@": unmatched,
            "@FROZEN@": payload["frozen_at"]}
    for k, v in subs.items():
        tpl = tpl.replace(k, v)
    return tpl


# ── 메인 ───────────────────────────────────────────────────────────────
def main():
    now = dt.datetime.now(KST)
    today = now.date()

    universe, meta = build_universe()
    price_by_symbol = {r["symbol"]: r.get("price") for r in universe}
    events, cov = collect(universe, use_news=os.environ.get("SKIP_NEWS") != "1")

    events = dedupe_releases(events)

    prev = load_json(os.path.join(DATA, "events.json"), {"events": []})
    all_events, transitions = merge_state(prev, events, today, price_by_symbol)

    accrued = verify.accrue(all_events, price_by_symbol, today)
    vsummary = verify.summarize(all_events)
    if accrued:
        print("[verify] 지평 도달 %d건 확정 기록" % accrued)

    all_events.sort(key=lambda e: -e["score"])
    transitions = [t for t in transitions if t["grade"] in ("A", "B")]
    transitions.sort(key=lambda e: -e["score"])

    # 캘린더는 '예정일이 확정된' 이벤트만. 관측일 폴백을 쓰면 과거 릴리스가 섞인다.
    calendar = sorted(
        [e for e in all_events
         if e.get("event_date") and 0 <= _dd(e["event_date"], today) <= 45],
        key=lambda e: (_dd(e["event_date"], today), -e["score"]))

    status = "OK" if cov["rate"] >= COVERAGE_MIN else "DEGRADED"
    if cov["rate"] == 0:
        status = "판정 불가"

    payload = {
        "as_of_kst": now.strftime("%Y-%m-%d %H:%M"),
        "universe": meta["counts"],
        "coverage": cov["rate"],
        "data_status": status,
        "event_count": len(all_events),
        "transitions": transitions,
        "calendar": calendar,
        "all_events": all_events,
        "verification": vsummary,
        "verification_line": verify.render_line(vsummary),
        "unmatched_all": meta["unmatched"],
        "unmatched_top": [u["symbol"] for u in meta["unmatched"][:8]],
        "frozen_at": meta["inclusion_rule"]["frozen_at"],
        "dashboard_url": "https://jinhae8971.github.io/korea-etf-calmar/catalyst-radar/",
        "http_stats": STATS,
    }
    payload["message"] = render_message(payload)

    save_json(os.path.join(DATA, "events.json"),
              {"as_of": payload["as_of_kst"], "events": all_events})
    save_json(os.path.join(DATA, "latest.json"),
              {k: v for k, v in payload.items() if k != "all_events"})
    os.makedirs(DOCS, exist_ok=True)
    with open(os.path.join(DOCS, "index.html"), "w", encoding="utf-8") as f:
        f.write(render_dashboard(payload))

    print(payload["message"])
    print("\n[coverage] %d/%d (%.0f%%) 실패=%s"
          % (cov["ok"], cov["attempted"], cov["rate"] * 100, cov["failed"][:6]))
    if status == "판정 불가":
        sys.exit(1)                       # 수집 전멸 시 "변화 없음" 발송 금지
    return 0


def _dd(when, today):
    try:
        return (dt.date.fromisoformat(str(when)[:10]) - today).days
    except ValueError:
        return 9999


if __name__ == "__main__":
    sys.exit(main())
