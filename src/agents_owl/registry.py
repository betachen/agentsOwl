"""Persistent index for native Codex and Claude sessions."""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import fcntl


SCHEMA_VERSION = 1
PROVIDERS = {"codex", "claude"}
ROLES = {"worker", "peer"}
LIFECYCLES = {"active", "completed", "archived", "missing"}


class RegistryError(RuntimeError):
    """Raised when the local session index is invalid or ambiguous."""


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def session_key(provider: str, native_session_id: str) -> str:
    return f"{provider}:{native_session_id}"


@dataclass
class SessionRecord:
    provider: str
    native_session_id: str
    native_name: str
    topic: str
    role: str
    repo: str
    lifecycle: str = "active"
    runtime_socket: str | None = None
    related_session_keys: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    completed_at: str | None = None
    outcome: str = ""
    git_refs: list[str] = field(default_factory=list)
    native_archived: bool = False
    archived_from: str | None = None
    transcript_path: str | None = None

    @property
    def key(self) -> str:
        return session_key(self.provider, self.native_session_id)

    def validate(self) -> None:
        if self.provider not in PROVIDERS:
            raise RegistryError(f"unknown provider: {self.provider}")
        if self.role not in ROLES:
            raise RegistryError(f"unknown role: {self.role}")
        if self.lifecycle not in LIFECYCLES:
            raise RegistryError(f"unknown lifecycle: {self.lifecycle}")
        for label, value in (
            ("native_session_id", self.native_session_id),
            ("native_name", self.native_name),
            ("topic", self.topic),
            ("repo", self.repo),
        ):
            if not isinstance(value, str) or not value.strip():
                raise RegistryError(f"{label} must be a non-empty string")
        if not isinstance(self.related_session_keys, list) or not all(
            isinstance(item, str) for item in self.related_session_keys
        ):
            raise RegistryError("related_session_keys must be a list of strings")
        if self.runtime_socket is not None and not isinstance(self.runtime_socket, str):
            raise RegistryError("runtime_socket must be a string or null")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SessionRecord":
        fields = cls.__dataclass_fields__
        filtered = {key: item for key, item in value.items() if key in fields}
        try:
            record = cls(**filtered)
        except TypeError as exc:
            raise RegistryError(f"invalid session record: {exc}") from exc
        record.validate()
        return record


class SessionRegistry:
    """An atomically written, human-readable session registry."""

    def __init__(self, state_home: Path):
        self.path = state_home / "sessions" / "index.json"
        self.lock_path = self.path.with_name("index.lock")

    def load(self) -> list[SessionRecord]:
        with self._locked(exclusive=False):
            return self._load_unlocked()

    def _load_unlocked(self) -> list[SessionRecord]:
        if not self.path.exists():
            return []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RegistryError(f"invalid session registry {self.path}: {exc}") from exc
        if not isinstance(raw, dict) or raw.get("schema_version") != SCHEMA_VERSION:
            raise RegistryError(f"unsupported session registry schema: {self.path}")
        sessions = raw.get("sessions")
        if not isinstance(sessions, list) or not all(isinstance(item, dict) for item in sessions):
            raise RegistryError(f"sessions must be a list of objects: {self.path}")
        records = [SessionRecord.from_dict(item) for item in sessions]
        keys = [record.key for record in records]
        if len(keys) != len(set(keys)):
            raise RegistryError(f"duplicate native session identities: {self.path}")
        return records

    def save(self, records: Iterable[SessionRecord]) -> None:
        with self._locked(exclusive=True):
            self._save_unlocked(records)

    def _save_unlocked(self, records: Iterable[SessionRecord]) -> None:
        values = sorted(records, key=lambda record: (record.created_at, record.key))
        payload = {
            "schema_version": SCHEMA_VERSION,
            "updated_at": now_iso(),
            "sessions": [record.to_dict() for record in values],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=".index.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary_name = handle.name
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, self.path)
        finally:
            if temporary_name and os.path.exists(temporary_name):
                os.unlink(temporary_name)

    def upsert(self, record: SessionRecord) -> SessionRecord:
        record.validate()
        with self._locked(exclusive=True):
            records = self._load_unlocked()
            for index, current in enumerate(records):
                if current.key == record.key:
                    record.created_at = current.created_at
                    records[index] = record
                    break
            else:
                records.append(record)
            self._link_topic(records, record)
            self._save_unlocked(records)
        return record

    def update(self, key: str, **changes: Any) -> SessionRecord:
        with self._locked(exclusive=True):
            records = self._load_unlocked()
            for index, record in enumerate(records):
                if record.key != key:
                    continue
                for name, value in changes.items():
                    if name not in record.__dataclass_fields__:
                        raise RegistryError(f"unknown session field: {name}")
                    setattr(record, name, value)
                record.updated_at = now_iso()
                record.validate()
                records[index] = record
                self._link_topic(records, record)
                self._save_unlocked(records)
                return record
        raise RegistryError(f"session not found: {key}")

    def find(
        self,
        *,
        repo: Path | None = None,
        provider: str | None = None,
        role: str | None = None,
        include_archived: bool = False,
    ) -> list[SessionRecord]:
        records = self.load()
        if repo is not None:
            resolved_repo = str(repo.resolve())
            records = [record for record in records if str(Path(record.repo).resolve()) == resolved_repo]
        if provider is not None:
            records = [record for record in records if record.provider == provider]
        if role is not None:
            records = [record for record in records if record.role == role]
        if not include_archived:
            records = [record for record in records if record.lifecycle != "archived"]
        return sorted(records, key=lambda record: record.updated_at, reverse=True)

    def resolve(
        self,
        selector: str,
        *,
        repo: Path | None = None,
        include_archived: bool = True,
    ) -> SessionRecord:
        candidates = self.find(repo=repo, include_archived=include_archived)
        exact = [
            record
            for record in candidates
            if selector in {record.key, record.native_session_id, record.native_name}
        ]
        if len(exact) == 1:
            return exact[0]
        if len(exact) > 1:
            raise RegistryError(f"ambiguous session selector: {selector}")
        prefix = [
            record
            for record in candidates
            if record.key.startswith(selector) or record.native_session_id.startswith(selector)
        ]
        if len(prefix) == 1:
            return prefix[0]
        if len(prefix) > 1:
            raise RegistryError(f"ambiguous session selector: {selector}")
        raise RegistryError(f"session not found: {selector}")

    @staticmethod
    def _link_topic(records: list[SessionRecord], changed: SessionRecord) -> None:
        peers = [
            record
            for record in records
            if record.key != changed.key
            and record.repo == changed.repo
            and record.topic == changed.topic
        ]
        changed.related_session_keys = sorted({record.key for record in peers})
        for peer in peers:
            peer.related_session_keys = sorted(
                {
                    record.key
                    for record in records
                    if record.key != peer.key
                    and record.repo == peer.repo
                    and record.topic == peer.topic
                }
            )

    @contextmanager
    def _locked(self, *, exclusive: bool):
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+", encoding="utf-8") as handle:
            operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
            fcntl.flock(handle.fileno(), operation)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
