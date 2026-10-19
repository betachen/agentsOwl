from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class SkillInstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = Path(__file__).resolve().parents[1]
        self.codex = self.root / "codex-skills"
        self.claude = self.root / "claude-skills"

    def install(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run([
            sys.executable, str(self.repo / "scripts/install_skills.py"),
            "--codex-skills-dir", str(self.codex),
            "--claude-skills-dir", str(self.claude),
        ], text=True, capture_output=True, check=False)

    def test_both_clients_share_the_same_skill_and_reinstall_is_idempotent(self) -> None:
        for _ in range(2):
            result = self.install()
            self.assertEqual(result.returncode, 0, result.stderr)
        source = self.repo / "skills/agents-owl"
        for directory in (self.codex, self.claude):
            installed = directory / "agents-owl"
            self.assertTrue(installed.is_symlink())
            self.assertEqual(installed.resolve(), source)
            self.assertTrue((installed / "SKILL.md").is_file())

    def test_conflict_preserves_existing_skill_and_does_not_partially_install(self) -> None:
        existing = self.claude / "agents-owl"
        existing.mkdir(parents=True)
        original = existing / "SKILL.md"
        original.write_text("user-owned skill\n", encoding="utf-8")
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("refusing to replace", result.stderr)
        self.assertEqual(original.read_text(encoding="utf-8"), "user-owned skill\n")
        self.assertFalse((self.codex / "agents-owl").exists())

    def test_dangling_skill_link_is_preserved(self) -> None:
        self.codex.mkdir()
        target = self.codex / "agents-owl"
        missing = self.root / "missing-skill"
        target.symlink_to(missing, target_is_directory=True)
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(target.is_symlink())
        self.assertEqual(target.readlink(), missing)
        self.assertFalse((self.claude / "agents-owl").exists())
