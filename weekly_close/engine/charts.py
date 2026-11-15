# -*- coding: utf-8 -*-
"""Charts for the weekly close report (same theme as us_close: dark surface, red = up, blue = down).

python3 charts.py <week>   -> out/<week>/img/s1..s4 (stock), c1..c3 (crypto)
"""
import json
import os
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib import font_manager as fm  # noqa: E402
from matplotlib.colors import LinearSegmentedColormap  # noqa: E402
from matplotlib.patches import Rectangle  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
SURF, SURF2 = "#1a1a19", "#242422"
INK, INK2, MUTED, GRID = "#f0efec", "#c3c2b7", "#8f8e86", "#33332f"
UP, DOWN, MID = "#e66767", "#3987e5", "#383835"
S = ["#3987e5", "#d95926", "#199e70", "#c98500", "#9085e9"]
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
W = 10.8


def header(fig, title, sub):
    H = fig.get_figheight()
    fig.text(0.035, 1 - 0.22 / H, title, fontsize=19, weight="bold", va="top", color=INK)
    fig.text(0.035, 1 - 0.62 / H, sub, fontsize=11, va="top", color=INK2)


def top_of(fig, inches=1.2):
    return 1 - inches / fig.get_figheight()


def footer(fig, txt):
    fig.text(0.035, 0.012, txt, fontsize=9, color=MUTED)


def md(s):
    return f"{int(s[5:7])}/{int(s[8:10])}"


def hbars(ax, rows, key="w1", unit="%", ghost=None, label_fs=12, gutter=False):
    """Horizontal bars; rows already ordered top→bottom. ghost = key of a faint comparison marker."""
    n = len(rows)
    vals = [r[key] for r in rows]
    lim = max(1e-9, max(abs(v) for v in vals + ([r[ghost] for r in rows if r.get(ghost) is not None] if ghost else []))) * 1.28
    ax.set_xlim(-lim * (1.75 if gutter else 1), lim); ax.set_ylim(n - 0.5, -0.5)
    ax.axvline(0, color=MUTED, lw=0.8)
    for i, r in enumerate(rows):
        v = r[key]
        ax.barh(i, v, height=0.62, color=UP if v > 0 else DOWN, lw=0)
        if ghost and r.get(ghost) is not None:
            ax.plot([r[ghost]], [i], marker="|", ms=16, mew=2, color=INK2, alpha=0.75)
        off = lim * 0.015
        ax.text(v + (off if v >= 0 else -off), i, f"{v:+.1f}{unit}", va="center", ha="left" if v >= 0 else "right",
                fontsize=label_fs, color=INK, weight="bold")
        if gutter:
            ax.text(-lim * 1.73, i, r["label"], va="center", ha="left", fontsize=label_fs, color=INK2)
        else:
            ax.text(-off if v >= 0 else off, i, r["label"], va="center", ha="right" if v >= 0 else "left",
                    fontsize=label_fs, color=INK2)
    ax.set_yticks([]); ax.set_xticks([])
    for sp in ax.spines.values():
        sp.set_visible(False)


def cell_color(v, scale):
    return SURF2 if v is None else DIV(0.5 + max(-1, min(1, v / scale)) / 2)


def heat(ax, rows, cols, keys, scales, label_w=0.36):
    n = len(rows)
    ax.set_xlim(0, 1); ax.set_ylim(n, 0); ax.axis("off")
    cw = (1 - label_w) / len(cols)
    for j, c in enumerate(cols):
        ax.text(label_w + cw * (j + 0.5), -0.3, c, ha="center", fontsize=10, color=MUTED)
    for i, r in enumerate(rows):
        ax.text(0.005, i + 0.5, r["label"], va="center", fontsize=12, color=INK)
        ax.text(label_w - 0.012, i + 0.5, r.get("sym", ""), va="center", ha="right", fontsize=9, color=MUTED)
        for j, k in enumerate(keys):
            v = r.get(k)
            x0 = label_w + cw * j
            ax.add_patch(Rectangle((x0 + 0.003, i + 0.06), cw - 0.006, 0.88, color=cell_color(v, scales[j]), lw=0))
            ax.text(x0 + cw / 2, i + 0.5, "–" if v is None else f"{v:+.1f}", ha="center", va="center", fontsize=11.5,
                    color=INK, weight="bold" if j == 0 else "normal")


def lines(ax, paths, unit="%"):
    ax.grid(True, axis="y"); ax.axhline(0, color=MUTED, lw=0.8)
    ends = []
    for i, (lab, pts) in enumerate(paths.items()):
        ys = [p["v"] for p in pts]
        ax.plot(range(len(ys)), ys, color=S[i % 5], lw=2.4, marker="o", ms=4)
        ends.append([ys[-1], lab, S[i % 5]])
    # de-overlap end labels
    ends.sort(key=lambda e: e[0])
    span = (ax.get_ylim()[1] - ax.get_ylim()[0]) or 1
    gap = span * 0.07
    for k in range(1, len(ends)):
        if ends[k][0] - ends[k - 1][0] < gap:
            ends[k][0] = ends[k - 1][0] + gap
    n = len(next(iter(paths.values())))
    orig = {lab: pts[-1]["v"] for lab, pts in paths.items()}
    for y, lab, col in ends:
        ax.text(n - 1 + 0.12, y, f"{lab} {orig[lab]:+.1f}{unit}", color=col, va="center", fontsize=11.5, weight="bold")
    ax.set_xlim(-0.2, n - 1 + 1.75)
    ax.set_xticks(range(n)); ax.set_xticklabels([md(p["d"]) for p in next(iter(paths.values()))])
    ax.yaxis.set_major_formatter(lambda v, _: f"{v:+.0f}%")
    for sp in ("top", "right"):
        ax.spines[sp].set_visible(False)


# ------------------------------------------------------------------ stock
def s1_indices(d, path):
    s = d["stock"]
    rows = sorted(s["indices"], key=lambda r: -r["w1"])
    fig = plt.figure(figsize=(W, 1.75 + 0.5 * len(rows)), dpi=100)
    header(fig, "주간 등락 · 글로벌 지수", f"{md(s['week_start'])}~{md(s['week_end'])} 주간 종가 기준 · 세로 표시(|)는 직전 주 등락")
    ax = fig.add_axes([0.04, 0.06, 0.92, top_of(fig) - 0.06])
    hbars(ax, rows, ghost="prev_w1", gutter=True)
    footer(fig, "빨강 상승 · 파랑 하락 · 데이터 Yahoo Finance")
    fig.savefig(path); plt.close(fig)


def s2_sectors(d, path):
    s = d["stock"]
    a = sorted(s["sectors"], key=lambda r: -r["w1"])
    b = sorted(s["themes"], key=lambda r: -r["w1"])
    m = [r for r in s["macro"] if r["kind"] == "idx"]
    fig = plt.figure(figsize=(W, 2.3 + 0.44 * (len(a) + len(b) + len(m))), dpi=100)
    header(fig, "섹터 · 테마 · 원자재", f"{md(s['week_start'])}~{md(s['week_end'])} · 1주 / 4주 / 연초 이후 등락률(%)")
    t = top_of(fig, 1.35)
    tot = len(a) + len(b) + len(m) + 2.4
    h = (t - 0.045) / tot
    y = t
    for rows, n in ((a, len(a)), (b, len(b)), (m, len(m))):
        ax = fig.add_axes([0.04, y - h * n, 0.92, h * n])
        heat(ax, rows, ["1주", "4주", "연초 이후"], ["w1", "w4", "ytd"], [5, 10, 30])
        y -= h * (n + 1.2)
    footer(fig, "위부터 S&P 섹터 ETF · 테마 ETF · 달러/환율/원자재 · 데이터 Yahoo Finance")
    fig.savefig(path); plt.close(fig)


def s3_movers(d, path):
    s = d["stock"]
    us = s["us_top"] + s["us_bottom"][::-1]
    kr = s["kr"]
    n = max(len(us), len(kr))
    fig = plt.figure(figsize=(W, 1.9 + 0.42 * n), dpi=100)
    b = s["breadth"]
    header(fig, "주간 상승·하락 종목", f"미국 대형주 {b['n']}종 중 {b['up']}종 상승({b['pct']}%) · 왼쪽 미국 상·하위 8, 오른쪽 한국 대표주")
    t = top_of(fig, 1.3)
    ax1 = fig.add_axes([0.03, 0.06, 0.45, t - 0.06]); hbars(ax1, us, label_fs=11)
    ax2 = fig.add_axes([0.52, 0.06, 0.45, (t - 0.06) * len(kr) / n]); hbars(ax2, kr, label_fs=11)
    ax2.set_position([0.52, t - (t - 0.06) * len(kr) / n, 0.45, (t - 0.06) * len(kr) / n])
    footer(fig, "주간 종가 등락률 · 데이터 Yahoo Finance")
    fig.savefig(path); plt.close(fig)


def s4_path(d, path):
    s = d["stock"]
    fig = plt.figure(figsize=(W, 5.6), dpi=100)
    header(fig, "한 주의 흐름", "직전 주 종가 = 0% 기준 누적 등락 · 요일별 종가")
    ax = fig.add_axes([0.075, 0.1, 0.89, top_of(fig, 1.25) - 0.1])
    lines(ax, s["paths"])
    footer(fig, "데이터 Yahoo Finance")
    fig.savefig(path); plt.close(fig)


# ------------------------------------------------------------------ crypto
def c1_majors(d, path):
    c = d["crypto"]
    fig = plt.figure(figsize=(W, 6.4), dpi=100)
    header(fig, "크립토 한 주 · 주요 코인", f"{md(c['week_start'])}~{md(c['week_end'])} (UTC 일봉 7개) · 왼쪽 BTC 60일, 오른쪽 시총 상위 7일 등락")
    t = top_of(fig, 1.3)
    ax = fig.add_axes([0.07, 0.1, 0.5, t - 0.1])
    bd = c["btc_daily"]
    ys = [x["c"] / 1000 for x in bd]
    ax.axvspan(len(ys) - 7.5, len(ys) - 0.5, color=SURF2, lw=0)
    ax.plot(range(len(ys)), ys, color=INK, lw=2)
    ax.plot(range(len(ys) - 8, len(ys)), ys[-8:], color=DOWN if ys[-1] < ys[-8] else UP, lw=3)
    ax.text(len(ys) - 4, max(ys) * 0.995, "이번 주", ha="center", va="top", color=MUTED, fontsize=10)
    ax.text(len(ys) - 0.6, ys[-1], f" {ys[-1]:.1f}K", va="center", color=INK, fontsize=12, weight="bold")
    ax.set_xlim(0, len(ys) + 6)
    tk = list(range(0, len(ys), 14))
    ax.set_xticks(tk); ax.set_xticklabels([md(bd[i]["d"]) for i in tk])
    ax.yaxis.set_major_formatter(lambda v, _: f"{v:.0f}K"); ax.grid(True, axis="y")
    for sp in ("top", "right"):
        ax.spines[sp].set_visible(False)
    rows = [{"label": x["sym"], "w1": x["w1"]} for x in c.get("leaders", [])][:12] or [{"label": m["label"], "w1": m["w1"]} for m in c["majors"]]
    ax2 = fig.add_axes([0.62, 0.08, 0.36, t - 0.08]); hbars(ax2, rows, label_fs=11)
    footer(fig, "BTC Coinbase 일봉 종가 · 등락 CoinGecko 7일 · 빨강 상승, 파랑 하락")
    fig.savefig(path); plt.close(fig)


def c2_movers(d, path):
    c = d["crypto"]; t100 = c["top100"]
    rows = [{"label": x["sym"], "w1": x["w1"]} for x in t100["top"] + t100["bottom"][::-1]]
    fig = plt.figure(figsize=(W, 1.9 + 0.42 * len(rows)), dpi=100)
    header(fig, "시총 상위 100 · 주간 상승·하락", f"{t100['n']}종 중 상승 {t100['up']}종 · 중앙값 {t100['median_w1']:+.1f}% · BTC보다 덜 빠진 코인 {t100['beat_btc']}종 (스테이블·래핑 제외)")
    ax = fig.add_axes([0.04, 0.06, 0.92, top_of(fig, 1.3) - 0.06]); hbars(ax, rows)
    footer(fig, "7일 등락률 · 데이터 CoinGecko")
    fig.savefig(path); plt.close(fig)


def c3_flows(d, path):
    c = d["crypto"]
    fig = plt.figure(figsize=(W, 9.2), dpi=100)
    header(fig, "심리 · 자금 흐름", "공포탐욕 지수 30일 · 미국 현물 BTC ETF 일별 순유입 · 스테이블코인 총공급 40일")
    t = top_of(fig, 1.35)
    h = (t - 0.07) / 3
    # fear & greed
    ax = fig.add_axes([0.08, t - h + 0.05, 0.86, h - 0.075])
    fg = c["fng"]["series"]
    ys = [x["v"] for x in fg]
    ax.axhspan(0, 25, color=DOWN, alpha=0.10, lw=0); ax.axhspan(75, 100, color=UP, alpha=0.10, lw=0)
    ax.plot(range(len(ys)), ys, color=S[3], lw=2.4)
    ax.text(len(ys) - 0.7, ys[-1], f" {ys[-1]}", color=INK, fontsize=13, weight="bold", va="center")
    ax.text(0.2, 4, "극단적 공포 구간(25 이하)", color=MUTED, fontsize=9.5)
    ax.set_ylim(0, 100); ax.set_xlim(0, len(ys) + 1.5)
    tk = list(range(0, len(ys), 7)); ax.set_xticks(tk); ax.set_xticklabels([md(fg[i]["d"]) for i in tk])
    ax.set_title("공포탐욕 지수", loc="left", fontsize=12.5, color=INK2, pad=6); ax.grid(True, axis="y")
    # etf
    ax = fig.add_axes([0.08, t - 2 * h + 0.05, 0.86, h - 0.075])
    e = c["etf"]["days"]
    v = [x["v"] / 1000 for x in e]
    ax.bar(range(len(v)), v, color=[UP if x > 0 else DOWN for x in v], width=0.62)
    ax.axhline(0, color=MUTED, lw=0.8)
    for i, x in enumerate(v):
        ax.text(i, x + (0.15 if x >= 0 else -0.15), f"{x:+.1f}", ha="center", va="bottom" if x >= 0 else "top", fontsize=10, color=INK2)
    ax.set_xticks(range(len(v))); ax.set_xticklabels([md(x["d"]) for x in e])
    ax.margins(y=0.25)
    ax.set_title("미국 현물 BTC ETF 순유입 (천 BTC)", loc="left", fontsize=12.5, color=INK2, pad=6); ax.grid(True, axis="y")
    # stables
    ax = fig.add_axes([0.08, t - 3 * h + 0.05, 0.86, h - 0.075])
    st = c["stables"]["series"]
    ys = [x["v"] / 1e9 for x in st]
    ax.plot(range(len(ys)), ys, color=S[2], lw=2.4)
    ax.text(len(ys) - 0.7, ys[-1], f" {ys[-1]:.1f}", color=INK, fontsize=12, weight="bold", va="center")
    ax.set_xlim(0, len(ys) + 2.5)
    tk = list(range(0, len(ys), 7)); ax.set_xticks(tk); ax.set_xticklabels([md(st[i]["d"]) for i in tk])
    ax.set_title("스테이블코인 총공급 (십억 달러)", loc="left", fontsize=12.5, color=INK2, pad=6); ax.grid(True, axis="y")
    for a in fig.axes:
        for sp in ("top", "right"):
            a.spines[sp].set_visible(False)
    footer(fig, "데이터 alternative.me · bitcoin-data.com · DefiLlama")
    fig.savefig(path); plt.close(fig)


def main(week):
    d = json.load(open(os.path.join(HERE, "out", week, "data.json")))
    img = os.path.join(HERE, "out", week, "img")
    os.makedirs(img, exist_ok=True)
    jobs = [("s1_indices.png", s1_indices), ("s2_sectors.png", s2_sectors), ("s3_movers.png", s3_movers),
            ("s4_path.png", s4_path), ("c1_majors.png", c1_majors), ("c2_movers.png", c2_movers), ("c3_flows.png", c3_flows)]
    for name, fn in jobs:
        try:
            fn(d, os.path.join(img, name)); print("ok", name)
        except Exception as e:  # noqa: BLE001
            print("FAIL", name, repr(e))


if __name__ == "__main__":
    main(sys.argv[1])
