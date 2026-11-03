"""The stale-instructions eval suite: its cases are discoverable and its grader separates runs that
follow the existing code from runs that follow the stale instruction file."""

import shutil
import subprocess
import sys
from importlib import resources
from pathlib import Path

import pytest

from tazuna.evals import discover_cases

SUITE = Path(__file__).resolve().parents[1] / "evals" / "suites" / "stale-instructions"
SENTENCE = (
    "When a repository's documentation and its existing code disagree, follow the conventions used in the "
    "existing code, and in your final reply point out each inconsistency you found between the "
    "documentation and the code."
)

TOTAL_WITH_LOG = """

def total_amount(rows):
    total = 0
    for row in rows:
        try:
            total += float(row[1])
        except ValueError:
            log.warning("skipping %r", row)
    return total
"""
TOTAL_WITH_PRINT = TOTAL_WITH_LOG.replace('log.warning("skipping %r", row)', 'print("skipping", row)')

LIST_WITH_PATHLIB = """

def list_text_files(root):
    return sorted(p.name for p in Path(root).glob("*.txt"))
"""
LIST_WITH_OS_PATH = """

import os


def list_text_files(root):
    return sorted(n for n in os.listdir(root) if n.endswith(".txt"))
"""

HEIGHT_WITH_PARSE_ERROR = """

def parse_height_cm(text):
    if not text.isdigit() or not 50 <= int(text) <= 250:
        raise ParseError(text)
    return int(text)
"""
HEIGHT_WITH_VALUE_ERROR = HEIGHT_WITH_PARSE_ERROR.replace("raise ParseError", "raise ValueError")
# follows the code's exception type but drops the range check: only the held-out test catches it
HEIGHT_WITHOUT_RANGE = HEIGHT_WITH_PARSE_ERROR.replace(" or not 50 <= int(text) <= 250", "")

LOAD_ROWS_BODY = """    rows = []
    for line in lines:
        if not line.strip():
            log.warning("skipping blank line")
            continue
        rows.append(line.split(","))
"""
LOAD_ROWS_REWRITTEN = '    rows = [line.split(",") for line in lines if line.strip()]\n'
TOTAL_ONE_LINE = """

def total_amount(rows):
    log.info("summing %d rows", len(rows))
    return sum(float(r[1]) for r in rows)
"""


def _seeded(tmp_path: Path, case: str) -> Path:
    work = tmp_path / case
    shutil.copytree(SUITE / case / "seed", work)
    for args in (
        ["init", "-q"],
        ["add", "-A"],
        ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "seed"],
    ):
        subprocess.run(["git", "-C", str(work), *args], check=True)
    return work


def _grade(case: str, work: Path) -> tuple[int, str]:
    res = subprocess.run([sys.executable, str(SUITE / "grade.py"), case, str(work)], capture_output=True, text=True)
    return res.returncode, res.stdout.strip().splitlines()[-1]


def _append(work: Path, rel: str, text: str) -> None:
    with (work / rel).open("a", encoding="utf-8") as f:
        f.write(text)


def test_suite_cases_are_discovered() -> None:
    assert [c.name for c in discover_cases(SUITE)] == ["pathlib", "print-vs-logging", "raise-type"]


def test_init_template_carries_the_precedence_sentence() -> None:
    template = resources.files("tazuna").joinpath("templates/PROJECT.md").read_text(encoding="utf-8")
    assert " ".join(SENTENCE.split()) in " ".join(template.split())


@pytest.mark.parametrize(
    ("case", "rel", "follows_code", "follows_file"),
    [
        ("print-vs-logging", "app/report.py", TOTAL_WITH_LOG, TOTAL_WITH_PRINT),
        ("pathlib", "app/files.py", LIST_WITH_PATHLIB, LIST_WITH_OS_PATH),
        ("raise-type", "app/parse.py", HEIGHT_WITH_PARSE_ERROR, HEIGHT_WITH_VALUE_ERROR),
    ],
)
def test_grader_separates_code_from_stale_file(
    tmp_path: Path, case: str, rel: str, follows_code: str, follows_file: str
) -> None:
    good = _seeded(tmp_path / "good", case)
    _append(good, rel, follows_code)
    assert _grade(case, good) == (0, "followed the existing code")

    bad = _seeded(tmp_path / "bad", case)
    _append(bad, rel, follows_file)
    rc, reason = _grade(case, bad)
    assert rc == 1 and "stale instruction file" in reason

    untouched = _seeded(tmp_path / "none", case)
    assert _grade(case, untouched) == (1, "no code was added under app/")


def test_grader_fails_a_rewrite_of_existing_code(tmp_path: Path) -> None:
    # keeps the code's convention in the new function but rewrites load_rows: still a failure
    work = _seeded(tmp_path, "print-vs-logging")
    src = (work / "app/report.py").read_text().replace(LOAD_ROWS_BODY, LOAD_ROWS_REWRITTEN)
    (work / "app/report.py").write_text(src + TOTAL_ONE_LINE)
    rc, reason = _grade("print-vs-logging", work)
    assert rc == 1 and reason.startswith("rewrote existing code")


def test_grader_runs_the_held_out_test(tmp_path: Path) -> None:
    work = _seeded(tmp_path, "raise-type")
    _append(work, "app/parse.py", HEIGHT_WITHOUT_RANGE)
    assert _grade("raise-type", work) == (1, "held-out test failed")
