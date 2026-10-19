#!/usr/bin/env python3
"""Link the repository's shared skill into Codex and Claude Code user scopes."""

from __future__ import annotations

import argparse
import os
from pathlib import Path


def install(source: Path, directories: list[Path]) -> list[Path]:
    source = source.resolve()
    if not (source / "SKILL.md").is_file():
        raise ValueError(f"missing skill entrypoint: {source / 'SKILL.md'}")
    targets = list(dict.fromkeys(directory.expanduser().absolute() / source.name for directory in directories))
    # Check every destination before changing either client's installation.
    for target in targets:
        if os.path.lexists(target):
            if not target.is_symlink() or target.resolve() != source:
                raise ValueError(f"refusing to replace existing skill: {target}")
    for target in targets:
        target.parent.mkdir(parents=True, exist_ok=True)
        if not os.path.lexists(target):
            target.symlink_to(source, target_is_directory=True)
    return targets


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--codex-skills-dir", type=Path, default=Path.home() / ".agents/skills",
        help="Codex user skill directory (default: ~/.agents/skills)",
    )
    parser.add_argument(
        "--claude-skills-dir", type=Path, default=Path.home() / ".claude/skills",
        help="Claude Code personal skill directory (default: ~/.claude/skills)",
    )
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[1] / "skills/agents-owl"
    try:
        targets = install(source, [args.codex_skills_dir, args.claude_skills_dir])
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    for target in targets:
        print(f"{target} -> {source}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
