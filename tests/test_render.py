from pathlib import Path

import pytest

from tazuna.cli import main
from tazuna.config import load_config
from tazuna.doctor import run_doctor
from tazuna.errors import ConfigError, UsageError
from tazuna.render import HEADER, adopt, is_generated, render


def test_render_writes_all_targets_with_marker_and_model_expansion(project: Path) -> None:
    cfg = load_config(project)
    results = render(cfg)
    assert [r.action for r in results] == ["written", "written", "written"]
    claude = (project / "CLAUDE.md").read_text(encoding="utf-8")
    assert claude.startswith(HEADER)
    assert "Use fake-fast-1 for routine work." in claude
    assert "{{model:" not in claude
    cursor = (project / ".cursor/rules/project.mdc").read_text(encoding="utf-8")
    assert cursor.startswith("---\ndescription:")
    assert HEADER in cursor
    assert is_generated(project / "AGENTS.md")


def test_render_is_idempotent_and_check_detects_drift(project: Path) -> None:
    cfg = load_config(project)
    render(cfg)
    assert [r.action for r in render(cfg)] == ["unchanged"] * 3
    assert all(r.action == "unchanged" for r in render(cfg, check=True))
    (project / "PROJECT.md").write_text("# changed\n", encoding="utf-8")
    actions = {r.target: r.action for r in render(cfg, check=True)}
    assert actions == {"claude": "drift", "codex": "drift", "cursor": "drift"}
    # --check never writes
    assert "Use fake-fast-1" in (project / "CLAUDE.md").read_text(encoding="utf-8")


def test_hand_written_file_is_refused_without_force(project: Path) -> None:
    cfg = load_config(project)
    (project / "CLAUDE.md").write_text("# my precious hand-written notes\n", encoding="utf-8")
    results = {r.target: r for r in render(cfg)}
    assert results["claude"].action == "refused"
    assert "hand-written" in results["claude"].message
    assert "precious" in (project / "CLAUDE.md").read_text(encoding="utf-8")
    assert results["codex"].action == "written"
    results = {r.target: r for r in render(cfg, force=True)}
    assert results["claude"].action == "written"


def test_adopt_promotes_claude_md(project: Path) -> None:
    cfg = load_config(project)
    (project / "PROJECT.md").unlink()
    (project / "CLAUDE.md").write_text("# legacy policy\n", encoding="utf-8")
    backup = adopt(cfg)
    assert backup.is_file()
    assert (project / "PROJECT.md").read_text(encoding="utf-8") == "# legacy policy\n"
    with pytest.raises(UsageError, match="already exists"):
        adopt(cfg)


def test_appendix_is_appended_per_target(project: Path) -> None:
    cfg = load_config(project)
    appendix = project / ".tazuna" / "appendix"
    appendix.mkdir(parents=True)
    (appendix / "claude.md").write_text("Claude-only note.\n", encoding="utf-8")
    render(cfg)
    assert "Claude-only note." in (project / "CLAUDE.md").read_text(encoding="utf-8")
    assert "Claude-only note." not in (project / "AGENTS.md").read_text(encoding="utf-8")


def test_unknown_model_token_fails(project: Path) -> None:
    cfg = load_config(project)
    (project / "PROJECT.md").write_text("{{model:ghost}}\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="no key 'ghost'"):
        render(cfg)


def test_builtin_codex_target_declares_the_codex_read_limit(sandbox: Path) -> None:
    targets = load_config(sandbox).targets
    assert targets["codex"]["max_bytes"] == 32768
    assert "max_bytes" not in targets["claude"]


def test_target_over_max_bytes_is_written_with_a_warning_and_fails_check(project: Path, capsys) -> None:
    toml = project / "tazuna.toml"
    toml.write_text(toml.read_text() + "\n[targets.codex]\nmax_bytes = 300\n", encoding="utf-8")
    (project / "PROJECT.md").write_text("# proj\n\n" + "policy line\n" * 40, encoding="utf-8")
    cfg = load_config(project)
    written = {r.target: r for r in render(cfg)}
    assert written["codex"].action == "written" and written["codex"].too_large
    assert (project / "AGENTS.md").stat().st_size > 300
    assert not written["claude"].too_large
    checked = {r.target: r for r in render(cfg, check=True)}
    assert checked["codex"].action == "too-large"
    assert "max_bytes 300" in checked["codex"].message
    assert checked["claude"].action == "unchanged"

    capsys.readouterr()
    assert main(["render", "--dir", str(project)]) == 0
    assert "WARNING   AGENTS.md:" in capsys.readouterr().out
    assert main(["render", "--check", "--dir", str(project)]) == 1
    assert "TOO-LARGE AGENTS.md" in capsys.readouterr().out
    text = run_doctor(project).render()
    assert "WARN render target codex: AGENTS.md is " in text and "over max_bytes 300" in text


def test_max_bytes_must_be_a_positive_integer(project: Path) -> None:
    toml = project / "tazuna.toml"
    toml.write_text(toml.read_text() + '\n[targets.codex]\nmax_bytes = "big"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="max_bytes must be a positive integer"):
        load_config(project)


def test_missing_policy_is_usage_error(project: Path) -> None:
    cfg = load_config(project)
    (project / "PROJECT.md").unlink()
    with pytest.raises(UsageError, match="not found"):
        render(cfg)
