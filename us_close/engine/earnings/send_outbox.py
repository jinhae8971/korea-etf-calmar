# -*- coding: utf-8 -*-
"""Send an ad-hoc earnings brief through Brief Hub (us_close outbox) and confirm delivery.

python3 send_outbox.py <id-suffix> <text.html> <caption.html> <img_dir> <docs_subdir>
  e.g. send_outbox.py nvda_q3fy27 out/.../telegram.html out/.../caption.html out/NVDA_2026-11-17 earnings/NVDA/2026-11-17
Images are copied to docs/<docs_subdir>/ (also public on GitHub Pages) and sent as an album before the text.
"""
import base64, json, os, shutil, subprocess, sys, time, urllib.request, atexit, tempfile
from datetime import datetime, timedelta, timezone

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
API = f"https://api.github.com/repos/{REPO}"; KST = timezone(timedelta(hours=9))


def sh(*cmd, check=True):
    r = subprocess.run(cmd, cwd=CLONE, capture_output=True, text=True)
    if check and r.returncode:
        raise RuntimeError(f"{' '.join(cmd)}\n{r.stdout}\n{r.stderr}")
    return r.stdout.strip()


def api(path):
    req = urllib.request.Request(API + path, headers={"Accept": "application/vnd.github+json", "User-Agent": "earnings-brief"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def main(suffix, text_p, cap_p, img_dir, docs_sub):
    text = open(text_p).read(); cap = open(cap_p).read()
    if len(text) > 3900 or len(cap) > 1000:
        sys.exit(f"too long: text {len(text)} caption {len(cap)}")
    h = datetime.now(KST).hour
    if not (7 <= h < 22):
        sys.exit("night guard (22-07 KST): not sending")
    fresh_clone()
    dst = os.path.join(CLONE, "docs", docs_sub); os.makedirs(dst, exist_ok=True)
    imgs = sorted(f for f in os.listdir(img_dir) if f.endswith(".png"))
    for f in imgs:
        shutil.copy(os.path.join(img_dir, f), os.path.join(dst, f))
    oid = f"us_close:{suffix}"
    out = {"as_of_kst": datetime.now(KST).strftime("%Y-%m-%dT%H:%M:%S+09:00"), "session": suffix,
           "report": f"https://jinhae8971.github.io/korea-etf-calmar/{docs_sub}/",
           "outbox": {"id": oid, "kind": "earnings", "text": text, "caption": cap,
                      "photos": [f"docs/{docs_sub}/{f}" for f in imgs]}}
    json.dump(out, open(os.path.join(CLONE, "us_close", "data", "latest.json"), "w"), ensure_ascii=False, indent=1)
    sh("git", "add", f"docs/{docs_sub}", "us_close/data/latest.json")
    sh("git", "commit", "-m", f"data(earnings): {suffix}")
    for i in range(4):
        try:
            sh("git", "push", "origin", "HEAD:main"); break
        except RuntimeError:
            if i == 3:
                raise
            sh("git", "fetch", "--depth", "1", "origin", "main")
            sh("git", "rebase", "origin/main", check=False); time.sleep(5)
    sha = sh("git", "rev-parse", "HEAD"); print("pushed", sha[:7], oid)
    run = None
    for _ in range(40):
        time.sleep(15)
        runs = api("/actions/workflows/brief-hub.yml/runs?per_page=10").get("workflow_runs", [])
        run = next((r for r in runs if r["head_sha"] == sha), None)
        if run and run["status"] == "completed":
            break
    if not run:
        sys.exit("hub run not found")
    print("hub run", run["html_url"], run["conclusion"])
    for _ in range(12):
        st = json.loads(base64.b64decode(api("/contents/brief_hub/data/sent.json?ref=main")["content"]))
        if st.get("us_close", {}).get("last_key") == oid:
            print("SENT OK", oid); return
        time.sleep(10)
    sys.exit(f"hub finished ({run['conclusion']}) but sent.json has no {oid}")


if __name__ == "__main__":
    main(*sys.argv[1:6])
