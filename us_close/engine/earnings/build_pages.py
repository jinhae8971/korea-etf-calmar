#!/usr/bin/env python3
"""Build the GitHub Pages copy of US Earnings Desk from the dashboard page in desk/.

    python3 build_pages.py <out_dir>

Writes <out_dir>/index.html, <out_dir>/data/*.json and <out_dir>/assets/d3.v7.9.0.min.js.
desk/index.html stays the single source of the page: this script wraps it with a small
fetch-based `dash` runtime, light/dark theme tokens, fonts, share (OG) tags and a
data-sources section with JSON links and CSV downloads.
"""
import json
import os
import re
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DESK = os.path.join(HERE, "desk")
IDS = ["quarters", "scorecard", "report", "price", "eps", "notes", "reactions", "calendar"]
SITE = "https://jinhae8971.github.io/korea-etf-calmar/earnings/"
D3 = "d3.v7.9.0.min.js"

LIGHT = ("--color-fg:#1f1e1c;--color-fg-muted:#6b6a64;--color-bg:#ffffff;--color-panel:#faf9f6;--color-border-line:#e6e3dc;"
         "--color-ok:#2f7d4f;--color-warn:#b7791f;--color-bad:#c0392b;--cds-text-secondary:#6b6a64;--cds-chart-muted:#d5d2ca;"
         "--cds-chart-grid:#ece9e2;--cds-chart-axis:#c9c5bb;--cds-chart-reference:#9a978f;--cds-surface-popover:#ffffff;"
         "--cds-border:#e0ddd5;--cds-shadow-popover:0 4px 14px rgba(0,0,0,.12);--c0:#2e6bd1;--c1:#d58a12;--c2:#16926b;--c3:#8f5bd6;")
DARK = ("--color-fg:#ecebe6;--color-fg-muted:#a3a19a;--color-bg:#1b1b1a;--color-panel:#242422;--color-border-line:#3a3936;"
        "--color-ok:#55b57f;--color-warn:#d9a441;--color-bad:#e5675f;--cds-text-secondary:#a3a19a;--cds-chart-muted:#4a4945;"
        "--cds-chart-grid:#33322f;--cds-chart-axis:#55534e;--cds-chart-reference:#7d7b74;--cds-surface-popover:#2c2c2a;"
        "--cds-border:#454440;--cds-shadow-popover:0 4px 14px rgba(0,0,0,.5);--c0:#5b93ee;--c1:#e3a43a;--c2:#3fbf91;--c3:#a983ea;")
FIXED = ("--font-anthropic-sans:\"Pretendard\";--font-anthropic-serif:Georgia;"
         "--cds-font-size-caption:12px;--cds-font-size-body:14px;--cds-font-size-heading:18px;--cds-font-size-title:32px;"
         "--cds-font-weight-medium:600;--cds-gap-xs:4px;--cds-gap-sm:8px;--cds-gap-md:14px;--cds-gap-lg:20px;--cds-gap-xl:32px;"
         "--cds-pad-sm:8px;--cds-pad-md:12px;--cds-pad-lg:16px;--cds-radius:8px;")
CHEVRON = ("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 12 12'%3E"
           "%3Cpath d='M2.5 4.5 6 8l3.5-3.5' fill='none' stroke='%238a877f' stroke-width='1.6' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E")
FAVICON = ("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='7' fill='%232e6bd1'/%3E"
           "%3Crect x='7' y='17' width='4' height='8' rx='1' fill='white'/%3E%3Crect x='14' y='12' width='4' height='13' rx='1' fill='white'/%3E"
           "%3Crect x='21' y='7' width='4' height='18' rx='1' fill='white'/%3E%3C/svg%3E")

BASE_CSS = """
:root{color-scheme:light;%(light)s%(fixed)s}
@media (prefers-color-scheme: dark){:root{color-scheme:dark;%(dark)s}}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%%}
body{margin:0;background:var(--color-bg);color:var(--color-fg);font-family:"Pretendard",-apple-system,BlinkMacSystemFont,"Apple SD Gothic Neo","Malgun Gothic","Noto Sans KR",system-ui,sans-serif;font-size:14px;line-height:1.5}
.ued-wrap{max-width:1200px;margin:0 auto;padding:32px 24px 56px}
@media (max-width:520px){.ued-wrap{padding:20px 16px 40px}}
.dash-select{font:inherit;font-size:14px;color:var(--color-fg);background-color:var(--color-panel);background-image:url("%(chevron)s");background-repeat:no-repeat;background-position:right 10px center;background-size:12px;border:1px solid var(--color-border-line);border-radius:var(--cds-radius);padding:7px 32px 7px 12px;-webkit-appearance:none;appearance:none;cursor:pointer}
.dash-select:focus-visible{outline:2px solid var(--color-fg);outline-offset:2px}
a.ued-btn{display:inline-block;text-decoration:none}
.ued-srclist{list-style:none;padding:0;margin:0;display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%%,320px),1fr));gap:var(--cds-gap-md)}
.ued-src{display:flex;flex-direction:column;gap:6px}
.ued-src h3{margin:0;font-size:var(--cds-font-size-body);font-weight:var(--cds-font-weight-medium)}
.ued-src p{margin:0;font-size:var(--cds-font-size-caption);color:var(--color-fg-muted);line-height:1.55}
.ued-srcmeta{display:flex;flex-wrap:wrap;align-items:center;gap:6px 10px;margin-top:4px;font-size:var(--cds-font-size-caption);color:var(--color-fg-muted)}
.ued-srcmeta span{margin-right:auto;font-variant-numeric:tabular-nums}
.ued-noscript{padding:12px 16px;border:1px solid var(--color-border-line);border-radius:var(--cds-radius);background:var(--color-panel)}
""" % {"light": LIGHT, "dark": DARK, "fixed": FIXED, "chevron": CHEVRON}

# Fetch-based stand-in for the dashboard runtime the page was written against.
SHIM = r"""
(function () {
  var IDS = %(ids)s;
  var DEFAULTS = { ticker: "NVDA" };
  var store = {}, draws = {}, onDataFns = [];
  IDS.forEach(function (id) { store[id] = { status: "loading", data: [], columns: [] }; });
  function readParams() {
    var p = Object.assign({}, DEFAULTS);
    try { var t = new URLSearchParams(location.search).get("ticker"); if (t) p.ticker = t.toUpperCase(); } catch (e) {}
    return p;
  }
  var params = readParams();
  function runDraw(name) {
    var fn = draws[name];
    if (!fn) return;
    document.querySelectorAll('[data-draw="' + name + '"]').forEach(function (el) {
      try { fn(el); } catch (e) { console.error("[desk] draw " + name, e); }
    });
  }
  function runAll() {
    onDataFns.forEach(function (f) { try { f(); } catch (e) { console.error("[desk] onData", e); } });
    Object.keys(draws).forEach(runDraw);
  }
  function syncUrl() {
    try {
      var u = new URL(location.href);
      if (params.ticker && params.ticker !== DEFAULTS.ticker) u.searchParams.set("ticker", params.ticker);
      else u.searchParams.delete("ticker");
      history.replaceState(null, "", u.toString());
    } catch (e) {}
  }
  window.dash = {
    data: function (id) { return store[id] || { status: "missing", data: [], columns: [] }; },
    onData: function (fn) { onDataFns.push(fn); try { fn(); } catch (e) { console.error("[desk] onData", e); } },
    draw: function (name, fn) { if (typeof fn === "function") draws[name] = fn; else runDraw(name); },
    params: function () { return Object.assign({}, params); },
    setParams: function (o) {
      Object.keys(o || {}).forEach(function (k) { if (o[k] == null) delete params[k]; else params[k] = o[k]; });
      if (!params.ticker) params.ticker = DEFAULTS.ticker;
      syncUrl(); runAll();
    },
    resetParams: function () { params = Object.assign({}, DEFAULTS); syncUrl(); runAll(); },
    colors: ["var(--c0)", "var(--c1)", "var(--c2)", "var(--c3)"],
    refresh: function () {}, setLink: function () {}
  };
  window.__uedStart = function () {
    runAll();
    Promise.all(IDS.map(function (id) {
      return fetch("data/" + id + ".json", { cache: "no-cache" })
        .then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
        .then(function (j) {
          var rows = Array.isArray(j) ? j : (j && j.rows) || [];
          store[id] = { status: "ok", data: rows, columns: rows.length ? Object.keys(rows[0]) : [] };
        })
        .catch(function (e) { store[id] = { status: "error", data: [], columns: [], message: String(e) }; });
    })).then(runAll);
    var lastW = window.innerWidth, timer = null;
    window.addEventListener("resize", function () {
      if (window.innerWidth === lastW) return;
      lastW = window.innerWidth; clearTimeout(timer); timer = setTimeout(runAll, 150);
    });
    /* charts measure their labels, so draw again once the web font has arrived */
    if (document.fonts && document.fonts.addEventListener) {
      var ft = null;
      document.fonts.addEventListener("loadingdone", function () { clearTimeout(ft); ft = setTimeout(runAll, 60); });
    }
  };
})();
""" % {"ids": json.dumps(IDS + ["datasets"])}

SOURCES_HTML = """
  <section class="ued-sec" id="sources" aria-labelledby="h-sources">
    <h2 id="h-sources">데이터 출처</h2>
    <p class="ued-note">페이지의 모든 숫자는 아래 파일에서 나와요. JSON으로 열어 보거나 CSV로 내려받아 블로그용 표·차트에 그대로 쓸 수 있어요.</p>
    <ul class="ued-srclist" data-draw="srclist"></ul>
  </section>
"""

SOURCES_JS = r"""
(function () {
  function el(tag, cls, text) { var e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }
  function kst(iso) {
    var t = new Date(iso);
    return isNaN(t) ? "" : d3.utcFormat("%-m/%-d %H:%M")(new Date(t.getTime() + 9 * 3600000)) + " KST";
  }
  function toCsv(rows) {
    var cols = [];
    rows.forEach(function (r) { Object.keys(r).forEach(function (k) { if (cols.indexOf(k) < 0) cols.push(k); }); });
    function q(v) {
      if (v == null) return "";
      var s = typeof v === "object" ? JSON.stringify(v) : String(v);
      return /[",\r\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
    }
    return "﻿" + [cols.join(",")].concat(rows.map(function (r) { return cols.map(function (c) { return q(r[c]); }).join(","); })).join("\r\n");
  }
  dash.draw("srclist", function (ul) {
    var m = dash.data("datasets");
    ul.replaceChildren();
    if (m.status !== "ok") { ul.appendChild(el("li", "ued-cap", m.status === "loading" ? "—" : "불러오지 못했어요")); return; }
    m.data.forEach(function (s) {
      var li = el("li", "ued-card ued-src");
      li.id = "src-" + s.id;
      li.appendChild(el("h3", null, s.title));
      if (s.description) li.appendChild(el("p", null, s.description));
      var meta = el("div", "ued-srcmeta");
      meta.appendChild(el("span", null, "갱신 " + kst(s.updated_at)));
      var a = el("a", "ued-btn", "JSON");
      a.href = "data/" + s.id + ".json"; a.target = "_blank"; a.rel = "noopener";
      var b = el("button", "ued-btn", "CSV 내려받기");
      b.type = "button";
      b.addEventListener("click", function () {
        var d = dash.data(s.id);
        if (d.status !== "ok") return;
        var url = URL.createObjectURL(new Blob([toCsv(d.data)], { type: "text/csv;charset=utf-8" }));
        var link = document.createElement("a");
        link.href = url; link.download = "us-earnings-desk_" + s.id + ".csv";
        document.body.appendChild(link); link.click(); link.remove();
        setTimeout(function () { URL.revokeObjectURL(url); }, 1500);
      });
      meta.append(a, b);
      li.appendChild(meta);
      ul.appendChild(li);
    });
  });
})();
"""


def esc_attr(s):
    return (str(s).replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;"))


def main(out):
    page = open(os.path.join(DESK, "index.html"), encoding="utf-8").read()
    page = page.replace('<meta name="dash-one-writer">\n', "", 1)
    m = re.search(r"<style>(.*?)</style>\s*", page, re.S)
    page_css, page = m.group(1), page[:m.start()] + page[m.end():]
    old_foot = "숫자를 누르면 출처와 계산 방법을 볼 수 있어요. "
    assert old_foot in page, "footer text changed in desk/index.html"
    page = page.replace(old_foot, "", 1)
    anchor = '  <p class="ued-foot">'
    assert anchor in page, "footer anchor missing"
    page = page.replace(anchor, SOURCES_HTML.lstrip("\n") + "\n" + anchor, 1)

    rows = {k: json.load(open(os.path.join(DESK, k + ".json"), encoding="utf-8")) for k in IDS}
    manifest = json.load(open(os.path.join(DESK, "datasets.json"), encoding="utf-8"))
    rp = rows["report"]["rows"][0]
    sc = {r["metric"]: r for r in rows["scorecard"]["rows"] if r["ticker"] == rp["ticker"]}
    img = sorted([r for r in rows["notes"]["rows"] if r["ticker"] == rp["ticker"] and r["section"] == "img" and r.get("url")], key=lambda r: r["n"])
    rev, guide = sc.get("revenue"), sc.get("guide")
    title = f"{rp['name']} {rp['quarter']} 실적 정리 · US Earnings Desk"
    desc = (f"{rp['name']} {rp['quarter']} 매출 {rev['actual']:,.0f}억 달러(예상 대비 {rev['diff']:+.1f}%), "
            f"다음 분기 가이던스 {guide['actual']:,.0f}억 달러(예상 대비 {guide['diff']:+.1f}%). 성적표·차트·쉬운 정리·블로그 키트.")
    og_img = img[0]["url"] if img else ""

    head = f"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc_attr(title)}</title>
<meta name="description" content="{esc_attr(desc)}">
<meta property="og:type" content="website">
<meta property="og:title" content="{esc_attr(title)}">
<meta property="og:description" content="{esc_attr(desc)}">
<meta property="og:url" content="{SITE}">
{f'<meta property="og:image" content="{esc_attr(og_img)}">' if og_img else ''}
<meta name="twitter:card" content="summary_large_image">
<link rel="icon" href="{FAVICON}">
<link rel="preconnect" href="https://cdn.jsdelivr.net">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/static/pretendard.min.css">
<style>{BASE_CSS}</style>
<style>{page_css}</style>
<script src="assets/{D3}"></script>
<script>{SHIM}</script>
</head>
<body>
<main class="ued-wrap">
<noscript><p class="ued-noscript">이 페이지는 자바스크립트를 켜야 숫자와 차트가 보여요.</p></noscript>
"""
    tail = f"""
<script>{SOURCES_JS}</script>
<script>window.__uedStart();</script>
</main>
</body>
</html>
"""
    os.makedirs(os.path.join(out, "data"), exist_ok=True)
    os.makedirs(os.path.join(out, "assets"), exist_ok=True)
    open(os.path.join(out, "index.html"), "w", encoding="utf-8").write(head + page.strip() + "\n" + tail)
    for k in IDS:
        d = rows[k]
        slim = {"rows": d["rows"], "meta": [x for x in d.get("meta", []) if x.get("type") != "code"]}
        json.dump(slim, open(os.path.join(out, "data", k + ".json"), "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    json.dump({"rows": manifest}, open(os.path.join(out, "data", "datasets.json"), "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    shutil.copyfile(os.path.join(HERE, "vendor", D3), os.path.join(out, "assets", D3))
    print("built", out, "| title:", title)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "out", "pages"))
