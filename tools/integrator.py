#!/usr/bin/env python3
"""Spike Integrator. Runs only trusted code from main.

workflow_run (auditor completed): merge the PR if the verdict is PROVEN, set by the real
Auditor run, labelled `queue`, with no protected path; then update the oldest other queued
PR and dispatch the Auditor for it.
push to main: open an issue unless the head commit is the Integrator's merge of a PROVEN PR.
workflow_dispatch: with input auditor_run, as workflow_run for that run; else advance the queue.

Env: GH_TOKEN, GITHUB_REPOSITORY, GITHUB_EVENT_NAME, GITHUB_EVENT_PATH
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import BOT, gh, gh_paged, is_protected  # noqa: E402

REPO = os.environ["GITHUB_REPOSITORY"]
AUDITOR_PATH = ".github/workflows/auditor.yml"


def log(msg):
    print(msg, flush=True)


def on_main(sha):
    cmp = gh(f"repos/{REPO}/compare/{sha}...main", check=False) or {}
    return cmp.get("status") in ("identical", "ahead")


def genuine_auditor_run(run):
    """Is this a run of main's auditor.yml, by an allowed event, on a commit of main?"""
    problems = []
    if run.get("path") != AUDITOR_PATH:
        problems.append(f"path is {run.get('path')}")
    if run.get("event") not in ("pull_request_target", "workflow_dispatch"):
        problems.append(f"event is {run.get('event')}")
    if (run.get("repository") or {}).get("full_name") != REPO:
        problems.append("run is from another repository")
    # pull_request_target runs the workflow file from the PR's base branch, and its
    # head_sha is the PR head, so the base branch is what proves main's definition ran.
    if run.get("event") == "pull_request_target":
        bases = [p["base"]["ref"] for p in run.get("pull_requests") or []]
        if not bases or any(b != "main" for b in bases):
            problems.append(f"PR base is {bases}, not main")
    elif run.get("head_branch") != "main" or not on_main(run.get("head_sha", "")):
        problems.append(f"dispatched on {run.get('head_branch')} at {run.get('head_sha', '')[:7]}, not main")
    return problems


def latest_status(sha, context="auditor/verdict"):
    for s in gh(f"repos/{REPO}/commits/{sha}/statuses?per_page=100"):
        if s["context"] == context:
            return s  # newest first
    return None


def run_verdict(run_id):
    """verdict.json from the run's trusted report job, or None."""
    d = tempfile.mkdtemp()
    proc = subprocess.run(["gh", "run", "download", str(run_id), "-R", REPO, "-n", "verdict", "-D", d],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        log(f"no verdict artifact on run {run_id}: {proc.stderr.strip()}")
        return None
    return json.load(open(os.path.join(d, "verdict.json")))


def advance_queue(skip=None):
    queued = [p for p in gh_paged(f"repos/{REPO}/pulls?state=open&sort=created&direction=asc&per_page=100")
              if p["number"] != skip and any(l["name"] == "queue" for l in p["labels"])]
    if not queued:
        log("queue is empty")
        return
    nxt = queued[0]
    n, old = nxt["number"], nxt["head"]["sha"]
    log(f"next in queue: PR #{n} at {old[:7]}")
    res = gh("-X", "PUT", f"repos/{REPO}/pulls/{n}/update-branch", data={"expected_head_sha": old}, check=False)
    log(f"update-branch: {json.dumps(res)[:300]}")
    for _ in range(30):
        new = gh(f"repos/{REPO}/pulls/{n}")["head"]["sha"]
        if new != old:
            log(f"PR #{n} head moved {old[:7]} -> {new[:7]}")
            break
        time.sleep(3)
    else:
        log(f"PR #{n} head did not move (already up to date?)")
    gh("-X", "POST", f"repos/{REPO}/actions/workflows/auditor.yml/dispatches",
       "-f", "ref=main", "-f", f"inputs[pr]={n}")
    log(f"dispatched auditor for PR #{n}")


def after_auditor(event):
    run = event["workflow_run"]
    log(f"auditor run {run['id']} ({run['event']}, {run['path']}, conclusion {run['conclusion']})")
    problems = genuine_auditor_run(run)
    if problems:
        log("not a genuine Auditor run, ignoring: " + "; ".join(problems))
        return
    v = run_verdict(run["id"])
    if not v:
        return
    n, head = int(v["pr"]), v["head"]
    pr = gh(f"repos/{REPO}/pulls/{n}")
    reasons = []
    if pr["state"] != "open":
        reasons.append(f"PR is {pr['state']}")
    if pr["head"]["sha"] != head:
        reasons.append(f"PR head is now {pr['head']['sha'][:7]}, verdict was for {head[:7]}")
    if v["verdict"] != "PROVEN":
        reasons.append(f"run verdict is {v['verdict']}")
    st = latest_status(head)
    if not st or st["state"] != "success":
        reasons.append(f"auditor/verdict status is {st and st['state']}")
    elif not re.search(rf"/actions/runs/{run['id']}(/|$)", st.get("target_url") or "") or st["creator"]["login"] != BOT:
        reasons.append(f"auditor/verdict was not set by this Auditor run (target {st.get('target_url')}, by {st['creator']['login']})")
    if not any(l["name"] == "queue" for l in pr["labels"]):
        reasons.append("no queue label")
    protected = [f["filename"] for f in gh_paged(f"repos/{REPO}/pulls/{n}/files") if is_protected(f["filename"])]
    if protected:
        reasons.append("touches protected paths: " + ", ".join(protected))
    if reasons:
        log(f"PR #{n} not merged: " + "; ".join(reasons))
        return
    res = gh("-X", "PUT", f"repos/{REPO}/pulls/{n}/merge", data={"merge_method": "merge", "sha": head})
    log(f"merged PR #{n}: {json.dumps(res)}")
    advance_queue(skip=n)


def after_push(event):
    sha = event["after"]
    commit = gh(f"repos/{REPO}/commits/{sha}")
    ok = False
    for p in gh(f"repos/{REPO}/commits/{sha}/pulls") or []:
        full = gh(f"repos/{REPO}/pulls/{p['number']}")
        st = latest_status(full["head"]["sha"])
        if (full.get("merge_commit_sha") == sha and (full.get("merged_by") or {}).get("login") == BOT
                and st and st["state"] == "success"):
            ok = True
    log(f"push {sha[:7]} by {commit['commit']['author']['name']}: audited merge = {ok}")
    if not ok:
        issue = gh("-X", "POST", f"repos/{REPO}/issues",
                   data={"title": f"Unaudited change on main: {sha[:7]}",
                         "body": f"Commit {sha} reached main without an Auditor-approved merge by the Integrator.\n\n"
                                 f"Pushed by: {event.get('pusher', {}).get('name')}"})
        log(f"opened {issue['html_url']}")


def main():
    event = json.load(open(os.environ["GITHUB_EVENT_PATH"], encoding="utf-8"))
    name = os.environ["GITHUB_EVENT_NAME"]
    if name == "workflow_run":
        after_auditor(event)
    elif name == "push":
        after_push(event)
    elif name == "workflow_dispatch":
        run_id = (event.get("inputs") or {}).get("auditor_run") or ""
        if run_id.isdigit():
            # Workaround: an Auditor run started with GITHUB_TOKEN does not fire workflow_run,
            # so the Auditor's report job dispatches the Integrator with its own run ID.
            after_auditor({"workflow_run": gh(f"repos/{REPO}/actions/runs/{run_id}")})
        else:
            advance_queue()


if __name__ == "__main__":
    main()
