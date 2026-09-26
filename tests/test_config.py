from pathlib import Path

import pytest

from tazuna.config import load_config
from tazuna.errors import ConfigError


def test_builtin_providers_and_targets_load(sandbox: Path) -> None:
    cfg = load_config(sandbox)
    assert {"claude", "codex", "cursor", "mock", "ollama"} <= set(cfg.providers)
    assert {"claude", "codex", "cursor", "gemini", "copilot"} <= set(cfg.targets)
    assert cfg.sources == ["<built-in>"]


def test_project_layer_overrides_and_validates(project: Path) -> None:
    cfg = load_config(project)
    assert cfg.roles["shim"]["provider"] == "fakecli"
    assert cfg.resolve_model("fast") == "fake-fast-1"
    assert cfg.resolve_model("literal-id:tag") == "literal-id:tag"
    assert cfg.retired_models == ["old-model-9"]


def test_user_config_layer_is_below_project(project: Path, sandbox: Path) -> None:
    user = sandbox / "home" / ".config" / "tazuna"
    user.mkdir(parents=True)
    (user / "config.toml").write_text('[models]\nfast = "user-fast"\nextra = "user-extra"\n', encoding="utf-8")
    cfg = load_config(project)
    assert cfg.models["fast"] == "fake-fast-1"  # project wins
    assert cfg.models["extra"] == "user-extra"  # user-only keys survive
    assert str(user / "config.toml") in cfg.sources


@pytest.mark.parametrize(
    "snippet, message",
    [
        ('[roles.bad]\nprovider = "nope"\n', "provider 'nope' is not defined"),
        ('[roles.bad]\nprovider = "mock"\neffort = "max"\n', "effort 'max' is not supported"),
        ('[roles.bad]\nprovider = "mock"\ntimeout = 0\n', "timeout must be an integer"),
        ('[roles.bad]\nprovider = "mock"\nfallback = "ghost"\n', "fallback 'ghost' is not a defined role"),
        ('[models]\nx = "bad model id!"\n', "invalid model id"),
        ('[models]\nx = "--yolo"\n', "invalid model id"),
        ('[models]\nx = "-c"\n', "invalid model id"),
        ('[render]\ntargets = ["nowhere"]\n', "unknown target"),
        ('[run]\nenv_extra = ["MY_API_KEY"]\n', "looks like a secret"),
        ('[providers.p]\ncommand = "not-a-list"\n', "must be a list of strings"),
        ('[roles.Bad-Name]\nprovider = "mock"\n', "must match"),
    ],
)
def test_validation_fails_closed(sandbox: Path, snippet: str, message: str) -> None:
    proj = sandbox / "work" / "v"
    proj.mkdir(parents=True)
    (proj / "tazuna.toml").write_text(snippet, encoding="utf-8")
    with pytest.raises(ConfigError, match=message):
        load_config(proj)


def test_invalid_toml_is_a_config_error(sandbox: Path) -> None:
    proj = sandbox / "work" / "t"
    proj.mkdir(parents=True)
    (proj / "tazuna.toml").write_text("[roles\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid TOML"):
        load_config(proj)


def test_pack_layer_adds_provider_target_and_role(sandbox: Path) -> None:
    pack = sandbox / "packs" / "demo"
    pack.mkdir(parents=True)
    (pack / "pack.toml").write_text(
        """
[pack]
name = "demo"
version = "1.0"

[providers.packtool]
command = ["{pack_dir}/bin/tool", "{task}"]
task_via = "arg"

[targets.demo]
path = "DEMO.md"
appendix = "appendix.md"

[roles.packrole]
provider = "packtool"
""",
        encoding="utf-8",
    )
    proj = sandbox / "work" / "p"
    proj.mkdir(parents=True)
    (proj / "tazuna.toml").write_text(f'[packs]\npaths = ["{pack}"]\n', encoding="utf-8")
    cfg = load_config(proj)
    assert cfg.providers["packtool"]["command"][0] == f"{pack}/bin/tool"
    assert cfg.targets["demo"]["appendix"] == str(pack / "appendix.md")
    assert "packrole" in cfg.roles
    assert cfg.packs[0]["name"] == "demo" and cfg.packs[0]["version"] == "1.0"


def test_missing_pack_is_an_error(sandbox: Path) -> None:
    proj = sandbox / "work" / "m"
    proj.mkdir(parents=True)
    (proj / "tazuna.toml").write_text('[packs]\npaths = ["./nope"]\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="pack not found"):
        load_config(proj)
