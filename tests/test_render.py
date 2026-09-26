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


def _use_targets(project: Path, targets: str) -> None:
    toml = project / "tazuna.toml"
    text = toml.read_text(encoding="utf-8").replace('targets = ["claude", "codex", "cursor"]', f"targets = {targets}")
    toml.write_text(text, encoding="utf-8")


def test_claude_shim_imports_agents_md_instead_of_copying_it(project: Path) -> None:
    _use_targets(project, '["claude-shim", "codex"]')
    appendix = project / ".tazuna" / "appendix"
    appendix.mkdir(parents=True)
    (appendix / "claude.md").write_text("Claude-only note.\n", encoding="utf-8")
    cfg = load_config(project)
    assert [r.action for r in render(cfg)] == ["written", "written"]
    claude = (project / "CLAUDE.md").read_text(encoding="utf-8")
    assert claude.startswith(HEADER)
    assert "\n@AGENTS.md\n" in claude
    assert "Use fake-fast-1" not in claude and "Claude-only note." in claude
    agents = (project / "AGENTS.md").read_text(encoding="utf-8")
    assert "Use fake-fast-1" in agents and "Claude-only note." not in agents
    assert all(r.action == "unchanged" for r in render(cfg, check=True))


@pytest.mark.parametrize(
    "targets, message",
    [
        ('["claude-shim"]', "imports AGENTS.md from target 'codex', which is not in \\[render\\] targets"),
        ('["claude", "claude-shim", "codex"]', "'claude' and 'claude-shim' both write CLAUDE.md"),
    ],
)
def test_claude_shim_needs_its_import_and_a_path_of_its_own(project: Path, targets: str, message: str) -> None:
    _use_targets(project, targets)
    with pytest.raises(ConfigError, match=message):
        load_config(project)


def test_new_projects_default_to_agents_md_and_the_claude_shim(sandbox: Path) -> None:
    proj = sandbox / "fresh"
    proj.mkdir()
    assert main(["init", "--dir", str(proj)]) == 0
    assert load_config(proj).render_targets == ["claude-shim", "codex"]
    # A config without [render] targets keeps the pre-0.4 list, so an old project is not switched.
    (proj / "tazuna.toml").write_text("", encoding="utf-8")
    assert load_config(proj).render_targets == ["claude", "codex", "cursor"]


@pytest.mark.parametrize(
    "extra, message",
    [
        ('[targets.mine]\npath = "./claude.md"\n', "both write"),
        ('[targets.claude-shim]\nimport_of = "claude-shim"\n', "or the target itself"),
        ('[targets.codex]\nimport_of = "claude-shim"\n', "is itself an import"),
        ('[targets.claude-shim]\npath = "docs/CLAUDE.md"\n', "at the project root"),
    ],
)
def test_import_and_path_checks_cover_the_edge_cases(project: Path, extra: str, message: str) -> None:
    _use_targets(project, '["claude-shim", "codex", "mine"]' if "mine" in extra else '["claude-shim", "codex"]')
    toml = project / "tazuna.toml"
    toml.write_text(toml.read_text(encoding="utf-8") + "\n" + extra, encoding="utf-8")
    with pytest.raises(ConfigError, match=message):
        load_config(project)


def test_render_target_outside_the_enabled_list_is_refused(project: Path) -> None:
    _use_targets(project, '["claude-shim", "codex"]')
    cfg = load_config(project)
    with pytest.raises(UsageError, match="not in \\[render\\] targets"):
        render(cfg, targets=["claude"])


def test_adopt_refuses_an_import_only_claude_md_and_keeps_other_hand_written_targets(project: Path, capsys) -> None:
    _use_targets(project, '["claude-shim", "codex"]')
    (project / "PROJECT.md").unlink()
    (project / "CLAUDE.md").write_text("@AGENTS.md\n", encoding="utf-8")
    (project / "AGENTS.md").write_text("# real hand-written policy\n", encoding="utf-8")
    with pytest.raises(UsageError, match="only imports"):
        adopt(load_config(project))
    assert not (project / "PROJECT.md").exists()

    (project / "CLAUDE.md").write_text("# claude policy\n", encoding="utf-8")
    assert main(["render", "--adopt", "--dir", str(project)]) == 0
    assert (project / "AGENTS.md.pre-tazuna.bak").read_text(encoding="utf-8") == "# real hand-written policy\n"
    assert "kept as AGENTS.md.pre-tazuna.bak" in capsys.readouterr().out


def test_doctor_warns_about_a_generated_file_of_a_disabled_target(project: Path) -> None:
    render(load_config(project))
    _use_targets(project, '["claude-shim", "codex"]')
    render(load_config(project))
    text = run_doctor(project).render()
    assert "WARN .cursor/rules/project.mdc was generated for target cursor" in text, text


def test_missing_policy_is_usage_error(project: Path) -> None:
    cfg = load_config(project)
    (project / "PROJECT.md").unlink()
    with pytest.raises(UsageError, match="not found"):
        render(cfg)
