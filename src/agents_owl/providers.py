"""Adapters for provider-owned session APIs and commands."""

from __future__ import annotations

import json
import os
import selectors
import shlex
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, TextIO


class ProviderError(RuntimeError):
    """Raised when a native provider cannot complete a session operation."""


class CodexAppServer:
    """Small synchronous JSONL client for the official Codex app-server API."""

    def __init__(self, command: list[str] | None = None, timeout: float = 20.0):
        self.command = command or shlex.split(os.environ.get("AGENTS_OWL_CODEX_CMD", "codex"))
        self.timeout = timeout
        self.process: subprocess.Popen[str] | None = None
        self.selector: selectors.BaseSelector | None = None
        self.stderr_file: TextIO | None = None
        self.next_id = 1

    def __enter__(self) -> "CodexAppServer":
        try:
            self.stderr_file = tempfile.TemporaryFile(mode="w+", encoding="utf-8")
            self.process = subprocess.Popen(
                [*self.command, "app-server", "--listen", "stdio://"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=self.stderr_file,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            if self.stderr_file is not None:
                self.stderr_file.close()
                self.stderr_file = None
            raise ProviderError(f"could not start Codex command {self.command[0]}: {exc}") from exc
        if self.process.stdout is None or self.process.stdin is None:
            self._close()
            raise ProviderError("could not open Codex app-server stdio")
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.process.stdout, selectors.EVENT_READ)
        try:
            self.request(
                "initialize",
                {
                    "clientInfo": {
                        "name": "agents_owl",
                        "title": "AgentsOwl",
                        "version": "0.4.1",
                    }
                },
            )
            self.notify("initialized", {})
        except Exception:
            self._close()
            raise
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self._close()

    def _close(self) -> None:
        if self.selector is not None:
            self.selector.close()
            self.selector = None
        if self.process is None:
            if self.stderr_file is not None:
                self.stderr_file.close()
                self.stderr_file = None
            return
        if self.process.stdin is not None:
            try:
                self.process.stdin.close()
            except OSError:
                pass
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)
        self.process = None
        if self.stderr_file is not None:
            self.stderr_file.close()
            self.stderr_file = None

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self._write({"method": method, "params": params})

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        request_id = self.next_id
        self.next_id += 1
        self._write({"method": method, "id": request_id, "params": params})
        deadline = time.monotonic() + self.timeout
        while True:
            message = self._read(deadline)
            if message.get("id") != request_id:
                continue
            if "error" in message:
                raise ProviderError(f"Codex {method} failed: {message['error']}")
            result = message.get("result")
            if not isinstance(result, dict):
                raise ProviderError(f"Codex {method} returned an invalid result")
            return result

    def _write(self, message: dict[str, Any]) -> None:
        if self.process is None or self.process.stdin is None:
            raise ProviderError("Codex app-server is not running")
        try:
            self.process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
            self.process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise ProviderError(self._process_error("Codex app-server pipe closed")) from exc

    def _read(self, deadline: float) -> dict[str, Any]:
        if self.process is None or self.process.stdout is None or self.selector is None:
            raise ProviderError("Codex app-server is not running")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ProviderError("Codex app-server request timed out")
        events = self.selector.select(timeout=remaining)
        if not events:
            raise ProviderError("Codex app-server request timed out")
        line = self.process.stdout.readline()
        if not line:
            raise ProviderError(self._process_error("Codex app-server exited"))
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            return self._read(deadline)
        if not isinstance(message, dict):
            return self._read(deadline)
        return message

    def _process_error(self, prefix: str) -> str:
        detail = ""
        if self.stderr_file is not None and self.process is not None and self.process.poll() is not None:
            self.stderr_file.seek(0)
            detail = self.stderr_file.read().strip()
        return f"{prefix}: {detail}" if detail else prefix


class CodexProvider:
    SOURCE_KINDS = [
        "cli",
        "vscode",
        "exec",
        "appServer",
        "subAgent",
        "subAgentReview",
        "subAgentCompact",
        "subAgentThreadSpawn",
        "subAgentOther",
        "unknown",
    ]

    def __init__(self, command: list[str] | None = None):
        self.command = command

    def create(self, repo: Path, name: str) -> dict[str, Any]:
        with CodexAppServer(self.command) as server:
            result = server.request(
                "thread/start",
                {"cwd": str(repo), "serviceName": "agents_owl"},
            )
            thread = self._thread(result, "thread/start")
            thread_id = self._thread_id(thread)
            renamed = server.request("thread/name/set", {"threadId": thread_id, "name": name})
            renamed_thread = renamed.get("thread")
            return renamed_thread if isinstance(renamed_thread, dict) else {**thread, "name": name}

    def rename(self, thread_id: str, name: str) -> dict[str, Any]:
        with CodexAppServer(self.command) as server:
            result = server.request("thread/name/set", {"threadId": thread_id, "name": name})
        thread = result.get("thread")
        return thread if isinstance(thread, dict) else {"id": thread_id, "name": name}

    def archive(self, thread_id: str) -> None:
        with CodexAppServer(self.command) as server:
            server.request("thread/archive", {"threadId": thread_id})

    def unarchive(self, thread_id: str) -> None:
        with CodexAppServer(self.command) as server:
            server.request("thread/unarchive", {"threadId": thread_id})

    def read(self, thread_id: str) -> dict[str, Any]:
        with CodexAppServer(self.command) as server:
            result = server.request("thread/read", {"threadId": thread_id, "includeTurns": False})
        return self._thread(result, "thread/read")

    def list(self, repo: Path | None = None, archived: bool = False) -> list[dict[str, Any]]:
        threads: list[dict[str, Any]] = []
        cursor: str | None = None
        with CodexAppServer(self.command) as server:
            while True:
                params: dict[str, Any] = {
                    "archived": archived,
                    "limit": 100,
                    "sortKey": "updated_at",
                    "sortDirection": "desc",
                    "sourceKinds": self.SOURCE_KINDS,
                }
                if repo is not None:
                    params["cwd"] = str(repo)
                if cursor is not None:
                    params["cursor"] = cursor
                result = server.request("thread/list", params)
                data = result.get("data")
                if not isinstance(data, list):
                    raise ProviderError("Codex thread/list returned invalid data")
                threads.extend(item for item in data if isinstance(item, dict))
                next_cursor = result.get("nextCursor")
                if not isinstance(next_cursor, str) or not next_cursor:
                    break
                cursor = next_cursor
        return threads

    def resume_command(self, native_session_id: str) -> list[str]:
        command = self.command or shlex.split(os.environ.get("AGENTS_OWL_CODEX_CMD", "codex"))
        return [*command, "resume", native_session_id]

    @staticmethod
    def _thread(result: dict[str, Any], method: str) -> dict[str, Any]:
        thread = result.get("thread")
        if not isinstance(thread, dict):
            raise ProviderError(f"Codex {method} did not return a thread")
        return thread

    @staticmethod
    def _thread_id(thread: dict[str, Any]) -> str:
        thread_id = thread.get("id")
        if not isinstance(thread_id, str) or not thread_id:
            raise ProviderError("Codex returned a thread without an id")
        return thread_id


def claude_command() -> list[str]:
    return shlex.split(os.environ.get("AGENTS_OWL_CLAUDE_CMD", "claude"))


def claude_new_command(name: str) -> list[str]:
    return [*claude_command(), "--name", name]


def claude_resume_command(native_session_id: str) -> list[str]:
    return [*claude_command(), "--resume", native_session_id]
