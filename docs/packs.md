# Packs

A pack is a directory with a `pack.toml` that contributes providers, render targets, roles, model aliases, and appendix files. Packs are the only extension mechanism; they use the same tables as `tazuna.toml`, so there is nothing new to learn.

```toml
# pack.toml
[pack]
name = "team-review"
version = "1.0.0"
description = "Second-opinion review roles and a shared appendix for Claude Code"

[providers.echo]
description = "A script shipped with the pack"
command = ["{pack_dir}/bin/echo-agent.sh", "{task}"]
task_via = "arg"

[targets.claude]
appendix = "appendix/claude.md"     # relative to the pack directory

[roles.review_second]
provider = "echo"

[models]
reviewer = "some-model-id"
```

Enable it:

```toml
# tazuna.toml (or ~/.config/tazuna/config.toml)
[packs]
paths = ["packs/example-pack", "~/tazuna-packs/team-review"]
```

Rules:

- Packs load after the user layer and before the project layer, in the order listed; the project file still wins.
- `{pack_dir}` in `command` and `binary` resolves to the pack's directory so packs can ship scripts.
- Relative `appendix` paths in a pack resolve inside the pack.
- A pack overriding a built-in target's `appendix` only changes that field; `path` and `frontmatter` stay.
- `tazuna packs` lists what is loaded; `tazuna doctor` validates the merged result.

Distribute a pack as a git repository or a directory in a monorepo. There is no registry; a pack is a path.

The repository ships `packs/example-pack` as a template.
