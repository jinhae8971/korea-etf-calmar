# -*- coding: utf-8 -*-
"""Charts for the US close briefing (dark surface, Korean colour convention: red = up, blue = down).

python3 charts.py <session>   -> out/<session>/img/*.png
"""
import json
import os
import pickle
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib import font_manager as fm  # noqa: E402
from matplotlib.colors import LinearSegmentedColormap  # noqa: E402
from matplotlib.patches import Rectangle  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import brief as B  # noqa: E402

# ---------------------------------------------------------------- theme (dataviz reference palette, dark)
SURF, SURF2 = "#1a1a19", "#242422"
INK, INK2, MUTED, GRID = "#f0efec", "#c3c2b7", "#8f8e86", "#33332f"
UP, DOWN, MID = "#e66767", "#3987e5", "#383835"          # diverging poles (red up / blue down) + gray mid
UP_D, DOWN_D = "#b94a4a", "#2a62ab"
S1, S2, S3, S4, S7 = "#3987e5", "#d95926", "#199e70", "#c98500", "#9085e9"  # categorical dark steps
DIV = LinearSegmentedColormap.from_list("div", [DOWN_D, DOWN, MID, UP, UP_D][::1], N=256)
DIV = LinearSegmentedColormap.from_list("div", [(0, "#1f4f8f"), (0.25, DOWN), (0.5, MID), (0.75, UP), (1, "#a83c3c")])

for f in fm.findSystemFonts():
    if "NotoSansCJK-Regular" in f or "NotoSansCJK-Bold" in f:
        fm.fontManager.addfont(f)
plt.rcParams.update({
    "font.family": ["Noto Sans CJK KR", "DejaVu Sans"], "axes.unicode_minus": False,
    "figure.facecolor": SURF, "axes.facecolor": SURF, "savefig.facecolor": SURF,
    "text.color": INK, "axes.labelcolor": INK2, "xtick.color": MUTED, "ytick.color": MUTED,
    "axes.edgecolor": GRID, "grid.color": GRID, "grid.linewidth": 0.6, "font.size": 11,
})
W = 10.8  # inches at dpi 100 -> 1080 px wide


def cell_color(v, scale):
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return SURF2
    return DIV(0.5 + max(-1, min(1, v / scale)) / 2)


def header(fig, title, sub):
    H = fig.get_figheight()
    fig.text(0.035, 1 - 0.22 / H, title, fontsize=19, weight="bold", va="top", color=INK)
    fig.text(0.035, 1 - 0.62 / H, sub, fontsize=11, va="top", color=INK2)


def top_of(fig, inches=1.25):
    return 1 - inches / fig.get_figheight()


def footer(fig, txt="빨강 상승 · 파랑 하락 · 데이터 Yahoo Finance"):
    fig.text(0.035, 0.012, txt, fontsize=9, color=MUTED)


def fmtv(v, unit="%", nd=1):
    if v is None:
        return "–"
    s = f"{v:+.{nd}f}"
    return s + ("" if unit == "%" else unit) if unit != "%" else s


def grid_table(ax, rows, cols, scales, units, label_w=0.30, close_w=0.15, close_label="종가"):
    """rows: list of dict(label, close, values[list])"""
    n = len(rows)
    ax.set_xlim(0, 1); ax.set_ylim(n, 0); ax.axis("off")
    cw = (1 - label_w - close_w) / len(cols)
    ax.text(label_w + close_w - 0.01, -0.35, close_label, ha="right", fontsize=10, color=MUTED)
    for j, c in enumerate(cols):
        ax.text(label_w + close_w + cw * (j + 0.5), -0.35, c, ha="center", fontsize=10, color=MUTED)
    for i, r in enumerate(rows):
        ax.text(0.005, i + 0.5, r["label"], va="center", fontsize=12, color=INK)
        if r.get("sub"):
            ax.text(label_w - 0.01, i + 0.5, r["sub"], va="center", ha="right", fontsize=9, color=MUTED)
        ax.text(label_w + close_w - 0.01, i + 0.5, r.get("close_s", ""), va="center", ha="right", fontsize=11, color=INK2)
        for j, v in enumerate(r["values"]):
            sc, un = r.get("scales", scales)[j], r.get("units", units)[j]
            x0 = label_w + close_w + cw * j
            ax.add_patch(Rectangle((x0 + 0.002, i + 0.06), cw - 0.004, 0.88, color=cell_color(v, sc), lw=0))
            ax.text(x0 + cw / 2, i + 0.5, fmtv(v, un, 1 if un == "%" else 0 if un == "bp" else 2),
                    ha="center", va="center", fontsize=11.5, color=INK, weight="bold" if j == 0 else "normal")


def close_str(r):
    c = r["close"]
    if r["kind"] == "yld":
        return f"{c:.2f}%"
    if c >= 10000:
        return f"{c:,.0f}"
    return f"{c:,.2f}"


def chart_cross(d, path):
    rows = []
    for r in d["cross"]:
        if r["kind"] == "yld":
            rows.append({"label": r["label"], "close_s": close_str(r), "values": [r["d1"], r["d5"], r["m1"]],
                         "scales": [8, 20, 40], "units": ["bp", "bp", "bp"], "sub": "bp"})
        elif r["kind"] == "lvl":
            rows.append({"label": r["label"], "close_s": close_str(r), "values": [r["d1"], r["d5"], r["m1"]],
                         "scales": [2, 5, 8], "units": ["pt", "pt", "pt"], "sub": "pt"})
        else:
            rows.append({"label": r["label"], "close_s": close_str(r), "values": [r["d1"], r["d5"], r["m1"]],
                         "scales": [2, 4, 8], "units": ["%", "%", "%"]})
    fig = plt.figure(figsize=(W, 1.6 + 0.52 * len(rows)), dpi=100)
    header(fig, "크로스에셋", f"{d['session']} 뉴욕 마감 기준 · 금리는 bp, VIX는 포인트 변화")
    t = top_of(fig)
    ax = fig.add_axes([0.035, 0.05, 0.93, t - 0.05])
    grid_table(ax, rows, ["1일", "5일", "1개월"], [2, 4, 8], ["%", "%", "%"], label_w=0.34, close_w=0.18)
    footer(fig)
    fig.savefig(path); plt.close(fig)


def chart_sectors(d, path):
    def mk(r):
        st = "▲" if r["st_up"] else "▼"
        return {"label": r["label"], "sub": r["sym"], "close_s": f"RSI {r['rsi']:.0f}  {st}",
                "values": [r["d1"], r["d5"], r["m1"], r["ytd"], r["vs_ema50"]]}
    secs = [mk(r) for r in sorted(d["sectors"], key=lambda r: -r["d1"])]
    thm = [mk(r) for r in sorted(d["themes"], key=lambda r: -r["d1"])]
    n = len(secs) + len(thm) + 1
    fig = plt.figure(figsize=(W, 1.9 + 0.5 * n), dpi=100)
    header(fig, "섹터·테마 히트맵", f"{d['session']} · 1일 수익률 순 · ▲▼ = 일봉 SuperTrend 방향 · 마지막 열 = EMA50 대비 괴리")
    top = top_of(fig, 1.35)
    span = top - 0.04
    h1 = span * len(secs) / n
    ax1 = fig.add_axes([0.035, top - h1, 0.93, h1])
    cols = ["1일", "5일", "1개월", "YTD", "EMA50"]
    grid_table(ax1, secs, cols, [2, 4, 8, 30, 8], ["%"] * 5, label_w=0.24, close_w=0.20, close_label="RSI · 추세")
    ax1.text(0.005, -0.35, "S&P 11개 섹터", fontsize=10, color=S1, weight="bold")
    h2 = span * len(thm) / n
    ax2 = fig.add_axes([0.035, top - h1 - span / n - h2, 0.93, h2])
    grid_table(ax2, thm, cols, [3, 6, 12, 40, 10], ["%"] * 5, label_w=0.24, close_w=0.20, close_label="RSI · 추세")
    ax2.text(0.005, -0.35, "테마 ETF", fontsize=10, color=S1, weight="bold")
    footer(fig)
    fig.savefig(path); plt.close(fig)


def squarify_layout(vals, x, y, w, h):
    """Squarified treemap (Bruls et al.). vals sorted desc, positive. returns rects (x,y,w,h)."""
    vals = list(vals)
    tot = sum(vals)
    vals = [v * w * h / tot for v in vals]
    rects = []

    def worst(row, side):
        s = sum(row)
        return max(max(side * side * r / (s * s), (s * s) / (side * side * r)) for r in row)

    def layout(row, x, y, w, h):
        s = sum(row)
        if w >= h:
            cw = s / h
            yy = y
            for r in row:
                rh = r / cw
                rects.append((x, yy, cw, rh)); yy += rh
            return x + cw, y, w - cw, h
        ch = s / w
        xx = x
        for r in row:
            rw = r / ch
            rects.append((xx, y, rw, ch)); xx += rw
        return x, y + ch, w, h - ch

    row = []
    while vals:
        side = min(w, h)
        v = vals[0]
        if not row or worst(row + [v], side) <= worst(row, side):
            row.append(v); vals.pop(0)
        else:
            x, y, w, h = layout(row, x, y, w, h); row = []
    if row:
        layout(row, x, y, w, h)
    return rects


def chart_treemap(d, path):
    st = [s for s in d["stocks"] if s.get("mcap") and s["d1"] is not None]
    by = {}
    for s in st:
        by.setdefault(s["sector"], []).append(s)
    secs = sorted(by.items(), key=lambda kv: -sum(x["mcap"] for x in kv[1]))
    fig = plt.figure(figsize=(W, 11.5), dpi=100)
    header(fig, "대형주 히트맵", f"{d['session']} · 미국 시총 상위 {len(st)}종 · 크기 = 시가총액, 색 = 1일 등락률(±4% 포화)")
    ax = fig.add_axes([0.02, 0.04, 0.96, 0.87]); ax.set_xlim(0, 100); ax.set_ylim(100, 0); ax.axis("off")
    srects = squarify_layout([sum(x["mcap"] for x in v) for _, v in secs], 0, 0, 100, 100)
    for (name, items), (sx, sy, sw, sh) in zip(secs, srects):
        items = sorted(items, key=lambda x: -x["mcap"])
        pad_top = 3.2 if sh > 8 else 0
        rects = squarify_layout([x["mcap"] for x in items], sx + 0.25, sy + pad_top, sw - 0.5, sh - pad_top - 0.25)
        if pad_top:
            sec_chg = sum(x["d1"] * x["mcap"] for x in items) / sum(x["mcap"] for x in items)
            ax.text(sx + 0.6, sy + 1.6, f"{name} {sec_chg:+.1f}%", fontsize=10, color=INK2, va="center", weight="bold")
        for it, (x, y, w, h) in zip(items, rects):
            ax.add_patch(Rectangle((x, y), w, h, facecolor=cell_color(it["d1"], 4), edgecolor=SURF, lw=1.6))
            area = w * h
            if area > 6 and w > 3.2:
                fs = max(7, min(20, (area ** 0.5) * 1.5))
                ax.text(x + w / 2, y + h / 2 - (fs * 0.035 if area > 14 else 0), it["sym"], ha="center", va="center",
                        fontsize=fs, color=INK, weight="bold")
                if area > 14:
                    ax.text(x + w / 2, y + h / 2 + fs * 0.075, f"{it['d1']:+.1f}%", ha="center", va="center",
                            fontsize=fs * 0.7, color=INK)
    footer(fig)
    fig.savefig(path); plt.close(fig)


def chart_breadth(d, path):
    b = d["breadth"]
    fig = plt.figure(figsize=(W, 9.6), dpi=100)
    header(fig, "브레드스·팩터", f"{d['session']} · 대형주 {b['n']}종 기준 · 상승 {b['adv']} / 하락 {b['dec']} · 52주 신고가 {b['new_high']} / 신저가 {b['new_low']}")
    ax = fig.add_axes([0.07, 0.53, 0.88, top_of(fig, 1.45) - 0.53])
    x = pd.to_datetime(b["hist"]["date"])
    ax.plot(x, b["hist"]["above50"], color=S1, lw=2, label="50일선 위 비율")
    ax.plot(x, b["hist"]["above200"], color=S2, lw=2, label="200일선 위 비율")
    ax.axhline(50, color=MUTED, lw=0.8, ls="--")
    ax.set_ylim(0, 100); ax.grid(axis="y"); ax.set_ylabel("%")
    a50, a200 = b["above50"], b["above200"]
    off = 7 if abs(a50 - a200) < 6 else 0
    for v, dy in ((a50, -off if a50 <= a200 else off), (a200, off if a50 <= a200 else -off)):
        ax.annotate(f"{v:.0f}%", (x[-1], v), xytext=(6, dy), textcoords="offset points", color=INK, fontsize=11, va="center")
    ax.legend(loc="upper left", frameon=False, ncol=2, labelcolor=INK)
    ax.spines[["top", "right"]].set_visible(False)
    ax.set_title("주가가 이동평균 위에 있는 종목 비율 (최근 6개월)", loc="left", fontsize=12, color=INK2)
    ax2 = fig.add_axes([0.07, 0.07, 0.88, 0.33])
    colors = [S1, S2, S3, S7]
    for f, c in zip(d["factors"], colors):
        xx = pd.to_datetime(f["hist"]["date"])
        ax2.plot(xx, f["hist"]["idx"], color=c, lw=2, label=f"{f['label']} ({f['long']}/{f['short']}) 1M {f['m1']:+.1f}%")
    ax2.axhline(100, color=MUTED, lw=0.8, ls="--")
    ax2.grid(axis="y"); ax2.spines[["top", "right"]].set_visible(False)
    ax2.legend(loc="upper left", frameon=False, fontsize=9.5, labelcolor=INK)
    ax2.set_title("팩터 상대강도 (3개월 전 = 100, 위로 갈수록 앞쪽이 우위)", loc="left", fontsize=12, color=INK2)
    footer(fig)
    fig.savefig(path); plt.close(fig)


def chart_tech(sym, t, bars, path, title_note=""):
    df = bars[sym].copy()
    c = df["close"]
    df["e50"], df["e120"] = B.ema(c, 50), B.ema(c, 120)
    st, dr, _ = B.supertrend(df, 20, 4.0)
    df["st"], df["dir"] = st, dr
    df["rsi"] = B.rsi(c, 14)
    df = df.iloc[-100:]
    fig = plt.figure(figsize=(W, 9.2), dpi=100)
    one = t["1d"]
    header(fig, f"{sym} 일봉", f"{t['price']} ({t['chg_pct']:+.2f}%) · ATH 대비 {t['ath']['dd_pct']:+.1f}% · "
                               f"SuperTrend 4H {'상승' if t['4h']['supertrend']['dir'] == 'up' else '하락'} / "
                               f"1D {'상승' if one['supertrend']['dir'] == 'up' else '하락'} / "
                               f"1W {'상승' if t['1w']['supertrend']['dir'] == 'up' else '하락'}{title_note}")
    ax = fig.add_axes([0.06, 0.37, 0.84, top_of(fig, 1.05) - 0.37])
    xs = np.arange(len(df))
    for i, (o, h, l, cl) in enumerate(zip(df["open"], df["high"], df["low"], df["close"])):
        col = UP if cl >= o else DOWN
        ax.vlines(i, l, h, color=col, lw=1)
        ax.add_patch(Rectangle((i - 0.32, min(o, cl)), 0.64, max(abs(cl - o), 1e-6), color=col, lw=0))
    ax.plot(xs, df["e50"], color="#6da7ec", lw=1.8, label=f"EMA50 {one['ma_mid']['v']}")
    ax.plot(xs, df["e120"], color=S7, lw=1.8, label=f"EMA120 {one['ma_long']['v']}")
    stv = df["st"].where(df["dir"] == -1)
    std = df["st"].where(df["dir"] == 1)
    ax.plot(xs, stv, color=UP, lw=1.4, ls=(0, (4, 2)), label="SuperTrend(상승)")
    ax.plot(xs, std, color=DOWN, lw=1.4, ls=(0, (4, 2)), label="SuperTrend(하락)")
    lo, hi = df["low"].min(), df["high"].max()
    lv = [(z["level"], "지지", DOWN) for z in t["swing_support"][:3]] + \
         [(z["level"], "저항", UP) for z in t["swing_resistance"][:3]]
    lv = sorted([x for x in lv if lo * 0.98 < x[0] < hi * 1.02])
    gap, last = (hi - lo) * 0.035, None
    for v, k, col in lv:
        ax.axhline(v, color=col, lw=0.8, ls=":", alpha=0.9)
        y = v if last is None or v - last >= gap else last + gap
        ax.text(len(df) + 0.6, y, f"{k} {v:.1f}", color=INK2, fontsize=9, va="center")
        last = y
    ax.set_xlim(-1, len(df) + 0.5); ax.grid(axis="y"); ax.spines[["top", "right"]].set_visible(False)
    ax.legend(loc="upper left", frameon=False, fontsize=9.5, ncol=2, labelcolor=INK)
    ticks = list(range(len(df) - 1, -1, -21))[::-1]
    ax.set_xticks(ticks); ax.set_xticklabels([df.index[i].strftime("%m/%d") for i in ticks])
    axv = fig.add_axes([0.06, 0.23, 0.84, 0.11], sharex=ax)
    vc = [UP if a >= b else DOWN for a, b in zip(df["close"], df["open"])]
    axv.bar(xs, df["volume"], color=vc, width=0.7)
    axv.plot(xs, df["volume"].rolling(20).mean(), color=INK2, lw=1)
    axv.set_yticks([]); axv.spines[["top", "right", "left"]].set_visible(False)
    axv.text(0, 1.04, "거래량 · 회색선 20일 평균", transform=axv.transAxes, fontsize=9, color=MUTED, va="bottom")
    plt.setp(axv.get_xticklabels(), visible=False)
    axr = fig.add_axes([0.06, 0.05, 0.84, 0.14], sharex=ax)
    axr.plot(xs, df["rsi"], color=S1, lw=1.6)
    axr.plot(xs, df["rsi"].rolling(14).mean(), color=S4, lw=1.2)
    for lv in (30, 50, 70):
        axr.axhline(lv, color=MUTED, lw=0.7, ls="--")
    axr.set_ylim(15, 85); axr.set_yticks([30, 50, 70]); axr.spines[["top", "right"]].set_visible(False)
    axr.text(0, 1.04, f"RSI 14 {one['rsi']['v']:.1f} · 노랑 = 14일 평균", transform=axr.transAxes, fontsize=9, color=MUTED, va="bottom")
    axr.set_xticks(ticks); axr.set_xticklabels([df.index[i].strftime("%m/%d") for i in ticks])
    plt.setp(ax.get_xticklabels(), visible=False)
    fig.savefig(path); plt.close(fig)


def main(session):
    sdir = os.path.join(HERE, "out", session)
    d = json.load(open(os.path.join(sdir, "data.json")))
    bars = pickle.load(open(os.path.join(sdir, "bars.pkl"), "rb"))
    bars = {s: df[df.index <= pd.Timestamp(session)] for s, df in bars.items()}
    img = os.path.join(sdir, "img"); os.makedirs(img, exist_ok=True)
    out = []
    jobs = [("01_cross.png", chart_cross), ("02_sectors.png", chart_sectors),
            ("03_treemap.png", chart_treemap), ("04_breadth.png", chart_breadth)]
    for name, fn in jobs:
        fn(d, os.path.join(img, name)); out.append(name)
    for i, s in enumerate(d["tech"], start=5):
        name = f"0{i}_{s.lower()}.png"
        chart_tech(s, d["tech"][s], bars, os.path.join(img, name)); out.append(name)
    print("charts:", out)
    return out


if __name__ == "__main__":
    main(sys.argv[1])
