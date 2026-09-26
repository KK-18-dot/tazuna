# example-pack

A minimal pack showing the three things a pack can contribute:

- a **provider** backed by a script shipped inside the pack (`bin/echo-agent.sh`, referenced via `{pack_dir}`)
- a **role** that uses it (`echo`)
- a per-target **appendix** (`appendix/claude.md`, appended to `CLAUDE.md` only)

Enable it from a project:

```toml
[packs]
paths = ["packs/example-pack"]
```

Then:

```bash
tazuna packs
tazuna routes echo
tazuna run --role echo "Say hello"
```

Copy the directory, rename it, and replace the script with a call to any CLI to make a real provider pack. See `docs/packs.md`.
