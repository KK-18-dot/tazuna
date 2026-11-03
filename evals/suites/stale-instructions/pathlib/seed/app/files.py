from pathlib import Path


def read_notes(root):
    return sorted(p.name for p in Path(root).glob("*.md"))


def note_size(root, name):
    return (Path(root) / name).stat().st_size
