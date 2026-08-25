"""Session lifecycle operations shared by the CLI and Claude hooks."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from .providers import (
    CodexProvider,
    ProviderError,
    claude_new_command,
    claude_resume_command,
)
from .registry import RegistryError, SessionRecord, SessionRegistry, now_iso


class SessionManagerError(RuntimeError):
    """Raised for a user-facing session management failure."""


def tmux_has_session(name: str) -> bool:
    try:
        result = subprocess.run(
            ["tmux", "has-session", "-t", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except FileNotFoundError as exc:
        raise SessionManagerError("tmux is not installed or not on PATH") from exc
    return result.returncode == 0


def runtime_lifecycle(record: SessionRecord) -> str:
    if record.lifecycle == "active":
        if record.tmux_session and tmux_has_session(record.tmux_session):
            return "running"
        return "suspended"
    return record.lifecycle


def make_native_name(topic: str, role: str) -> str:
    return f"{topic.strip()} [{role}]"


def make_tmux_name(repo: Path, topic: str, role: str) -> str:
    readable = re.sub(r"[^a-z0-9]+", "-", topic.lower()).strip("-")[:24]
    if not readable:
        readable = "topic"
    digest = hashlib.sha256(f"{repo}:{topic}:{role}".encode()).hexdigest()[:8]
    repo_name = re.sub(r"[^a-z0-9]+", "-", repo.name.lower()).strip("-") or "repo"
    return f"owl-{repo_name[:20]}-{readable}-{role}-{digest}"


def shell_command(command: list[str], environment: dict[str, str]) -> str:
    assignments = " ".join(
        f"{key}={shlex.quote(value)}" for key, value in sorted(environment.items())
    )
    executable = " ".join(shlex.quote(item) for item in command)
    return f"exec env {assignments} {executable}"


def create_tmux(name: str, repo: Path, command: list[str], environment: dict[str, str]) -> None:
    if tmux_has_session(name):
        raise SessionManagerError(f"tmux session already exists: {name}")
    try:
        subprocess.run(
            ["tmux", "new-session", "-d", "-s", name, "-c", str(repo), shell_command(command, environment)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip() if exc.stderr else "unknown tmux error"
        raise SessionManagerError(f"could not create tmux session {name}: {detail}") from exc


def attach_tmux(name: str) -> None:
    if not tmux_has_session(name):
        raise SessionManagerError(f"tmux session does not exist: {name}")
    os.execvp("tmux", ["tmux", "attach-session", "-t", name])


class SessionManager:
    def __init__(self, repo: Path, state_home: Path, codex: CodexProvider | None = None):
        self.repo = repo.resolve()
        self.state_home = state_home.resolve()
        self.registry = SessionRegistry(self.state_home)
        self.codex = codex or CodexProvider()

    def new(
        self,
        *,
        provider: str,
        topic: str,
        role: str,
        native_name: str | None = None,
    ) -> SessionRecord | None:
        topic = topic.strip()
        if not topic:
            raise SessionManagerError("topic must not be empty")
        if provider not in {"codex", "claude"}:
            raise SessionManagerError(f"unknown provider: {provider}")
        if role not in {"worker", "peer"}:
            raise SessionManagerError(f"unknown role: {role}")
        name = (native_name or make_native_name(topic, role)).strip()
        if not name:
            raise SessionManagerError("native name must not be empty")
        tmux_name = make_tmux_name(self.repo, topic, role)
        environment = self._hook_environment(topic, role, name, tmux_name)

        if provider == "codex":
            thread = self.codex.create(self.repo, name)
            native_id = thread.get("sessionId") or thread.get("id")
            if not isinstance(native_id, str) or not native_id:
                raise SessionManagerError("Codex did not return a native session id")
            record = SessionRecord(
                provider="codex",
                native_session_id=native_id,
                native_name=name,
                topic=topic,
                role=role,
                repo=str(self.repo),
                tmux_session=tmux_name,
            )
            self.registry.upsert(record)
            create_tmux(tmux_name, self.repo, self.codex.resume_command(native_id), environment)
            return record

        create_tmux(tmux_name, self.repo, claude_new_command(name), environment)
        return self._wait_for_claude_record(tmux_name)

    def resume(self, record: SessionRecord) -> SessionRecord:
        if record.lifecycle == "archived":
            raise SessionManagerError("session is archived; unarchive it before resuming")
        if record.tmux_session and tmux_has_session(record.tmux_session):
            return record
        tmux_name = record.tmux_session or make_tmux_name(
            Path(record.repo), record.topic, record.role
        )
        environment = self._hook_environment(
            record.topic,
            record.role,
            record.native_name,
            tmux_name,
            repo=Path(record.repo),
        )
        if record.provider == "codex":
            command = self.codex.resume_command(record.native_session_id)
        else:
            command = claude_resume_command(record.native_session_id)
        create_tmux(tmux_name, Path(record.repo), command, environment)
        return self.registry.update(
            record.key,
            lifecycle="active",
            tmux_session=tmux_name,
            completed_at=None,
        )

    def finish(self, record: SessionRecord, outcome: str = "", stop: bool = False) -> SessionRecord:
        if stop and record.tmux_session and tmux_has_session(record.tmux_session):
            subprocess.run(["tmux", "kill-session", "-t", record.tmux_session], check=True)
        return self.registry.update(
            record.key,
            lifecycle="completed",
            completed_at=now_iso(),
            outcome=outcome.strip(),
            git_refs=self._git_refs(Path(record.repo)),
        )

    def rename(self, record: SessionRecord, name: str) -> SessionRecord:
        name = name.strip()
        if not name:
            raise SessionManagerError("native name must not be empty")
        if record.provider == "codex":
            self.codex.rename(record.native_session_id, name)
        else:
            if not record.tmux_session or not tmux_has_session(record.tmux_session):
                raise SessionManagerError(
                    "Claude rename requires a running managed session; resume it first"
                )
            self._inject(record.tmux_session, f"/rename {name}")
        return self.registry.update(record.key, native_name=name)

    def archive(self, record: SessionRecord) -> SessionRecord:
        if record.tmux_session and tmux_has_session(record.tmux_session):
            raise SessionManagerError("finish or stop the running session before archiving")
        if record.provider == "codex":
            self.codex.archive(record.native_session_id)
        return self.registry.update(
            record.key,
            lifecycle="archived",
            native_archived=record.provider == "codex",
            archived_from=record.lifecycle,
        )

    def unarchive(self, record: SessionRecord) -> SessionRecord:
        if record.lifecycle != "archived":
            raise SessionManagerError("session is not archived")
        if record.provider == "codex" and record.native_archived:
            self.codex.unarchive(record.native_session_id)
        restored = record.archived_from or "completed"
        if restored == "archived":
            restored = "completed"
        return self.registry.update(
            record.key,
            lifecycle=restored,
            native_archived=False,
            archived_from=None,
        )

    def _hook_environment(
        self,
        topic: str,
        role: str,
        native_name: str,
        tmux_session: str,
        *,
        repo: Path | None = None,
    ) -> dict[str, str]:
        selected_repo = (repo or self.repo).resolve()
        return {
            "AGENTS_OWL_MANAGED_SESSION": "1",
            "AGENTS_OWL_STATE_HOME": str(self.state_home),
            "AGENTS_OWL_REPO": str(selected_repo),
            "AGENTS_OWL_TOPIC": topic,
            "AGENTS_OWL_ROLE": role,
            "AGENTS_OWL_NATIVE_NAME": native_name,
            "AGENTS_OWL_TMUX_SESSION": tmux_session,
        }

    def _wait_for_claude_record(self, tmux_name: str) -> SessionRecord | None:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            matches = [
                record
                for record in self.registry.find(repo=self.repo, include_archived=True)
                if record.provider == "claude" and record.tmux_session == tmux_name
            ]
            if matches:
                return matches[0]
            time.sleep(0.1)
        return None

    def _git_refs(self, repo: Path) -> list[str]:
        result = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        value = result.stdout.strip()
        return [value] if result.returncode == 0 and value else []

    @staticmethod
    def _inject(tmux_session: str, prompt: str) -> None:
        try:
            subprocess.run(["tmux", "set-buffer", "--", prompt], check=True)
            subprocess.run(["tmux", "paste-buffer", "-t", tmux_session], check=True)
            subprocess.run(["tmux", "send-keys", "-t", tmux_session, "C-m"], check=True)
        except subprocess.CalledProcessError as exc:
            raise SessionManagerError(f"could not send command to {tmux_session}") from exc


def handle_claude_hook(payload: dict[str, Any], environment: dict[str, str]) -> None:
    if environment.get("AGENTS_OWL_MANAGED_SESSION") != "1":
        return
    native_id = payload.get("session_id")
    if not isinstance(native_id, str) or not native_id:
        raise SessionManagerError("Claude hook payload has no session_id")
    state_value = environment.get("AGENTS_OWL_STATE_HOME")
    repo = environment.get("AGENTS_OWL_REPO") or payload.get("cwd")
    topic = environment.get("AGENTS_OWL_TOPIC")
    role = environment.get("AGENTS_OWL_ROLE")
    native_name = payload.get("session_title") or environment.get("AGENTS_OWL_NATIVE_NAME")
    if not all(isinstance(value, str) and value for value in (state_value, repo, topic, role, native_name)):
        raise SessionManagerError("Claude hook is missing AgentsOwl session metadata")
    registry = SessionRegistry(Path(state_value))
    key = f"claude:{native_id}"
    event_name = payload.get("hook_event_name")
    if event_name == "SessionEnd":
        try:
            registry.update(key)
        except RegistryError:
            pass
        return
    if event_name != "SessionStart":
        return
    existing: SessionRecord | None = None
    try:
        existing = registry.resolve(key)
    except RegistryError:
        pass
    record = SessionRecord(
        provider="claude",
        native_session_id=native_id,
        native_name=str(native_name),
        topic=str(topic),
        role=str(role),
        repo=str(Path(str(repo)).resolve()),
        lifecycle="active",
        tmux_session=environment.get("AGENTS_OWL_TMUX_SESSION"),
        created_at=existing.created_at if existing else now_iso(),
        outcome=existing.outcome if existing else "",
        git_refs=existing.git_refs if existing else [],
        transcript_path=(
            str(payload["transcript_path"])
            if isinstance(payload.get("transcript_path"), str)
            else None
        ),
    )
    registry.upsert(record)


def claude_hook_command() -> str:
    executable = Path(__file__).resolve().parents[2] / "bin" / "agents-owl"
    return f"{shlex.quote(str(executable))} hook claude"


def install_claude_hooks(settings_path: Path, retention_days: int | None = None) -> bool:
    """Merge the two official lifecycle hooks into Claude user settings."""
    if settings_path.exists():
        try:
            raw = json.loads(settings_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SessionManagerError(f"invalid Claude settings {settings_path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise SessionManagerError(f"Claude settings must be a JSON object: {settings_path}")
    else:
        raw = {}
    hooks = raw.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise SessionManagerError(f"hooks must be an object: {settings_path}")
    command = claude_hook_command()
    changed = False
    if retention_days is not None:
        if retention_days < 1:
            raise SessionManagerError("Claude retention days must be at least 1")
        if raw.get("cleanupPeriodDays") != retention_days:
            raw["cleanupPeriodDays"] = retention_days
            changed = True
    for event in ("SessionStart", "SessionEnd"):
        groups = hooks.setdefault(event, [])
        if not isinstance(groups, list):
            raise SessionManagerError(f"hooks.{event} must be a list: {settings_path}")
        migrated_groups = [_remove_legacy_agents_owl_hooks(group, command) for group in groups]
        groups[:] = [group for group in migrated_groups if group is not None]
        if groups != migrated_groups:
            changed = True
        if any(_hook_group_has_command(group, command) for group in groups):
            continue
        groups.append({"hooks": [{"type": "command", "command": command}]})
        changed = True
    if changed:
        _atomic_json_write(settings_path, raw)
    return changed


def _hook_group_has_command(group: object, command: str) -> bool:
    if not isinstance(group, dict):
        return False
    hooks = group.get("hooks")
    return isinstance(hooks, list) and any(
        isinstance(item, dict) and item.get("command") == command for item in hooks
    )


def _remove_legacy_agents_owl_hooks(group: object, desired_command: str) -> object | None:
    if not isinstance(group, dict):
        return group
    hooks = group.get("hooks")
    if not isinstance(hooks, list):
        return group
    retained = [
        item
        for item in hooks
        if not (
            isinstance(item, dict)
            and isinstance(item.get("command"), str)
            and item["command"] != desired_command
            and item["command"].endswith("/agents-owl hook claude")
        )
    ]
    if len(retained) == len(hooks):
        return group
    if not retained:
        return None
    return {**group, "hooks": retained}


def _atomic_json_write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_name = handle.name
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    finally:
        if temporary_name and os.path.exists(temporary_name):
            os.unlink(temporary_name)
