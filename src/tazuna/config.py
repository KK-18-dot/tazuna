"""Configuration loading and validation.

Layers, lowest precedence first:

1. built-in defaults shipped with the package (providers, targets)
2. user config       ``$XDG_CONFIG_HOME/tazuna/config.toml`` (personal provider choices)
3. packs             directories listed under ``[packs] paths`` (each has ``pack.toml``)
4. project config    ``tazuna.toml`` in the project root

Later layers override earlier ones table-by-table (``[roles.x]`` replaces ``[roles.x]``,
scalar keys under ``[run]`` merge key-by-key).
"""

from __future__ import annotations

import copy
import os
import re
import tomllib
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

from .errors import ConfigError

PROJECT_CONFIG_NAME = "tazuna.toml"
PACK_CONFIG_NAME = "pack.toml"

# No leading "-": a model id is passed as the value after --model and must not read as a flag.
MODEL_ID_RE = re.compile(r"^[A-Za-z0-9._:/][A-Za-z0-9._:/-]*(\[1m\])?$")
NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SECRET_LIKE_RE = re.compile(r"(KEY|TOKEN|SECRET|PASS|CRED|AUTH|COOKIE|SESSION|PRIVATE|BEARER|_PAT$|ACCESS)")

# Environment variables that are forwarded to provider processes. Everything else is dropped,
# so API keys and tokens in the parent environment structurally cannot reach a provider
# unless the provider definition (or the user, via [run] env_extra) lists them.
DEFAULT_ENV_ALLOWLIST = [
    "HOME",
    "PATH",
    "USER",
    "LOGNAME",
    "SHELL",
    "TERM",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "COLORTERM",
    "NO_COLOR",
    "TMPDIR",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_STATE_HOME",
    "XDG_CACHE_HOME",
    "XDG_RUNTIME_DIR",
]

DEFAULT_RUN = {
    "timeout": 900,
    "min_output_bytes": 40,
    "max_depth": 2,
    "env_extra": [],
}


def user_config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "tazuna"


def state_dir() -> Path:
    base = os.environ.get("TAZUNA_STATE_DIR")
    if base:
        return Path(base)
    xdg = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(xdg) / "tazuna"


def ensure_state_dir(*parts: str) -> Path:
    """Create the state directory (or a subdirectory of it) with mode 0700 and return it.

    Logs keep full provider output and the ledger keeps task heads, so both must be private.
    The mode goes to mkdir rather than through the umask, because provider processes inherit
    the umask. An existing directory is left alone; ``doctor`` reports one that is too open.
    """
    base = state_dir()
    path = base.joinpath(*parts)
    for directory in (base, path):
        if not directory.is_dir():
            os.makedirs(directory, mode=0o700, exist_ok=True)
    return path


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: invalid TOML: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"{path}: cannot read: {exc}") from exc


def _builtin_defaults() -> dict[str, Any]:
    """Providers and targets shipped in the package data directory."""
    data: dict[str, Any] = {"providers": {}, "targets": {}, "run": dict(DEFAULT_RUN)}
    pkg = resources.files("tazuna")
    for entry in (pkg / "providers").iterdir():
        if entry.name.endswith(".toml"):
            with entry.open("rb") as fh:
                spec = tomllib.load(fh)
            name = spec.get("name") or entry.name[:-5]
            data["providers"][name] = spec
    with (pkg / "targets.toml").open("rb") as fh:
        data["targets"] = tomllib.load(fh).get("targets", {})
    return data


def _merge(base: dict[str, Any], overlay: dict[str, Any], *, source: str) -> None:
    for key, value in overlay.items():
        if key in ("providers", "targets", "roles", "models", "packs_meta"):
            base.setdefault(key, {})
            if not isinstance(value, dict):
                raise ConfigError(f"{source}: [{key}] must be a table")
            for sub, subval in value.items():
                if key == "models" and not isinstance(subval, dict):
                    base[key][sub] = subval
                elif isinstance(subval, dict) and isinstance(base[key].get(sub), dict):
                    merged = dict(base[key][sub])
                    merged.update(subval)
                    base[key][sub] = merged
                else:
                    base[key][sub] = subval
        elif isinstance(value, dict) and isinstance(base.get(key), dict):
            base[key] = {**base[key], **value}
        else:
            base[key] = value


@dataclass
class Config:
    project_dir: Path
    data: dict[str, Any]
    sources: list[str] = field(default_factory=list)
    packs: list[dict[str, Any]] = field(default_factory=list)

    # ---- accessors -------------------------------------------------------------------------
    @property
    def project_file(self) -> Path:
        return self.project_dir / PROJECT_CONFIG_NAME

    @property
    def policy_file(self) -> Path:
        name = self.data.get("project", {}).get("policy", "PROJECT.md")
        return self.project_dir / name

    @property
    def providers(self) -> dict[str, dict[str, Any]]:
        return self.data.get("providers", {})

    @property
    def targets(self) -> dict[str, dict[str, Any]]:
        return self.data.get("targets", {})

    @property
    def roles(self) -> dict[str, dict[str, Any]]:
        return self.data.get("roles", {})

    @property
    def models(self) -> dict[str, str]:
        return {k: v for k, v in self.data.get("models", {}).items() if isinstance(v, str)}

    @property
    def retired_models(self) -> list[str]:
        retired = self.data.get("models", {}).get("retired", [])
        return list(retired) if isinstance(retired, list) else []

    @property
    def render_targets(self) -> list[str]:
        return list(self.data.get("render", {}).get("targets", ["claude", "codex", "cursor"]))

    @property
    def run(self) -> dict[str, Any]:
        return self.data.get("run", dict(DEFAULT_RUN))

    def resolve_model(self, value: str) -> str:
        """A model may be written as a ``[models]`` key or as a literal model id."""
        resolved = self.models.get(value, value)
        if not MODEL_ID_RE.match(resolved):
            raise ConfigError(
                f"invalid model id: {resolved!r} (letters, digits, . _ : / and -, not starting with -, "
                "and an optional [1m])"
            )
        return resolved

    def env_allowlist(self) -> list[str]:
        names = list(DEFAULT_ENV_ALLOWLIST)
        for name in self.run.get("env_extra", []) or []:
            if not ENV_NAME_RE.match(name):
                raise ConfigError(f"[run] env_extra: invalid variable name {name!r}")
            if SECRET_LIKE_RE.search(name.upper()):
                raise ConfigError(f"[run] env_extra: {name!r} looks like a secret and is refused")
            names.append(name)
        return names


def load_config(
    project_dir: Path | str | None = None, *, require_project: bool = False, ignore_project: bool = False
) -> Config:
    """Load the layered configuration for ``project_dir`` (default: cwd)."""
    project_dir = Path(project_dir or os.getcwd()).resolve()
    data = _builtin_defaults()
    sources = ["<built-in>"]
    packs: list[dict[str, Any]] = []

    user_file = user_config_dir() / "config.toml"
    if user_file.is_file():
        _merge(data, _read_toml(user_file), source=str(user_file))
        sources.append(str(user_file))

    project_file = project_dir / PROJECT_CONFIG_NAME
    project_data: dict[str, Any] = {}
    if project_file.is_file() and not ignore_project:
        project_data = _read_toml(project_file)
    elif require_project:
        raise ConfigError(f"no {PROJECT_CONFIG_NAME} in {project_dir} (run `tazuna init` first)")

    # Packs listed by the user layer and the project layer are both honoured; project last.
    pack_paths: list[str] = []
    for layer in (data.get("packs", {}), project_data.get("packs", {})):
        if isinstance(layer, dict):
            pack_paths.extend(layer.get("paths", []) or [])
    for raw in pack_paths:
        pack_dir = Path(os.path.expanduser(raw))
        if not pack_dir.is_absolute():
            pack_dir = project_dir / pack_dir
        pack_file = pack_dir / PACK_CONFIG_NAME
        if not pack_file.is_file():
            raise ConfigError(f"pack not found: {pack_file}")
        pack_data = _read_toml(pack_file)
        meta = dict(pack_data.pop("pack", {}))
        meta.setdefault("name", pack_dir.name)
        meta["path"] = str(pack_dir)
        # Appendix files inside a pack are relative to the pack directory, and provider commands
        # may reference scripts shipped with the pack through {pack_dir}.
        for target in pack_data.get("targets", {}).values():
            if isinstance(target, dict) and "appendix" in target and not Path(target["appendix"]).is_absolute():
                target["appendix"] = str(pack_dir / target["appendix"])
        for prov in pack_data.get("providers", {}).values():
            if isinstance(prov, dict):
                for key in ("command", "binary"):
                    val = prov.get(key)
                    if isinstance(val, list):
                        prov[key] = [part.replace("{pack_dir}", str(pack_dir)) for part in val]
                    elif isinstance(val, str):
                        prov[key] = val.replace("{pack_dir}", str(pack_dir))
        _merge(data, pack_data, source=str(pack_file))
        packs.append(meta)
        sources.append(str(pack_file))

    if project_data:
        _merge(data, project_data, source=str(project_file))
        sources.append(str(project_file))

    cfg = Config(project_dir=project_dir, data=data, sources=sources, packs=packs)
    validate(cfg)
    return cfg


def validate(cfg: Config) -> None:
    """Fail closed on anything a later command would otherwise have to guess about."""
    for name, spec in cfg.providers.items():
        if not NAME_RE.match(name):
            raise ConfigError(f"provider name {name!r} must match {NAME_RE.pattern}")
        kind = spec.get("kind", "cli")
        if kind not in ("cli", "mock"):
            raise ConfigError(f"provider {name}: kind must be 'cli' or 'mock'")
        if kind == "cli" and not spec.get("command"):
            raise ConfigError(f"provider {name}: 'command' (list of argv strings) is required")
        for key in ("command", "model_args", "effort_args", "read_only_args", "write_args", "env_extra", "efforts"):
            val = spec.get(key)
            if val is not None and (not isinstance(val, list) or not all(isinstance(x, str) for x in val)):
                raise ConfigError(f"provider {name}: '{key}' must be a list of strings")
        if spec.get("task_via", "stdin") not in ("stdin", "arg"):
            raise ConfigError(f"provider {name}: task_via must be 'stdin' or 'arg'")
        for var in spec.get("env_extra", []) or []:
            if not ENV_NAME_RE.match(var) or SECRET_LIKE_RE.search(var.upper()):
                raise ConfigError(f"provider {name}: env_extra {var!r} is not allowed")
    for key, value in cfg.data.get("models", {}).items():
        if key == "retired":
            continue
        if not isinstance(value, str) or not MODEL_ID_RE.match(value):
            raise ConfigError(f"[models] {key}: invalid model id {value!r}")
    for role, spec in cfg.roles.items():
        if not NAME_RE.match(role):
            raise ConfigError(f"role name {role!r} must match {NAME_RE.pattern}")
        if not isinstance(spec, dict):
            raise ConfigError(f"[roles.{role}] must be a table")
        provider = spec.get("provider")
        if provider not in cfg.providers:
            raise ConfigError(f"[roles.{role}] provider {provider!r} is not defined")
        if "model" in spec:
            cfg.resolve_model(str(spec["model"]))
        timeout = spec.get("timeout")
        if timeout is not None and (not isinstance(timeout, int) or not 1 <= timeout <= 7200):
            raise ConfigError(f"[roles.{role}] timeout must be an integer between 1 and 7200")
        fallback = spec.get("fallback")
        if fallback is not None and fallback not in cfg.roles:
            raise ConfigError(f"[roles.{role}] fallback {fallback!r} is not a defined role")
        effort = spec.get("effort")
        if effort is not None:
            allowed = cfg.providers[provider].get("efforts", []) or []
            if effort not in allowed:
                raise ConfigError(
                    f"[roles.{role}] effort {effort!r} is not supported by provider {provider} "
                    f"(allowed: {', '.join(allowed) or 'none'})"
                )
    for name, target in cfg.targets.items():
        limit = target.get("max_bytes") if isinstance(target, dict) else None
        if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 1):
            raise ConfigError(f"[targets.{name}] max_bytes must be a positive integer")
    for name in cfg.render_targets:
        if name not in cfg.targets:
            raise ConfigError(f"[render] targets: unknown target {name!r} (known: {', '.join(sorted(cfg.targets))})")
    run = cfg.run
    for key in ("timeout", "min_output_bytes", "max_depth"):
        val = run.get(key, DEFAULT_RUN[key])
        if not isinstance(val, int) or val < 0:
            raise ConfigError(f"[run] {key} must be a non-negative integer")
    cfg.env_allowlist()


def default_project_config(name: str) -> str:
    """The tazuna.toml written by ``tazuna init``."""
    template = resources.files("tazuna").joinpath("templates/tazuna.toml").read_text(encoding="utf-8")
    return template.replace("{{name}}", name)


def deep_copy(cfg: Config) -> Config:
    return Config(cfg.project_dir, copy.deepcopy(cfg.data), list(cfg.sources), list(cfg.packs))
