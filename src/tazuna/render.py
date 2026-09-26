"""Render the project policy (PROJECT.md) into every enabled target.

Generated files start with a marker line. Files without the marker are treated as hand-written
and are never overwritten unless ``--force`` (or ``--adopt``, which first promotes the
hand-written CLAUDE.md to PROJECT.md). ``--check`` reports drift and exits 1 without writing.
"""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .errors import ConfigError, UsageError

MARKER = "<!-- GENERATED-FROM: PROJECT.md"
HEADER = "<!-- GENERATED-FROM: PROJECT.md by tazuna — do not edit. Edit PROJECT.md and run `tazuna render`. -->"
MODEL_TOKEN_RE = re.compile(r"\{\{model:([A-Za-z0-9_-]+)\}\}")


@dataclass
class RenderResult:
    target: str
    path: Path
    action: str  # written | unchanged | drift | refused | missing | too-large (check mode only)
    message: str = ""
    size: int = 0  # bytes of the rendered text
    max_bytes: int | None = None  # the target's declared read limit, if any

    @property
    def too_large(self) -> bool:
        return self.max_bytes is not None and self.size > self.max_bytes

    @property
    def size_note(self) -> str:
        return (
            f"{self.size} bytes, over max_bytes {self.max_bytes}: the harness may ignore everything past "
            "that point (shorten PROJECT.md or the appendix, or raise max_bytes if you raised the harness limit)"
        )


def is_generated(path: Path) -> bool:
    """True when the file carries the generated marker in its first eight lines
    (frontmatter, as in Cursor rules, may precede the marker)."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            head = [fh.readline() for _ in range(8)]
    except OSError:
        return False
    return any(MARKER in line for line in head)


def expand_models(text: str, cfg: Config) -> str:
    models = cfg.models

    def sub(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in models:
            raise ConfigError(f"PROJECT.md uses {{{{model:{key}}}}} but [models] has no key {key!r}")
        return models[key]

    return MODEL_TOKEN_RE.sub(sub, text)


def render_text(cfg: Config, target: str, policy_text: str) -> str:
    spec = cfg.targets[target]
    parts = []
    if spec.get("frontmatter"):
        parts.append(spec["frontmatter"])
    parts.append(HEADER + "\n\n")
    parts.append(policy_text)
    if not policy_text.endswith("\n"):
        parts.append("\n")
    appendix = spec.get("appendix")
    if appendix:
        appendix_path = Path(appendix)
        if not appendix_path.is_absolute():
            appendix_path = cfg.project_dir / appendix_path
        if appendix_path.is_file():
            parts.append(f"\n<!-- appendix: {appendix_path.name} -->\n\n")
            body = appendix_path.read_text(encoding="utf-8")
            parts.append(body if body.endswith("\n") else body + "\n")
    return expand_models("".join(parts), cfg)


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tazuna-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def adopt(cfg: Config) -> Path:
    """Promote a hand-written CLAUDE.md into PROJECT.md (keeping a backup)."""
    policy = cfg.policy_file
    if policy.exists():
        raise UsageError(f"{policy.name} already exists; --adopt is not needed")
    claude = cfg.project_dir / "CLAUDE.md"
    if not claude.is_file():
        raise UsageError("--adopt needs an existing hand-written CLAUDE.md")
    text = claude.read_text(encoding="utf-8")
    policy.write_text(text, encoding="utf-8")
    backup = cfg.project_dir / "CLAUDE.md.pre-tazuna.bak"
    backup.write_text(text, encoding="utf-8")
    return backup


def render(
    cfg: Config, *, check: bool = False, force: bool = False, targets: list[str] | None = None
) -> list[RenderResult]:
    policy = cfg.policy_file
    if not policy.is_file():
        raise UsageError(f"{policy} not found (run `tazuna init`, or `tazuna render --adopt` to promote CLAUDE.md)")
    policy_text = policy.read_text(encoding="utf-8")
    results: list[RenderResult] = []
    for name in targets or cfg.render_targets:
        if name not in cfg.targets:
            raise UsageError(f"unknown target {name!r}")
        out = cfg.project_dir / cfg.targets[name]["path"]
        text = render_text(cfg, name, policy_text)
        # Some harnesses stop reading an instruction file at a fixed size (Codex: 32 KiB), so an
        # oversized target silently loses its tail. It is still written; check mode fails on it.
        size = {"size": len(text.encode("utf-8")), "max_bytes": cfg.targets[name].get("max_bytes")}
        current = out.read_text(encoding="utf-8") if out.is_file() else None
        if check:
            if current is None:
                res = RenderResult(name, out, "missing", "not rendered yet", **size)
            elif current != text:
                res = RenderResult(name, out, "drift", "differs from PROJECT.md", **size)
            else:
                res = RenderResult(name, out, "unchanged", **size)
                if res.too_large:
                    res.action, res.message = "too-large", res.size_note
            results.append(res)
            continue
        if current is not None and not force and not is_generated(out):
            results.append(
                RenderResult(
                    name, out, "refused", "hand-written file (no generated marker); use --adopt or --force", **size
                )
            )
            continue
        if current != text:
            _atomic_write(out, text)
        res = RenderResult(name, out, "unchanged" if current == text else "written", **size)
        if res.too_large:
            res.message = res.size_note
        results.append(res)
    return results
