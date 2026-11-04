#!/usr/bin/env python3
"""Spike Auditor, report step. Runs only trusted code from main; never runs PR code.

Validates result.json, applies G6 again from the API, applies the G5 lock by reading the
first-seen hashes from earlier `auditor-lock` check runs on this PR's earlier head SHAs
(including heads lost to a force-push, found through the PR timeline), then posts the PR
comment, the `auditor/verdict` status and the `auditor-lock` check run, and writes
verdict.json for the Integrator.

Usage: report.py --result in/result.json --pr N --head SHA --run-url URL
Env: GH_TOKEN, GITHUB_REPOSITORY
"""
import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import BOT, CRITERION, ROW_VERDICTS, VERDICTS, gh, gh_paged, is_protected  # noqa: E402

REPO = os.environ["GITHUB_REPOSITORY"]
MARKER = "<!-- auditor-verdict -->"
HEX64 = re.compile(r"^[0-9a-f]{64}$")


def load_result(path, head):
    """Return (result, problem). result is None if the shape is wrong."""
    try:
        r = json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError) as e:
        return None, f"result.json missing or unreadable: {e}"
    try:
        assert r["verdict"] in VERDICTS
        assert r["head"] == head, "result is for a different head SHA"
        assert isinstance(r["rows"], list) and len(r["rows"]) <= 100
        for row in r["rows"]:
            assert CRITERION.fullmatch(row["criterion"]) and row["verdict"] in ROW_VERDICTS
            assert isinstance(row["why"], str) and len(row["why"]) < 300
        assert isinstance(r["locks"], dict) and len(r["locks"]) <= 200
        for k, v in r["locks"].items():
            assert isinstance(k, str) and len(k) < 300 and HEX64.match(v)
        assert isinstance(r["protected_paths_touched"], list)
    except (AssertionError, KeyError, TypeError) as e:
        return None, f"result.json has the wrong shape: {e}"
    return r, None


def earlier_heads(pr, head):
    """Earlier head SHAs of this PR, oldest first: force-pushed-away heads, then PR commits."""
    owner, name = REPO.split("/")
    q = """query($o:String!,$n:String!,$p:Int!){repository(owner:$o,name:$n){pullRequest(number:$p){
      timelineItems(first:100,itemTypes:[HEAD_REF_FORCE_PUSHED_EVENT]){nodes{
        ... on HeadRefForcePushedEvent{createdAt beforeCommit{oid} afterCommit{oid}}}}}}}"""
    data = gh("graphql", "-f", f"query={q}", "-f", f"o={owner}", "-f", f"n={name}", "-F", f"p={pr}")
    shas = []
    for ev in data["data"]["repository"]["pullRequest"]["timelineItems"]["nodes"]:
        if ev.get("beforeCommit"):
            shas.append(ev["beforeCommit"]["oid"])
    shas += [c["sha"] for c in gh_paged(f"repos/{REPO}/pulls/{pr}/commits")]
    seen, out = set(), []
    for s in shas:
        if s != head and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def first_seen_locks(pr, head):
    """{test key: (first hash, check run url)} from earlier auditor-lock check runs."""
    found = []
    for sha in earlier_heads(pr, head):
        runs = gh(f"repos/{REPO}/commits/{sha}/check-runs?check_name=auditor-lock&filter=all",
                  check=False) or {}
        for run in runs.get("check_runs", []):
            if (run.get("app") or {}).get("slug") != "github-actions":
                continue
            try:
                locks = json.loads(run["output"]["text"])["locks"]
            except (KeyError, TypeError, ValueError):
                continue
            found.append((run.get("completed_at") or "", run["html_url"], locks))
    first = {}
    for _, url, locks in sorted(found, key=lambda x: x[0]):
        for k, v in locks.items():
            if isinstance(v, str) and HEX64.match(v):
                first.setdefault(k, (v, url))
    return first


def main():
    ap = argparse.ArgumentParser()
    for a in ("--result", "--pr", "--head", "--run-url"):
        ap.add_argument(a, required=True)
    args = ap.parse_args()
    pr, head = int(args.pr), args.head
    r, problem = load_result(args.result, head)
    notes = []
    if r is None:
        r = {"verdict": "NOT PROVEN", "rows": [], "locks": {}, "protected_paths_touched": []}
        notes.append(problem)
    verdict = r["verdict"]

    # G6 again, from the API rather than from the evaluate job.
    files = [f["filename"] for f in gh_paged(f"repos/{REPO}/pulls/{pr}/files")]
    protected = sorted(p for p in files if is_protected(p))
    if protected and verdict == "PROVEN":
        verdict = "PROVEN WITH LIMITS"
    if protected:
        notes.append("Protected paths changed, waiting for the owner: " + ", ".join(protected))

    # G5 lock.
    changed_locks = []
    try:
        first = first_seen_locks(pr, head)
        for key, digest in r["locks"].items():
            if key in first and first[key][0] != digest:
                changed_locks.append((key, first[key][0], digest, first[key][1]))
    except RuntimeError as e:
        notes.append(f"Could not read earlier locks: {e}")
        first = {}
    if changed_locks:
        if verdict == "PROVEN":
            verdict = "PROVEN WITH LIMITS"
        for key, old, new, url in changed_locks:
            notes.append(f"Locked test changed since first seen: `{key}` was {old[:12]} ({url}), now {new[:12]}")

    # PR comment.
    lines = [MARKER, f"## Auditor verdict: **{verdict}**", "", f"Head `{head[:7]}` · [run]({args.run_url})", "",
             "| Criterion | Result | Why |", "|---|---|---|"]
    for row in r["rows"]:
        lines.append(f"| {row['criterion']} | {row['verdict']} | {row['why']} |")
    if not r["rows"]:
        lines.append("| (none) | NOT PROVEN | no criterion test found |")
    if notes:
        lines += [""] + [f"- {n}" for n in notes]
    body = "\n".join(lines)
    mine = [c for c in gh_paged(f"repos/{REPO}/issues/{pr}/comments")
            if c["user"]["login"] == BOT and c["body"].startswith(MARKER)]
    if mine:
        gh("-X", "PATCH", f"repos/{REPO}/issues/comments/{mine[0]['id']}", data={"body": body})
    else:
        gh("-X", "POST", f"repos/{REPO}/issues/{pr}/comments", data={"body": body})

    # Commit status: success only for PROVEN.
    state = {"PROVEN": "success", "PROVEN WITH LIMITS": "pending", "NOT PROVEN": "failure"}[verdict]
    gh("-X", "POST", f"repos/{REPO}/statuses/{head}",
       data={"state": state, "context": "auditor/verdict", "target_url": args.run_url,
             "description": verdict if state != "pending" else "PROVEN WITH LIMITS: waiting for the owner"})

    # Check run holding the lock hashes.
    gh("-X", "POST", f"repos/{REPO}/check-runs",
       data={"name": "auditor-lock", "head_sha": head, "status": "completed", "conclusion": "neutral",
             "details_url": args.run_url,
             "output": {"title": f"{len(r['locks'])} criterion test(s) locked",
                        "summary": "SHA-256 of each criterion test function's source at this head.",
                        "text": json.dumps({"pr": pr, "head": head, "locks": r["locks"]})}})

    out = {"pr": pr, "head": head, "verdict": verdict, "run_url": args.run_url,
           "changed_locks": [c[0] for c in changed_locks], "protected": protected}
    json.dump(out, open("verdict.json", "w"), indent=2)
    print(body)
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
