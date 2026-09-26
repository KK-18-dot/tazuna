"""Execute one task on one provider and record the result.

Safety boundaries (all fail closed, exit 3):
- the working directory must exist and must not be $HOME or an ancestor of it
- the HANDOFF output path must be inside the working directory and must not be a symlink
- only an allowlist of environment variables reaches the provider process
- a provider may delegate once more (TAZUNA_DEPTH), never deeper than [run] max_depth
- provider commands may not contain flags that disable the harness's own sandbox/approvals
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import BinaryIO

from .config import Config, ensure_state_dir
from .errors import (
    EXIT_CANCELLED_INT,
    EXIT_CANCELLED_TERM,
    EXIT_EMPTY,
    EXIT_TIMEOUT,
    ConfigError,
    SafetyError,
    TazunaError,
    UsageError,
)
from .gitstate import diff_git_state, git_state
from .handoff import (
    PREAMBLE,
    RunRecord,
    append_record,
    failure_kind,
    mask_secrets_bytes,
    new_run_id,
    self_report,
    status_for,
    task_head,
    write_handoff,
)
from .routing import Route, provider_status, resolve_route

DEPTH_VAR = "TAZUNA_DEPTH"
PARENT_VAR = "TAZUNA_PARENT"
PARENT_RE = re.compile(r"^[A-Za-z0-9._:/@+-]{1,200}$")
# Claude Code's --dangerously-skip-permissions / --allow-dangerously-skip-permissions and Codex's
# --dangerously-bypass-approvals-and-sandbox (also as a config key).
FORBIDDEN_KEY_WORD = "dangerously"
# -f / -y are the short forms of --force (Cursor) and --yolo (Gemini CLI).
FORBIDDEN_FLAGS = {"--yolo", "--force", "-f", "-y"}
# Values that select a harness's unattended mode, whether passed as the next argument
# (--permission-mode bypassPermissions), after "=" (--sandbox=danger-full-access), or in a
# config override (-c sandbox_mode=danger-full-access).
FORBIDDEN_VALUES = {"bypasspermissions", "danger-full-access", "yolo"}
FORBIDDEN_ANYWHERE = ("bypasspermissions", "danger-full-access")
PROVIDER_ARG_KEYS = ("command", "model_args", "effort_args", "read_only_args", "write_args")
PROVIDER_SCALAR_KEYS = ("write_mode", "read_only_mode", "task_stdin_marker")
CANCEL_SIGNALS = ("SIGINT", "SIGTERM", "SIGHUP")
OUTPUT_GRACE = 30  # seconds to collect output after the provider exited on its own
STOP_GRACE = 5  # the same after a timeout or cancel, before the whole group is killed


@dataclass
class RunOutcome:
    code: int
    status: str
    record: RunRecord
    # The signal that cancelled the run, or one that arrived while its record was written. The
    # record stands either way; callers that loop (eval) stop instead of starting the next run.
    stop_signal: int | None = None


def cancel_exit_code(signum: int | None) -> int:
    """130 for SIGINT, 143 for SIGTERM and SIGHUP (the documented exit codes)."""
    return EXIT_CANCELLED_INT if signum == signal.SIGINT else EXIT_CANCELLED_TERM


# ---- boundary checks ---------------------------------------------------------------------------


def check_dir(target: Path) -> Path:
    try:
        resolved = target.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise SafetyError(f"--dir does not exist: {target} ({exc})") from exc
    if not resolved.is_dir():
        raise SafetyError(f"--dir is not a directory: {target}")
    home = Path.home().resolve()
    ancestor = home
    while True:
        if resolved == ancestor:
            raise SafetyError(f"--dir may not be $HOME or an ancestor of it (write boundary too wide): {target}")
        if ancestor.parent == ancestor:
            break
        ancestor = ancestor.parent
    return resolved


def check_out(out: Path | None, target: Path) -> Path:
    if out is None:
        out = target / "HANDOFF.md"
    if out.name in ("", ".", ".."):
        raise SafetyError(f"invalid --out basename: {out}")
    if out.is_symlink():
        raise SafetyError(f"--out must not be an existing symlink: {out}")
    parent = out.parent
    if not parent.is_absolute():
        parent = Path.cwd() / parent
    try:
        parent = parent.resolve(strict=True)
    except OSError as exc:
        raise SafetyError(f"--out parent directory does not exist: {out}") from exc
    final = parent / out.name
    if final != target and target not in final.parents:
        raise SafetyError(f"--out must be inside --dir ({target}): {final}")
    return final


def check_depth(cfg: Config) -> int:
    raw = os.environ.get(DEPTH_VAR, "0")
    depth = int(raw) if raw.isdigit() else 0
    if depth >= int(cfg.run.get("max_depth", 2)):
        raise SafetyError(f"{DEPTH_VAR}={depth}: delegation depth limit reached; split the task and run it directly")
    return depth


def parent_from_env(*, warn: bool = True) -> str | None:
    """The caller's lineage id from TAZUNA_PARENT, if it is a plain token.

    It ends up in the ledger that other tools parse, so anything outside a conservative
    character set is dropped rather than recorded.
    """
    raw = os.environ.get(PARENT_VAR)
    if not raw:
        return None
    if PARENT_RE.fullmatch(raw):  # fullmatch: "$" alone would accept a trailing newline
        return raw
    if warn:
        print(
            f"tazuna: warning: ignoring {PARENT_VAR} (expected 1-200 characters of A-Z a-z 0-9 . _ : / @ + -)",
            file=sys.stderr,
        )
    return None


# ---- command assembly ----------------------------------------------------------------------------


def is_forbidden_arg(arg: str) -> bool:
    """True when an argv element would switch off a harness's own approvals or sandbox.

    Exact matching let ``--dangerously-skip-permissions=true``, ``--approval-mode yolo`` and
    ``--config=sandbox_mode=danger-full-access`` through. The element is split at every ``=``:
    a key part (the flag name or a config key) must not mention ``dangerously``, the flag name
    must not be a bypass flag, and no part may be a bypass value. The two unambiguous bypass
    values are also refused anywhere inside the element (JSON settings). Case, whitespace and
    quotes are ignored.
    """
    parts = [part.strip().strip("'\"").strip() for part in arg.lower().split("=")]
    keys = parts[:-1] if len(parts) > 1 else [parts[0]] if parts[0].startswith("-") else []
    if any(FORBIDDEN_KEY_WORD in key for key in keys) or (parts[0].startswith("-") and parts[0] in FORBIDDEN_FLAGS):
        return True
    if any(part in FORBIDDEN_VALUES for part in parts):
        return True
    return any(value in arg.lower() for value in FORBIDDEN_ANYWHERE)


def forbidden_in_definition(spec: dict) -> list[tuple[str, str]]:
    """(key, element) pairs in a provider table that ``build_command`` would refuse.

    Used by ``doctor`` so a bad user or pack definition surfaces before anyone runs it.
    """
    hits: list[tuple[str, str]] = []
    for key in PROVIDER_ARG_KEYS:
        hits.extend((key, part) for part in spec.get(key, []) or [] if is_forbidden_arg(part))
    for key in PROVIDER_SCALAR_KEYS:
        value = spec.get(key)
        if isinstance(value, str) and value and is_forbidden_arg(value):
            hits.append((key, value))
    return hits


def empty_mcp_path() -> Path:
    """The empty MCP config handed to Claude Code. Its content is checked on every call, because
    a file that lists servers would silently give a delegated run the tools it was meant to lack."""
    template = resources.files("tazuna").joinpath("templates/empty-mcp.json").read_text(encoding="utf-8")
    path = ensure_state_dir() / "empty-mcp.json"
    for _ in range(3):
        try:
            current = None if path.is_symlink() else path.read_text(encoding="utf-8")
        except OSError:
            current = None
        if current == template:
            return path
        path.unlink(missing_ok=True)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        except FileExistsError:  # a concurrent run is writing it; read it again
            time.sleep(0.05)
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(template)
        return path
    raise TazunaError(f"cannot prepare the empty MCP config at {path}")


def build_command(cfg: Config, route: Route, task: str, target: Path) -> tuple[list[str], str | None]:
    """Return (argv, stdin_text). ``stdin_text`` is None when the task travels as an argument."""
    spec = cfg.providers[route.provider]
    mode_key = "read_only_mode" if route.read_only else "write_mode"
    values = {
        "model": route.model or "",
        "effort": route.effort or "",
        "task": task,
        "dir": str(target),
        "empty_mcp": str(empty_mcp_path()),
        "permission_mode": spec.get(mode_key, ""),
    }
    argv: list[str] = []
    # The task text is the user's, not configuration: check each element with the task blanked
    # so a task that happens to read "yolo" or "-f" is never refused.
    checked: list[str] = []
    no_task = {**values, "task": ""}

    def add(parts: list[str]) -> None:
        for part in parts:
            if "{permission_mode}" in part and not values["permission_mode"]:
                raise ConfigError(f"provider {route.provider}: '{mode_key}' is required by its command template")
            argv.append(part.format_map(values))
            checked.append(part.format_map(no_task))

    add(list(spec.get("command", [])))
    if route.model and spec.get("model_args"):
        add(list(spec["model_args"]))
    if route.effort and spec.get("effort_args"):
        add(list(spec["effort_args"]))
    if route.read_only:
        add(list(spec.get("read_only_args", []) or []))
    else:
        add(list(spec.get("write_args", []) or []))

    stdin_text: str | None
    if spec.get("task_via", "stdin") == "stdin":
        marker = spec.get("task_stdin_marker")
        if marker:
            argv.append(marker)
            checked.append(marker)
        stdin_text = task
    else:
        if not any("{task}" in part for part in spec.get("command", [])):
            argv.append(task)
        stdin_text = None

    for arg in checked:
        if is_forbidden_arg(arg):
            raise SafetyError(
                f"provider {route.provider} command contains a forbidden flag or value: {arg!r} "
                "(it would disable the harness's own approvals or sandbox)"
            )
    return argv, stdin_text


def build_env(cfg: Config, route: Route, depth: int, run_id: str) -> dict[str, str]:
    names = cfg.env_allowlist() + list(cfg.providers[route.provider].get("env_extra", []) or [])
    env = {name: os.environ[name] for name in names if name in os.environ}
    env[DEPTH_VAR] = str(depth + 1)
    # Not a secret: lets a nested `tazuna run` record which run launched it.
    env[PARENT_VAR] = run_id
    return env


# ---- execution ----------------------------------------------------------------------------------


def _create_log(provider: str) -> tuple[str, Path, int]:
    """Reserve a run id by creating its log file exclusively.

    The id already carries a random suffix; O_EXCL makes a collision a retry instead of two runs
    silently sharing (and overwriting) one log.
    """
    log_dir = ensure_state_dir("logs")
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    for _ in range(5):
        run_id = new_run_id(provider)
        log_path = log_dir / f"{run_id}.log"
        try:
            return run_id, log_path, os.open(log_path, flags, 0o600)
        except FileExistsError:
            continue
    raise TazunaError(f"could not allocate a unique run id in {log_dir}")


def _run_mock(cfg: Config, route: Route, task: str, target: Path, log: BinaryIO, echo: bool, trap: CancelTrap) -> int:
    spec = cfg.providers[route.provider]
    response = os.environ.get("TAZUNA_MOCK_RESPONSE")
    if response is None:
        response = spec.get("response")
    if response is None:
        response = (
            f"[mock provider] model={route.model or '-'} effort={route.effort or '-'} read_only={route.read_only}\n"
            f"Task received ({len(task)} chars):\n{task}\n\nTAZUNA-STATUS: DONE\n"
        )
    code_raw = os.environ.get("TAZUNA_MOCK_EXIT", str(spec.get("exit_code", 0)))
    code = int(code_raw) if str(code_raw).lstrip("-").isdigit() else 0
    sleep = float(os.environ.get("TAZUNA_MOCK_SLEEP", "0") or 0)
    if not trap.sleep(min(sleep, route.timeout)):
        log.write(b"[mock] cancelled\n")
        return trap.exit_code
    if sleep > route.timeout:
        log.write(b"[mock] still running\n")
        return EXIT_TIMEOUT
    if not route.read_only:
        for rel, content in (spec.get("writes") or {}).items():
            path = target / rel
            if target not in path.resolve().parents:
                raise SafetyError(f"mock writes outside --dir: {rel}")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(str(content), encoding="utf-8")
    log.write(response.encode("utf-8"))
    if echo:
        sys.stdout.write(response)
        sys.stdout.flush()
    return code


class CancelTrap:
    """Turn SIGINT / SIGTERM / SIGHUP into a recorded cancellation while a run is in progress.

    Providers start in their own session so a terminal's Ctrl-C never reaches them directly.
    Without this trap tazuna itself died on the signal and left the provider running, with
    no HANDOFF and no ledger row. The handler only records the signal (raising inside
    Popen.wait could lose the child's status); the wait loop in ``_run_cli`` stops the
    provider's process group, and any signal received before the run's status is decided makes
    the run ``cancelled``, which also rules out the fallback. Signals the caller already ignores
    (``nohup``, background jobs) stay ignored, and handlers can only be installed from the main
    thread, so the trap is inert elsewhere.
    """

    def __init__(self) -> None:
        self.received: int | None = None  # first signal seen while armed
        self._previous: dict[int, object] = {}

    def __enter__(self) -> CancelTrap:
        if threading.current_thread() is threading.main_thread():
            for name in CANCEL_SIGNALS:
                sig = getattr(signal, name, None)
                if sig is not None and signal.getsignal(sig) is not signal.SIG_IGN:
                    self._previous[sig] = signal.signal(sig, self._record)
        return self

    def __exit__(self, *exc: object) -> None:
        for sig, previous in self._previous.items():
            signal.signal(sig, previous if previous is not None else signal.SIG_DFL)
        self._previous.clear()

    def _record(self, signum: int, frame: object) -> None:
        if self.received is None:
            self.received = signum

    @property
    def exit_code(self) -> int:
        return cancel_exit_code(self.received)

    def sleep(self, seconds: float) -> bool:
        """Sleep in short steps; False as soon as a cancel signal has arrived."""
        end = time.monotonic() + seconds
        while (left := end - time.monotonic()) > 0:
            if self.received is not None:
                return False
            time.sleep(min(left, 0.1))
        return self.received is None


def _kill_group(pgid: int) -> None:
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _run_cli(
    argv: list[str],
    stdin_text: str | None,
    env: dict[str, str],
    cwd: Path,
    timeout: int,
    log: BinaryIO,
    echo: bool,
    trap: CancelTrap,
) -> int:
    try:
        proc = subprocess.Popen(
            argv,
            cwd=str(cwd),
            env=env,
            stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except OSError as exc:
        raise TazunaError(f"cannot start provider: {exc}") from exc

    # Once the run is over the log is masked and rewritten; output that arrives later is drained
    # (so a straggler never blocks on a full pipe) but never written.
    log_lock = threading.Lock()
    log_closed = threading.Event()

    def pump() -> None:
        assert proc.stdout is not None
        # read1, not read: read(4096) waits to fill the buffer, so a provider's short last lines
        # stayed unread while a detached child still held the pipe, and were dropped at the end.
        for chunk in iter(lambda: proc.stdout.read1(4096), b""):
            with log_lock:
                if log_closed.is_set():
                    continue
                log.write(chunk)
                log.flush()
            if echo:
                sys.stdout.buffer.write(chunk)
                sys.stdout.buffer.flush()

    def feed() -> None:
        # In a thread: a provider that never reads a large task must not block the timeout
        # and cancellation checks below.
        assert proc.stdin is not None and stdin_text is not None
        try:
            proc.stdin.write(stdin_text.encode("utf-8"))
        except (BrokenPipeError, OSError, ValueError):
            pass
        finally:
            # Always close: a provider waiting for EOF would otherwise run until the timeout.
            try:
                proc.stdin.close()
            except (BrokenPipeError, OSError, ValueError):
                pass

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    if stdin_text is not None and proc.stdin is not None:
        threading.Thread(target=feed, daemon=True).start()
    timed_out = False
    deadline = time.monotonic() + timeout
    while True:
        if trap.received is not None:
            _terminate(proc)
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            timed_out = True
            _terminate(proc)
            break
        try:
            proc.wait(timeout=min(remaining, 0.2))
            break
        except subprocess.TimeoutExpired:
            continue
    stopping = timed_out or trap.received is not None
    grace_end = time.monotonic() + (STOP_GRACE if stopping else OUTPUT_GRACE)
    while reader.is_alive() and time.monotonic() < grace_end and (stopping or trap.received is None):
        reader.join(timeout=0.2)
    if reader.is_alive():
        # Something the provider started still holds its output pipe after the provider ended (or
        # a cancel arrived meanwhile). The run is over: kill the whole process group. The kernel
        # does not reuse a group id while a member is alive; a holder that left the group with
        # setsid is out of reach, and whatever it still prints is dropped below.
        _kill_group(proc.pid)
        reader.join(timeout=STOP_GRACE)
    with log_lock:
        log_closed.set()
    if trap.received is not None:
        return trap.exit_code
    return EXIT_TIMEOUT if timed_out else proc.returncode


def _terminate(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        proc.wait(timeout=15)


def run_task(
    cfg: Config,
    task: str,
    *,
    route: Route,
    target_dir: Path,
    out: Path | None = None,
    echo: bool = True,
    allow_fallback: bool = True,
    fallback_from: str | None = None,
) -> RunOutcome:
    if not task.strip():
        raise UsageError("task is empty (pass it as an argument, --task-file, or on stdin)")
    try:
        task.encode("utf-8")
    except UnicodeEncodeError as exc:
        # Bytes that are not UTF-8 reach Python as surrogates; nothing downstream can write them.
        raise UsageError(f"task is not valid UTF-8 (at character {exc.start}); convert it to UTF-8 and rerun") from exc
    if "\0" in task:
        raise UsageError("task contains a NUL byte, which cannot be passed to a provider")
    target = check_dir(target_dir)
    out_path = check_out(out, target)
    depth = check_depth(cfg)
    parent = parent_from_env(warn=fallback_from is None)  # warn once, not again on the retry

    status = provider_status(cfg, route.provider)
    if not status.available:
        raise UsageError(
            f"provider {route.provider} is not available: {status.note}. "
            f"Run `tazuna doctor`, or choose another role/provider."
        )

    full_task = PREAMBLE + task
    spec = cfg.providers[route.provider]
    if spec.get("kind", "cli") == "mock":
        argv, stdin_text = ["<mock>"], None
    else:
        argv, stdin_text = build_command(cfg, route, full_task, target)
    run_id, log_path, log_fd = _create_log(route.provider)

    before = git_state(target)
    start = time.monotonic()
    # Armed from launch until the status is decided: a signal in that window cancels the run.
    with CancelTrap() as trap, os.fdopen(log_fd, "w+b") as log:
        if spec.get("kind", "cli") == "mock":
            code = _run_mock(cfg, route, task, target, log, echo, trap)
        else:
            env = build_env(cfg, route, depth, run_id)
            code = _run_cli(argv, stdin_text, env, target, route.timeout, log, echo, trap)
        seconds = int(time.monotonic() - start)
        log.flush()
        log.seek(0)
        raw = log.read()
        # The log is kept indefinitely; mask token shapes on disk too (fences stay: it is not a HANDOFF).
        masked = mask_secrets_bytes(raw)
        if masked != raw:
            log.seek(0)
            log.truncate()
            log.write(masked)
        log_text = masked.decode("utf-8", errors="replace")
        log.close()
        changed = diff_git_state(before, git_state(target))
        cancelled = trap.received
        if cancelled is not None:
            # A provider's own exit status 130/143 is an ordinary failure; only tazuna's trap
            # makes a run "cancelled", and a cancelled run is never classified or retried.
            exit_code, kind = trap.exit_code, None
            print(f"tazuna: cancelled by {signal.Signals(cancelled).name}; provider stopped", file=sys.stderr)
        else:
            if code == 0 and len(masked) < int(cfg.run.get("min_output_bytes", 200)):
                print(
                    f"tazuna: warning: exit 0 but output is shorter than min_output_bytes: {log_path}",
                    file=sys.stderr,
                )
                code = EXIT_EMPTY
            exit_code = code if code in (0, EXIT_EMPTY, EXIT_TIMEOUT) else 1
            kind = (
                failure_kind(log_text, spec.get("quota_markers", []) or [], spec.get("auth_markers", []) or [])
                if exit_code == 1
                else None
            )
    reported = self_report(log_text)
    status_name, next_note = status_for(exit_code, reported, kind)

    record = RunRecord(
        id=run_id,
        ts=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        provider=route.provider,
        role=route.role,
        model=route.model,
        effort=route.effort,
        dir=str(target),
        out=str(out_path),
        log=str(log_path),
        exit=exit_code,
        status=status_name,
        seconds=seconds,
        self_report=reported,
        changed=changed,
        fallback_from=fallback_from,
        parent=parent,
        task_head=task_head(task),
        # task_via = "arg" puts the whole preamble + task into argv; the ledger keeps a placeholder.
        command=[arg.replace(full_task, "<task>") for arg in argv],
        read_only=route.read_only,
        depth=depth,
    )

    # One automatic retry on a different route when the failure is not about the task itself.
    if kind in ("quota-exhausted", "auth-required") and allow_fallback and route.fallback and fallback_from is None:
        with CancelTrap() as late:
            append_record(record)
            if late.received is not None:
                # No fallback after a signal: this run is the result, so the HANDOFF must describe it.
                _write_result(record, task, log_text, next_note)
        if late.received is not None:
            return RunOutcome(record.exit, record.status, record, stop_signal=late.received)
        print(
            f"tazuna: {route.provider} reported {kind}; retrying once via fallback role {route.fallback}",
            file=sys.stderr,
        )
        # Fallback may only tighten read-only: passing False here would override a fallback
        # role that declares read_only = true and run it writable.
        fb_route = resolve_route(
            cfg,
            role=route.fallback,
            timeout=int(route.explicit["timeout"]) if "timeout" in route.explicit else None,
            read_only=True if route.read_only else None,
        )
        return run_task(
            cfg,
            task,
            route=fb_route,
            target_dir=target,
            out=out_path,
            echo=echo,
            allow_fallback=False,
            fallback_from=f"{route.role or route.provider}/{kind}/{log_path}",
        )

    # The status is decided; a signal now must not kill the process between HANDOFF and ledger.
    with CancelTrap() as late:
        _write_result(record, task, log_text, next_note)
        append_record(record)
    if late.received is not None:
        print(
            f"tazuna: {signal.Signals(late.received).name} arrived after the run finished; the record is complete",
            file=sys.stderr,
        )
    return RunOutcome(record.exit, record.status, record, stop_signal=cancelled or late.received)


def _write_result(record: RunRecord, task: str, log_text: str, next_note: str) -> None:
    """Write the HANDOFF; any failure turns the run into ``handoff-write-failed`` so the ledger row
    that follows still records it."""
    try:
        write_handoff(record, task, log_text, next_note)
    except Exception as exc:  # the row must be written whatever went wrong here
        print(f"tazuna: cannot write HANDOFF: {exc}", file=sys.stderr)
        record.exit = 1
        record.status = "handoff-write-failed"
