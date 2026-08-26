from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agents_owl.cli import normalize_argv
from agents_owl.registry import RegistryError, SessionRecord, SessionRegistry
from agents_owl.session_manager import (
    SessionManager,
    SessionManagerError,
    handle_claude_hook,
    install_claude_hooks,
    make_tmux_name,
    runtime_lifecycle,
)


class FakeCodex:
    def create(self, repo: Path, name: str) -> dict[str, str]:
        return {"id": "codex-native-id", "sessionId": "codex-native-id", "name": name}

    def resume_command(self, native_session_id: str) -> list[str]:
        return ["codex", "resume", native_session_id]


class SessionRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.registry = SessionRegistry(self.root / "state")

    def record(self, provider: str, native_id: str, role: str) -> SessionRecord:
        return SessionRecord(
            provider=provider,
            native_session_id=native_id,
            native_name=f"topic [{role}]",
            topic="topic",
            role=role,
            repo=str(self.repo),
        )

    def test_native_identity_is_unique_and_topic_links_are_symmetric(self) -> None:
        codex = self.registry.upsert(self.record("codex", "codex-id", "worker"))
        claude = self.registry.upsert(self.record("claude", "claude-id", "peer"))
        values = {record.key: record for record in self.registry.load()}
        self.assertEqual(values[codex.key].related_session_keys, [claude.key])
        self.assertEqual(values[claude.key].related_session_keys, [codex.key])

    def test_resolve_accepts_key_native_id_name_and_unique_prefix(self) -> None:
        record = self.registry.upsert(self.record("codex", "12345678-abcd", "worker"))
        for selector in (record.key, record.native_session_id, record.native_name, "12345678"):
            with self.subTest(selector=selector):
                self.assertEqual(self.registry.resolve(selector).key, record.key)

    def test_ambiguous_native_name_is_rejected(self) -> None:
        self.registry.upsert(self.record("codex", "one", "worker"))
        self.registry.upsert(self.record("claude", "two", "worker"))
        with self.assertRaisesRegex(RegistryError, "ambiguous"):
            self.registry.resolve("topic [worker]")

    def test_claude_hook_registers_only_managed_sessions(self) -> None:
        state = self.root / "state"
        environment = {
            "AGENTS_OWL_MANAGED_SESSION": "1",
            "AGENTS_OWL_STATE_HOME": str(state),
            "AGENTS_OWL_REPO": str(self.repo),
            "AGENTS_OWL_TOPIC": "topic",
            "AGENTS_OWL_ROLE": "worker",
            "AGENTS_OWL_NATIVE_NAME": "topic [worker]",
            "AGENTS_OWL_TMUX_SESSION": "owl-test",
        }
        handle_claude_hook(
            {
                "hook_event_name": "SessionStart",
                "session_id": "claude-native-id",
                "cwd": str(self.repo),
                "transcript_path": str(self.root / "transcript.jsonl"),
            },
            environment,
        )
        record = SessionRegistry(state).resolve("claude-native-id")
        self.assertEqual(record.tmux_session, "owl-test")
        self.assertEqual(record.topic, "topic")

        empty_state = self.root / "empty"
        handle_claude_hook(
            {"hook_event_name": "SessionStart", "session_id": "ignored"},
            {"AGENTS_OWL_STATE_HOME": str(empty_state)},
        )
        self.assertFalse((empty_state / "sessions" / "index.json").exists())

    def test_claude_hook_records_direct_runtime_without_tmux(self) -> None:
        state = self.root / "direct-state"
        environment = {
            "AGENTS_OWL_MANAGED_SESSION": "1",
            "AGENTS_OWL_STATE_HOME": str(state),
            "AGENTS_OWL_REPO": str(self.repo),
            "AGENTS_OWL_TOPIC": "direct-topic",
            "AGENTS_OWL_ROLE": "worker",
            "AGENTS_OWL_NATIVE_NAME": "direct-topic [worker]",
        }
        handle_claude_hook(
            {
                "hook_event_name": "SessionStart",
                "session_id": "claude-direct-id",
                "cwd": str(self.repo),
            },
            environment,
        )
        record = SessionRegistry(state).resolve("claude-direct-id")
        self.assertIsNone(record.tmux_session)
        self.assertEqual(runtime_lifecycle(record), "direct")

    def test_hook_install_merges_and_is_idempotent(self) -> None:
        settings = self.root / "settings.json"
        settings.write_text(json.dumps({"model": "opus", "hooks": {"PreToolUse": []}}), encoding="utf-8")
        self.assertTrue(install_claude_hooks(settings, retention_days=3650))
        self.assertFalse(install_claude_hooks(settings, retention_days=3650))
        value = json.loads(settings.read_text(encoding="utf-8"))
        self.assertEqual(value["model"], "opus")
        self.assertEqual(value["cleanupPeriodDays"], 3650)
        self.assertEqual(len(value["hooks"]["SessionStart"]), 1)
        self.assertEqual(len(value["hooks"]["SessionEnd"]), 1)

    def test_tmux_name_is_stable_and_topic_sensitive(self) -> None:
        first = make_tmux_name(self.repo, "中文主题", "worker")
        self.assertEqual(first, make_tmux_name(self.repo, "中文主题", "worker"))
        self.assertNotEqual(first, make_tmux_name(self.repo, "另一主题", "worker"))
        self.assertRegex(first, r"^[a-z0-9-]+$")

    def test_legacy_session_spelling_is_preserved(self) -> None:
        self.assertEqual(
            normalize_argv(["session", "alchemist", "worker"]),
            ["pair-session", "alchemist", "worker"],
        )
        self.assertEqual(normalize_argv(["session", "new", "--topic", "x"])[0], "session")
        self.assertEqual(normalize_argv(["session", "--help"])[0], "session")

    def test_new_codex_defaults_to_direct_native_launch(self) -> None:
        manager = SessionManager(self.repo, self.root / "direct-manager", codex=FakeCodex())
        with patch.dict(os.environ, {"TMUX": ""}), patch.object(
            manager, "_launch_direct"
        ) as launch, patch("agents_owl.session_manager.create_tmux") as create_tmux:
            record = manager.new(provider="codex", topic="topic", role="worker")
        self.assertIsNotNone(record)
        assert record is not None
        self.assertIsNone(record.tmux_session)
        launch.assert_called_once()
        create_tmux.assert_not_called()

    def test_tmux_is_only_created_when_explicitly_requested(self) -> None:
        manager = SessionManager(self.repo, self.root / "tmux-manager", codex=FakeCodex())
        with patch.object(manager, "_launch_direct") as launch, patch(
            "agents_owl.session_manager.create_tmux"
        ) as create_tmux:
            record = manager.new(
                provider="codex",
                topic="topic",
                role="worker",
                use_tmux=True,
            )
        self.assertIsNotNone(record)
        assert record is not None
        self.assertIsNotNone(record.tmux_session)
        create_tmux.assert_called_once()
        launch.assert_not_called()

    def test_direct_resume_rejects_a_live_tmux_runtime(self) -> None:
        manager = SessionManager(self.repo, self.root / "resume-manager", codex=FakeCodex())
        record = self.record("codex", "codex-running", "worker")
        record.tmux_session = "owl-running"
        manager.registry.upsert(record)
        with patch.dict(os.environ, {"TMUX": ""}), patch(
            "agents_owl.session_manager.tmux_has_session", return_value=True
        ), self.assertRaisesRegex(SessionManagerError, "already running in tmux"):
            manager.resume(record)


if __name__ == "__main__":
    unittest.main()
