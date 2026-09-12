from __future__ import annotations

import argparse
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agents_owl.cli import (
    archive_inbox,
    build_parser,
    choose_pair,
    command_sessions,
    initialize_pair,
    initialized_pairs,
    latest_artifacts,
    pair_runtime_socket,
    policy_text,
    project_config,
    read_json_object,
    validate_pair,
)


class AgentsOwlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.state = self.root / "state"
        self.repo.mkdir()
        (self.repo / "AGENTS.md").write_text("project policy\n", encoding="utf-8")

    def test_pair_name_rejects_path_traversal(self) -> None:
        for value in ("../escape", "has space", "pair/name"):
            with self.subTest(value=value), self.assertRaises(SystemExit):
                validate_pair(value)

    def test_project_config_validates_policy_files(self) -> None:
        (self.repo / ".agents-owl.json").write_text(
            json.dumps({"policy_files": ["AGENTS.md"], "collaboration_note": "advisory"}),
            encoding="utf-8",
        )
        config = project_config(self.repo)
        self.assertEqual(config["policy_files"], ["AGENTS.md"])
        self.assertIn("advisory", config["collaboration_note"])

    def test_invalid_json_has_controlled_error(self) -> None:
        broken = self.root / "broken.json"
        broken.write_text("{", encoding="utf-8")
        with self.assertRaisesRegex(SystemExit, "invalid pair metadata"):
            read_json_object(broken, "pair metadata")

    def test_initialize_records_repository_and_policy(self) -> None:
        root = initialize_pair(self.repo, self.state, "demo")
        metadata = json.loads((root / "pair.json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["repo"], str(self.repo))
        self.assertEqual(metadata["policy_files"], ["AGENTS.md"])
        self.assertIn("not project governance authority", (root / "decisions.md").read_text(encoding="utf-8"))

    def test_initialized_pairs_are_filtered_by_repository(self) -> None:
        other_repo = self.root / "other-repo"
        other_repo.mkdir()
        initialize_pair(self.repo, self.state, "current")
        initialize_pair(other_repo, self.state, "other")

        self.assertEqual(initialized_pairs(self.state, self.repo), [("current", self.repo)])
        self.assertEqual([pair for pair, _ in initialized_pairs(self.state)], ["current", "other"])

    def test_status_pair_is_optional_and_can_be_selected(self) -> None:
        initialize_pair(self.repo, self.state, "demo")
        parser = build_parser()
        self.assertIsNone(parser.parse_args(["status"]).pair)

        with patch("agents_owl.cli.sys.stdin.isatty", return_value=True), patch(
            "agents_owl.cli.sys.stdout.isatty", return_value=True
        ), patch("builtins.input", return_value="1"), patch(
            "agents_owl.cli.runtime_state", return_value="running"
        ):
            self.assertEqual(choose_pair(self.repo, self.state), "demo")

    def test_worker_selector_attaches_a_running_fixed_pair(self) -> None:
        initialize_pair(self.repo, self.state, "demo")
        args = argparse.Namespace(
            repo=str(self.repo),
            state_home=str(self.state),
            global_scope=False,
            provider=None,
            role="worker",
            all=False,
            json=False,
            no_select=False,
        )
        expected_socket = str(pair_runtime_socket(self.state, self.repo, "demo", "worker"))
        with patch("agents_owl.cli.sys.stdin.isatty", return_value=True), patch(
            "agents_owl.cli.sys.stdout.isatty", return_value=True
        ), patch("builtins.input", return_value="1"), patch(
            "agents_owl.cli.runtime_state", return_value="running"
        ), patch("agents_owl.cli.attach_runtime") as attach:
            command_sessions(args)

        attach.assert_called_once_with(expected_socket)

    def test_archive_consumes_inbox_and_records_hash(self) -> None:
        root = initialize_pair(self.repo, self.state, "demo")
        inbox = root / "inbox" / "worker-handoff.md"
        inbox.write_text("handoff\n", encoding="utf-8")
        artifact = archive_inbox(root, "demo", "worker-handoff")
        self.assertFalse(inbox.exists())
        self.assertEqual(artifact.read_text(encoding="utf-8"), "handoff\n")
        event = json.loads((root / "events.jsonl").read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(len(event["sha256"]), 64)
        self.assertEqual(latest_artifacts(root, "worker-handoff", 1), [artifact])

    def test_policy_text_uses_absolute_paths_and_note(self) -> None:
        metadata = {"policy_files": ["AGENTS.md"], "collaboration_note": "Human decides."}
        rendered = policy_text(self.repo, metadata)
        self.assertIn(str(self.repo / "AGENTS.md"), rendered)
        self.assertIn("Human decides.", rendered)


if __name__ == "__main__":
    unittest.main()
