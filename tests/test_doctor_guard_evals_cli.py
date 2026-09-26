import io
import json
import os
import signal
import subprocess
import threading
from pathlib import Path

import pytest

from tazuna import doctor as doctor_mod
from tazuna.cli import main
from tazuna.config import load_config, state_dir
from tazuna.doctor import run_doctor
from tazuna.errors import TazunaError
from tazuna.evals import load_results, render_report, run_suite
from tazuna.guard import check_paths, claude_hook
from tazuna.handoff import read_records
from tazuna.render import render

# ---- doctor -----------------------------------------------------------------------------------


def _levels(report):
    return {(f.level, f.message.split(":")[0]) for f in report.findings}


def test_doctor_all_ok_after_render(project: Path) -> None:
    render(load_config(project))
    (project / "tazuna.toml").write_text(
        (project / "tazuna.toml").read_text().replace('provider = "fakecli"', 'provider = "mock"'), encoding="utf-8"
    )
    report = run_doctor(project)
    assert report.warnings == 0, report.render()
    assert "RESULT: ALL OK" in report.render()


def test_doctor_warns_on_missing_provider_used_by_role_and_drift(project: Path) -> None:
    render(load_config(project))
    (project / "PROJECT.md").write_text("# drifted\n", encoding="utf-8")
    report = run_doctor(project)
    text = report.render()
    assert "WARN provider fakecli: 'fakecli' not found in PATH; used by roles: ro, shim" in text
    assert "WARN render target claude: CLAUDE.md drifted" in text
    assert "NOTE provider claude" in text  # optional provider, not used by any role
    assert report.warnings >= 4


def test_doctor_detects_retired_models_and_tracked_secret_names(project: Path) -> None:
    (project / "PROJECT.md").write_text("Never use old-model-9 again.\n", encoding="utf-8")
    render(load_config(project))
    (project / ".env").write_text("X=1\n", encoding="utf-8")
    (project / ".env.example").write_text("X=\n", encoding="utf-8")
    subprocess.run(["git", "add", "-f", ".env", ".env.example"], cwd=project, check=True)
    text = run_doctor(project).render()
    assert "WARN retired model ids in use: CLAUDE.md mentions old-model-9" in text
    assert "WARN tracked files with secret-like names: .env" in text
    assert ".env.example" not in text


def test_doctor_warns_on_forbidden_flags_before_anything_runs(project: Path, capsys) -> None:
    toml = project / "tazuna.toml"
    toml.write_text(
        toml.read_text()
        + '\n[providers.risky]\ncommand = ["risky", "--sandbox", "{permission_mode}"]\n'
        + 'write_mode = "danger-full-access"\nread_only_mode = "read-only"\n'
        + 'read_only_args = ["--approval-mode", "yolo"]\n',
        encoding="utf-8",
    )
    text = run_doctor(project).render()
    assert "WARN provider risky: forbidden flag or value 'danger-full-access' in write_mode" in text
    assert "WARN provider risky: forbidden flag or value 'yolo' in read_only_args" in text
    assert "provider fakecli: forbidden" not in text
    assert main(["run", "--provider", "risky", "--dir", str(project), "--dry-run", "x"]) == 3
    assert "forbidden flag" in capsys.readouterr().err


def test_doctor_warns_when_state_is_readable_by_others(project: Path) -> None:
    render(load_config(project))
    sd = state_dir()
    sd.mkdir(parents=True)
    os.chmod(sd, 0o755)
    ledger = sd / "runs.jsonl"
    ledger.write_text("", encoding="utf-8")
    os.chmod(ledger, 0o644)
    text = run_doctor(project).render()
    assert f"WARN state directory {sd} is accessible by group/other (mode 755)" in text
    assert f"WARN run ledger {ledger} is readable by group/other (mode 644)" in text
    assert "chmod -R go-rwx" in text
    os.chmod(sd, 0o700)
    os.chmod(ledger, 0o600)
    text = run_doctor(project).render()
    assert "state directory" in text and "accessible by group/other" not in text


def test_doctor_warns_when_state_belongs_to_another_user(project: Path, monkeypatch) -> None:
    state_dir().mkdir(parents=True, mode=0o700)
    real_uid = os.getuid()
    monkeypatch.setattr(doctor_mod.os, "getuid", lambda: real_uid + 1)
    text = run_doctor(project).render()
    assert f"WARN state directory {state_dir()} is owned by uid {real_uid}" in text


def test_doctor_and_status_never_run_commands_planted_in_git_config(
    project: Path, sandbox: Path, monkeypatch, capsys
) -> None:
    marker = sandbox / "planted-command-ran"
    planted = sandbox / "planted.sh"
    planted.write_text(f"#!/bin/sh\nenv >> {marker}\ncat\n")
    planted.chmod(0o755)
    (project / "a.txt").write_text("base\n")
    ident = ["-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    subprocess.run(["git", "add", "a.txt"], cwd=project, check=True)
    subprocess.run(["git", *ident, "commit", "-qm", "base"], cwd=project, check=True)
    for key in ("core.fsmonitor", "filter.evil.clean", "filter.evil.process"):
        subprocess.run(["git", "config", key, str(planted)], cwd=project, check=True)
    (project / ".gitattributes").write_text("a.txt filter=evil\n")
    (project / "a.txt").write_text("BASE\n")
    monkeypatch.setenv("MY_API_TOKEN", "sk-must-not-reach-git-1234")
    run_doctor(project)
    capsys.readouterr()
    assert main(["status", "--dir", str(project)]) == 0
    assert "dirty files: 4" in capsys.readouterr().out
    assert not marker.exists(), marker.read_text()[:300]


def test_status_refuses_git_where_a_filter_cannot_be_neutralised(project: Path, capsys) -> None:
    subprocess.run(["git", "config", "filter.a=b.clean", "/bin/false"], cwd=project, check=True)
    assert main(["status", "--dir", str(project)]) == 0
    assert "- git status skipped: git config defines a filter driver" in capsys.readouterr().out


def test_doctor_reports_broken_config_as_warn(sandbox: Path) -> None:
    proj = sandbox / "work" / "broken"
    proj.mkdir(parents=True)
    (proj / "tazuna.toml").write_text('[roles.x]\nprovider = "ghost"\n', encoding="utf-8")
    report = run_doctor(proj)
    assert report.warnings == 1 and "configuration" in report.findings[-1].message


# ---- guard --------------------------------------------------------------------------------------


def test_guard_flags_generated_files_only(project: Path) -> None:
    render(load_config(project))
    assert check_paths([str(project / "CLAUDE.md"), str(project / "PROJECT.md"), "/nonexistent"]) == [
        project / "CLAUDE.md"
    ]


def test_claude_hook_asks_for_generated_and_is_silent_otherwise(project: Path) -> None:
    render(load_config(project))
    event = {"tool_name": "Edit", "tool_input": {"file_path": str(project / "CLAUDE.md")}}
    out = json.loads(claude_hook(json.dumps(event)))
    assert out["hookSpecificOutput"]["permissionDecision"] == "ask"
    assert (
        claude_hook(json.dumps({"tool_name": "Edit", "tool_input": {"file_path": str(project / "PROJECT.md")}})) is None
    )
    assert claude_hook(json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"}})) is None
    assert claude_hook("not json") is None


def test_guard_cli_hook_answers_through_the_documented_command(project: Path, monkeypatch, capsys) -> None:
    render(load_config(project))
    event = {"tool_name": "Edit", "tool_input": {"file_path": str(project / "CLAUDE.md")}}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(event)))
    assert main(["guard", "--hook", "claude"]) == 0
    assert json.loads(capsys.readouterr().out)["hookSpecificOutput"]["permissionDecision"] == "ask"


def test_guard_cli_print_hook_and_paths(project: Path, capsys) -> None:
    render(load_config(project))
    assert main(["guard", "--print-hook"]) == 0
    snippet = json.loads(capsys.readouterr().out)
    assert snippet["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == "tazuna guard --hook claude"
    assert main(["guard", str(project / "CLAUDE.md"), str(project / "PROJECT.md")]) == 2
    assert main(["guard", str(project / "PROJECT.md")]) == 0


# ---- evals --------------------------------------------------------------------------------------


def _suite(sandbox: Path) -> Path:
    suite = sandbox / "suite"
    ok = suite / "echo-ok"
    ok.mkdir(parents=True)
    (ok / "task.md").write_text("Say hello\n", encoding="utf-8")
    (ok / "case.toml").write_text(
        'expect_exit = 0\nexpect_status = "done"\nexpect_output_regex = "Say hello"\n', encoding="utf-8"
    )
    (ok / "check.sh").write_text(
        '#!/usr/bin/env bash\ntest -f "$TAZUNA_HANDOFF" && grep -q "status: done" "$TAZUNA_HANDOFF"\n',
        encoding="utf-8",
    )
    seeded = suite / "seeded"
    (seeded / "seed" / "src").mkdir(parents=True)
    (seeded / "seed" / "src" / "app.py").write_text("print('hi')\n", encoding="utf-8")
    (seeded / "task.md").write_text("Inspect src/app.py\n", encoding="utf-8")
    (seeded / "case.toml").write_text('expect_files = ["src/app.py", "src/missing.py"]\n', encoding="utf-8")
    return suite


def test_eval_suite_runs_grades_and_reports(project: Path, sandbox: Path) -> None:
    suite = _suite(sandbox)
    cfg = load_config(project)
    results_path, results = run_suite(cfg, suite, role="dry", provider=None, trials=2, results_dir=sandbox / "out")
    by_case = {(r.case, r.trial): r for r in results}
    assert by_case[("echo-ok", 1)].passed and by_case[("echo-ok", 2)].passed
    assert not by_case[("seeded", 1)].passed
    assert by_case[("seeded", 1)].reasons == ["expected file missing: src/missing.py"]
    rows = load_results(results_path)
    assert len(rows) == 4
    report = render_report(rows, None)
    assert "| echo-ok | mock/-/low | 2/2 | 100% |" in report
    assert "| seeded | mock/-/low | 0/2 | 0% |" in report
    assert (sandbox / "out" / "report.md").is_file()
    # signed diff against a baseline in which seeded passed
    baseline = [dict(r, passed=True) for r in rows]
    diff = render_report(rows, baseline)
    assert "-100pt" in diff and "+0pt" in diff


def _row(case: str, passed: bool, status: str = "done", trial: int = 1) -> dict:
    return {
        "suite": "s",
        "case": case,
        "trial": trial,
        "role": "r",
        "provider": "p",
        "model": "m",
        "effort": "e",
        "exit": 0 if passed else 1,
        "status": status,
        "seconds": 1,
        "passed": passed,
        "reasons": [] if passed else [f"status {status}"],
        "workdir": "/w",
        "ts": "",
    }


def test_report_excludes_infrastructure_failures_but_counts_timeouts() -> None:
    rows = [
        _row("a", True),
        _row("a", False, "quota-exhausted", 2),
        _row("a", False, "auth-required", 3),
        _row("b", True),
        _row("b", False, "timeout", 2),
    ]
    report = render_report(rows, None)
    assert "| case | route | pass | rate | excluded |" in report
    assert "| a | p/m/e | 1/1 | 100% | 2 |" in report
    assert "| b | p/m/e | 1/2 | 50% | 0 |" in report
    assert "overall: 2/3 (67%); excluded (quota, login or cancelled): 2" in report
    failures = report.split("## failures")[1].split("## excluded")[0]
    assert "b trial 2" in failures and "a trial 2" not in failures
    assert "- a trial 2: status quota-exhausted" in report.split("## excluded (quota, login or cancelled)")[1]
    only_infra = render_report([_row("c", False, "quota-exhausted")], [_row("c", True)])
    assert "| c | p/m/e | 0/0 | n/a | 1 | 1/1 (100%) | n/a |" in only_infra


def test_a_cancelled_trial_stops_the_suite_and_is_not_counted(
    project: Path, sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TAZUNA_MOCK_SLEEP", "3")
    timer = threading.Timer(0.5, os.kill, (os.getpid(), signal.SIGINT))
    timer.start()
    try:
        with pytest.raises(TazunaError) as info:
            run_suite(
                load_config(project), _suite(sandbox), role="dry", provider=None, trials=2, results_dir=sandbox / "out"
            )
    finally:
        timer.cancel()
    assert info.value.code == 130
    rows = load_results(sandbox / "out" / "results.jsonl")
    assert [(r["case"], r["trial"], r["status"]) for r in rows] == [("echo-ok", 1, "cancelled")]
    assert "| echo-ok | mock/-/low | 0/0 | n/a | 1 |" in (sandbox / "out" / "report.md").read_text()


def test_eval_report_fail_on_regression(sandbox: Path, capsys) -> None:
    def write(name: str, rows: list[dict]) -> str:
        path = sandbox / f"{name}.jsonl"
        path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        return str(path)

    def gate(results: str, baseline: str) -> tuple[int, str]:
        capsys.readouterr()
        code = main(["eval", "report", results, "--baseline", baseline, "--fail-on-regression"])
        return code, capsys.readouterr().err

    gone = _row("gone", True)
    base = write("base", [_row("a", True), _row("a", True, trial=2), _row("b", False), gone])
    same = write("same", [_row("a", True), _row("a", True, trial=2), _row("b", False), gone, _row("new", False)])
    better = write("better", [_row("a", True), _row("b", True), gone])
    worse = write("worse", [_row("a", True), _row("a", False, trial=2), _row("b", False), gone])
    infra = write("infra", [_row("a", True), _row("a", False, "quota-exhausted", 2), _row("b", False), gone])
    for results in (same, better):
        assert gate(results, base)[0] == 0, results
    code, err = gate(worse, base)
    assert code == 1 and "regression: a: 1/2 (50%) < baseline 2/2 (100%)" in err
    capsys.readouterr()
    assert main(["eval", "report", worse, "--baseline", base]) == 0
    assert "| a | p/m/e | 1/2 | 50% | 0 | 2/2 (100%) | -50pt |" in capsys.readouterr().out
    assert main(["eval", "report", worse, "--fail-on-regression"]) == 2
    assert "--fail-on-regression needs --baseline" in capsys.readouterr().err
    # results that cannot show the baseline's cases fail the gate: a stopped suite, a dropped case, all runs excluded
    dropped = write("dropped", [_row("a", True), _row("a", True, trial=2), _row("b", False)])
    code, err = gate(dropped, base)
    assert code == 1 and "incomplete: gone (in the baseline, missing from these results)" in err
    all_infra = write("all-infra", [_row("a", False, "auth-required"), _row("b", False), gone])
    code, err = gate(all_infra, base)
    assert code == 1 and "incomplete: a trial 1 was excluded (auth-required)" in err
    # one excluded trial is enough: a failure whose output mentions a quota word must not hide a regression
    code, err = gate(infra, base)
    assert code == 1 and "incomplete: a trial 2 was excluded (quota-exhausted)" in err
    cancelled = write("cancelled", [_row("a", True), _row("a", False, "cancelled", 2), _row("b", False), gone])
    code, err = gate(cancelled, base)
    assert code == 1 and "incomplete: a trial 2 was cancelled" in err
    # a baseline case whose runs were all excluded cannot be compared, and does not fail the candidate
    base_infra = write("base-infra", [_row("a", False, "quota-exhausted"), _row("b", False), gone])
    code, err = gate(write("plain", [_row("a", False), _row("b", False), gone]), base_infra)
    assert code == 0 and "not compared: a (every baseline run excluded: quota, login or cancelled)" in err


def test_a_cancelled_trial_stops_the_suite_even_when_its_handoff_cannot_be_written(
    project: Path, sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tazuna import run as run_mod

    def broken(*args, **kwargs):
        raise TazunaError("disk full")

    monkeypatch.setattr(run_mod, "write_handoff", broken)
    monkeypatch.setenv("TAZUNA_MOCK_SLEEP", "3")
    timer = threading.Timer(0.5, os.kill, (os.getpid(), signal.SIGINT))
    timer.start()
    try:
        with pytest.raises(TazunaError) as info:
            run_suite(
                load_config(project), _suite(sandbox), role="dry", provider=None, trials=2, results_dir=sandbox / "out"
            )
    finally:
        timer.cancel()
    assert info.value.code == 130
    rows = load_results(sandbox / "out" / "results.jsonl")
    assert [(r["case"], r["status"]) for r in rows] == [("echo-ok", "handoff-write-failed")]


def test_a_late_sighup_stops_the_suite_with_the_documented_code() -> None:
    from tazuna.run import cancel_exit_code

    assert cancel_exit_code(signal.SIGHUP) == 143
    assert cancel_exit_code(signal.SIGTERM) == 143
    assert cancel_exit_code(signal.SIGINT) == 130


def test_check_script_output_is_masked_in_results(project: Path, sandbox: Path) -> None:
    suite = sandbox / "leaky"
    case = suite / "prints-token"
    case.mkdir(parents=True)
    (case / "task.md").write_text("anything\n", encoding="utf-8")
    (case / "check.sh").write_text("echo 'failed with ghp_abcdefghijklmnop123'; exit 1\n", encoding="utf-8")
    results_path, results = run_suite(
        load_config(project), suite, role="dry", provider=None, trials=1, results_dir=sandbox / "leaky-out"
    )
    assert results[0].reasons == ["check.sh failed: failed with gh-REDACTED"]
    assert "ghp_abcdefghijklmnop123" not in results_path.read_text()


def test_eval_rows_carry_the_ledger_run_id(project: Path, sandbox: Path) -> None:
    cfg = load_config(project)
    results_path, results = run_suite(cfg, _suite(sandbox), role="dry", provider=None, trials=2, results_dir=None)
    ledger_ids = [r["id"] for r in read_records()]
    assert [r.run_id for r in results] == ledger_ids
    assert [row["run_id"] for row in load_results(results_path)] == ledger_ids


def test_bundled_smoke_suite_passes_offline(project: Path) -> None:
    suite = Path(__file__).resolve().parents[1] / "evals" / "suites" / "smoke"
    cfg = load_config(project)
    _, results = run_suite(cfg, suite, role="dry", provider=None, trials=1, results_dir=None)
    assert results and all(r.passed for r in results), [(r.case, r.reasons) for r in results]
    assert (state_dir() / "evals").stat().st_mode & 0o777 == 0o700


# ---- cli ----------------------------------------------------------------------------------------


def test_cli_end_to_end(project: Path, capsys) -> None:
    assert main(["render", "--dir", str(project)]) == 0
    assert main(["render", "--check", "--dir", str(project)]) == 0
    assert main(["routes", "--dir", str(project)]) == 0
    out = capsys.readouterr().out
    assert "shim" in out and "unavailable" in out and "dry" in out
    assert main(["routes", "shim", "--dir", str(project)]) == 0
    assert "fallback: dry" in capsys.readouterr().out
    assert main(["run", "--role", "dry", "--dir", str(project), "--quiet", "hello"]) == 0
    assert "HANDOFF:" in capsys.readouterr().out
    assert main(["run", "--role", "shim", "--dir", str(project), "--dry-run", "hello"]) == 0
    out = capsys.readouterr().out
    assert "command: fakecli --mode write --model fake-fast-1 --effort high" in out
    assert main(["runs"]) == 0
    assert main(["status", "--dir", str(project)]) == 0
    assert "## recent runs" in capsys.readouterr().out
    assert main(["run", "--role", "nope", "--dir", str(project), "x"]) == 2
    assert "unknown role" in capsys.readouterr().err
    assert main([]) == 2


def test_cli_run_json_prints_exactly_one_record(project: Path, capsys, monkeypatch) -> None:
    capsys.readouterr()
    assert main(["run", "--role", "dry", "--dir", str(project), "--json", "hello"]) == 0
    out = capsys.readouterr().out
    record = json.loads(out)  # nothing else on stdout: no provider echo, no HANDOFF line
    assert record["status"] == "done" and record["exit"] == 0 and record["parent"] is None
    assert record["id"] == read_records()[-1]["id"]
    monkeypatch.setenv("TAZUNA_MOCK_EXIT", "9")
    assert main(["run", "--role", "dry", "--dir", str(project), "--json", "fail please"]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "failed"
    assert main(["run", "--role", "dry", "--dir", str(project), "--json", "--dry-run", "x"]) == 2
    assert "--json cannot be combined with --dry-run" in capsys.readouterr().err


def test_cli_init_creates_files_and_doctor_is_clean(sandbox: Path, capsys) -> None:
    proj = sandbox / "work" / "fresh"
    proj.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=proj, check=True)
    assert main(["init", "--dir", str(proj)]) == 0
    assert (proj / "tazuna.toml").is_file() and (proj / "PROJECT.md").is_file()
    cfg = load_config(proj)
    assert set(cfg.roles) == {"dry"}  # no real CLI on PATH → only the offline role
    assert main(["render", "--dir", str(proj)]) == 0
    assert main(["doctor", "--dir", str(proj)]) == 0
    assert "RESULT: ALL OK" in capsys.readouterr().out
    assert main(["init", "--dir", str(proj)]) == 2


def test_cli_init_generates_roles_for_detected_clis(sandbox: Path, fake_cli, capsys) -> None:
    fake_cli("claude")
    fake_cli("codex")
    proj = sandbox / "work" / "detected"
    proj.mkdir(parents=True)
    assert main(["init", "--dir", str(proj)]) == 0
    cfg = load_config(proj)
    assert {"impl", "impl_claude", "review", "dry"} <= set(cfg.roles)
    assert cfg.roles["impl"]["fallback"] == "impl_claude"
