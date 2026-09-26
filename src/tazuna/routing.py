"""Roles → (provider, model, effort, timeout, fallback), and provider availability.

Routing is deliberately static and declarative: a role is a named row in ``tazuna.toml``,
and ``tazuna routes`` prints exactly what ``tazuna run --role`` will do. Nothing is
chosen at runtime from heuristics, so a user can always predict which provider bills a task.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from typing import Any

from .config import Config
from .errors import ConfigError, UsageError


@dataclass
class Route:
    role: str | None
    provider: str
    model: str | None
    effort: str | None
    timeout: int
    read_only: bool = False
    fallback: str | None = None
    explicit: dict[str, str] = field(default_factory=dict)  # which fields came from CLI flags

    def describe(self) -> str:
        parts = [f"provider={self.provider}"]
        parts.append(f"model={self.model or '(provider default)'}")
        parts.append(f"effort={self.effort or '-'}")
        parts.append(f"timeout={self.timeout}s")
        if self.read_only:
            parts.append("read-only")
        if self.fallback:
            parts.append(f"fallback={self.fallback}")
        return " ".join(parts)


@dataclass
class ProviderStatus:
    name: str
    kind: str
    binary: str | None
    available: bool
    path: str | None
    experimental: bool = False
    note: str = ""


def provider_status(cfg: Config, name: str) -> ProviderStatus:
    spec = cfg.providers[name]
    kind = spec.get("kind", "cli")
    if kind == "mock":
        return ProviderStatus(name, kind, None, True, None, note="built-in, offline")
    binary = spec.get("binary") or (spec.get("command") or [None])[0]
    path = shutil.which(binary) if binary else None
    note = "" if path else f"'{binary}' not found in PATH"
    return ProviderStatus(name, kind, binary, path is not None, path, bool(spec.get("experimental")), note)


def all_provider_status(cfg: Config) -> list[ProviderStatus]:
    return [provider_status(cfg, name) for name in sorted(cfg.providers)]


def resolve_route(
    cfg: Config,
    *,
    role: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    timeout: int | None = None,
    read_only: bool | None = None,
) -> Route:
    """Combine a role (optional) with explicit overrides. Explicit flags always win."""
    explicit: dict[str, str] = {}
    spec: dict[str, Any] = {}
    if role is not None:
        if role not in cfg.roles:
            known = ", ".join(sorted(cfg.roles)) or "(none defined)"
            raise UsageError(f"unknown role {role!r}; defined roles: {known}")
        spec = cfg.roles[role]
        if provider and provider != spec.get("provider"):
            raise UsageError(f"--provider {provider} conflicts with role {role} (provider {spec.get('provider')})")
    provider_name = provider or spec.get("provider")
    if not provider_name:
        raise UsageError("a provider is required: pass --role ROLE or --provider NAME")
    if provider_name not in cfg.providers:
        raise UsageError(f"unknown provider {provider_name!r}; defined: {', '.join(sorted(cfg.providers))}")
    pspec = cfg.providers[provider_name]

    if model is not None:
        explicit["model"] = model
        resolved_model: str | None = cfg.resolve_model(model)
    elif spec.get("model") is not None:
        resolved_model = cfg.resolve_model(str(spec["model"]))
    elif pspec.get("default_model"):
        resolved_model = cfg.resolve_model(str(pspec["default_model"]))
    else:
        resolved_model = None
    if resolved_model and resolved_model in cfg.retired_models:
        raise ConfigError(f"model {resolved_model} is listed under [models] retired")

    allowed_efforts = pspec.get("efforts", []) or []
    if effort is not None:
        explicit["effort"] = effort
        if effort not in allowed_efforts:
            raise UsageError(
                f"--effort {effort} is not supported by provider {provider_name} "
                f"(allowed: {', '.join(allowed_efforts) or 'none; encode effort in the model id'})"
            )
        resolved_effort: str | None = effort
    elif spec.get("effort") is not None:
        resolved_effort = str(spec["effort"])
    elif pspec.get("default_effort") and pspec["default_effort"] in allowed_efforts:
        resolved_effort = str(pspec["default_effort"])
    else:
        resolved_effort = None

    if timeout is not None:
        explicit["timeout"] = str(timeout)
        resolved_timeout = timeout
    else:
        resolved_timeout = int(spec.get("timeout") or cfg.run.get("timeout") or 900)
    if not 1 <= resolved_timeout <= 7200:
        raise UsageError(f"timeout must be between 1 and 7200 seconds: {resolved_timeout}")

    if read_only is None:
        resolved_read_only = bool(spec.get("read_only", False))
    else:
        resolved_read_only = read_only
    if pspec.get("read_only_only"):
        resolved_read_only = True

    return Route(
        role=role,
        provider=provider_name,
        model=resolved_model,
        effort=resolved_effort,
        timeout=resolved_timeout,
        read_only=resolved_read_only,
        fallback=spec.get("fallback"),
        explicit=explicit,
    )


def explain_routes(cfg: Config) -> list[dict[str, Any]]:
    """Rows for ``tazuna routes``: one per role, with provider availability."""
    statuses = {s.name: s for s in all_provider_status(cfg)}
    rows = []
    for role in sorted(cfg.roles):
        try:
            route = resolve_route(cfg, role=role)
            problem = ""
        except (UsageError, ConfigError) as exc:
            rows.append({"role": role, "problem": str(exc)})
            continue
        status = statuses[route.provider]
        rows.append(
            {
                "role": role,
                "provider": route.provider,
                "model": route.model or "-",
                "effort": route.effort or "-",
                "timeout": route.timeout,
                "read_only": route.read_only,
                "fallback": route.fallback or "-",
                "available": status.available,
                "note": status.note,
                "problem": problem,
            }
        )
    return rows
