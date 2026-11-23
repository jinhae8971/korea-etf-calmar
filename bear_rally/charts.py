"""차트 (matplotlib, 다크 표면). 러너에 한글 글꼴이 없으므로 차트 글자는 영문만 쓴다 — 한글 설명은 리포트 본문이 맡는다."""
from __future__ import annotations

import os
from datetime import date

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402
from matplotlib.patches import Patch  # noqa: E402

from engine import S_BEAR, S_BULL, S_FAILED, S_RALLY, S_SHIFT, STATE_EN  # noqa: E402

SURF, INK, INK2, MUTED, GRID, AXIS = "#1a1a19", "#f0efec", "#c3c2b7", "#898781", "#2c2c2a", "#383835"
C_MA200, C_MA50, C_BAND, C_VW, C_FIB = "#3987e5", "#898781", "#d95926", "#9085e9", "#898781"
STATE_FILL = {S_BEAR: ("#2a62ab", 0.20), S_RALLY: ("#c98500", 0.22), S_FAILED: ("#d55181", 0.26), S_SHIFT: ("#199e70", 0.22)}
EV_STYLE = {  # type: (marker, color, label, above?)
    "bear_enter": ("v", "#5598e7", "Bear", True),
    "rally": ("^", "#eda100", "Rally", False),
    "bear_rally": ("v", "#e87ba4", "Bear rally", True),
    "shift_fail": ("v", "#e87ba4", "Shift failed", True),
    "shift": ("^", "#1baf7a", "Shift?", False),
    "bull": ("^", "#f0efec", "Bull", False),
}

plt.rcParams.update({
    "font.family": ["DejaVu Sans"], "axes.unicode_minus": False, "font.size": 10,
    "figure.facecolor": SURF, "axes.facecolor": SURF, "savefig.facecolor": SURF,
    "text.color": INK, "axes.labelcolor": INK2, "xtick.color": MUTED, "ytick.color": MUTED,
    "axes.edgecolor": AXIS, "grid.color": GRID, "grid.linewidth": 0.6,
})


def _save(fig, path: str) -> None:
    """PNG 저장 후 64색 팔레트로 줄인다(매일 커밋되는 이미지 용량을 1/3로). Pillow는 matplotlib 의존성이라 항상 있다."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fig.savefig(path)
    plt.close(fig)
    try:
        from PIL import Image
        with Image.open(path) as im:
            q = im.convert("RGB").quantize(colors=64, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
        q.save(path, optimize=True)
    except Exception:  # noqa: BLE001 — 줄이기 실패해도 원본 PNG는 남는다
        pass


def _d(s: str) -> date:
    return date.fromisoformat(s)


def _segments(rows: list) -> list[tuple[int, int, int]]:
    """연속 상태 구간 [(start, end, state)]"""
    segs, start = [], 0
    for i in range(1, len(rows) + 1):
        if i == len(rows) or rows[i]["state"] != rows[start]["state"]:
            segs.append((start, i - 1, rows[start]["state"]))
            start = i
    return segs


def _shade(ax, rows: list, dates: list) -> None:
    for s, e, st in _segments(rows):
        if st in STATE_FILL:
            col, a = STATE_FILL[st]
            x1 = dates[e + 1] if e + 1 < len(dates) else dates[e]
            ax.axvspan(dates[s], x1, color=col, alpha=a, lw=0, zorder=0)


def _fmt_price(v: float) -> str:
    return f"{v:,.0f}" if abs(v) >= 100 else f"{v:,.2f}"


def _style(ax) -> None:
    ax.grid(True, axis="y")
    for sp in ("top", "right"):
        ax.spines[sp].set_visible(False)
    ax.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=4, maxticks=8))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%y.%m"))
    big = ax.get_ylim()[1] >= 100                    # 축 단위로 소수 자릿수 통일 (80.00·100 혼용 방지)
    ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"{v:,.0f}" if big else f"{v:,.2f}"))


def asset_chart(key: str, res: dict, path: str, months: int = 24) -> str:
    rows = res["rows"]
    n0 = max(0, len(rows) - int(months * (31 if res["mode"] == "crypto" else 21.5)))
    rows = rows[n0:]
    dates = [_d(r["d"]) for r in rows]
    fig, ax = plt.subplots(figsize=(10.8, 6.0), dpi=100)
    fig.subplots_adjust(left=0.075, right=0.985, top=0.865, bottom=0.165)
    _shade(ax, rows, dates)

    def series(k):
        return [r[k] for r in rows]

    ax.plot(dates, series("c"), color=INK, lw=1.4, label="Close", zorder=4)
    ax.plot(dates, series("ma200"), color=C_MA200, lw=2.0, label="200D MA", zorder=3)
    ax.plot(dates, series("ma50"), color=C_MA50, lw=1.0, label="50D MA", zorder=3)
    handles = [Line2D([], [], color=INK, lw=1.4, label="Close"), Line2D([], [], color=C_MA200, lw=2, label="200D MA"),
               Line2D([], [], color=C_MA50, lw=1, label="50D MA")]
    if res["mode"] == "crypto":
        top, bot = series("w_top"), series("w_bot")
        ax.plot(dates, top, color=C_BAND, lw=0.8, alpha=0.85, zorder=3)
        ax.plot(dates, bot, color=C_BAND, lw=0.8, alpha=0.85, zorder=3)
        ax.fill_between(dates, [b if b is not None else float("nan") for b in bot],
                        [t if t is not None else float("nan") for t in top], color=C_BAND, alpha=0.12, lw=0, zorder=2)
        handles.append(Patch(color=C_BAND, alpha=0.5, label="Bull market support band (20W/21W)"))
    vw = [r["avwap"] if r["state"] != S_BULL else None for r in rows]
    if any(v is not None for v in vw):
        ax.plot(dates, [v if v is not None else float("nan") for v in vw], color=C_VW, lw=1.2, zorder=3)
        handles.append(Line2D([], [], color=C_VW, lw=1.2, label="AVWAP from peak"))
    f3, f6 = series("fib382"), series("fib618")
    if any(v is not None for v in f3):
        ax.plot(dates, [v if v is not None else float("nan") for v in f3], color=C_FIB, lw=0.9, ls=(0, (4, 3)), zorder=2)
        ax.plot(dates, [v if v is not None else float("nan") for v in f6], color=C_FIB, lw=0.9, ls=(0, (4, 3)), zorder=2)
        handles.append(Line2D([], [], color=C_FIB, lw=0.9, ls=(0, (4, 3)), label="Fib 38.2% / 61.8%"))

    lo, hi = min(r["l"] for r in rows), max(r["h"] for r in rows)
    pad = (hi - lo) * 0.06
    first_d = rows[0]["d"]
    last_lab = {True: -999, False: -999}            # 같은 쪽 라벨이 12봉 안에 겹치면 글자는 생략(표식은 유지)
    for e in res["events"]:
        if e["d"] < first_d or e["type"] not in EV_STYLE:
            continue
        mk, col, lab, above = EV_STYLE[e["type"]]
        r = rows[e["i"] - n0]
        y = r["h"] + pad * 0.6 if above else r["l"] - pad * 0.6
        ax.scatter([_d(e["d"])], [y], marker=mk, s=64, color=col, edgecolor=SURF, linewidth=1.2, zorder=6)
        if e["i"] - last_lab[above] >= 12:
            ax.annotate(lab, (_d(e["d"]), y), xytext=(0, 9 if above else -12), textcoords="offset points",
                        ha="center", fontsize=7.5, color=INK2, zorder=7)
            last_lab[above] = e["i"]
    ax.set_ylim(lo - pad * 2, hi + pad * 2)
    _style(ax)
    for st in (S_BEAR, S_RALLY, S_FAILED, S_SHIFT):
        col, a = STATE_FILL[st]
        handles.append(Patch(color=col, alpha=min(1, a * 2.6), label=STATE_EN[st]))
    fig.legend(handles=handles, loc="lower center", fontsize=8, frameon=False, ncol=5, labelcolor=INK2,
               bbox_to_anchor=(0.53, 0.0), handlelength=1.6, columnspacing=1.2)

    last = res["rows"][-1]
    fig.text(0.075, 0.965, f"{key}  ·  {STATE_EN[last['state']]}", fontsize=15, weight="bold", color=INK, va="top")
    gap = (last["c"] / last["ma200"] - 1) * 100 if last["ma200"] else None
    sub = f"Close {_fmt_price(last['c'])}  ·  vs 200D {gap:+.1f}%" if gap is not None else f"Close {_fmt_price(last['c'])}"
    if last["dd_peak"] is not None:
        sub += f"  ·  from peak {last['dd_peak'] * 100:+.1f}%"
    sub += f"  ·  as of {last['d']}"
    fig.text(0.075, 0.915, sub, fontsize=9.5, color=INK2, va="top")
    _save(fig, path)
    return path


def overview_chart(items: list[tuple[str, dict]], path: str, months: int = 12) -> str:
    """종목별 최근 12개월 미니 차트 (2열 그리드) — 텔레그램 첨부용 한 장."""
    n = len(items)
    rows_n = (n + 1) // 2
    H = 2.45 * rows_n + 1.0
    fig, axes = plt.subplots(rows_n, 2, figsize=(10.8, H), dpi=100, squeeze=False)
    fig.subplots_adjust(left=0.07, right=0.985, top=1 - 1.0 / H, bottom=0.35 / H, hspace=0.42, wspace=0.16)
    for k, (key, res) in enumerate(items):
        ax = axes[k // 2][k % 2]
        rows = res["rows"]
        n0 = max(0, len(rows) - int(months * (31 if res["mode"] == "crypto" else 21.5)))
        rows = rows[n0:]
        dates = [_d(r["d"]) for r in rows]
        _shade(ax, rows, dates)
        ax.plot(dates, [r["c"] for r in rows], color=INK, lw=1.1, zorder=4)
        ax.plot(dates, [r["ma200"] for r in rows], color=C_MA200, lw=1.4, zorder=3)
        _style(ax)
        ax.xaxis.set_major_locator(mdates.MonthLocator(bymonth=(1, 4, 7, 10)))
        ax.tick_params(labelsize=7.5)
        last = res["rows"][-1]
        gap = (last["c"] / last["ma200"] - 1) * 100 if last["ma200"] else 0.0
        ax.set_title(f"{key}   {STATE_EN[last['state']]}   ({gap:+.1f}% vs 200D)", fontsize=10, color=INK, loc="left", pad=5)
    for k in range(n, rows_n * 2):
        axes[k // 2][k % 2].axis("off")
    legend = [Line2D([], [], color=INK, lw=1.1, label="Close"), Line2D([], [], color=C_MA200, lw=1.4, label="200D MA")]
    for st in (S_BEAR, S_RALLY, S_FAILED, S_SHIFT):
        col, a = STATE_FILL[st]
        legend.append(Patch(color=col, alpha=min(1, a * 2.6), label=STATE_EN[st]))
    fig.text(0.07, 1 - 0.14 / H, "Bear Market Rally Radar  ·  last 12 months", fontsize=12.5,
             weight="bold", color=INK, va="top")
    fig.legend(handles=legend, loc="upper left", ncol=6, fontsize=8.5, frameon=False, labelcolor=INK2,
               bbox_to_anchor=(0.06, 1 - 0.42 / H), handlelength=1.6, columnspacing=1.4)
    _save(fig, path)
    return path
