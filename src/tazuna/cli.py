"""Command-line entry point."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import asdict
from importlib import resources
from pathlib import Path

from . import __version__
from .config import PROJECT_CONFIG_NAME, Config, default_project_config, load_config
from .errors import EXIT_USAGE, TazunaError, UsageError
from .handoff import read_records
from .routing import all_provider_status, explain_routes, resolve_route

PROG = "tazuna"


def _out(text: str = "") -> None:
    sys.stdout.write(text + "\n")


def _err(text: str) -> None:
    sys.stderr.write(f"{PROG}: {text}\n")


# ---- init --------------------------------------------------------------------------------------


def _detected_roles(cfg: Config) -> str:
    """Roles for the providers actually installed, so a fresh `doctor` is clean."""
    available = {s.name for s in all_provider_status(cfg) if s.available and s.kind != "mock"}
    blocks: list[str] = []
    if "codex" in available:
        blocks.append(
            '[roles.impl]\nprovider = "codex"\nmodel = "codex_default"\neffort = "low"\ntimeout = 900\n'
            + ('fallback = "impl_claude"\n' if "claude" in available else "")
        )
    if "claude" in available:
        blocks.append('[roles.impl_claude]\nprovider = "claude"\nmodel = "fast"\neffort = "high"\n')
        blocks.append('[roles.review]\nprovider = "claude"\nmodel = "strong"\neffort = "high"\nread_only = true\n')
    if "cursor" in available:
        blocks.append('[roles.bulk_edit]\nprovider = "cursor"\nmodel = "cursor_default"\n')
    if "gemini" in available:
        blocks.append('[roles.gemini]\nprovider = "gemini"\nmodel = "gemini_default"\n')
    if "ollama" in available:
        blocks.append('[roles.local]\nprovider = "ollama"\nmodel = "local"\n')
    blocks.append('[roles.dry]\nprovider = "mock"\neffort = "low"\n')
    return "\n".join(blocks)


def cmd_init(args: argparse.Namespace) -> int:
    project_dir = Path(args.dir or os.getcwd()).resolve()
    name = args.name or project_dir.name
    cfg_path = project_dir / PROJECT_CONFIG_NAME
    if cfg_path.exists() and not args.force:
        raise UsageError(f"{cfg_path} already exists (use --force to overwrite)")
    base = load_config(project_dir, ignore_project=True)
    models_extra = ""
    available = {s.name for s in all_provider_status(base) if s.available}
    if "cursor" in available:
        models_extra += 'cursor_default = "composer-2.5"\n'
    if "gemini" in available:
        models_extra += 'gemini_default = "gemini-3.1-pro"\n'
    text = (
        default_project_config(name)
        .replace("{{models_extra}}", models_extra)
        .replace("{{roles}}", _detected_roles(base))
    )
    project_dir.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(text, encoding="utf-8")
    _out(f"wrote {cfg_path}")
    policy = project_dir / "PROJECT.md"
    if not policy.exists():
        template = resources.files("tazuna").joinpath("templates/PROJECT.md").read_text(encoding="utf-8")
        policy.write_text(template.replace("{{name}}", name), encoding="utf-8")
        _out(f"wrote {policy}")
    elif (project_dir / "CLAUDE.md").is_file():
        _out("PROJECT.md exists; leaving it untouched")
    if not policy.exists() and (project_dir / "CLAUDE.md").is_file():
        _out("hint: a hand-written CLAUDE.md exists; `tazuna render --adopt` promotes it to PROJECT.md")
    _out("next: edit PROJECT.md, then `tazuna render` and `tazuna doctor`")
    return 0


# ---- render -------------------------------------------------------------------------------------


def cmd_render(args: argparse.Namespace) -> int:
    from .render import adopt, render

    cfg = load_config(args.dir)
    if args.adopt:
        backup = adopt(cfg)
        _out(f"adopted CLAUDE.md into PROJECT.md (backup: {backup.name})")
        args.force = True
    results = render(cfg, check=args.check, force=args.force, targets=args.target or None)
    bad = oversize = 0
    for res in results:
        rel = res.path.relative_to(cfg.project_dir)
        if res.action in ("written", "unchanged"):
            _out(f"{res.action:<9} {rel}")
            if res.too_large:
                _out(f"{'WARNING':<9} {rel}: {res.size_note}")
        elif res.action == "too-large":
            oversize += 1
            _out(f"{res.action.upper():<9} {rel}: {res.message}")
        else:
            bad += 1
            _out(f"{res.action.upper():<9} {rel}: {res.message}")
    if args.check:
        if bad:
            _out(f"DRIFT: {bad} target(s) out of sync")
        if oversize:
            _out(f"TOO-LARGE: {oversize} target(s) over max_bytes")
        if not bad and not oversize:
            _out("OK: all targets in sync with PROJECT.md")
    return 1 if bad or oversize else 0


# ---- run ----------------------------------------------------------------------------------------


def _read_task(args: argparse.Namespace) -> str:
    # Bytes that are not UTF-8 are kept as surrogates (as argv already is) so run_task refuses all
    # three sources the same way, with exit 2, instead of a traceback here.
    if args.task_file:
        return Path(args.task_file).read_bytes().decode("utf-8", "surrogateescape")
    if args.task:
        return " ".join(args.task)
    if not sys.stdin.isatty():
        return sys.stdin.buffer.read().decode("utf-8", "surrogateescape")
    return ""


def cmd_run(args: argparse.Namespace) -> int:
    from .run import run_task

    cfg = load_config(args.dir)
    route = resolve_route(
        cfg,
        role=args.role,
        provider=args.provider,
        model=args.model,
        effort=args.effort,
        timeout=args.timeout,
        read_only=True if args.read_only else None,
    )
    task = _read_task(args)
    if args.json and args.dry_run:
        raise UsageError("--json cannot be combined with --dry-run")
    if args.dry_run:
        from .run import build_command

        target = Path(args.dir or os.getcwd()).resolve()
        _out(f"route: {route.describe()}")
        if cfg.providers[route.provider].get("kind", "cli") == "mock":
            _out("command: <built-in mock>")
        else:
            argv, stdin_text = build_command(cfg, route, task or "<task>", target)
            _out("command: " + " ".join(_shell_quote(a) if a != task else "<task>" for a in argv))
            _out("task via: " + ("stdin" if stdin_text is not None else "argument"))
        return 0
    outcome = run_task(
        cfg,
        task,
        route=route,
        target_dir=Path(args.dir or os.getcwd()),
        out=Path(args.out) if args.out else None,
        # With --json, stdout carries exactly one JSON object, so provider output is not echoed.
        echo=not (args.quiet or args.json),
        allow_fallback=not args.no_fallback,
    )
    if args.json:
        _out(json.dumps(asdict(outcome.record)))
    else:
        _out(f"HANDOFF: {outcome.record.out} (exit={outcome.code}, status={outcome.status}, {outcome.record.seconds}s)")
    return outcome.code


def _shell_quote(arg: str) -> str:
    import shlex

    return shlex.quote(arg)


# ---- routes / doctor / status / runs / packs -------------------------------------------------


def cmd_routes(args: argparse.Namespace) -> int:
    cfg = load_config(args.dir)
    if args.role:
        route = resolve_route(cfg, role=args.role)
        spec = cfg.providers[route.provider]
        _out(f"role {args.role}: {route.describe()}")
        _out(f"  provider: {spec.get('description', route.provider)}")
        if route.fallback:
            fb = resolve_route(cfg, role=route.fallback)
            _out(
                f"  fallback: {route.fallback} -> {fb.describe()} (used once, only on quota-exhausted / auth-required)"
            )
        _out("  explicit --model/--effort/--timeout/--read-only always override the role")
        return 0
    rows = explain_routes(cfg)
    if args.json:
        _out(json.dumps(rows, indent=2))
        return 0
    if not rows:
        _out("no roles defined in " + str(cfg.project_file))
        return 0
    width = max(len(r["role"]) for r in rows)
    _out(f"{'ROLE':<{width}}  PROVIDER  MODEL                      EFFORT  TIMEOUT  RO  FALLBACK      STATUS")
    for r in rows:
        if r.get("problem"):
            _out(f"{r['role']:<{width}}  !! {r['problem']}")
            continue
        status = "ready" if r["available"] else f"unavailable ({r['note']})"
        _out(
            f"{r['role']:<{width}}  {r['provider']:<8}  {r['model']:<25}  {r['effort']:<6}  {r['timeout']:>6}s  "
            f"{'y' if r['read_only'] else '-':<2}  {r['fallback']:<12}  {status}"
        )
    _out("")
    _out("providers: " + ", ".join(f"{s.name}={'ok' if s.available else 'missing'}" for s in all_provider_status(cfg)))
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    from .doctor import run_doctor

    report = run_doctor(Path(args.dir) if args.dir else None)
    if args.json:
        _out(json.dumps([f.__dict__ for f in report.findings], indent=2))
    else:
        _out(report.render())
    return 1 if report.warnings else 0


def _age(path: Path) -> str:
    secs = time.time() - path.stat().st_mtime
    if secs < 3600:
        return f"{int(secs // 60)}m ago"
    if secs < 86400:
        return f"{int(secs // 3600)}h ago"
    return f"{int(secs // 86400)}d ago"


def cmd_status(args: argparse.Namespace) -> int:
    from .render import render

    cfg = load_config(args.dir)
    _out(f"# tazuna status — {cfg.project_dir}")
    _out("")
    _out("## git")
    if shutil.which("git"):
        from .gitstate import UnsafeRepository, filter_overrides, safe_git

        # The project may be a --dir a provider wrote to: git must not run anything planted in it.
        try:
            res = safe_git(
                cfg.project_dir, "status", "--porcelain", "--branch", "--ignore-submodules=dirty",
                config=filter_overrides(cfg.project_dir),
            )  # fmt: skip
        except UnsafeRepository as exc:
            _out(f"- git status skipped: {exc}")
        except (OSError, subprocess.TimeoutExpired) as exc:
            _out(f"- git status failed: {exc}")
        else:
            if res.returncode == 0:
                lines = res.stdout.decode("utf-8", "replace").splitlines()
                _out(f"- {lines[0][3:] if lines else '?'}; dirty files: {len(lines) - 1 if lines else 0}")
            else:
                _out("- not a git repository")
    _out("")
    _out("## contract")
    if cfg.policy_file.is_file():
        try:
            for res in render(cfg, check=True):
                _out(f"- {res.target}: {res.action}")
        except TazunaError as exc:
            _out(f"- ⚠ {exc}")
    else:
        _out(f"- ⚠ {cfg.policy_file.name} missing (tazuna init)")
    handoff = cfg.project_dir / "HANDOFF.md"
    _out("")
    _out("## handoff")
    if handoff.is_file():
        status_line = next(
            (
                line
                for line in handoff.read_text(encoding="utf-8", errors="replace").splitlines()
                if line.startswith("- status:")
            ),
            "",
        )
        _out(f"- HANDOFF.md {_age(handoff)} {status_line}")
    else:
        _out("- no HANDOFF.md")
    _out("")
    _out("## recent runs")
    rows = [r for r in read_records() if r.get("dir") == str(cfg.project_dir)][-5:]
    if not rows:
        _out("- none for this project")
    for r in rows:
        _out(
            f"- {r['ts']} {r['provider']}/{r.get('role') or '-'} exit={r['exit']} {r['status']} "
            f"{r['seconds']}s — {r.get('task_head', '')[:60]}"
        )
    return 0


def cmd_runs(args: argparse.Namespace) -> int:
    rows = read_records(limit=args.limit)
    if args.json:
        _out(json.dumps(rows, indent=2, ensure_ascii=False))
        return 0
    if not rows:
        _out("no runs recorded yet")
        return 0
    for r in rows:
        _out(
            f"{r['id']}  {r['provider']:<7} {(r.get('role') or '-'):<12} exit={r['exit']:<3} "
            f"{r['status']:<18} {r['seconds']:>5}s  {r.get('task_head', '')[:50]}"
        )
    return 0


def cmd_packs(args: argparse.Namespace) -> int:
    cfg = load_config(args.dir)
    if not cfg.packs:
        _out("no packs loaded (add directories under [packs] paths)")
        return 0
    for pack in cfg.packs:
        _out(f"{pack.get('name')}  {pack.get('version', '')}  {pack['path']}")
        if pack.get("description"):
            _out(f"  {pack['description']}")
    return 0


def cmd_eval(args: argparse.Namespace) -> int:
    from .evals import incomplete, load_results, regressions, render_report, run_suite, uncompared

    if args.eval_cmd == "run":
        cfg = load_config(args.dir)
        results_path, results = run_suite(
            cfg,
            Path(args.suite).resolve(),
            role=args.role,
            provider=args.provider,
            trials=args.trials,
            results_dir=Path(args.out).resolve() if args.out else None,
            only=args.only,
        )
        passed = sum(1 for r in results if r.passed)
        _out(f"results: {results_path} ({passed}/{len(results)} passed); report: {results_path.parent / 'report.md'}")
        return 0 if passed == len(results) else 1
    if args.eval_cmd == "report":
        if args.fail_on_regression and not args.baseline:
            raise UsageError("--fail-on-regression needs --baseline")
        rows = load_results(Path(args.results))
        baseline = load_results(Path(args.baseline)) if args.baseline else None
        _out(render_report(rows, baseline).rstrip("\n"))
        if args.fail_on_regression and baseline is not None:
            found = regressions(rows, baseline)
            gaps = incomplete(rows, baseline)
            for line in found:
                _err(f"regression: {line}")
            for line in gaps:
                _err(f"incomplete: {line}")
            for line in uncompared(rows, baseline):
                _err(f"not compared: {line}")
            return 1 if found or gaps else 0
        return 0
    raise UsageError("eval needs a subcommand: run | report")


def cmd_guard(args: argparse.Namespace) -> int:
    from .guard import main as guard_main

    return guard_main(args.paths, hook=args.hook, print_hook=args.print_hook)


# ---- parser -------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description=(
            "Define project policy once, render it into every agent harness, "
            "route work across providers, and get auditable results."
        ),
    )
    parser.add_argument("--version", action="version", version=f"{PROG} {__version__}")
    sub = parser.add_subparsers(dest="cmd", metavar="COMMAND")

    p = sub.add_parser("init", help="create tazuna.toml and PROJECT.md in a project")
    p.add_argument("--dir", help="project directory (default: current)")
    p.add_argument("--name", help="project name (default: directory name)")
    p.add_argument("--force", action="store_true", help="overwrite an existing tazuna.toml")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("render", help="render PROJECT.md into the enabled targets")
    p.add_argument("--dir")
    p.add_argument("--check", action="store_true", help="report drift only; exit 1 if any target differs")
    p.add_argument("--force", action="store_true", help="overwrite hand-written files that lack the generated marker")
    p.add_argument("--adopt", action="store_true", help="promote an existing CLAUDE.md to PROJECT.md first")
    p.add_argument("--target", action="append", help="render only this target (repeatable)")
    p.set_defaults(func=cmd_render)

    p = sub.add_parser("run", help="run a task on a provider and write HANDOFF.md")
    p.add_argument("task", nargs="*", help="task text (or --task-file, or stdin)")
    p.add_argument("--role", help="role from [roles] in tazuna.toml")
    p.add_argument("--provider", help="provider name (when not using --role)")
    p.add_argument("--model", help="override the model id or [models] alias")
    p.add_argument("--effort", help="override the reasoning effort")
    p.add_argument("--timeout", type=int, help="seconds (1-7200)")
    p.add_argument("--dir", help="working directory the provider may write to (default: current)")
    p.add_argument("--out", help="HANDOFF path (default: <dir>/HANDOFF.md; must be inside --dir)")
    p.add_argument("--task-file", help="read the task from a file")
    p.add_argument("--read-only", action="store_true", help="ask the provider not to modify files")
    p.add_argument("--no-fallback", action="store_true", help="never retry on the role's fallback")
    p.add_argument("--quiet", action="store_true", help="do not echo provider output")
    p.add_argument("--dry-run", action="store_true", help="print the resolved route and command, run nothing")
    p.add_argument(
        "--json", action="store_true", help="print the run's ledger record as one JSON object instead (implies --quiet)"
    )
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("routes", help="show what each role resolves to and whether its provider is available")
    p.add_argument("role", nargs="?", help="explain one role")
    p.add_argument("--dir")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_routes)

    p = sub.add_parser("doctor", help="diagnose configuration, providers, render drift, and state")
    p.add_argument("--dir")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("status", help="one-screen project status")
    p.add_argument("--dir")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("runs", help="list recorded runs")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_runs)

    p = sub.add_parser("packs", help="list loaded packs")
    p.add_argument("--dir")
    p.set_defaults(func=cmd_packs)

    p = sub.add_parser("eval", help="run or report reproducible eval suites")
    es = p.add_subparsers(dest="eval_cmd", metavar="SUBCOMMAND")
    er = es.add_parser("run", help="run every case in a suite directory")
    er.add_argument("suite")
    er.add_argument("--role")
    er.add_argument("--provider")
    er.add_argument("--trials", type=int, default=1)
    er.add_argument("--only", help="regex on case names")
    er.add_argument("--out", help="results directory (default: under the state dir)")
    er.add_argument("--dir", help="project whose tazuna.toml supplies roles (default: current)")
    ep = es.add_parser("report", help="render a results.jsonl as markdown")
    ep.add_argument("results")
    ep.add_argument("--baseline", help="another results.jsonl to diff against")
    ep.add_argument(
        "--fail-on-regression",
        action="store_true",
        help="exit 1 when a case present in both files has a lower pass rate than the baseline",
    )
    p.set_defaults(func=cmd_eval)

    p = sub.add_parser("guard", help="refuse edits to generated files (for harness hooks)")
    # Not argparse.REMAINDER: it rejects arguments that start with "-", so the documented
    # `guard --hook claude` exited 2, which Claude Code reads as "block this tool call".
    p.add_argument("paths", nargs="*", help="exit 2 when any of these is a generated file")
    p.add_argument("--hook", choices=["claude"], help="answer a harness hook event read from stdin")
    p.add_argument("--print-hook", action="store_true", help="print the Claude Code settings snippet")
    p.set_defaults(func=cmd_guard)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return EXIT_USAGE
    try:
        return int(args.func(args))
    except TazunaError as exc:
        _err(str(exc))
        return exc.code
    except KeyboardInterrupt:
        _err("interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
