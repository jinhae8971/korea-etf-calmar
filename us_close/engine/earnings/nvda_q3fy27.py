# -*- coding: utf-8 -*-
"""NVIDIA Q3 FY27 (reported 2026-11-17) — figures for the Telegram brief and blog.

All figures come from NVIDIA's 8-K press releases (Q4 FY26, Q1/Q2/Q3 FY27), the Q3 FY27 CFO commentary,
Reuters/LSEG consensus as quoted by IBTimes/AOL, and Yahoo Finance prices. See SOURCES.
"""
import json, os, sys, urllib.request, datetime as dt
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager as fm
import numpy as np

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out", "NVDA_2026-11-17")
os.makedirs(OUT, exist_ok=True)

SURF, SURF2, SURF3 = "#1a1a19", "#242422", "#2c2c29"
INK, INK2, MUTED, GRID = "#f0efec", "#c3c2b7", "#8f8e86", "#33332f"
UP, DOWN, ACC, ACC2, GOLD = "#e66767", "#3987e5", "#76b900", "#199e70", "#c98500"
for f in fm.findSystemFonts():
    if "NotoSansCJK-Regular" in f or "NotoSansCJK-Bold" in f:
        fm.fontManager.addfont(f)
plt.rcParams.update({
    "font.family": ["Noto Sans CJK KR", "DejaVu Sans"], "axes.unicode_minus": False,
    "figure.facecolor": SURF, "axes.facecolor": SURF, "savefig.facecolor": SURF,
    "text.color": INK, "axes.labelcolor": INK2, "xtick.color": MUTED, "ytick.color": MUTED,
    "axes.edgecolor": GRID, "grid.color": GRID, "grid.linewidth": 0.6, "font.size": 11,
})

# ----------------------------------------------------------------------------- data (sourced)
Q = ["4Q FY25", "1Q FY26", "2Q FY26", "3Q FY26", "4Q FY26", "1Q FY27", "2Q FY27", "3Q FY27", "4Q FY27\n가이던스"]
QEND = ["25.1", "25.4", "25.7", "25.10", "26.1", "26.4", "26.7", "26.10", "27.1"]
REV = [39.331, 44.062, 46.743, 57.006, 68.127, 81.615, 96.221, 115.8, 130.0]   # $B
GM = [73.5, 60.8, 72.5, 73.5, 75.1, 75.0, 75.0, 74.9, 71.5]                    # non-GAAP %, 4Q FY27 = midpoint of 71-72
EPS = [0.89, 0.78, 1.01, 1.27, 1.59, 1.87, 2.22, 2.68, None]                   # non-GAAP diluted
GUIDE_MID = {"1Q FY27": 78.0, "2Q FY27": 91.0, "3Q FY27": 108.0}               # company guidance midpoints
D = dict(
    rev=115.8, rev_est=109.11, eps=2.68, eps_est=2.47, dc=107.6, dc_yoy=110, edge=8.2, edge_yoy=42,
    gm=74.9, gm_est=74.1, gm_q4_lo=71.0, gm_q4_hi=72.0, gm_q4_est=71.7,
    guide=130.0, guide_est=125.25, fy28_growth=75, fy28_prev=70, supply=390, supply_prev=279,
    fcf=43.8, returned=28.1, buyback_add=150, buyback_left=227.3, hyperscale=60.1, china_hopper=2.4,
)


def yahoo(sym, rng, interval, prepost=False):
    u = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range={rng}&interval={interval}" + ("&includePrePost=true" if prepost else "")
    r = json.load(urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"}), timeout=25))["chart"]["result"][0]
    ts = [dt.datetime.fromtimestamp(t, dt.timezone.utc) for t in r["timestamp"]]
    return ts, r["indicators"]["quote"][0]["close"], r["meta"]


def header(fig, title, sub):
    H = fig.get_figheight()
    fig.text(0.04, 1 - 0.30 / H, title, fontsize=20, weight="bold", va="top", color=INK)
    fig.text(0.04, 1 - 0.78 / H, sub, fontsize=11.5, va="top", color=INK2)


def footer(fig, txt):
    fig.text(0.04, 0.018, txt, fontsize=9, color=MUTED)


def eok(b):  # $B -> 억 달러 string (half-up)
    from decimal import Decimal, ROUND_HALF_UP
    v = int(Decimal(str(b * 10)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return f"{v:,}억 달러"


# ----------------------------------------------------------------------------- 1. scorecard
def scorecard(px):
    fig = plt.figure(figsize=(10.8, 8.6))
    header(fig, "엔비디아 3분기(FY27) 성적표", "2026-11-17 장 마감 후 발표 · 괄호 안은 시장 예상치")
    tiles = [
        ("매출", eok(D["rev"]), f"예상 {eok(D['rev_est'])}", f"+{(D['rev']/D['rev_est']-1)*100:.1f}% 상회", UP,
         f"작년 같은 분기보다 +103%"),
        ("주당순이익(조정)", f"{D['eps']:.2f}달러", f"예상 {D['eps_est']:.2f}달러", f"+{(D['eps']/D['eps_est']-1)*100:.1f}% 상회", UP,
         "작년 같은 분기 1.27달러"),
        ("데이터센터 매출", eok(D["dc"]), "전년 대비", f"+{D['dc_yoy']}%", UP,
         f"전체 매출의 {D['dc']/D['rev']*100:.0f}%"),
        ("4분기 매출 가이던스", eok(D["guide"]) + " ±3%", f"예상 {eok(D['guide_est'])}", f"+{(D['guide']/D['guide_est']-1)*100:.1f}% 상회", UP,
         "14주 분기 · 중국 데이터센터 매출 0 가정"),
        ("총이익률(조정)", f"{D['gm']:.1f}%", f"예상 {D['gm_est']:.1f}%", f"+{D['gm']-D['gm_est']:.1f}%p", UP,
         f"4분기 전망 {D['gm_q4_lo']:.0f}~{D['gm_q4_hi']:.0f}% (예상 {D['gm_q4_est']}%)"),
        ("주가 반응", f"시간외 {px['ah_pct']:+.1f}%", f"정규장 {px['close']:.2f}달러", f"→ {px['ah']:.2f}달러", UP if px['ah_pct'] >= 0 else DOWN,
         f"10월 고점({px['hi']:.2f}) 대비 {px['dd']:.0f}%"),
    ]
    cols, rows = 3, 2
    x0, y0, cw, ch, gx, gy = 0.04, 0.845, 0.293, 0.315, 0.0165, 0.035
    for i, (lab, big, ref, delta, col, foot) in enumerate(tiles):
        r, c = divmod(i, cols)
        x = x0 + c * (cw + gx); y = y0 - r * (ch + gy) - ch
        ax = fig.add_axes([x, y, cw, ch]); ax.set_xlim(0, 1); ax.set_ylim(0, 1); ax.axis("off")
        ax.add_patch(plt.Rectangle((0, 0), 1, 1, color=SURF2, transform=ax.transAxes, clip_on=False))
        ax.add_patch(plt.Rectangle((0, 0.965), 1, 0.035, color=col, transform=ax.transAxes, clip_on=False))
        ax.text(0.07, 0.85, lab, fontsize=12.5, color=INK2, va="top")
        ax.text(0.07, 0.66, big, fontsize=20 if len(big) < 11 else 16.5, weight="bold", color=INK, va="top")
        ax.text(0.07, 0.40, ref, fontsize=11, color=MUTED, va="top")
        ax.text(0.07, 0.27, delta, fontsize=14, weight="bold", color=col, va="top")
        ax.text(0.07, 0.10, foot, fontsize=9.6, color=INK2, va="bottom")
    # bottom strip
    ax = fig.add_axes([0.04, 0.07, 0.92, 0.07]); ax.axis("off")
    ax.add_patch(plt.Rectangle((0, 0), 1, 1, color=SURF3, transform=ax.transAxes, clip_on=False))
    ax.text(0.02, 0.5, f"콜 핵심   내년(FY28) 매출 성장 전망 {D['fy28_prev']}% → {D['fy28_growth']}%   ·   공급 약정 {eok(D['supply_prev']).replace(' 달러','')} → {eok(D['supply'])}   ·   "
                      f"3분기 주주환원 {eok(D['returned'])}", fontsize=11, color=INK, va="center")
    footer(fig, "예상치: 매출·가이던스 LSEG(로이터), EPS 컨센서스 2.47달러 · 회사 자료: NVIDIA 8-K · 주가: Yahoo Finance")
    p = os.path.join(OUT, "01_scorecard.png"); fig.savefig(p, dpi=150); plt.close(fig); return p


# ----------------------------------------------------------------------------- 2. quarterly trend
def trend():
    fig = plt.figure(figsize=(10.8, 7.6))
    header(fig, "분기 매출과 총이익률", "막대: 분기 매출(억 달러) · 선: 조정 총이익률 · 마지막 막대는 회사 가이던스(중간값)")
    ax = fig.add_axes([0.075, 0.13, 0.85, 0.66])
    x = np.arange(len(Q))
    cols = [SURF3] * 4 + ["#4b4b46"] * 3 + [ACC] + ["none"]
    bars = ax.bar(x, REV, color=cols, width=0.62, edgecolor=[c if c != "none" else ACC for c in cols], linewidth=1.2)
    bars[-1].set_hatch("///"); bars[-1].set_edgecolor(ACC)
    for i, v in enumerate(REV):
        ax.text(i, v + 2.5, f"{v*10:,.0f}", ha="center", fontsize=10.5, color=INK if i >= 7 else INK2, weight="bold" if i >= 7 else None)
    yoy = {4: 68.127 / 39.331, 5: 81.615 / 44.062, 6: 96.221 / 46.743, 7: 115.8 / 57.006, 8: 130.0 / 68.127}
    for i, r in yoy.items():
        ax.text(i, REV[i] / 2, f"+{(r-1)*100:.0f}%\n전년비", ha="center", va="center", fontsize=9, color=INK if i >= 7 else INK2,
                bbox=dict(boxstyle="round,pad=0.25", fc=SURF, ec="none") if i == 8 else None)
    for qn, g in GUIDE_MID.items():
        i = Q.index(qn)
        ax.plot([i - 0.33, i + 0.33], [g, g], color=GOLD, lw=2)
    ax.text(5, GUIDE_MID["1Q FY27"] - 9, "직전 가이던스", fontsize=8.5, color=GOLD, ha="center")
    ax.set_xticks(x); ax.set_xticklabels([f"{q}\n({e})" for q, e in zip(Q, QEND)], fontsize=9.2)
    ax.set_ylim(0, 190); ax.set_yticks([0, 50, 100, 150]); ax.set_yticklabels(["0", "500", "1,000", "1,500"])
    ax.grid(axis="y"); ax.set_axisbelow(True)
    for s in ["top", "right"]: ax.spines[s].set_visible(False)
    ax2 = ax.twinx()
    ax2.plot(x, GM, color=UP, marker="o", lw=2, ms=5)
    for i in [0, 1, 4, 7, 8]:
        ax2.annotate(f"{GM[i]:.1f}%" if i != 8 else "71~72%", (x[i], GM[i]), textcoords="offset points", xytext=(0, 9 if i != 1 else -17),
                     ha="center", fontsize=9.5, color=UP)
    ax2.set_ylim(20, 80); ax2.set_yticks([60, 70, 80]); ax2.set_yticklabels(["60%", "70%", "80%"], color=UP)
    for s in ["top", "left"]: ax2.spines[s].set_visible(False)
    ax2.spines["right"].set_color(GRID)
    footer(fig, "괄호: 분기 말(연.월) · 노란 선: 직전 분기에 회사가 낸 매출 가이던스 중간값 · 1Q FY26 이익률 급락은 H20 관련 비용 · 자료 NVIDIA 8-K")
    p = os.path.join(OUT, "02_trend.png"); fig.savefig(p, dpi=150); plt.close(fig); return p


# ----------------------------------------------------------------------------- 3. price
def price(px):
    ts, cl, _ = yahoo("NVDA", "6mo", "1d")
    pts = [(t, c) for t, c in zip(ts, cl) if c]
    fig = plt.figure(figsize=(10.8, 6.6))
    header(fig, "주가는 고점 대비 −35%에서 실적을 맞았어요", f"엔비디아 일봉 종가(달러) · 최근 6개월 · 실적 후 시간외 {px['ah']:.2f}달러({px['ah_pct']:+.1f}%)")
    ax = fig.add_axes([0.075, 0.12, 0.88, 0.66])
    xs = [t for t, _ in pts]; ys = [c for _, c in pts]
    ax.plot(xs, ys, color=INK2, lw=1.6)
    ax.fill_between(xs, ys, min(ys) * 0.92, color=INK2, alpha=0.06)
    ev = [("2026-10-06", "고점 239.24 (10/6)", -150, 6), ("2026-11-05", "11/5 AI 데이터센터 둔화 공포 181.60", -265, 4),
          ("2026-11-12", "11/12 CEO 사임 발표 166.19", -235, -6), ("2026-11-17", "11/17 실적 발표일 종가 155.46", -250, -30)]
    lookup = {t.date().isoformat(): c for t, c in pts}
    for d0, lab, dx, dy in ev:
        if d0 in lookup:
            t = dt.datetime.fromisoformat(d0).replace(tzinfo=dt.timezone.utc)
            ax.scatter([t], [lookup[d0]], color=GOLD if "고점" in lab else INK, s=26, zorder=5)
            ax.annotate(lab, (t, lookup[d0]), textcoords="offset points", xytext=(dx, dy), fontsize=10, color=INK2,
                        arrowprops=dict(arrowstyle="-", color=MUTED, lw=0.8))
    t_ah = xs[-1] + dt.timedelta(days=1)
    ax.scatter([t_ah], [px["ah"]], color=UP if px["ah_pct"] >= 0 else DOWN, s=46, zorder=6)
    ax.annotate(f"시간외 {px['ah_pct']:+.1f}%", (t_ah, px["ah"]), textcoords="offset points", xytext=(12, 18), ha="right",
                fontsize=10.5, color=UP if px["ah_pct"] >= 0 else DOWN, weight="bold", bbox=dict(boxstyle="round,pad=0.25", fc=SURF, ec="none"))
    ax.grid(axis="y"); ax.set_axisbelow(True)
    for s in ["top", "right"]: ax.spines[s].set_visible(False)
    ax.set_xlim(xs[0], xs[-1] + dt.timedelta(days=9)); ax.set_ylim(min(ys) * 0.88, max(ys) * 1.06)
    import matplotlib.dates as mdates
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%y.%m"))
    footer(fig, f"올해 수익률 {px['ytd']:+.1f}%(11/17 종가 기준) · 자료 Yahoo Finance")
    p = os.path.join(OUT, "03_price.png"); fig.savefig(p, dpi=150); plt.close(fig); return p


def price_info():
    ts, cl, meta = yahoo("NVDA", "1d", "5m", prepost=True)
    last = [c for c in cl if c][-1]
    close = meta["regularMarketPrice"]
    ts2, cl2, _ = yahoo("NVDA", "2y", "1d")
    rows = [(t.date().isoformat(), c) for t, c in zip(ts2, cl2) if c]
    hi = max(rows, key=lambda r: r[1])
    prev_year = [r for r in rows if r[0] < "2026-01-01"][-1][1]
    return dict(close=close, ah=last, ah_pct=(last / close - 1) * 100, hi=hi[1], hi_date=hi[0],
                dd=(close / hi[1] - 1) * 100, ytd=(close / prev_year - 1) * 100)


if __name__ == "__main__":
    px = price_info()
    print(json.dumps(px))
    print(scorecard(px)); print(trend()); print(price(px))
    json.dump(dict(px=px, D=D, Q=Q, REV=REV, GM=GM, EPS=EPS), open(os.path.join(OUT, "data.json"), "w"), ensure_ascii=False, indent=1)
