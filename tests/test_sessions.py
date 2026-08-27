from __future__ import annotations

import json
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agents_owl.cli import build_parser, normalize_argv
from agents_owl.registry import RegistryError, SessionRecord, SessionRegistry
from agents_owl.session_manager import (
    SessionManager,
    SessionManagerError,
    attach_runtime,
    handle_claude_hook,
    install_claude_hooks,
    make_runtime_socket,
    runtime_lifecycle,
    runtime_state,
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
            "AGENTS_OWL_RUNTIME_SOCKET": str(self.root / "managed.sock"),
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
        self.assertEqual(record.runtime_socket, str(self.root / "managed.sock"))
        self.assertEqual(record.topic, "topic")

        empty_state = self.root / "empty"
        handle_claude_hook(
            {"hook_event_name": "SessionStart", "session_id": "ignored"},
            {"AGENTS_OWL_STATE_HOME": str(empty_state)},
        )
        self.assertFalse((empty_state / "sessions" / "index.json").exists())

    def test_claude_hook_preserves_existing_runtime_when_environment_omits_it(self) -> None:
        state = self.root / "existing-state"
        existing = self.record("claude", "claude-existing-id", "worker")
        existing.runtime_socket = str(self.root / "existing.sock")
        SessionRegistry(state).upsert(existing)
        environment = {
            "AGENTS_OWL_MANAGED_SESSION": "1",
            "AGENTS_OWL_STATE_HOME": str(state),
            "AGENTS_OWL_REPO": str(self.repo),
            "AGENTS_OWL_TOPIC": "topic",
            "AGENTS_OWL_ROLE": "worker",
            "AGENTS_OWL_NATIVE_NAME": "topic [worker]",
        }
        handle_claude_hook(
            {
                "hook_event_name": "SessionStart",
                "session_id": "claude-existing-id",
                "cwd": str(self.repo),
            },
            environment,
        )
        record = SessionRegistry(state).resolve("claude-existing-id")
        self.assertEqual(record.runtime_socket, str(self.root / "existing.sock"))
        self.assertEqual(runtime_lifecycle(record), "exited")

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

    def test_runtime_socket_is_stable_with_discriminator_and_topic_sensitive(self) -> None:
        state = self.root / "state"
        first = make_runtime_socket(state, self.repo, "中文主题", "worker", "fixed")
        self.assertEqual(first, make_runtime_socket(state, self.repo, "中文主题", "worker", "fixed"))
        self.assertNotEqual(first, make_runtime_socket(state, self.repo, "另一主题", "worker", "fixed"))
        self.assertEqual(first.parent, state / "runtimes")
        self.assertRegex(first.name, r"^[a-f0-9]{20}\.sock$")

    def test_runtime_state_distinguishes_running_exited_and_orphaned(self) -> None:
        path = self.root / "probe.sock"
        self.assertEqual(runtime_state(str(path)), "exited")
        path.write_text("not a socket", encoding="utf-8")
        self.assertEqual(runtime_state(str(path)), "orphaned")
        path.unlink()
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(server.close)
        server.bind(str(path))
        server.listen()
        self.assertEqual(runtime_state(str(path)), "running")

    def test_legacy_session_spelling_is_preserved(self) -> None:
        self.assertEqual(
            normalize_argv(["session", "alchemist", "worker"]),
            ["pair-session", "alchemist", "worker"],
        )
        self.assertEqual(normalize_argv(["session", "new", "--topic", "x"])[0], "session")
        self.assertEqual(normalize_argv(["session", "--help"])[0], "session")

    def test_short_commands_map_to_provider_roles(self) -> None:
        parser = build_parser()
        impl = parser.parse_args(["impl", "topic"])
        self.assertEqual((impl.provider, impl.role, impl.topic), ("claude", "worker", "topic"))
        review = parser.parse_args(["review", "topic"])
        self.assertEqual((review.provider, review.role, review.topic), ("codex", "peer", "topic"))
        self.assertEqual(parser.parse_args(["i"]).role, "worker")
        self.assertEqual(parser.parse_args(["r"]).role, "peer")
        self.assertEqual(normalize_argv([]), ["sessions"])

    def test_new_codex_always_uses_protected_runtime(self) -> None:
        manager = SessionManager(self.repo, self.root / "protected-manager", codex=FakeCodex())
        with patch.object(manager, "_launch_runtime") as launch:
            record = manager.new(provider="codex", topic="topic", role="worker")
        self.assertIsNotNone(record)
        assert record is not None
        self.assertIsNotNone(record.runtime_socket)
        launch.assert_called_once()

    def test_resume_attaches_existing_live_runtime(self) -> None:
        manager = SessionManager(self.repo, self.root / "resume-manager", codex=FakeCodex())
        record = self.record("codex", "codex-running", "worker")
        record.runtime_socket = str(self.root / "running.sock")
        manager.registry.upsert(record)
        with patch("agents_owl.session_manager.runtime_state", return_value="running"), patch(
            "agents_owl.session_manager.attach_runtime"
        ) as attach:
            manager.resume(record)
        attach.assert_called_once_with(record.runtime_socket)

    def test_finish_rejects_a_live_runtime(self) -> None:
        manager = SessionManager(self.repo, self.root / "finish-manager", codex=FakeCodex())
        record = self.record("codex", "codex-running", "worker")
        record.runtime_socket = str(self.root / "running.sock")
        with patch("agents_owl.session_manager.runtime_state", return_value="running"), self.assertRaisesRegex(
            SessionManagerError, "still running"
        ):
            manager.finish(record)

    def test_attach_forces_full_screen_redraw(self) -> None:
        with patch("agents_owl.session_manager.runtime_state", return_value="running"), patch(
            "agents_owl.session_manager.require_dtach", return_value=["dtach"]
        ), patch("agents_owl.session_manager.os.execvp") as execute:
            attach_runtime("/tmp/agent.sock")
        execute.assert_called_once_with(
            "dtach",
            ["dtach", "-a", "/tmp/agent.sock", "-Ez", "-r", "ctrl_l"],
        )


if __name__ == "__main__":
    unittest.main()
