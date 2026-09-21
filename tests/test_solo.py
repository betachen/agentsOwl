from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agents_owl.cli import build_parser
from agents_owl.session_manager import SessionManagerError, launch_runtime


class SoloTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.work = self.root / "project" / "subdir"
        self.work.mkdir(parents=True)
        (self.work.parent / ".git").mkdir()
        self.state = self.root / "state"
        self.claude_home = self.root / "claude-home"
        self.codex_home = self.root / "codex-home"
        environment = patch.dict(
            os.environ,
            {"CLAUDE_CONFIG_DIR": str(self.claude_home), "CODEX_HOME": str(self.codex_home)},
        )
        environment.start()
        self.addCleanup(environment.stop)

    def args(self, provider: str = "claude", *options: str):
        return build_parser().parse_args([
            "--state-home", str(self.state), "solo", provider, *options,
        ])

    def test_fresh_solo_uses_exact_cwd_and_no_collaboration_context(self) -> None:
        inherited = {
            "AGENTS_OWL_REPO": "/old/project",
            "AGENTS_OWL_MANAGED_SESSION": "1",
            "AGENTS_OWL_ROLE": "peer",
            "AGENTS_OWL_TOPIC": "old topic",
            "AGENTS_OWL_WORKER_CMD": "claude --resume old-session",
            "IMPLEMENTER_CMD": "old-worker",
        }
        for provider in ("claude", "codex"):
            with self.subTest(provider=provider), patch.dict(os.environ, inherited), patch(
                "agents_owl.cli.Path.cwd", return_value=self.work
            ), patch("agents_owl.cli.project_config", side_effect=AssertionError("read project config")), patch(
                "agents_owl.cli.launch_runtime"
            ) as launch:
                args = self.args(provider)
                args.func(args)
            runtime_socket, cwd, command, environment = launch.call_args.args
            self.assertEqual(cwd, self.work)
            self.assertEqual(command[0], provider)
            self.assertEqual(environment, {"PWD": str(self.work)})
            self.assertEqual(runtime_socket.parent, self.state / "runtimes")
            self.assertTrue(set(inherited).issubset(launch.call_args.kwargs["remove_environment"]))
        self.assertEqual(list(self.work.iterdir()), [])
        self.assertFalse((self.state / "pairs").exists())
        self.assertFalse((self.state / "sessions").exists())

    def test_repeat_command_reconnects_without_launching_another_agent(self) -> None:
        args = self.args("codex", "--name", "quick task")
        with patch("agents_owl.cli.Path.cwd", return_value=self.work), patch(
            "agents_owl.cli.runtime_state", return_value="exited"
        ), patch("agents_owl.cli.launch_runtime") as launch:
            args.func(args)
        runtime_socket = launch.call_args.args[0]
        with patch("agents_owl.cli.Path.cwd", return_value=self.work), patch(
            "agents_owl.cli.runtime_state", return_value="running"
        ), patch("agents_owl.cli.attach_runtime") as attach, patch(
            "agents_owl.cli.launch_runtime"
        ) as launch:
            args.func(args)
        attach.assert_called_once_with(str(runtime_socket))
        launch.assert_not_called()

    def launched_command(self, provider: str, *options: str) -> list[str]:
        args = self.args(provider, *options)
        with patch("agents_owl.cli.Path.cwd", return_value=self.work), patch(
            "agents_owl.cli.launch_runtime"
        ) as launch:
            args.func(args)
        return launch.call_args.args[2]

    def test_restarted_claude_solo_resumes_the_same_conversation(self) -> None:
        first = self.launched_command("claude")
        self.assertEqual(first[:2], ["claude", "--session-id"])
        native_id = first[2]
        self.assertEqual(first[3:], ["--name", "solo default"])

        transcript = self.claude_home / "projects" / "-work" / f"{native_id}.jsonl"
        transcript.parent.mkdir(parents=True)
        transcript.write_text("{}\n", encoding="utf-8")
        self.assertEqual(self.launched_command("claude"), ["claude", "--resume", native_id])

        other = self.launched_command("claude", "--name", "other")
        self.assertNotEqual(other[2], native_id)
        fresh = self.launched_command("claude", "--fresh")
        self.assertEqual(fresh[1], "--session-id")
        self.assertNotEqual(fresh[2], native_id)

    def test_restarted_codex_solo_resumes_the_same_thread(self) -> None:
        self.assertEqual(self.launched_command("codex"), ["codex"])
        rollout = self.codex_home / "sessions" / "2026" / "01" / "rollout-codex-thread.jsonl"
        rollout.parent.mkdir(parents=True)
        rollout.write_text(
            json.dumps({"payload": {"session_id": "codex-thread", "cwd": str(self.work)}})
            + "\n{}\n",
            encoding="utf-8",
        )
        self.assertEqual(self.launched_command("codex"), ["codex", "resume", "codex-thread"])
        self.assertEqual(self.launched_command("codex", "--fresh"), ["codex"])

    def test_directory_provider_and_name_have_independent_runtimes(self) -> None:
        sockets = []
        for directory, provider, name in (
            (self.work, "claude", "default"),
            (self.work.parent, "claude", "default"),
            (self.work, "codex", "default"),
            (self.work, "claude", "another-task"),
        ):
            with patch("agents_owl.cli.Path.cwd", return_value=directory), patch(
                "agents_owl.cli.launch_runtime"
            ) as launch:
                args = self.args(provider, "--name", name)
                args.func(args)
            sockets.append(launch.call_args.args[0])
        self.assertEqual(len(set(sockets)), 4)

    def test_explicit_repo_override_is_honored(self) -> None:
        args = build_parser().parse_args([
            "--state-home", str(self.state), "--repo", str(self.work), "solo", "claude",
        ])
        with patch("agents_owl.cli.launch_runtime") as launch:
            args.func(args)
        self.assertEqual(launch.call_args.args[1], self.work)

    def test_empty_name_does_not_start_runtime(self) -> None:
        args = self.args("claude", "--name", " ")
        with patch("agents_owl.cli.launch_runtime") as launch, self.assertRaisesRegex(
            SessionManagerError, "name must not be empty"
        ):
            args.func(args)
        launch.assert_not_called()

    def test_launch_removes_context_but_keeps_provider_environment(self) -> None:
        inherited = {"AGENTS_OWL_MANAGED_SESSION": "1", "AGENTS_OWL_ROLE": "worker", "TERM": "xterm"}
        with patch.dict(os.environ, inherited), patch(
            "agents_owl.session_manager.require_dtach", return_value=["dtach"]
        ), patch("agents_owl.session_manager.os.chdir"), patch(
            "agents_owl.session_manager.os.execvpe"
        ) as execute:
            launch_runtime(
                self.state / "runtimes" / "solo.sock", self.work, ["claude"],
                {"PWD": str(self.work)},
                remove_environment=("AGENTS_OWL_MANAGED_SESSION", "AGENTS_OWL_ROLE"),
            )
        environment = execute.call_args.args[2]
        self.assertNotIn("AGENTS_OWL_MANAGED_SESSION", environment)
        self.assertNotIn("AGENTS_OWL_ROLE", environment)
        self.assertEqual(environment["TERM"], "xterm")
        self.assertEqual(environment["PWD"], str(self.work))


if __name__ == "__main__":
    unittest.main()
