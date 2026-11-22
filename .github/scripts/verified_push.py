#!/usr/bin/env python3
"""Publish the local HEAD commit to the branch as a GitHub-signed ("Verified") commit.

Drop-in for `git push` in workflows: the step still does `git add` + `git commit` locally,
then calls this instead of pushing. The files changed by HEAD (vs HEAD~1) are sent through
the Git Data API on top of the branch's current head, so GitHub creates and signs the commit
as github-actions[bot]. If the API route fails it falls back to a plain `git push`. Exit 0 = branch updated (or nothing to publish).

Token: GH_TOKEN / GITHUB_TOKEN, else the credential actions/checkout stored in git config.
"""
import base64
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

API = os.environ.get("GITHUB_API_URL", "https://api.github.com")
INLINE_MAX = 400_000


def git(*a, binary=False):
    r = subprocess.run(["git", *a], capture_output=True)
    if r.returncode:
        raise RuntimeError(f"git {' '.join(a)}: {r.stderr.decode(errors='replace').strip()}")
    return r.stdout if binary else r.stdout.decode().strip()


def token():
    t = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if t:
        return t
    try:
        h = git("config", "--get-all", "http.https://github.com/.extraheader").splitlines()[-1]
        return base64.b64decode(h.split()[-1]).decode().split(":", 1)[1]
    except Exception as e:  # noqa: BLE001
        sys.exit(f"verified_push: no token (set GH_TOKEN or keep checkout credentials): {e}")


def call(method, path, body=None, tok=None):
    req = urllib.request.Request(f"{API}/repos/{os.environ['GITHUB_REPOSITORY']}{path}", method=method,
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json",
                                          "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "verified-push",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, {"message": e.read().decode(errors="replace")[:500]}


def changes():
    """[(status, path)] for HEAD vs HEAD~1; renames are reported as delete + add."""
    out = git("diff-tree", "-r", "--no-renames", "--name-status", "-z", "HEAD~1", "HEAD", binary=True).split(b"\0")
    return [(out[i].decode(), out[i + 1].decode()) for i in range(0, len(out) - 1, 2)]


def main():
    os.chdir(git("rev-parse", "--show-toplevel"))
    branch = os.environ.get("GITHUB_REF_NAME") or git("rev-parse", "--abbrev-ref", "HEAD")
    tok = token()
    ch = changes()
    if not ch:
        print("verified_push: HEAD changes no files, nothing to publish"); return 0
    message = git("log", "-1", "--format=%B")
    entries = []
    for st, path in ch:
        if st == "D":
            entries.append({"path": path, "mode": "100644", "type": "blob", "sha": None}); continue
        mode = git("ls-tree", "HEAD", "--", path).split()[0]
        data = git("cat-file", "blob", f"HEAD:{path}", binary=True)
        e = {"path": path, "mode": mode, "type": "blob"}
        text = None
        if len(data) <= INLINE_MAX:
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                text = None
        if text is not None:
            e["content"] = text
        else:
            code, b = call("POST", "/git/blobs", {"content": base64.b64encode(data).decode(), "encoding": "base64"}, tok)
            if code != 201:
                print(f"verified_push: blob {path} failed {code} {b.get('message')}"); return 1
            e["sha"] = b["sha"]
        entries.append(e)
    last = ""
    for attempt in range(1, 6):
        code, ref = call("GET", f"/git/ref/heads/{branch}", tok=tok)
        if code != 200:
            last = f"ref {code} {ref.get('message')}"; time.sleep(attempt * 3); continue
        head = ref["object"]["sha"]
        code, base = call("GET", f"/git/commits/{head}", tok=tok)
        if code != 200:
            last = f"base commit {code}"; time.sleep(attempt * 3); continue
        code, tree = call("POST", "/git/trees", {"base_tree": base["tree"]["sha"], "tree": entries}, tok)
        if code != 201:
            # a deleted path may already be gone on the branch: drop deletions the base tree lacks and retry once more
            last = f"tree {code} {tree.get('message')}"
            entries[:] = [x for x in entries if x.get("sha", "") is not None or
                          call("GET", f"/contents/{x['path']}?ref={head}", tok=tok)[0] == 200]
            time.sleep(attempt * 2); continue
        if tree["sha"] == base["tree"]["sha"]:
            print("verified_push: branch already has these contents"); return 0
        code, commit = call("POST", "/git/commits", {"message": message, "tree": tree["sha"], "parents": [head]}, tok)
        if code != 201:
            last = f"commit {code} {commit.get('message')}"; time.sleep(attempt * 3); continue
        code, upd = call("PATCH", f"/git/refs/heads/{branch}", {"sha": commit["sha"], "force": False}, tok)
        if code == 200:
            v = commit.get("verification", {})
            print(f"verified_push: {branch} -> {commit['sha'][:7]} ({len(entries)} files, verified={v.get('verified')})")
            return 0
        last = f"ref update {code} {upd.get('message')}"      # branch moved: rebuild on the new head
        time.sleep(attempt * 3)
    print(f"verified_push: gave up after 5 attempts: {last}")
    return 1


def fallback():
    """Data must still land if the API route fails: plain push (the commit then shows as unverified)."""
    branch = os.environ.get("GITHUB_REF_NAME") or git("rev-parse", "--abbrev-ref", "HEAD")
    print("::warning::verified_push: API route failed, falling back to git push (unverified commit)")
    return subprocess.run(["git", "push", "origin", f"HEAD:{branch}"]).returncode


if __name__ == "__main__":
    try:
        rc = main()
    except Exception as e:  # noqa: BLE001
        print(f"verified_push: error {e}"); rc = 1
    sys.exit(rc if rc == 0 else fallback())
