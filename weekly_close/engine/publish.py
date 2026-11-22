# -*- coding: utf-8 -*-
"""Publish the weekly close report.

python3 publish.py <week> <narrative.json> [--only stock|crypto] [--resend]
  1. docs/weekly-close/<week>/{index.html,blog.md,img/*.png}, docs/weekly-close/index.html (redirect to latest), archive.html
  2. weekly_close/data/{stock,crypto}.json outbox {text, caption, photos} -> push -> Brief Hub sends album + text per message
  3. waits until brief_hub/data/sent.json records both outbox ids ("SENT OK")
"""
import argparse, json, os, shutil, subprocess, sys, time, atexit, tempfile
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import render  # noqa: E402

REPO = "jinhae8971/korea-etf-calmar"
CLONE = tempfile.mkdtemp(prefix="kec_publish_")       # fresh clone per run, removed on exit: no long-lived checkout
atexit.register(shutil.rmtree, CLONE, True)


def fresh_clone():
    """Shallow-clone main into CLONE. Commits made there use the global git identity and signing key."""
    if os.path.isdir(os.path.join(CLONE, ".git")):
        return
    r = subprocess.run(["git", "clone", "-q", "--depth", "1", "--branch", "main", f"https://github.com/{REPO}", CLONE],
                       capture_output=True, text=True)
    if r.returncode:
        sys.exit(f"clone failed (add_repo {REPO} push first?): {r.stderr.strip()[:300]}")
KST = timezone(timedelta(hours=9))
IMGS = {"stock": [f for f, _ in render.STOCK_IMGS], "crypto": [f for f, _ in render.CRYPTO_IMGS]}
ORDER = {"stock": ["s1_indices.png", "s4_path.png", "s2_sectors.png", "s3_movers.png"], "crypto": IMGS["crypto"]}


def sh(*cmd, check=True):
    r = subprocess.run(cmd, cwd=CLONE, capture_output=True, text=True)
    if check and r.returncode:
        raise RuntimeError(f"{' '.join(cmd)}\n{r.stdout}\n{r.stderr}")
    return r.stdout.strip()


def sent_state():
    fresh_clone()
    sh("git", "fetch", "--depth", "1", "origin", "main")
    try:
        return json.loads(sh("git", "show", "origin/main:brief_hub/data/sent.json"))
    except Exception:  # noqa: BLE001
        return {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("week"); ap.add_argument("narrative"); ap.add_argument("--only", choices=["stock", "crypto"])
    ap.add_argument("--resend", action="store_true", help="append a suffix to the outbox id so the hub sends again")
    a = ap.parse_args()
    odir = os.path.join(HERE, "out", a.week)
    d, n = render.main(a.week, a.narrative)
    kinds = [a.only] if a.only else ["stock", "crypto"]
    for k in kinds:
        if d[k].get("stale"):
            sys.exit(f"{k} data is stale: rerun collect.py")
        for f in IMGS[k]:
            if not os.path.exists(os.path.join(odir, "img", f)):
                sys.exit(f"missing chart {f}: run charts.py {a.week}")
    fresh_clone()

    base = os.path.join(CLONE, "docs", "weekly-close")
    wk = os.path.join(base, a.week)
    os.makedirs(os.path.join(wk, "img"), exist_ok=True)
    for f in os.listdir(os.path.join(odir, "img")):
        shutil.copy(os.path.join(odir, "img", f), os.path.join(wk, "img", f))
    shutil.copy(os.path.join(odir, "index.html"), os.path.join(wk, "index.html"))
    shutil.copy(os.path.join(odir, "blog.md"), os.path.join(wk, "blog.md"))
    weeks = sorted([x for x in os.listdir(base) if x[:2] == "20" and os.path.isdir(os.path.join(base, x))], reverse=True)
    titles = {}
    for x in weeks:
        p = os.path.join(CLONE, "weekly_close", "engine", "narratives", f"{x}.json")
        titles[x] = n["title"] if x == a.week else (json.load(open(p)).get("title", x) if os.path.exists(p) else x)
    open(os.path.join(base, "index.html"), "w").write(
        f"<!doctype html><meta charset='utf-8'><meta http-equiv='refresh' content='0; url={weeks[0]}/'>"
        f"<title>주간 마감 리포트</title><a href='{weeks[0]}/'>최신 주간 마감 리포트</a>")
    open(os.path.join(base, "archive.html"), "w").write(
        "<!doctype html><html lang='ko'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>주간 마감 리포트 지난 호</title><style>{render.CSS}</style><main><h1>주간 마감 리포트</h1><ul>"
        + "".join(f"<li><a href='{x}/'>{x} 주 · {render.e(titles[x])}</a></li>" for x in weeks) + "</ul></main></html>")

    # engine backup (the workspace can be reset): code + runbook + narratives
    eng = os.path.join(CLONE, "weekly_close", "engine")
    os.makedirs(os.path.join(eng, "narratives"), exist_ok=True)
    for f in os.listdir(HERE):
        if f.endswith(".py") or f == "RUNBOOK.md":
            shutil.copy(os.path.join(HERE, f), os.path.join(eng, f))
    shutil.copy(a.narrative, os.path.join(eng, "narratives", f"{a.week}.json"))
    shutil.copy(os.path.join(odir, "data.json"), os.path.join(eng, "narratives", f"{a.week}.data.json"))

    suffix = f":r{datetime.now(KST):%H%M}" if a.resend else ""
    ids = {}
    os.makedirs(os.path.join(CLONE, "weekly_close", "data"), exist_ok=True)
    for k in kinds:
        ids[k] = f"weekly_{k}:{a.week}{suffix}"
        out = {"as_of_kst": datetime.now(KST).strftime("%Y-%m-%dT%H:%M:%S+09:00"), "week": a.week,
               "report": f"{render.REPORT_URL}{a.week}/#{k}",
               "outbox": {"id": ids[k], "kind": "weekly", "text": open(os.path.join(odir, f"telegram_{k}.html")).read(),
                          "caption": open(os.path.join(odir, f"caption_{k}.html")).read(),
                          "photos": [f"docs/weekly-close/{a.week}/img/{f}" for f in ORDER[k]]}}
        json.dump(out, open(os.path.join(CLONE, "weekly_close", "data", f"{k}.json"), "w"), ensure_ascii=False, indent=1)
    sh("git", "add", "docs/weekly-close", "weekly_close")
    if sh("git", "status", "--porcelain"):
        sh("git",
           "commit", "-m", f"data(weekly-close): {a.week} weekly")
        for i in range(4):
            try:
                sh("git", "-c", "push.negotiate=true", "push", "origin", "HEAD:main"); break
            except RuntimeError:
                if i == 3:
                    raise
                sh("git", "pull", "--rebase", "--depth", "1", "origin", "main", check=False); time.sleep(5)
        print("pushed", sh("git", "rev-parse", "--short", "HEAD"), ids)
    else:
        print("nothing new to push; checking sent state", ids)
    for _ in range(40):                                   # up to ~10 min
        time.sleep(15)
        st = sent_state()
        done = {k: st.get(f"weekly_{k}", {}).get("last_key") == ids[k] for k in kinds}
        if all(done.values()):
            print("SENT OK", ids, "·", f"{render.REPORT_URL}{a.week}/"); return
    sys.exit(f"hub did not record {ids} in sent.json (state: {done}) — check Actions > Brief Hub")


if __name__ == "__main__":
    main()
