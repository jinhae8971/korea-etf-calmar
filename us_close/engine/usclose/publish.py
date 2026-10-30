# -*- coding: utf-8 -*-
"""Publish a rendered US close briefing.

python3 publish.py <session> <narrative.json> [--kind daily]
  1. docs/us-close/<session>/{index.html,img/*.png}, docs/us-close/index.html (latest), archive.html
  2. us_close/data/latest.json outbox {text, caption, photos} -> push -> Brief Hub sends album + text
  3. waits for the hub run and confirms brief_hub/data/sent.json recorded the outbox id ("SENT OK")
"""
import argparse, base64, json, os, shutil, subprocess, sys, time, urllib.request
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import render  # noqa: E402

REPO = "jinhae8971/korea-etf-calmar"
CLONE = "/home/claude/korea-etf-calmar"
API = f"https://api.github.com/repos/{REPO}"
KST = timezone(timedelta(hours=9))


def sh(*cmd, check=True):
    r = subprocess.run(cmd, cwd=CLONE, capture_output=True, text=True)
    if check and r.returncode:
        raise RuntimeError(f"{' '.join(cmd)}\n{r.stdout}\n{r.stderr}")
    return r.stdout.strip()


def api(path):
    req = urllib.request.Request(API + path, headers={"Accept": "application/vnd.github+json", "User-Agent": "us-close"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("session"); ap.add_argument("narrative"); ap.add_argument("--kind", default="daily"); ap.add_argument("--text-only", action="store_true")
    a = ap.parse_args()
    sdir = os.path.join(HERE, "out", a.session)
    render.main(a.session, a.narrative)
    d = json.load(open(os.path.join(sdir, "data.json")))
    n = json.load(open(a.narrative))
    imgs = sorted(os.listdir(os.path.join(sdir, "img")))
    if not os.path.isdir(os.path.join(CLONE, ".git")):
        sys.exit(f"clone missing: {CLONE} (add_repo {REPO} push → git clone --depth 1)")
    sh("git", "fetch", "--depth", "1", "origin", "main"); sh("git", "reset", "--hard", "origin/main")

    base = os.path.join(CLONE, "docs", "us-close")
    day = os.path.join(base, a.session)
    os.makedirs(os.path.join(day, "img"), exist_ok=True)
    for f in imgs:
        shutil.copy(os.path.join(sdir, "img", f), os.path.join(day, "img", f))
    open(os.path.join(day, "index.html"), "w").write(render.report(d, n, imgs, "img/").replace("href='archive.html'", "href='../archive.html'"))
    open(os.path.join(base, "index.html"), "w").write(render.report(d, n, imgs, f"{a.session}/img/"))
    days = sorted([x for x in os.listdir(base) if x[:2] == "20" and os.path.isdir(os.path.join(base, x))], reverse=True)
    open(os.path.join(base, "archive.html"), "w").write(
        "<!doctype html><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>미증시 마감 브리핑 지난 호</title><style>{render.CSS}</style><main><h1>지난 브리핑</h1><ul>"
        + "".join(f"<li><a href='{x}/'>{x}</a></li>" for x in days) + "</ul><p><a href='./'>최신 브리핑</a></p></main>")

    # engine backup (this workspace can be reset): code + runbook + probability/snapshot state
    eng = os.path.join(CLONE, "us_close", "engine")
    mb = os.path.dirname(HERE)
    for src, dst in [(os.path.join(mb, "brief.py"), "brief.py"), (os.path.join(mb, "RUNBOOK.md"), "RUNBOOK.md")] + \
            [(os.path.join(HERE, f), f"usclose/{f}") for f in os.listdir(HERE) if f.endswith(".py")] + \
            [(os.path.join(HERE, "state", f), f"usclose/state/{f}") for f in os.listdir(os.path.join(HERE, "state"))] + \
            [(os.path.join(mb, "state", f), f"state/{f}") for f in os.listdir(os.path.join(mb, "state")) if f.startswith("probs_")] + \
            [(a.narrative, f"narratives/{a.session}.json")]:
        os.makedirs(os.path.dirname(os.path.join(eng, dst)), exist_ok=True)
        shutil.copy(src, os.path.join(eng, dst))

    oid = f"us_close:{a.session}:{a.kind}"
    out = {"as_of_kst": datetime.now(KST).strftime("%Y-%m-%dT%H:%M:%S+09:00"), "session": a.session,
           "report": render.REPORT_URL + f"{a.session}/",
           "outbox": {"id": oid, "kind": a.kind,
                      "text": open(os.path.join(sdir, "telegram.html")).read(),
                      "caption": open(os.path.join(sdir, "caption.html")).read(),
                      "photos": [] if a.text_only else [f"docs/us-close/{a.session}/img/{f}" for f in imgs]}}
    os.makedirs(os.path.join(CLONE, "us_close", "data"), exist_ok=True)
    json.dump(out, open(os.path.join(CLONE, "us_close", "data", "latest.json"), "w"), ensure_ascii=False, indent=1)
    sh("git", "add", "docs/us-close", "us_close/data/latest.json", "us_close/engine")
    if not sh("git", "status", "--porcelain"):
        print("nothing to publish"); return
    sh("git", "-c", "user.name=us-close", "-c", "user.email=us-close@users.noreply.github.com",
       "commit", "-m", f"data(us-close): {a.session} {a.kind}")
    for i in range(4):
        try:
            sh("git", "push", "origin", "HEAD:main"); break
        except RuntimeError:
            if i == 3:
                raise
            sh("git", "pull", "--rebase", "--depth", "1", "origin", "main", check=False); time.sleep(5)
    sha = sh("git", "rev-parse", "HEAD")
    print("pushed", sha[:7], oid)
    run = None
    for _ in range(40):
        time.sleep(15)
        runs = api("/actions/workflows/brief-hub.yml/runs?per_page=10").get("workflow_runs", [])
        run = next((r for r in runs if r["head_sha"] == sha), None)
        if run and run["status"] == "completed":
            break
    if not run:
        sys.exit("hub run not found — check Actions")
    print("hub run", run["html_url"], run["conclusion"])
    for _ in range(12):
        st = json.loads(base64.b64decode(api("/contents/brief_hub/data/sent.json?ref=main")["content"]))
        if st.get("us_close", {}).get("last_key") == oid:
            print("SENT OK", oid, "·", render.REPORT_URL); return
        time.sleep(10)
    sys.exit(f"hub finished ({run['conclusion']}) but sent.json has no {oid}")


if __name__ == "__main__":
    main()
