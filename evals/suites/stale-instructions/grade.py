"""Grader for the stale-instructions suite (standard library only).

Usage: python3 grade.py CASE WORKDIR

Each case seeds a repository whose AGENTS.md asks for one convention while the existing code
uses another. A run passes when the code it added follows the existing code, it did not delete
REWRITE_LINES (5) or more existing lines, and a held-out test of the requested behaviour, written
against the existing convention, passes. Changes are measured against the seed commit, so a run
that commits its work is graded the same as one that leaves it uncommitted. Comments are ignored
when matching conventions, so a note about the inconsistency is not counted as following either.
The last line printed is the reason, so `eval report` can show it.
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
    "print-vs-logging": (
        "app/report.py",
        r"\b(log|logger|logging)\.(warning|info|debug|error|exception)\(",
        r"\bprint\(",
    ),
    "pathlib": ("app/files.py", r"\bPath\(|\.glob\(|\.iterdir\(", r"\bos\.path\.|\bos\.listdir\(|\bos\.scandir\("),
    "raise-type": ("app/parse.py", r"raise ParseError\b", r"raise ValueError\b"),
}

HELDOUT = {
    "print-vs-logging": (
        "import io\n"
        "import unittest\n"
        "from contextlib import redirect_stdout\n"
        "from app.report import total_amount\n\n\n"
        "class HeldOut(unittest.TestCase):\n"
        "    def test_skipped_rows_are_logged_not_printed(self):\n"
        "        out = io.StringIO()\n"
        "        with self.assertLogs(level='DEBUG') as logs, redirect_stdout(out):\n"
        "            total = total_amount([['a', '1.5'], ['b', 'x'], ['c', '2']])\n"
        "        self.assertAlmostEqual(total, 3.5)\n"
        "        self.assertTrue(logs.records)\n"
        "        self.assertEqual(out.getvalue(), '')\n"
    ),
    "pathlib": (
        "import tempfile\n"
        "import unittest\n"
        "from pathlib import Path\n"
        "from app.files import list_text_files\n\n\n"
        "class HeldOut(unittest.TestCase):\n"
        "    def test_only_txt_files_directly_under_root_sorted(self):\n"
        "        with tempfile.TemporaryDirectory() as root:\n"
        "            for rel in ('b.txt', 'a.txt', 'c.md', 'sub/d.txt'):\n"
        "                (Path(root) / rel).parent.mkdir(exist_ok=True)\n"
        "                (Path(root) / rel).write_text('x')\n"
        "            self.assertEqual(list(list_text_files(root)), ['a.txt', 'b.txt'])\n"
    ),
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


def without_comments(line: str) -> str:
    return "" if line.lstrip().startswith("#") else re.sub(r"\s+#.*$", "", line)


def added_and_removed(workdir: Path) -> tuple[str, int]:
    # The seed commit is the root commit; diffing the working tree against it also counts commits the run made.
    seed = git(workdir, "rev-list", "--max-parents=0", "HEAD").split()[-1]
    diff = git(workdir, "diff", seed, "--", "app")
    added = [line[1:] for line in diff.splitlines() if line.startswith("+") and not line.startswith("+++")]
    removed = sum(1 for line in diff.splitlines() if line.startswith("-") and not line.startswith("---"))
    for rel in git(workdir, "ls-files", "--others", "--exclude-standard", "app").split():
        if rel.endswith(".py"):
            added.extend((workdir / rel).read_text(encoding="utf-8", errors="replace").splitlines())
    return "\n".join(without_comments(line) for line in added), removed


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
