#!/usr/bin/env python3
"""Spike Auditor, evaluate step. Adapted from failfirst.py (Python only, one file at a time).

G3: run the tests at the head; then lay each changed test file over the base, one at a
time and unchanged, and run again. G6: list protected paths touched. G5: SHA-256 of each
criterion test function's source. Writes result.json.

Usage: auditor.py --repo pr --base SHA --head SHA --requirements trusted/requirements.md --out result.json
"""
import argparse
import ast
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import CRITERION, is_protected  # noqa: E402

TEST_FILE = re.compile(r"(^|/)(test_[^/]*\.py|[^/]*_test\.py)$")


def sh(cmd, cwd):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)


def norm(text):
    return re.sub(r"[^a-z0-9]", "", text.lower())


def matches(test_name, criterion):
    return re.search(re.escape(norm(criterion)) + r"(?![0-9])", norm(test_name)) is not None


def parse_junit(path):
    """Return ({test name: status}, load_errors) from a JUnit XML file."""
    results, load_errors = {}, []
    if not os.path.exists(path):
        return results, ["no report written"]
    for case in ET.parse(path).getroot().iter("testcase"):
        name = case.get("name") or ""
        status = "pass"
        for child in case:
            if child.tag in ("failure", "error", "skipped"):
                status = {"failure": "fail", "error": "error", "skipped": "skip"}[child.tag]
                break
        if status == "error" and (not name or name == case.get("classname")):
            load_errors.append((case.get("classname") or name or "?") + " could not load")
        results[name] = status
    return results, load_errors


def run_python(wt):
    proc = sh([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--junitxml=_junit.xml"], wt)
    results, load_errors = parse_junit(os.path.join(wt, "_junit.xml"))
    if "errors during collection" in proc.stdout or "ERROR collecting" in proc.stdout:
        load_errors.append("pytest collection error")
    return results, load_errors, proc


def find(results, criterion):
    return {name: st for name, st in results.items() if matches(name, criterion)}


def verdict(head_hits, base_hits, base_load_errors):
    if not head_hits:
        return "NOT PROVEN", "no test carries this criterion ID"
    if any(st != "pass" for st in head_hits.values()):
        return "NOT PROVEN", "fails even with the change"
    if base_hits:
        if all(st in ("fail", "error") for st in base_hits.values()):
            return "PROVEN", "fails without the change, passes with it (same test file)"
        if any(st == "pass" for st in base_hits.values()):
            return "NOT EVIDENCE OF THIS CHANGE", "passes without the change too"
        return "NOT PROVEN", "skipped without the change"
    if base_load_errors:
        return ("PROVEN WITH LIMITS",
                "the test file could not load or compile without the change; "
                "its own result without the change is unknown")
    return "NOT PROVEN", "test not found without the change"


def criterion_tests(wt, path, criteria):
    """{criterion: {test name: sha256 of its source}} for one test file at the head."""
    try:
        source = open(os.path.join(wt, path), encoding="utf-8").read()
        tree = ast.parse(source)
    except (OSError, SyntaxError, ValueError):
        return {}
    found = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
            for c in criteria:
                if matches(node.name, c):
                    text = ast.get_source_segment(source, node) or ""
                    found.setdefault(c, {})[node.name] = hashlib.sha256(text.encode()).hexdigest()
    return found


def main():
    ap = argparse.ArgumentParser()
    for a in ("--repo", "--base", "--head", "--requirements", "--out"):
        ap.add_argument(a, required=True)
    args = ap.parse_args()
    repo = args.repo
    criteria = sorted(set(CRITERION.findall(open(args.requirements, encoding="utf-8").read())))
    base = sh(["git", "merge-base", args.base, args.head], repo).stdout.strip() or args.base
    all_changed = sh(["git", "diff", "--name-only", base, args.head], repo).stdout.split()
    changed = [p for p in all_changed if TEST_FILE.search(p)]
    protected = [p for p in all_changed if is_protected(p)]
    tmp = tempfile.mkdtemp(prefix="audit-")
    head_wt = os.path.join(tmp, "head")
    sh(["git", "worktree", "add", "--detach", head_wt, args.head], repo)
    try:
        head_res, _, head_proc = run_python(head_wt)
        # Which criteria this PR claims: those with a test function in a changed test file.
        claimed, locks = {}, {}
        for path in changed:
            for c, tests in criterion_tests(head_wt, path, criteria).items():
                claimed.setdefault(c, set()).add(path)
                for name, digest in tests.items():
                    locks[f"{path}::{name}"] = digest
        # One changed test file at a time over the base.
        base_res, base_err_by_file, outputs = {}, {}, []
        for path in changed:
            one = os.path.join(tmp, "base-" + re.sub(r"[^a-z0-9]", "_", path.lower()))
            sh(["git", "worktree", "add", "--detach", one, base], repo)
            sh(["git", "checkout", args.head, "--", path], one)
            res, err, proc = run_python(one)
            outputs.append(f"== {path} over base ==\n" + proc.stdout[-800:])
            for name, st in res.items():
                base_res.setdefault(name, st)
            base_err_by_file[path] = err
            sh(["git", "worktree", "remove", "--force", one], repo)
        rows = []
        for c in criteria:
            if c not in claimed:
                continue
            errs = [e for p in claimed[c] for e in base_err_by_file.get(p, [])]
            v, why = verdict(find(head_res, c), find(base_res, c), errs)
            rows.append({"criterion": c, "verdict": v, "why": why,
                         "with_change": find(head_res, c), "without_change": find(base_res, c)})
        if not rows:
            overall, reason = "NOT PROVEN", "no changed test carries a criterion ID"
        elif any(r["verdict"] in ("NOT PROVEN", "NOT EVIDENCE OF THIS CHANGE") for r in rows):
            overall, reason = "NOT PROVEN", "at least one criterion is not proven"
        elif any(r["verdict"] == "PROVEN WITH LIMITS" for r in rows):
            overall, reason = "PROVEN WITH LIMITS", "at least one criterion is proven with limits"
        else:
            overall, reason = "PROVEN", "every criterion in this change is proven"
        if protected and overall == "PROVEN":
            overall, reason = "PROVEN WITH LIMITS", "protected paths changed: waiting for the owner"
        out = {"verdict": overall, "reason": reason, "base": base, "head": args.head,
               "rows": rows, "protected_paths_touched": protected, "locks": locks,
               "test_files_overlaid": changed, "head_exit": head_proc.returncode,
               "output_tail": ("\n".join(outputs))[-3000:]}
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=2)
        print(json.dumps(out, indent=2))
    finally:
        sh(["git", "worktree", "remove", "--force", head_wt], repo)
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
