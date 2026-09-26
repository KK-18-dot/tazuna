"""Reproducible evals: fixture directories → tazuna run → deterministic checks → report.

A suite is a directory of cases. Each case directory contains:

    task.md       the task text (required)
    seed/         files copied into a fresh git-initialised workdir before the run (optional)
    check.sh      code grader; exit 0 = PASS (optional). Receives WORKDIR as $1, and the env
                  TAZUNA_LOG, TAZUNA_HANDOFF, TAZUNA_EXIT, TAZUNA_STATUS
    case.toml     expectations and per-case overrides (optional):
                    role = "impl"           read_only = true      timeout = 300
                    expect_exit = 0         expect_status = "done"
                    expect_output_regex = "..."   expect_files = ["src/x.py"]

Every run appends one JSON line to results.jsonl; ``eval report`` renders a markdown table and,
with ``--baseline``, signed pass-rate diffs so improvements and regressions surface together.
Rows whose status says nothing about the route's capability (quota-exhausted, auth-required,
cancelled) are excluded from pass rates and counted separately; ``--fail-on-regression`` turns a
lower pass rate into exit 1 for CI. A cancelled trial (Ctrl-C, SIGTERM) also stops the suite.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .config import Config, ensure_state_dir
from .errors import TazunaError, UsageError
from .handoff import mask_secrets
from .routing import resolve_route
from .run import cancel_exit_code, run_task

CASE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
EXCLUDED_STATUSES = ("quota-exhausted", "auth-required", "cancelled")


@dataclass
class CaseResult:
    suite: str
    case: str
    trial: int
    role: str | None
    provider: str
    model: str | None
    effort: str | None
    exit: int
    status: str
    seconds: int
    passed: bool
    reasons: list[str] = field(default_factory=list)
    workdir: str = ""
    ts: str = ""
    run_id: str = ""  # the run's ledger id, to join a result row with runs.jsonl and its log

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)


def discover_cases(suite_dir: Path) -> list[Path]:
    if not suite_dir.is_dir():
        raise UsageError(f"suite directory not found: {suite_dir}")
    cases = sorted(p for p in suite_dir.iterdir() if p.is_dir() and (p / "task.md").is_file())
    if not cases:
        raise UsageError(f"no cases in {suite_dir} (a case is a directory containing task.md)")
    for case in cases:
        if not CASE_NAME_RE.match(case.name):
            raise UsageError(f"case name {case.name!r} must match {CASE_NAME_RE.pattern}")
    return cases


def _load_case(case_dir: Path) -> dict[str, Any]:
    spec_path = case_dir / "case.toml"
    if spec_path.is_file():
        with spec_path.open("rb") as fh:
            return tomllib.load(fh)
    return {}


def _prepare_workdir(case_dir: Path, workdir: Path) -> None:
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True)
    seed = case_dir / "seed"
    if seed.is_dir():
        shutil.copytree(seed, workdir, dirs_exist_ok=True)
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "tazuna-eval",
        "GIT_AUTHOR_EMAIL": "eval@localhost",
        "GIT_COMMITTER_NAME": "tazuna-eval",
        "GIT_COMMITTER_EMAIL": "eval@localhost",
    }
    for args in (["init", "-q"], ["add", "-A"], ["commit", "-q", "--allow-empty", "-m", "seed"]):
        subprocess.run(["git", "-C", str(workdir), *args], check=True, capture_output=True, env=env)


def _check(
    case_dir: Path, spec: dict[str, Any], workdir: Path, log_path: Path, handoff: Path, exit_code: int, status: str
) -> list[str]:
    reasons: list[str] = []
    if "expect_exit" in spec and exit_code != int(spec["expect_exit"]):
        reasons.append(f"exit {exit_code} != expected {spec['expect_exit']}")
    if "expect_status" in spec and status != str(spec["expect_status"]):
        reasons.append(f"status {status!r} != expected {spec['expect_status']!r}")
    log_text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.is_file() else ""
    if "expect_output_regex" in spec and not re.search(str(spec["expect_output_regex"]), log_text, re.S):
        reasons.append(f"output does not match /{spec['expect_output_regex']}/")
    for rel in spec.get("expect_files", []) or []:
        if not (workdir / rel).exists():
            reasons.append(f"expected file missing: {rel}")
    check = case_dir / "check.sh"
    if check.is_file():
        env = {
            **os.environ,
            "TAZUNA_LOG": str(log_path),
            "TAZUNA_HANDOFF": str(handoff),
            "TAZUNA_EXIT": str(exit_code),
            "TAZUNA_STATUS": status,
        }
        try:
            res = subprocess.run(
                ["bash", str(check), str(workdir)],
                capture_output=True,
                text=True,
                timeout=300,
                env=env,
                cwd=str(workdir),
            )
        except subprocess.TimeoutExpired:
            reasons.append("check.sh timed out")
        else:
            if res.returncode != 0:
                detail = (res.stdout + res.stderr).strip().splitlines()
                # The reason is stored in results.jsonl and report.md: mask token shapes like the log.
                reasons.append("check.sh failed" + (f": {mask_secrets(detail[-1])}" if detail else ""))
    return reasons


def run_suite(
    cfg: Config,
    suite_dir: Path,
    *,
    role: str | None,
    provider: str | None,
    trials: int,
    results_dir: Path | None,
    only: str | None = None,
) -> tuple[Path, list[CaseResult]]:
    cases = discover_cases(suite_dir)
    if only:
        cases = [c for c in cases if re.search(only, c.name)]
        if not cases:
            raise UsageError(f"no case matches --only {only!r}")
    stamp = time.strftime("%Y%m%d%H%M%S")
    results_dir = results_dir or (ensure_state_dir("evals") / f"{suite_dir.name}-{stamp}")
    results_dir.mkdir(parents=True, exist_ok=True)
    results_path = results_dir / "results.jsonl"
    results: list[CaseResult] = []
    stopped: str | None = None  # set when a signal cancels a trial; no further trial starts
    stop_code = 0
    with results_path.open("a", encoding="utf-8") as fh:
        for case_dir in cases:
            if stopped:
                break
            spec = _load_case(case_dir)
            case_role = role or spec.get("role")
            route = resolve_route(
                cfg,
                role=case_role,
                provider=provider if not case_role else None,
                timeout=int(spec["timeout"]) if "timeout" in spec else None,
                read_only=bool(spec["read_only"]) if "read_only" in spec else None,
            )
            task = (case_dir / "task.md").read_text(encoding="utf-8")
            for trial in range(1, trials + 1):
                if stopped:
                    break
                workdir = results_dir / case_dir.name / f"trial-{trial}"
                _prepare_workdir(case_dir, workdir)
                handoff = workdir / "HANDOFF.md"
                outcome = run_task(cfg, task, route=route, target_dir=workdir, out=handoff, echo=False)
                rec = outcome.record
                reasons = _check(case_dir, spec, workdir, Path(rec.log), handoff, rec.exit, rec.status)
                result = CaseResult(
                    suite=suite_dir.name,
                    case=case_dir.name,
                    trial=trial,
                    role=route.role,
                    provider=route.provider,
                    model=route.model,
                    effort=route.effort,
                    exit=rec.exit,
                    status=rec.status,
                    seconds=rec.seconds,
                    passed=not reasons,
                    reasons=reasons,
                    workdir=str(workdir),
                    ts=rec.ts,
                    run_id=rec.id,
                )
                results.append(result)
                fh.write(result.to_json() + "\n")
                fh.flush()
                mark = "PASS" if result.passed else "FAIL"
                print(
                    f"{mark} {case_dir.name} trial {trial} "
                    f"({route.provider} {route.model or '-'} {route.effort or '-'}) "
                    f"exit={rec.exit} status={rec.status} {rec.seconds}s"
                )
                for reason in reasons:
                    print(f"     - {reason}")
                if outcome.stop_signal is not None:
                    stopped, stop_code = f"{case_dir.name} trial {trial}", cancel_exit_code(outcome.stop_signal)
    report_path = results_dir / "report.md"
    report_path.write_text(render_report(results, None), encoding="utf-8")
    if stopped:
        raise TazunaError(f"eval stopped by a signal during {stopped}; results so far: {results_path}", stop_code)
    return results_path, results


def load_results(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise UsageError(f"results file not found: {path}")
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def is_excluded(row: dict[str, Any]) -> bool:
    """A quota or login failure, or a run someone cancelled, says nothing about the route's
    capability on the case."""
    return row.get("status") in EXCLUDED_STATUSES


def _pass_rates(rows: list[dict[str, Any]]) -> dict[str, tuple[int, int, int]]:
    """case -> (passed, counted, excluded). Timeouts stay counted: a route that is too slow
    did not deliver."""
    rates: dict[str, tuple[int, int, int]] = {}
    for row in rows:
        passed, counted, excluded = rates.get(row["case"], (0, 0, 0))
        if is_excluded(row):
            rates[row["case"]] = (passed, counted, excluded + 1)
        else:
            rates[row["case"]] = (passed + (1 if row["passed"] else 0), counted + 1, excluded)
    return rates


def _pct(passed: int, counted: int) -> str:
    return f"{passed / counted * 100:.0f}%" if counted else "n/a"


def regressions(rows: list[dict[str, Any]], baseline: list[dict[str, Any]]) -> list[str]:
    """Cases present in both runs whose pass rate went down (excluded rows not counted)."""
    rates, base_rates = _pass_rates(rows), _pass_rates(baseline)
    found = []
    for case in sorted(set(rates) & set(base_rates)):
        passed, counted, _ = rates[case]
        bpassed, bcounted, _ = base_rates[case]
        if counted and bcounted and passed * bcounted < bpassed * counted:
            found.append(
                f"{case}: {passed}/{counted} ({_pct(passed, counted)}) < baseline "
                f"{bpassed}/{bcounted} ({_pct(bpassed, bcounted)})"
            )
    return found


def incomplete(rows: list[dict[str, Any]], baseline: list[dict[str, Any]]) -> list[str]:
    """Why these results cannot pass the regression gate even without a lower pass rate.

    Any excluded trial counts: the gate cannot tell an outage from a failure whose output happens to
    contain a quota or login word (the markers are plain substrings), or from a provider that
    signals its own evaluator. A stopped suite or a dropped case shows nothing about the baseline's
    cases either. Excluded rows still stay out of the pass rates in the report.
    """
    rates, base_rates = _pass_rates(rows), _pass_rates(baseline)
    notes = []
    for r in rows:
        if r.get("status") == "cancelled":
            notes.append(f"{r['case']} trial {r['trial']} was cancelled")
        elif is_excluded(r):
            notes.append(f"{r['case']} trial {r['trial']} was excluded ({r['status']})")
    notes += [f"{case} (in the baseline, missing from these results)" for case in sorted(set(base_rates) - set(rates))]
    return notes


def uncompared(rows: list[dict[str, Any]], baseline: list[dict[str, Any]]) -> list[str]:
    """Cases the gate skips without failing: every baseline run was excluded."""
    rates, base_rates = _pass_rates(rows), _pass_rates(baseline)
    return [
        f"{case} (every baseline run excluded: quota, login or cancelled)"
        for case in sorted(set(rates) & set(base_rates))
        if not base_rates[case][1]
    ]


def render_report(results: list[CaseResult] | list[dict[str, Any]], baseline: list[dict[str, Any]] | None) -> str:
    rows = [asdict(r) if isinstance(r, CaseResult) else r for r in results]
    rates = _pass_rates(rows)
    base_rates = _pass_rates(baseline) if baseline else {}
    lines = ["# tazuna eval report", ""]
    if rows:
        lines.append(f"suite: {rows[0]['suite']}  runs: {len(rows)}  cases: {len(rates)}")
        lines.append("")
    header = "| case | route | pass | rate | excluded |" + (" baseline | diff |" if baseline else "")
    lines.append(header)
    lines.append("|---|---|---|---|---|" + ("---|---|" if baseline else ""))
    total_pass = total_runs = total_excluded = 0
    for case, (passed, counted, excluded) in sorted(rates.items()):
        total_pass += passed
        total_runs += counted
        total_excluded += excluded
        route = next(
            (f"{r['provider']}/{r['model'] or '-'}/{r['effort'] or '-'}" for r in rows if r["case"] == case), "-"
        )
        line = f"| {case} | {route} | {passed}/{counted} | {_pct(passed, counted)} | {excluded} |"
        if baseline:
            if case not in base_rates:
                line += " - | new |"
            else:
                bpassed, bcounted, _ = base_rates[case]
                line += f" {bpassed}/{bcounted} ({_pct(bpassed, bcounted)}) |"
                if counted and bcounted:
                    line += f" {(passed / counted - bpassed / bcounted) * 100:+.0f}pt |"
                else:
                    line += " n/a |"
        lines.append(line)
    if total_runs or total_excluded:
        lines.append("")
        overall = f"overall: {total_pass}/{total_runs} ({_pct(total_pass, total_runs)})"
        lines.append(overall + (f"; excluded (quota, login or cancelled): {total_excluded}" if total_excluded else ""))
    for title, selected in (
        ("failures", [r for r in rows if not r["passed"] and not is_excluded(r)]),
        ("excluded (quota, login or cancelled)", [r for r in rows if is_excluded(r)]),
    ):
        if selected:
            lines.extend(["", f"## {title}", ""])
            for r in selected:
                lines.append(f"- {r['case']} trial {r['trial']}: {'; '.join(r['reasons'])} (workdir: {r['workdir']})")
    return "\n".join(lines) + "\n"
