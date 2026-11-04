"""Shared helpers for the spike's Auditor and Integrator (standard library only)."""
import json
import re
import subprocess

CRITERION = re.compile(r"\bE\d+-S\d+-AC\d+\b")
VERDICTS = ("PROVEN", "PROVEN WITH LIMITS", "NOT PROVEN")
ROW_VERDICTS = ("PROVEN", "PROVEN WITH LIMITS", "NOT EVIDENCE OF THIS CHANGE", "NOT PROVEN")
BOT = "github-actions[bot]"


def is_protected(path):
    """.github/**, pytest.ini, **/conftest.py, requirements.md"""
    return (path.startswith(".github/") or path in ("pytest.ini", "requirements.md")
            or path == "conftest.py" or path.endswith("/conftest.py"))


def gh(*args, data=None, check=True):
    """Run `gh api ...` and return parsed JSON (or None)."""
    cmd = ["gh", "api", *args]
    if data is not None:
        cmd += ["--input", "-"]
    proc = subprocess.run(cmd, input=json.dumps(data) if data is not None else None,
                          capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise RuntimeError(f"gh api {' '.join(args)} failed: {proc.stdout.strip()} {proc.stderr.strip()}")
    try:
        return json.loads(proc.stdout) if proc.stdout.strip() else None
    except ValueError:
        return proc.stdout


def gh_paged(path):
    """GET every page of a list endpoint."""
    out = gh("--paginate", "--slurp", path)
    return [item for page in out for item in page]
