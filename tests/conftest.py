"""Shared fixtures.

Every test runs against a throwaway HOME, XDG dirs, and state dir, and a PATH that contains only
a `fakebin` directory plus the system directories, so no real provider CLI can ever be reached.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

SYSTEM_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(home / ".local" / "state"))
    monkeypatch.setenv("TAZUNA_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("PATH", f"{fakebin}:{SYSTEM_PATH}")
    for var in (
        "TAZUNA_DEPTH",
        "TAZUNA_PARENT",
        "TAZUNA_MOCK_RESPONSE",
        "TAZUNA_MOCK_EXIT",
        "TAZUNA_MOCK_SLEEP",
    ):
        monkeypatch.delenv(var, raising=False)
    # Path.home() reads HOME on POSIX; make sure nothing cached the real one.
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return tmp_path


@pytest.fixture
def project(sandbox: Path) -> Path:
    """A git-initialised project directory outside HOME with a minimal tazuna.toml."""
    proj = sandbox / "work" / "proj"
    proj.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=proj, check=True)
    (proj / "tazuna.toml").write_text(
        """
[project]
name = "proj"

[render]
targets = ["claude", "codex", "cursor"]

[models]
fast = "fake-fast-1"
strong = "fake-strong-1"
retired = ["old-model-9"]

[roles.dry]
provider = "mock"
effort = "low"

[roles.shim]
provider = "fakecli"
model = "fast"
effort = "high"
timeout = 5
fallback = "dry"

[roles.ro]
provider = "fakecli"
model = "strong"
read_only = true

[providers.fakecli]
binary = "fakecli"
command = ["fakecli", "--mode", "{permission_mode}"]
model_args = ["--model", "{model}"]
effort_args = ["--effort", "{effort}"]
efforts = ["low", "high"]
write_mode = "write"
read_only_mode = "plan"
task_via = "stdin"
quota_markers = ["usage limit reached"]
auth_markers = ["please log in"]
""",
        encoding="utf-8",
    )
    (proj / "PROJECT.md").write_text("# proj — PROJECT.md\n\nUse {{model:fast}} for routine work.\n", encoding="utf-8")
    return proj


def write_fake_cli(sandbox: Path, name: str, script: str) -> Path:
    """Install an argv/stdin-recording shim into fakebin. ``script`` is bash after the recording."""
    fakebin = sandbox / "fakebin"
    record = sandbox / f"{name}.argv"
    stdin_file = sandbox / f"{name}.stdin"
    path = fakebin / name
    path.write_text(
        "#!/usr/bin/env bash\n"
        f"printf '%s\\n' \"$@\" > {record}\n"
        f"cat > {stdin_file}\n"
        f"env > {sandbox / (name + '.env')}\n" + script + "\n",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


@pytest.fixture
def fake_cli(sandbox: Path):
    def _make(
        name: str = "fakecli",
        script: str = "echo 'work done, verified with the test suite'; echo 'TAZUNA-STATUS: DONE'",
    ) -> Path:
        return write_fake_cli(sandbox, name, script)

    return _make


def run_cli(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Run the CLI in-process-equivalent via `python -m tazuna` for end-to-end checks."""
    import sys

    return subprocess.run(
        [sys.executable, "-m", "tazuna", *args], capture_output=True, text=True, cwd=cwd, env={**os.environ}
    )
