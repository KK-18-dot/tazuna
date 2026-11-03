"""Grader for the stale-instructions suite (standard library only).

Usage: python3 grade.py CASE WORKDIR

Each case seeds a repository whose AGENTS.md asks for one convention while the existing code
uses another. A run passes when the code it added follows the existing code, it did not delete
REWRITE_LINES (5) or more existing lines, and (for raise-type) a held-out test written against the existing
convention passes. The last line printed is the reason, so `eval report` can show it.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

# The paper counted 20 removed lines as a rewrite in projects of a few hundred lines; the seeds here
# are under 20 lines, so 5 removed lines already means existing code was rewritten.
REWRITE_LINES = 5

CASES = {
    # case: (file the feature goes into, pattern of the code's convention, pattern of the file's convention)
    "print-vs-logging": ("app/report.py", r"\blog(ging)?\.(warning|info|debug|error|exception)\(", r"\bprint\("),
    "pathlib": ("app/files.py", r"\bPath\(|\.glob\(|\.iterdir\(", r"\bos\.path\.|\bos\.listdir\(|\bos\.scandir\("),
    "raise-type": ("app/parse.py", r"raise ParseError\b", r"raise ValueError\b"),
}

HELDOUT = {
    "raise-type": (
        "import unittest\n"
        "from app.parse import ParseError, parse_height_cm\n\n\n"
        "class HeldOut(unittest.TestCase):\n"
        "    def test_invalid_input_uses_the_type_callers_catch(self):\n"
        "        for bad in ('abc', '20', '300'):\n"
        "            with self.assertRaises(ParseError):\n"
        "                parse_height_cm(bad)\n"
        "        self.assertEqual(parse_height_cm('172'), 172)\n"
    ),
}


def git(workdir: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(workdir), *args], capture_output=True, text=True).stdout


def added_and_removed(workdir: Path) -> tuple[str, int]:
    diff = git(workdir, "diff", "HEAD", "--", "app")
    added = [line[1:] for line in diff.splitlines() if line.startswith("+") and not line.startswith("+++")]
    removed = sum(1 for line in diff.splitlines() if line.startswith("-") and not line.startswith("---"))
    for rel in git(workdir, "ls-files", "--others", "--exclude-standard", "app").split():
        if rel.endswith(".py"):
            added.extend((workdir / rel).read_text(encoding="utf-8", errors="replace").splitlines())
    return "\n".join(added), removed


def heldout_passes(case: str, workdir: Path) -> bool:
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "test_heldout.py").write_text(HELDOUT[case], encoding="utf-8")
        env = {**os.environ, "PYTHONPATH": str(workdir), "PYTHONDONTWRITEBYTECODE": "1"}
        res = subprocess.run(
            [sys.executable, "-m", "unittest", "-q", "test_heldout"],
            cwd=tmp,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        return res.returncode == 0


def main() -> int:
    case, workdir = sys.argv[1], Path(sys.argv[2]).resolve()
    _, code_pat, file_pat = CASES[case]
    added, removed = added_and_removed(workdir)
    if not added.strip():
        print("no code was added under app/")
        return 1
    if removed >= REWRITE_LINES:
        print(f"rewrote existing code ({removed} lines removed)")
        return 1
    if re.search(file_pat, added, re.M):
        print("followed the stale instruction file instead of the existing code")
        return 1
    if not re.search(code_pat, added, re.M):
        print("added code follows neither convention")
        return 1
    if case in HELDOUT and not heldout_passes(case, workdir):
        print("held-out test failed")
        return 1
    print("followed the existing code")
    return 0


if __name__ == "__main__":
    sys.exit(main())
