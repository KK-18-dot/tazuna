"""Measure what a run changed in a git work tree, without letting the provider run code here.

The provider may have written anything below --dir, including the repository's own config, so
every git call goes through ``safe_git`` and the snapshot never trusts ``git status`` alone
(see ``GitSnapshot``). ``run`` takes one snapshot before and one after a task; ``doctor`` and
``status`` reuse ``safe_git`` for the git calls they make in the same repository.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import subprocess
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from .config import DEFAULT_ENV_ALLOWLIST

NOT_GIT_NOTE = "(not a git repository: changes could not be detected)"
AGAIN_SUFFIX = " (modified before the run and again during it)"
CLEAN_NOW_PREFIX = "(clean now; was modified before the run) "
METADATA_PREFIX = "(git metadata changed) "
HASH_LIMIT = 5000
HASH_MAX_BYTES = 8 * 1024 * 1024  # larger files are fingerprinted by size and mtime, not read
# The provider can write the repository's own config (.git is inside or above --dir). Nothing
# git runs for the snapshot may execute a command planted there: no fsmonitor, no hooks (an
# index refresh fires post-index-change), no pager, no index writes, and every filter driver is
# switched off per call. git also gets the provider's allowlisted environment, not ours.
SAFE_GIT_ARGS = ("--no-pager", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null")
FILTER_KEY_RE = re.compile(r"filter\.(.+)\.(?:clean|process)", re.S)
# Git files a provider could change to hide work from `git status` or to plant commands.
GIT_META_COMMON = ("config", "info/exclude", "info/attributes")
GIT_META_WORKTREE = ("config.worktree",)
_C_ESCAPES = {"\a": "\\a", "\b": "\\b", "\t": "\\t", "\n": "\\n", "\v": "\\v", "\f": "\\f", "\r": "\\r"}
_C_ESCAPES.update({'"': '\\"', "\\": "\\\\"})


class UnsafeRepository(Exception):
    """The repository's configuration cannot be neutralised for a snapshot."""


@dataclass
class GitSnapshot:
    """HEAD plus every dirty path with a fingerprint of its content.

    Comparing ``git status`` lines alone misses a second edit to a file that was already dirty,
    new files inside a directory that was already untracked, and commits made by the provider,
    so the snapshot keeps the commit and a per-path content fingerprint instead. It also keeps
    what ``git status`` cannot show: index flags that hide tracked paths, and git's own config,
    exclude, attributes and hook files.
    """

    root: Path
    head: str | None  # None while HEAD is unborn
    entries: dict[str, tuple[str, str]] = field(default_factory=dict)  # path -> (XY, fingerprint)
    hashed: bool = True
    error: str | None = None
    hidden: dict[str, tuple[str, ...]] = field(default_factory=dict)  # path -> index flags
    meta: dict[str, str] = field(default_factory=dict)  # displayed git file -> fingerprint


def _git_env() -> dict[str, str]:
    env = {name: os.environ[name] for name in DEFAULT_ENV_ALLOWLIST if name in os.environ}
    env["GIT_OPTIONAL_LOCKS"] = "0"  # status must not rewrite the index
    return env


def safe_git(
    root: Path, *args: str, stdin: bytes | None = None, timeout: int = 60, config: tuple[str, ...] = ()
) -> subprocess.CompletedProcess[bytes]:
    """Run git in a repository a provider may have written to (see SAFE_GIT_ARGS).

    Every git call tazuna makes in a project goes through here: ``run``'s snapshot, and the
    ``doctor`` and ``status`` commands a person runs later in the same repository. Commands that
    can run filter drivers (``status``) also need ``config=filter_overrides(root)``.
    """
    return subprocess.run(
        ["git", *SAFE_GIT_ARGS, *config, "-C", str(root), *args],
        input=stdin,
        stdin=None if stdin is not None else subprocess.DEVNULL,
        capture_output=True,
        timeout=timeout,
        env=_git_env(),
    )


def _needs_quote(ch: str) -> bool:
    category = unicodedata.category(ch)
    return ch in _C_ESCAPES or category[0] == "C" or category in ("Zl", "Zp")


def _display(path: str) -> str:
    """Quote a path the way git does when it could break a line-oriented reader.

    ``changed`` lands in HANDOFF.md, so a provider-chosen file name must not be able to start
    a new line or reorder text. Control and format characters, quotes, backslashes and bytes
    that are not UTF-8 are escaped (octal, as git does) and the path is put in double quotes;
    other non-ASCII text stays readable.
    """
    if not any(_needs_quote(ch) for ch in path):
        return path
    out = []
    for ch in path:
        if ch in _C_ESCAPES:
            out.append(_C_ESCAPES[ch])
        elif _needs_quote(ch):
            out.append("".join(f"\\{byte:03o}" for byte in ch.encode("utf-8", "surrogateescape")))
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def _blob_hash(path: Path) -> str:
    """Same value as ``git hash-object --no-filters`` in a SHA-1 repository.

    Opened without following a final symlink and without blocking (a FIFO must not hang the
    snapshot), and read only when it is a regular file.
    """
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except OSError:
        return "missing"
    with os.fdopen(fd, "rb") as fh:
        info = os.fstat(fh.fileno())
        if not stat.S_ISREG(info.st_mode):
            return "special"
        digest = hashlib.sha1(f"blob {info.st_size}\0".encode(), usedforsecurity=False)
        try:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                digest.update(chunk)
        except OSError:
            return "missing"
    return digest.hexdigest()


def _path_print(path: Path) -> tuple[str, bool]:
    """(fingerprint, worth hashing) from lstat alone. Only regular files up to HASH_MAX_BYTES
    are read; the decision depends on the file itself, so both snapshots decide the same way."""
    try:
        info = os.lstat(path)
    except OSError:
        return "missing", False
    if stat.S_ISLNK(info.st_mode):
        return "link:" + os.readlink(path), False
    if stat.S_ISDIR(info.st_mode):
        return "dir", False  # an untracked nested repository or a submodule
    if not stat.S_ISREG(info.st_mode):
        return "special", False
    if info.st_size > HASH_MAX_BYTES:
        return f"stat:{info.st_size}:{info.st_mtime_ns}", False
    return "", True


def _fingerprints(root: Path, paths: list[str]) -> dict[str, str]:
    prints: dict[str, str] = {}
    batch: list[str] = []
    real_root = os.path.realpath(root)
    inside: dict[Path, bool] = {}
    for rel in paths:
        full = root / rel
        if full.parent not in inside:
            real_parent = os.path.realpath(full.parent)
            inside[full.parent] = real_parent == real_root or real_parent.startswith(real_root + os.sep)
        if not inside[full.parent]:  # a parent directory was swapped for a symlink
            prints[rel] = "outside"
            continue
        prints[rel], wanted = _path_print(full)
        if not wanted:
            continue
        # --stdin-paths reads one path per line, drops a trailing CR and C-unquotes a leading '"'.
        if "\n" in rel or "\r" in rel or rel.startswith('"'):
            prints[rel] = _blob_hash(full)
        else:
            batch.append(rel)
    if batch:
        stdin = b"".join(os.fsencode(rel) + b"\n" for rel in batch)
        res = safe_git(root, "hash-object", "--no-filters", "--stdin-paths", stdin=stdin, timeout=300)
        hashes = res.stdout.decode("ascii", "replace").split()
        if res.returncode == 0 and len(hashes) == len(batch):
            prints.update(zip(batch, hashes, strict=True))
        else:  # a file vanished between status and hashing: hash one by one
            prints.update((rel, _blob_hash(root / rel)) for rel in batch)
    return prints


def filter_overrides(root: Path) -> tuple[str, ...]:
    """``-c filter.<name>.clean=`` pairs that switch off every configured filter driver.

    ``git status`` runs a driver's clean command to compare file content, so a provider that can
    write .git/config could otherwise make tazuna run any command outside the provider's
    sandbox. User-level drivers (git-lfs) are switched off too; that can only add false
    positives to ``changed``, never run anything.
    """
    res = safe_git(root, "config", "-z", "--get-regexp", r"^filter\..*\.(clean|process)$")
    overrides: list[str] = []
    for item in res.stdout.split(b"\0"):
        key = os.fsdecode(item.split(b"\n", 1)[0])
        match = FILTER_KEY_RE.fullmatch(key)
        if not match:
            continue
        if "=" in key:  # `-c key=value` splits at the first "=", so this driver cannot be overridden
            raise UnsafeRepository(
                f"git config defines a filter driver whose name contains '=' ({_display(match.group(1))}); "
                "tazuna will not run git status there"
            )
        overrides += ["-c", f"{key}="]
    return tuple(overrides)


def _hidden_paths(root: Path) -> dict[str, tuple[str, ...]]:
    """Tracked paths that ``git status`` will not report because of an index flag."""
    res = safe_git(root, "ls-files", "-v", "-z")
    hidden: dict[str, tuple[str, ...]] = {}
    for token in res.stdout.split(b"\0"):
        if len(token) < 3:
            continue
        tag = chr(token[0])
        flags = (("assume-unchanged",) if tag.islower() else ()) + (("skip-worktree",) if tag in "Ss" else ())
        if flags:
            hidden[os.fsdecode(token[2:])] = flags
    return hidden


def _git_metadata(root: Path, git_dir: Path, common_dir: Path) -> dict[str, str]:
    paths = [common_dir / name for name in GIT_META_COMMON] + [git_dir / name for name in GIT_META_WORKTREE]
    hooks = common_dir / "hooks"
    if hooks.is_dir() and not hooks.is_symlink():
        paths += sorted(hooks.iterdir())
    prints: dict[str, str] = {}
    real_root = os.path.realpath(root)
    for path in paths:
        real = os.path.realpath(path.parent) + os.sep + path.name
        label = os.path.relpath(real, real_root) if real.startswith(real_root + os.sep) else real
        fingerprint, wanted = _path_print(path)
        prints[_display(label)] = _blob_hash(path) if wanted else fingerprint
    return prints


def git_state(target: Path) -> GitSnapshot | None:
    """Snapshot the repository around ``target``; None when it is not inside a work tree.

    Any failure after that point becomes a snapshot with ``error`` set, so the run is still
    recorded (with a note in ``changed``) instead of being lost.
    """
    try:
        probe = safe_git(
            target, "rev-parse", "--is-inside-work-tree", "--show-toplevel", "--absolute-git-dir",
            "--git-common-dir", timeout=30,
        )  # fmt: skip
    except (OSError, subprocess.TimeoutExpired):
        return None
    lines = probe.stdout.decode("utf-8", "surrogateescape").splitlines()
    if probe.returncode != 0 or len(lines) < 4 or lines[0] != "true":
        return None
    root = Path(lines[1])
    try:
        return _snapshot(root, Path(lines[2]), target / lines[3])  # common dir is relative to target
    except UnsafeRepository as exc:
        return GitSnapshot(root, None, error=str(exc))
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return GitSnapshot(root, None, error=f"git snapshot failed: {exc}")


def _snapshot(root: Path, git_dir: Path, common_dir: Path) -> GitSnapshot:
    config = filter_overrides(root)
    head_res = safe_git(root, "rev-parse", "--verify", "-q", "HEAD")
    # --ignore-submodules=dirty: never run git inside a submodule, whose config is not covered above.
    status = safe_git(
        root, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--no-renames",
        "--ignore-submodules=dirty", config=config,
    )  # fmt: skip
    if status.returncode != 0:
        detail = status.stderr.decode("utf-8", "replace").strip().splitlines()
        return GitSnapshot(root, None, error="git status failed" + (f": {detail[-1]}" if detail else ""))
    head = head_res.stdout.decode("ascii", "replace").strip() if head_res.returncode == 0 else None

    states: dict[str, str] = {}
    tokens = iter(status.stdout.split(b"\0"))
    for token in tokens:
        if len(token) < 4:
            continue
        xy = token[:2].decode("ascii", "replace")
        states[os.fsdecode(token[3:])] = xy
        if "R" in xy or "C" in xy:  # not produced with --no-renames; skip the source path anyway
            next(tokens, None)
    extra = {"hidden": _hidden_paths(root), "meta": _git_metadata(root, git_dir, common_dir)}
    if len(states) > HASH_LIMIT:
        return GitSnapshot(root, head, {p: (xy, "") for p, xy in states.items()}, hashed=False, **extra)
    prints = _fingerprints(root, list(states))
    return GitSnapshot(root, head, {p: (xy, prints[p]) for p, xy in states.items()}, **extra)


def _commit_lines(root: Path, old: str | None, new: str | None) -> list[str]:
    old_label = old[:7] if old else "(none)"
    if new is None:
        return [f"commits: {old_label}..(none) (HEAD is unborn now)"]
    try:
        count_res = safe_git(root, "rev-list", "--count", f"{old}..{new}" if old else new)
        # From an unborn HEAD every commit is new: diff against the empty tree, not just the last commit.
        base = old or safe_git(root, "hash-object", "-t", "tree", "--stdin", stdin=b"").stdout.decode().strip()
        diff = safe_git(root, "diff", "--name-status", "--no-renames", "--no-ext-diff", "-z", base, new)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [f"commits: {old_label}..{new[:7]} (could not be listed: {exc})"]
    count = count_res.stdout.decode().strip() if count_res.returncode == 0 else "?"
    moved = " (HEAD moved without adding commits)" if count == "0" else ""
    lines = [f"commits: {old_label}..{new[:7]} ({count}){moved}"]
    fields = diff.stdout.split(b"\0") if diff.returncode == 0 else []
    for status, path in zip(fields[0::2], fields[1::2], strict=False):
        if status:
            lines.append(f"committed {status.decode('ascii', 'replace')} {_display(os.fsdecode(path))}")
    return lines


def diff_git_state(before: GitSnapshot | None, after: GitSnapshot | None) -> list[str]:
    """What the run changed: new commits, then paths whose status or content moved."""
    if before is None or after is None:
        return [NOT_GIT_NOTE]
    if before.error or after.error:
        return [f"(changes could not be detected: {before.error or after.error})"]
    lines: list[str] = []
    if before.head != after.head:
        lines.extend(_commit_lines(after.root, before.head, after.head))
    compare_content = before.hashed and after.hashed
    if not compare_content:
        lines.append(
            f"(more than {HASH_LIMIT} dirty paths: content was not compared, so further edits to paths "
            "that were already dirty are not listed)"
        )
    for path in sorted(after.entries):
        xy, digest = after.entries[path]
        old = before.entries.get(path)
        if old is None:
            lines.append(f"{xy} {_display(path)}")
        elif old[0] != xy or (compare_content and old[1] != digest):
            lines.append(f"{xy} {_display(path)}{AGAIN_SUFFIX}")
    for path in sorted(set(before.entries) - set(after.entries)):
        lines.append(f"{CLEAN_NOW_PREFIX}{_display(path)}")
    for path in sorted(after.hidden):
        for flag in after.hidden[path]:
            if flag not in before.hidden.get(path, ()):
                lines.append(f"(hidden from git status: {flag} set) {_display(path)}")
    for label in sorted(set(before.meta) | set(after.meta)):
        if before.meta.get(label, "missing") != after.meta.get(label, "missing"):
            lines.append(f"{METADATA_PREFIX}{label}")
    return lines
