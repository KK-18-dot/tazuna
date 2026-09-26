"""Diagnostics: ``tazuna doctor``.

Every line is ``OK``, ``WARN`` or ``NOTE``. WARN means something is broken or drifted and the
exit code is 1. NOTE is informational (an optional provider you do not have, a legitimate human
choice) and never changes the exit code. That split keeps the exit code meaningful in CI.
"""

from __future__ import annotations

import fnmatch
import os
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config, ensure_state_dir, load_config, pre_rename_leftovers, state_dir
from .errors import TazunaError
from .gitstate import safe_git
from .handoff import ledger_path
from .render import is_generated, render
from .routing import all_provider_status, explain_routes
from .run import forbidden_in_definition

SECRET_NAME_GLOBS = [
    ".env",
    ".env.*",
    "*/.env",
    "*/.env.*",
    "secrets/*",
    "*/secrets/*",
    "*.pem",
    "*.p12",
    "*.key",
    "id_rsa",
    "*/id_rsa",
    "id_ed25519",
    "*/id_ed25519",
    "*.keystore",
    "credentials.json",
    "*/credentials.json",
]
SECRET_NAME_EXEMPT = ["*.example", "*.sample", "*.template", "*.dist"]


@dataclass
class Finding:
    level: str  # OK | WARN | NOTE
    message: str


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)

    def ok(self, msg: str) -> None:
        self.findings.append(Finding("OK", msg))

    def warn(self, msg: str) -> None:
        self.findings.append(Finding("WARN", msg))

    def note(self, msg: str) -> None:
        self.findings.append(Finding("NOTE", msg))

    @property
    def warnings(self) -> int:
        return sum(1 for f in self.findings if f.level == "WARN")

    def render(self) -> str:
        lines = [f"{f.level:<4} {f.message}" for f in self.findings]
        lines.append("---")
        lines.append("RESULT: WARN" if self.warnings else "RESULT: ALL OK")
        return "\n".join(lines)


def check_tracked_secret_names(project_dir: Path, report: Report) -> None:
    """Only file *names* are inspected, never contents."""
    try:
        res = safe_git(project_dir, "ls-files", "-z")
    except (OSError, subprocess.TimeoutExpired):
        res = None
    if res is None or res.returncode != 0:
        report.note("not a git repository: tracked-secret-name check skipped")
        return
    hits = []
    for name in res.stdout.decode("utf-8", "replace").split("\0"):
        if not name:
            continue
        if any(fnmatch.fnmatch(name, g) for g in SECRET_NAME_EXEMPT):
            continue
        if any(fnmatch.fnmatch(name, g) for g in SECRET_NAME_GLOBS):
            hits.append(name)
    if hits:
        report.warn(
            f"tracked files with secret-like names: {', '.join(hits[:10])}" + (" ..." if len(hits) > 10 else "")
        )
    else:
        report.ok("no tracked files with secret-like names")


def run_doctor(project_dir: Path | None = None) -> Report:
    report = Report()
    report.ok(f"python {sys.version.split()[0]}")

    try:
        cfg = load_config(project_dir)
    except TazunaError as exc:
        report.warn(f"configuration: {exc}")
        return report
    report.ok("configuration loaded from: " + " < ".join(cfg.sources))
    for old in pre_rename_leftovers(cfg.project_dir):
        new = old.with_name(old.name.replace("agentplane", "tazuna"))
        report.warn(f"{old} is from before the rename to tazuna and is not read; rename it to {new}")
    if not cfg.project_file.is_file():
        report.note(f"no {cfg.project_file.name} in {cfg.project_dir} (run `tazuna init` to create one)")

    _check_providers(cfg, report)
    _check_models(cfg, report)
    _check_render(cfg, report)
    _check_state(report)
    check_tracked_secret_names(cfg.project_dir, report)
    depth = os.environ.get("TAZUNA_DEPTH")
    if depth:
        report.note(f"running inside a delegated provider (TAZUNA_DEPTH={depth})")
    return report


def _check_providers(cfg: Config, report: Report) -> None:
    statuses = {s.name: s for s in all_provider_status(cfg)}
    used = {spec.get("provider") for spec in cfg.roles.values()}
    real_available = [s.name for s in statuses.values() if s.available and s.kind != "mock"]
    for status in statuses.values():
        label = f"provider {status.name}"
        if status.available:
            where = f" ({status.path})" if status.path else ""
            report.ok(f"{label}: available{where}" + (" [experimental]" if status.experimental else ""))
        elif status.name in used:
            roles = ", ".join(sorted(r for r, s in cfg.roles.items() if s.get("provider") == status.name))
            report.warn(f"{label}: {status.note}; used by roles: {roles}")
        else:
            report.note(f"{label}: {status.note} (not used by any role)")
    if not real_available:
        report.note(
            "no real provider CLI found; only the mock provider will run. "
            "Install one (claude, codex, cursor, gemini, ollama) and rerun."
        )
    for name in sorted(cfg.providers):
        for key, arg in forbidden_in_definition(cfg.providers[name]):
            report.warn(
                f"provider {name}: forbidden flag or value {arg!r} in {key}; `tazuna run` refuses it "
                "because it disables the harness's own approvals or sandbox (remove it from the provider table)"
            )
    for row in explain_routes(cfg):
        if row.get("problem"):
            report.warn(f"role {row['role']}: {row['problem']}")
    if not cfg.roles:
        report.note("no roles defined; `tazuna run` needs --provider until you add [roles.*]")


def _check_models(cfg: Config, report: Report) -> None:
    retired = set(cfg.retired_models)
    if not retired:
        report.ok("no retired models declared")
        return
    hits = [f"[models] {k}" for k, v in cfg.models.items() if v in retired]
    for name in cfg.render_targets:
        path = cfg.project_dir / cfg.targets[name]["path"]
        if path.is_file():
            text = path.read_text(encoding="utf-8", errors="replace")
            for model in retired:
                if model in text:
                    hits.append(f"{path.name} mentions {model}")
    if hits:
        report.warn("retired model ids in use: " + "; ".join(hits))
    else:
        report.ok(f"retired models absent ({len(retired)} declared)")


def _check_render(cfg: Config, report: Report) -> None:
    if not cfg.policy_file.is_file():
        report.note(f"{cfg.policy_file.name} not found; nothing to render")
        return
    try:
        results = render(cfg, check=True)
    except TazunaError as exc:
        report.warn(f"render: {exc}")
        return
    for res in results:
        rel = res.path.relative_to(cfg.project_dir)
        if res.too_large:
            report.warn(f"render target {res.target}: {rel} is {res.size_note}")
        if res.action == "unchanged":
            report.ok(f"render target {res.target}: {rel} in sync")
        elif res.action == "missing":
            report.note(f"render target {res.target}: {rel} not rendered yet (run `tazuna render`)")
        elif res.action == "drift":
            if is_generated(res.path):
                report.warn(f"render target {res.target}: {rel} drifted from PROJECT.md (run `tazuna render`)")
            else:
                report.warn(
                    f"render target {res.target}: {rel} is hand-written (use `tazuna render --adopt` or --force)"
                )


def _check_state(report: Report) -> None:
    sd = state_dir()
    try:
        ensure_state_dir()
        probe = sd / ".write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        report.ok(f"state directory writable: {sd}")
    except OSError as exc:
        report.warn(f"state directory not writable: {sd} ({exc})")
        return
    # Directories created by agentplane 0.1.0 (or by hand) follow the umask; logs hold full
    # provider output, so an open state directory is a finding, with the one-line fix.
    fix = f"chmod -R go-rwx {shlex.quote(str(sd))}"
    info = sd.stat()
    if info.st_uid != os.getuid():
        report.warn(
            f"state directory {sd} is owned by uid {info.st_uid}, not by you (uid {os.getuid()}); "
            "point TAZUNA_STATE_DIR at a directory of your own"
        )
    mode = info.st_mode & 0o777
    if mode & 0o077:
        report.warn(
            f"state directory {sd} is accessible by group/other (mode {mode:o}); "
            f"it holds provider logs and the run ledger: {fix}"
        )
    ledger = ledger_path()
    if ledger.is_file():
        ledger_mode = ledger.stat().st_mode & 0o777
        if ledger_mode & 0o077:
            report.warn(f"run ledger {ledger} is readable by group/other (mode {ledger_mode:o}): {fix}")
