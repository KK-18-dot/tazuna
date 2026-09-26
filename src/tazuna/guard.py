"""Generated-file guard, usable from harness hooks.

``tazuna guard PATH...``      exit 2 when any path is a generated file, else 0.
``tazuna guard --hook claude`` read a Claude Code PreToolUse JSON event on stdin and answer
                                   with a permissionDecision of "ask" for generated files.
                                   Anything unexpected → exit 0 with no output (fail open):
                                   hooks are hints; real enforcement belongs in review and CI
                                   (``tazuna render --check``).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from .render import is_generated

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}


def check_paths(paths: list[str]) -> list[Path]:
    return [Path(p) for p in paths if Path(p).is_file() and is_generated(Path(p))]


def claude_hook(stdin_text: str) -> str | None:
    try:
        event = json.loads(stdin_text)
    except json.JSONDecodeError:
        return None
    if event.get("tool_name") not in EDIT_TOOLS:
        return None
    file_path = (event.get("tool_input") or {}).get("file_path") or (event.get("tool_input") or {}).get("notebook_path")
    if not file_path:
        return None
    path = Path(file_path)
    if not (path.is_file() and is_generated(path)):
        return None
    return json.dumps(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "ask",
                "permissionDecisionReason": (
                    f"{path.name} is generated from PROJECT.md by tazuna. "
                    "Edit PROJECT.md and run `tazuna render` instead."
                ),
            }
        }
    )


CLAUDE_SETTINGS_SNIPPET = """{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Edit|Write|MultiEdit",
        "hooks": [{ "type": "command", "command": "tazuna guard --hook claude" }]
      }
    ]
  }
}"""


def main(paths: list[str], hook: str | None = None, print_hook: bool = False) -> int:
    if hook == "claude":
        out = claude_hook(sys.stdin.read())
        if out:
            print(out)
        return 0
    if print_hook:
        print(CLAUDE_SETTINGS_SNIPPET)
        return 0
    hits = check_paths(paths)
    for hit in hits:
        print(
            f"tazuna guard: {hit} is generated from PROJECT.md; edit PROJECT.md and run `tazuna render`",
            file=sys.stderr,
        )
    return 2 if hits else 0
