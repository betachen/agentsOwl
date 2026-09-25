#!/usr/bin/env python3
"""Terminal-first controller for optional worker/peer collaboration."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import signal
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from string import Template
from typing import Any

from .providers import ProviderError
from .registry import RegistryError, SessionRecord
from .session_manager import (
    SessionManager,
    SessionManagerError,
    _atomic_json_write,
    attach_runtime,
    handle_claude_hook,
    inject_runtime,
    install_claude_hooks,
    launch_runtime,
    make_runtime_socket,
    runtime_lifecycle,
    runtime_state,
)


PAIR_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
ROLE_ALIASES = {
    "worker": "worker",
    "impl": "worker",
    "implementer": "worker",
    "peer": "peer",
    "review": "peer",
    "reviewer": "peer",
}
KIND_ALIASES = {
    "worker": "worker-handoff",
    "worker-handoff": "worker-handoff",
    "implementer": "worker-handoff",
    "implementer-done": "worker-handoff",
    "peer": "peer-response",
    "peer-response": "peer-response",
    "review": "peer-response",
    "reviewer": "peer-response",
    "reviewer-done": "peer-response",
}
DECISIONS = {"accept", "revise", "reject", "fork", "skip-peer"}


@dataclass(frozen=True)
class PairRuntimeRecord:
    """A fixed pair runtime exposed alongside indexed topic sessions."""

    pair: str
    role: str
    repo: Path
    runtime_socket: str

    @property
    def provider(self) -> str:
        return "claude" if self.role == "worker" else "codex"

    @property
    def native_name(self) -> str:
        return session_name(self.pair, self.role)

    @property
    def state(self) -> str:
        return runtime_state(self.runtime_socket)


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def artifact_stamp() -> str:
    return datetime.now().strftime("%Y%m%dT%H%M%S.%f")


def validate_pair(pair: str) -> str:
    if not PAIR_PATTERN.fullmatch(pair):
        raise SystemExit("error: pair must match [A-Za-z0-9_-]+")
    return pair


def resolve_repo(value: str | None) -> Path:
    configured = value or os.environ.get("AGENTS_OWL_REPO")
    if configured:
        repo = Path(configured).expanduser().resolve()
    else:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        repo = Path(result.stdout.strip()).resolve() if result.returncode == 0 else Path.cwd().resolve()
    if not repo.is_dir():
        raise SystemExit(f"error: repository path does not exist: {repo}")
    return repo


def resolve_state_home(value: str | None) -> Path:
    configured = value or os.environ.get("AGENTS_OWL_STATE_HOME")
    if configured:
        return Path(configured).expanduser().resolve()
    xdg_state = os.environ.get("XDG_STATE_HOME")
    if xdg_state:
        return Path(xdg_state).expanduser().resolve() / "agents-owl"
    return Path.home() / ".local" / "state" / "agents-owl"


def read_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise SystemExit(f"error: invalid {label} {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise SystemExit(f"error: {label} must be a JSON object: {path}")
    return raw


def project_config(repo: Path) -> dict[str, Any]:
    path = repo / ".agents-owl.json"
    if not path.exists():
        defaults = [name for name in ("AGENTS.md", "CLAUDE.md", "CONTRIBUTING.md") if (repo / name).is_file()]
        return {"policy_files": defaults}
    raw = read_json_object(path, "project config")
    policy_files = raw.get("policy_files", [])
    if not isinstance(policy_files, list) or not all(isinstance(item, str) for item in policy_files):
        raise SystemExit("error: policy_files must be a list of strings")
    for item in policy_files:
        candidate = (repo / item).resolve()
        if not candidate.is_relative_to(repo) or not candidate.is_file():
            raise SystemExit(f"error: policy file is missing or outside repository: {item}")
    note = raw.get("collaboration_note", "")
    if not isinstance(note, str):
        raise SystemExit("error: collaboration_note must be a string")
    return {"policy_files": policy_files, "collaboration_note": note}


def pair_root(state_home: Path, pair: str) -> Path:
    return state_home / "pairs" / validate_pair(pair)


def initialize_pair(repo: Path, state_home: Path, pair: str) -> Path:
    root = pair_root(state_home, pair)
    existing_path = root / "pair.json"
    if existing_path.is_file():
        existing = read_json_object(existing_path, "pair metadata")
        repo_value = existing.get("repo")
        if not isinstance(repo_value, str):
            raise SystemExit(f"error: pair metadata repo must be a string: {existing_path}")
        existing_repo = Path(repo_value).resolve()
        if existing_repo != repo:
            raise SystemExit(
                f"error: pair {pair!r} already belongs to {existing_repo}; choose another pair name"
            )
    for name in ("inbox", "artifacts"):
        (root / name).mkdir(parents=True, exist_ok=True)
    (root / "events.jsonl").touch(exist_ok=True)
    decisions = root / "decisions.md"
    if not decisions.exists():
        decisions.write_text(
            f"# AgentsOwl coordination decisions: {pair}\n\n"
            "These notes are local workflow records, not project governance authority.\n",
            encoding="utf-8",
        )
    config = project_config(repo)
    metadata = {
        "pair": pair,
        "repo": str(repo),
        "policy_files": config.get("policy_files", []),
        "collaboration_note": config.get("collaboration_note", ""),
        "updated_at": now_iso(),
    }
    existing_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return root


def require_pair(repo: Path, state_home: Path, pair: str) -> tuple[Path, dict[str, Any]]:
    root = pair_root(state_home, pair)
    metadata_path = root / "pair.json"
    if not metadata_path.is_file():
        raise SystemExit(f"error: pair is not initialized; run: agents-owl init {pair}")
    metadata = read_json_object(metadata_path, "pair metadata")
    repo_value = metadata.get("repo")
    if not isinstance(repo_value, str):
        raise SystemExit(f"error: pair metadata repo must be a string: {metadata_path}")
    recorded_repo = Path(repo_value).resolve()
    if recorded_repo != repo:
        raise SystemExit(f"error: pair {pair!r} belongs to {recorded_repo}, not {repo}")
    return root, metadata


def initialized_pairs(state_home: Path, repo: Path | None = None) -> list[tuple[str, Path]]:
    """Return initialized fixed pairs, optionally limited to one repository."""

    pairs_dir = state_home / "pairs"
    if not pairs_dir.is_dir():
        return []
    expected_repo = repo.resolve() if repo is not None else None
    pairs: list[tuple[str, Path]] = []
    for metadata_path in sorted(pairs_dir.glob("*/pair.json")):
        metadata = read_json_object(metadata_path, "pair metadata")
        pair = metadata.get("pair")
        repo_value = metadata.get("repo")
        if not isinstance(pair, str) or pair != metadata_path.parent.name:
            raise SystemExit(f"error: invalid pair name in metadata: {metadata_path}")
        validate_pair(pair)
        if not isinstance(repo_value, str):
            raise SystemExit(f"error: pair metadata repo must be a string: {metadata_path}")
        pair_repo = Path(repo_value).expanduser().resolve()
        if expected_repo is None or pair_repo == expected_repo:
            pairs.append((pair, pair_repo))
    return pairs


def pair_runtime_records(
    state_home: Path,
    *,
    repo: Path | None,
    provider: str | None,
    role: str | None,
) -> list[PairRuntimeRecord]:
    records: list[PairRuntimeRecord] = []
    roles = (role,) if role else ("worker", "peer")
    for pair, pair_repo in initialized_pairs(state_home, repo):
        for selected_role in roles:
            record = PairRuntimeRecord(
                pair=pair,
                role=selected_role,
                repo=pair_repo,
                runtime_socket=str(pair_runtime_socket(state_home, pair_repo, pair, selected_role)),
            )
            if provider is None or record.provider == provider:
                records.append(record)
    return records


def choose_pair(repo: Path, state_home: Path) -> str:
    pairs = initialized_pairs(state_home, repo)
    if not pairs:
        raise SessionManagerError(f"no initialized pairs for {repo}; run: agents-owl init PAIR")
    print(f"{'#':>3}  {'pair':<28} {'worker':<10} peer")
    for index, (pair, pair_repo) in enumerate(pairs, 1):
        worker = runtime_state(str(pair_runtime_socket(state_home, pair_repo, pair, "worker")))
        peer = runtime_state(str(pair_runtime_socket(state_home, pair_repo, pair, "peer")))
        print(f"{index:>3}  {pair:<28} {worker:<10} {peer}")
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise SessionManagerError("a pair selector is required outside an interactive terminal")
    choice = input(f"Select pair [1-{len(pairs)}, q]: ").strip()
    if choice.lower() == "q":
        raise SystemExit(0)
    try:
        index = int(choice)
    except ValueError as exc:
        raise SessionManagerError(f"invalid selection: {choice}") from exc
    if index < 1 or index > len(pairs):
        raise SessionManagerError(f"selection out of range: {index}")
    return pairs[index - 1][0]


def append_event(root: Path, pair: str, event: str, **data: object) -> None:
    record = {"ts": now_iso(), "pair": pair, "event": event, **data}
    with (root / "events.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def unique_artifact(root: Path, kind: str) -> Path:
    return root / "artifacts" / f"{artifact_stamp()}-{kind}.md"


def archive_inbox(root: Path, pair: str, kind: str) -> Path:
    inbox = root / "inbox" / f"{kind}.md"
    if not inbox.is_file():
        raise SystemExit(f"error: missing inbox artifact: {inbox}")
    artifact = unique_artifact(root, kind)
    digest = sha256(inbox)
    shutil.move(str(inbox), artifact)
    append_event(root, pair, f"archive-{kind}", artifact=str(artifact), sha256=digest)
    return artifact


def template_path(name: str) -> Path:
    root = Path(__file__).resolve().parents[2]
    path = root / "templates" / name
    if not path.is_file():
        raise SystemExit(f"error: missing template: {path}")
    return path


def template(name: str) -> Template:
    return Template(template_path(name).read_text(encoding="utf-8"))


def policy_text(repo: Path, metadata: dict[str, Any]) -> str:
    files = metadata.get("policy_files", [])
    lines = [f"- {repo / item}" for item in files] or [
        "- No policy files configured; discover repository instructions before acting."
    ]
    note = metadata.get("collaboration_note", "").strip()
    if note:
        lines.extend(("", "Project collaboration note:", note))
    return "\n".join(lines)


def session_name(pair: str, role: str) -> str:
    return f"owl-{validate_pair(pair)}-{ROLE_ALIASES[role]}"


def pair_runtime_socket(state_home: Path, repo: Path, pair: str, role: str) -> Path:
    normalized_role = ROLE_ALIASES[role]
    return make_runtime_socket(
        state_home,
        repo,
        pair,
        normalized_role,
        discriminator=f"pair:{validate_pair(pair)}:{normalized_role}",
    )


def pair_native_session_id(state_home: Path, pair: str, role: str) -> str | None:
    path = pair_root(state_home, pair) / "native-sessions.json"
    if not path.is_file():
        return None
    bindings = read_json_object(path, "native session bindings")
    value = bindings.get(ROLE_ALIASES[role])
    if not isinstance(value, dict):
        return None
    native_id = value.get("native_session_id")
    return native_id if isinstance(native_id, str) and native_id else None


def require_runtime_target(target: str) -> None:
    if runtime_state(target) != "running":
        raise SystemExit(f"error: protected runtime is not running: {target}")


def _proc_cmdline(pid: int) -> list[str]:
    try:
        values = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    except (OSError, ValueError):
        return []
    return [os.fsdecode(value) for value in values if value]


def _proc_ppid(pid: int) -> int | None:
    try:
        for line in Path(f"/proc/{pid}/status").read_text(encoding="utf-8").splitlines():
            if line.startswith("PPid:"):
                return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


def _runtime_codex_pid(runtime_socket: str) -> int | None:
    """Find the Codex process owned by one of our dtach servers."""

    server_pid: int | None = None
    for entry in Path("/proc").glob("[0-9]*"):
        try:
            pid = int(entry.name)
        except ValueError:
            continue
        command = _proc_cmdline(pid)
        if not command or Path(command[0]).name != "dtach":
            continue
        if "-c" not in command:
            continue
        try:
            socket_index = command.index("-c") + 1
        except ValueError:
            continue
        if socket_index < len(command) and command[socket_index] == runtime_socket:
            server_pid = pid
            break
    if server_pid is None:
        return None

    pending = [server_pid]
    while pending:
        parent = pending.pop()
        for entry in Path("/proc").glob("[0-9]*"):
            try:
                pid = int(entry.name)
            except ValueError:
                continue
            if _proc_ppid(pid) != parent:
                continue
            command = _proc_cmdline(pid)
            if command and Path(command[0]).name == "codex":
                return pid
            pending.append(pid)
    return None


def replay_idle_codex_runtime(runtime_socket: str) -> bool:
    """Stop an idle Codex child so the same session can redraw on resume.

    Sending ``/quit`` through dtach is unreliable when Codex is in its transcript
    pager or alternate screen. Killing only the idle Codex child leaves the
    rollout intact; the dtach shell then exits and can be relaunched normally.
    """

    pid = _runtime_codex_pid(runtime_socket)
    if pid is None:
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return False
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if runtime_state(runtime_socket) != "running":
            return True
        time.sleep(0.05)
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if runtime_state(runtime_socket) != "running":
            return True
        time.sleep(0.05)
    return runtime_state(runtime_socket) != "running"


def codex_runtime_ready(runtime_socket: str, native_session_id: str) -> bool:
    """Return whether Codex has written startup state for this runtime."""

    pid = _runtime_codex_pid(runtime_socket)
    if pid is None:
        return False
    try:
        started_ns = Path(f"/proc/{pid}").stat().st_ctime_ns
    except OSError:
        return False
    codex_home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()
    sessions = codex_home / "sessions"
    try:
        paths = list(sessions.rglob(f"*{native_session_id}.jsonl"))
        return any(path.stat().st_mtime_ns >= started_ns for path in paths)
    except OSError:
        return False


def wait_for_codex_runtime(runtime_socket: str, native_session_id: str, timeout: float = 45.0) -> bool:
    """Wait for a resumed Codex TUI to finish its startup handshake."""

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if runtime_state(runtime_socket) != "running":
            return False
        if codex_runtime_ready(runtime_socket, native_session_id):
            return True
        time.sleep(0.1)
    return codex_runtime_ready(runtime_socket, native_session_id)


def inject_prompt(target: str, prompt: str) -> None:
    try:
        inject_runtime(target, prompt)
    except SessionManagerError as exc:
        raise SystemExit(f"error: {exc}") from exc


def queue_codex_prompt(native_session_id: str, prompt: str) -> bool:
    """Queue a prompt through Codex's native session transport when available."""

    executable = shutil.which("codex")
    if not executable:
        return False
    try:
        result = subprocess.run(
            [executable, "queue", "--thread", native_session_id, "--message", prompt],
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            check=False,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def latest_artifacts(root: Path, kind: str | None = None, limit: int = 5) -> list[Path]:
    files = [path for path in (root / "artifacts").glob("*.md") if path.is_file()]
    if kind:
        files = [path for path in files if path.name.endswith(f"-{kind}.md")]
    return sorted(files, key=lambda path: path.stat().st_mtime_ns, reverse=True)[:limit]


def describe(path: Path) -> str:
    if not path.exists():
        return "missing"
    stat = path.stat()
    return f"file, {stat.st_size} bytes, mtime {datetime.fromtimestamp(stat.st_mtime).isoformat(timespec='seconds')}"


def context(args: argparse.Namespace) -> tuple[Path, Path]:
    return resolve_repo(args.repo), resolve_state_home(args.state_home)


def command_init(args: argparse.Namespace) -> None:
    repo, state_home = context(args)
    root = initialize_pair(repo, state_home, args.pair)
    append_event(root, args.pair, "init", repo=str(repo))
    print(f"initialized {root}")


def command_status(args: argparse.Namespace) -> None:
    repo, state_home = context(args)
    pair = args.pair or choose_pair(repo, state_home)
    root, metadata = require_pair(repo, state_home, pair)
    print(f"pair: {pair}")
    print(f"repo: {repo}")
    print(f"state: {root}")
    print("policy files:")
    for item in metadata.get("policy_files", []):
        print(f"  {item}")
    if not metadata.get("policy_files"):
        print("  none configured")
    print("\ninbox:")
    for kind in ("worker-handoff", "peer-response"):
        path = root / "inbox" / f"{kind}.md"
        print(f"  {path}: {describe(path)}")
    print("\nlatest artifacts:")
    files = latest_artifacts(root)
    for path in files:
        print(f"  {path}")
    if not files:
        print("  none")
    print("\nprotected runtimes:")
    for role in ("worker", "peer"):
        runtime_socket = pair_runtime_socket(state_home, repo, pair, role)
        print(f"  {role}: {runtime_state(str(runtime_socket))} ({runtime_socket})")
    print(f"\nevents: {root / 'events.jsonl'}")
    print(f"decisions: {root / 'decisions.md'}")


def command_session(args: argparse.Namespace) -> None:
    repo, state_home = context(args)
    root = initialize_pair(repo, state_home, args.pair)
    role = ROLE_ALIASES[args.role]
    session = session_name(args.pair, role)
    runtime_socket = pair_runtime_socket(state_home, repo, args.pair, role)
    if runtime_state(str(runtime_socket)) == "running":
        attach_runtime(str(runtime_socket))
        return
    default_cmd = "claude" if role == "worker" else "codex"
    legacy_env = "IMPLEMENTER_CMD" if role == "worker" else "REVIEWER_CMD"
    owl_env = "AGENTS_OWL_WORKER_CMD" if role == "worker" else "AGENTS_OWL_PEER_CMD"
    agent_cmd = args.command or os.environ.get(owl_env) or os.environ.get(legacy_env) or default_cmd
    extra, native_id, resumed = native_session_args(
        root / "native-sessions.json", role, repo, session, agent_cmd,
        fresh=getattr(args, "fresh", False),
    )
    recent_context = bool(getattr(args, "recent_context", False) and resumed and native_id)
    try:
        agent_tokens = shlex.split(agent_cmd)
    except ValueError:
        agent_tokens = []
    provider = Path(agent_tokens[0]).name if agent_tokens else ""
    recent_preview = recent_codex_context(native_id) if recent_context and provider == "codex" else ""
    if recent_context and provider == "codex" and "--no-alt-screen" not in agent_tokens and "--no-alt-screen" not in extra:
        extra = [*extra, "--no-alt-screen"]
    if extra:
        agent_cmd = f"{agent_cmd} {shlex.join(extra)}"
    output_name = "worker-handoff.md" if role == "worker" else "peer-response.md"
    output_path = root / "inbox" / output_name
    role_template = template_path(output_name)
    recent_startup = f"printf '%s\\n' {shlex.quote(recent_preview)}; " if recent_preview else ""
    startup = (
        "printf '\\n=== AgentsOwl ===\\n'; "
        f"printf 'pair: %s\\n' {shlex.quote(args.pair)}; "
        f"printf 'role: %s\\n' {shlex.quote(role)}; "
        f"printf 'repo: %s\\n' {shlex.quote(str(repo))}; "
        f"printf 'session: %s\\n' {shlex.quote(session)}; "
        f"printf 'deliverable: %s\\n' {shlex.quote(str(output_path))}; "
        f"printf 'handoff template: %s\\n\\n' {shlex.quote(str(role_template))}; "
        f"{recent_startup}"
        f"exec {agent_cmd}"
    )
    event: dict[str, object] = {"role": role, "session": session, "command": agent_cmd}
    if native_id:
        event.update(native_session_id=native_id, resumed=resumed)
    append_event(root, args.pair, "session-start", **event)
    launch_runtime(runtime_socket, repo, ["/bin/sh", "-lc", startup], {})


CLAUDE_SESSION_FLAGS = {"-r", "--resume", "-c", "--continue", "--session-id", "--fork-session"}


def claude_transcript_exists(native_session_id: str) -> bool:
    config_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude").expanduser()
    return any((config_dir / "projects").glob(f"*/{native_session_id}.jsonl"))


def codex_rollout_exists(native_session_id: str) -> bool:
    """Return whether Codex has a resumable rollout for a native thread."""

    codex_home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()
    sessions = codex_home / "sessions"
    if not sessions.is_dir():
        return False
    return any(
        _codex_rollout_info(path, require_turn=True)[0] == native_session_id
        for path in sessions.rglob(f"*{native_session_id}.jsonl")
    )


def codex_rollout_is_idle(native_session_id: str) -> bool:
    """Return whether the latest recorded Codex turn has completed.

    Resuming a session appends non-turn events such as
    ``thread_settings_applied``. Those must not make an otherwise idle rollout
    look active, so only task lifecycle events update the state.
    """

    codex_home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()
    sessions = codex_home / "sessions"
    try:
        paths = sorted(
            sessions.rglob(f"*{native_session_id}.jsonl"),
            key=lambda item: item.stat().st_mtime_ns,
        )
    except OSError:
        return False
    if not paths:
        return False
    last_turn_state: str | None = None
    try:
        with paths[-1].open(encoding="utf-8") as handle:
            for line in handle:
                record = json.loads(line)
                payload = record.get("payload") if isinstance(record, dict) else None
                if not isinstance(payload, dict):
                    continue
                event_type = payload.get("type")
                if event_type == "task_started":
                    last_turn_state = "active"
                elif event_type in {"task_complete", "turn_aborted"}:
                    last_turn_state = "idle"
    except (OSError, json.JSONDecodeError):
        return False
    return last_turn_state == "idle"


def _codex_rollout_info(path: Path, *, require_turn: bool = False) -> tuple[str | None, str | None]:
    try:
        with path.open(encoding="utf-8") as handle:
            first = json.loads(handle.readline())
            has_turn = any(line.strip() for line in handle) if require_turn else True
        payload = first.get("payload") if isinstance(first, dict) else None
        cwd = payload.get("cwd") if isinstance(payload, dict) else None
        native_id = payload.get("session_id") if isinstance(payload, dict) else None
    except (OSError, json.JSONDecodeError):
        return None, None
    if require_turn and not has_turn:
        return None, None
    if not isinstance(native_id, str) or not native_id:
        return None, None
    return native_id, cwd if isinstance(cwd, str) else None


def codex_latest_rollout_id(repo: Path) -> str | None:
    """Find the newest recorded Codex session whose cwd is ``repo``."""

    codex_home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()
    sessions = codex_home / "sessions"
    if not sessions.is_dir():
        return None
    candidates: list[tuple[float, Path]] = []
    for path in sessions.rglob("rollout-*.jsonl"):
        try:
            candidates.append((path.stat().st_mtime, path))
        except OSError:
            continue
    for _, path in sorted(candidates, reverse=True):
        native_id, cwd = _codex_rollout_info(path, require_turn=True)
        if isinstance(cwd, str) and Path(cwd).expanduser().resolve() == repo.resolve():
            if isinstance(native_id, str) and native_id:
                return native_id
    return None


def native_session_owned_elsewhere(path: Path, provider: str, native_session_id: str) -> bool:
    """Return whether a pair binding already owns ``native_session_id``.

    Codex does not let the plain interactive command choose a new thread ID
    before its first turn.  AgentsOwl therefore discovers that ID on a later
    invocation by looking at the newest rollout in the repository.  Never
    adopt a rollout that another pair already owns: doing so would make two
    independent pair roles resume the same provider conversation.
    """

    pairs_dir = path.parent.parent
    if pairs_dir.name != "pairs" or not pairs_dir.is_dir():
        return False
    try:
        current = path.resolve()
    except OSError:
        current = path
    for candidate in pairs_dir.glob("*/native-sessions.json"):
        try:
            if candidate.resolve() == current:
                continue
            payload = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        for binding in payload.values():
            if not isinstance(binding, dict):
                continue
            if (
                binding.get("provider") == provider
                and binding.get("native_session_id") == native_session_id
            ):
                return True
    return False


def _codex_rollout_paths(native_session_id: str) -> list[Path]:
    codex_home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()
    sessions = codex_home / "sessions"
    if not sessions.is_dir():
        return []
    try:
        return sorted(
            (path for path in sessions.rglob(f"*{native_session_id}.jsonl") if path.is_file()),
            key=lambda path: path.stat().st_mtime_ns,
        )
    except OSError:
        return []


def _codex_message_text(payload: dict[str, Any]) -> str:
    content = payload.get("content")
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type not in {"input_text", "output_text", "text"}:
            continue
        text = item.get("text")
        if isinstance(text, str) and text.strip():
            parts.append(text.strip())
    return "\n".join(parts)


def _trim_recent_text(text: str, limit: int = 1800) -> str:
    if len(text) <= limit:
        return text
    half = (limit - 80) // 2
    return f"{text[:half]}\n… [truncated] …\n{text[-half:]}"


def _tail_jsonl_lines(path: Path, max_bytes: int = 4 * 1024 * 1024) -> list[str]:
    """Read only a bounded tail of a JSONL transcript."""

    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            offset = max(0, size - max_bytes)
            handle.seek(offset)
            data = handle.read()
    except OSError:
        return []
    if offset:
        _, _, data = data.partition(b"\n")
    return data.decode("utf-8", errors="replace").splitlines()


def recent_codex_context(native_session_id: str, turns: int = 4) -> str:
    """Format a small recent-turn preview without changing native context."""

    if turns < 1:
        return ""
    conversations: list[dict[str, Any]] = []
    paths = _codex_rollout_paths(native_session_id)
    for line in _tail_jsonl_lines(paths[-1]) if paths else []:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        payload = record.get("payload") if isinstance(record, dict) else None
        if not isinstance(payload, dict) or payload.get("type") != "message":
            continue
        role = payload.get("role")
        if role not in {"user", "assistant"}:
            continue
        text = _codex_message_text(payload)
        if not text:
            continue
        if role == "user":
            conversations.append({"user": text, "assistant": []})
        elif conversations:
            conversations[-1]["assistant"].append(text)
    if not conversations:
        return ""
    lines = ["=== Recent Codex context (native history remains complete) ==="]
    for index, conversation in enumerate(conversations[-turns:], 1):
        lines.append(f"\n[Recent turn {index}] User:")
        lines.append(_trim_recent_text(str(conversation["user"])))
        replies = conversation["assistant"]
        lines.append("Assistant:")
        lines.append(_trim_recent_text("\n".join(replies)) if replies else "(no completed reply yet)")
    lines.append("\n=== End recent context; Ctrl+T opens the full transcript ===")
    return "\n".join(lines)


def native_session_args(
    path: Path, key: str, repo: Path, native_name: str, agent_cmd: str, *, fresh: bool = False
) -> tuple[list[str], str | None, bool]:
    """Bind one pair role or solo slot to a provider-native session across restarts.

    Returns the arguments to append to ``agent_cmd``, the native session id
    (None for custom commands) and whether an existing conversation is resumed.
    """
    try:
        tokens = shlex.split(agent_cmd)
    except ValueError:
        return [], None, False
    provider = Path(tokens[0]).name if tokens else ""
    # Custom commands and explicit session selection (e.g. `claude --resume X`,
    # `codex resume X`, `codex exec`) stay untouched.
    if provider == "claude":
        supported = CLAUDE_SESSION_FLAGS.isdisjoint(tokens)
    elif provider == "codex":
        supported = all(token.startswith("-") or "=" in token for token in tokens[1:])
    else:
        supported = False
    if not supported:
        return [], None, False
    sessions = read_json_object(path, "native session bindings") if path.is_file() else {}
    stored = sessions.get(key)
    native_id = None
    if not fresh and isinstance(stored, dict) and stored.get("provider") == provider:
        value = stored.get("native_session_id")
        native_id = value if isinstance(value, str) and value else None
    if native_id and native_session_owned_elsewhere(path, provider, native_id):
        native_id = None
    if provider == "claude":
        resumed = native_id is not None and claude_transcript_exists(native_id)
        native_id = native_id or str(uuid.uuid4())
        extra = ["--resume", native_id] if resumed else ["--session-id", native_id, "--name", native_name]
    else:
        resumed = native_id is not None and codex_rollout_exists(native_id)
        if not fresh and not resumed:
            candidate = codex_latest_rollout_id(repo)
            if candidate and not native_session_owned_elsewhere(path, provider, candidate):
                native_id = candidate
                resumed = True
        # A brand-new Codex TUI must create its own rollout. An app-server
        # thread/start ID cannot be resumed until it has a first turn.
        extra = ["resume", native_id] if resumed else []
    if native_id is None:
        if key in sessions:
            sessions.pop(key)
            _atomic_json_write(path, sessions)
    elif not isinstance(stored, dict) or stored.get("native_session_id") != native_id:
        sessions[key] = {"provider": provider, "native_session_id": native_id, "created_at": now_iso()}
        _atomic_json_write(path, sessions)
    return extra, native_id, resumed


def command_solo(args: argparse.Namespace) -> None:
    # Solo follows the exact working directory, not the Git root or an inherited
    # AGENTS_OWL_REPO from a worker/peer session. An explicit --repo still wins.
    repo = Path(args.repo).expanduser().resolve() if args.repo else Path.cwd().resolve()
    if not repo.is_dir():
        raise SessionManagerError(f"working directory does not exist: {repo}")
    state_home = resolve_state_home(args.state_home)
    name = args.name.strip()
    if not name:
        raise SessionManagerError("solo session name must not be empty")
    runtime_socket = make_runtime_socket(
        state_home, repo, name, "solo", discriminator=f"solo:{args.provider}:{name}"
    )
    if runtime_state(str(runtime_socket)) == "running":
        print(f"attaching solo {args.provider} ({name}) in {repo}", flush=True)
        if args.provider == "codex":
            print("Full transcript: press Ctrl+T after attach; press q to return.", flush=True)
        attach_runtime(str(runtime_socket))
        return
    # Same directory + provider + name means the same native conversation.
    bindings = state_home / "solo" / "native-sessions.json"
    extra, _, resumed = native_session_args(
        bindings, f"{repo}::{args.provider}::{name}", repo, f"solo {name}", args.provider,
        fresh=args.fresh,
    )
    print(f"{'resuming' if resumed else 'starting'} solo {args.provider} ({name}) in {repo}", flush=True)
    print("Ctrl+\\ to detach; repeat this command to reconnect.", flush=True)
    # Do not inherit managed-session hooks/roles or collaboration context. The
    # normal provider executable handles its own settings and project rules.
    inherited_context = tuple(
        key for key in os.environ
        if key.startswith("AGENTS_OWL_") or key in {"IMPLEMENTER_CMD", "REVIEWER_CMD"}
    )
    launch_runtime(
        runtime_socket, repo, [args.provider, *extra], {"PWD": str(repo)},
        remove_environment=inherited_context,
    )


def command_send_peer(args: argparse.Namespace) -> None:
    repo, state_home = context(args)
    focus = args.focus.strip() if args.focus is not None else None
    if focus == "":
        raise SystemExit("error: --focus must contain a review question or requirement")
    root, metadata = require_pair(repo, state_home, args.pair)
    target = args.target or str(pair_runtime_socket(state_home, repo, args.pair, "peer"))
    require_runtime_target(target)
    native_id = pair_native_session_id(state_home, args.pair, "peer")
    if native_id and not wait_for_codex_runtime(target, native_id):
        raise SystemExit("error: peer Codex runtime is still starting; handoff was not archived")
    handoff = root / "inbox" / "worker-handoff.md"
    if not handoff.is_file():
        raise SystemExit(f"error: missing inbox artifact: {handoff}")
    artifact = archive_inbox(root, args.pair, "worker-handoff")
    prompt = template("peer-prompt.md").safe_substitute(
        pair=args.pair,
        repo=str(repo),
        artifact_path=str(artifact),
        peer_output_path=str(root / "inbox" / "peer-response.md"),
        peer_template_path=str(template_path("peer-response.md")),
        policy_files=policy_text(repo, metadata),
        coordinator_focus=(
            "## Coordinator's review questions and requirements\n\n"
            "These requirements come from this send-peer invocation, separately from the worker handoff.\n"
            "Use them to focus the review; repository policy remains authoritative.\n\n"
            f"{focus}\n\n"
            "## End of coordinator focus\n"
            if focus else ""
        ),
        response_format=(
            "Answer the coordinator's questions first, in their order, with a direct conclusion "
            "and concise reasoning for each. Use the requested language and format.\n"
            "Use the template below as a coverage checklist, not mandatory headings. "
            "Omit irrelevant or empty sections and repetitive governance boilerplate.\n"
            "Keep evidence near the claim it supports; prefer repository-relative paths where "
            "unambiguous, and avoid a separate inventory of every file read.\n"
            "State material uncertainty and anything not checked that affects the answers. "
            "Mention other findings only if they materially affect the requested conclusions.\n"
            "Reference checklist:"
            if focus else "Use the structure in:"
        ),
    )
    if not native_id or not queue_codex_prompt(native_id, prompt):
        inject_prompt(target, prompt)
    append_event(
        root, args.pair, "send-peer", artifact=str(artifact), target=target,
        **({"focus": focus} if focus else {}),
    )
    print(f"sent optional peer request to {target}")
    print(f"archived worker handoff: {artifact}")


def command_send_back(args: argparse.Namespace) -> None:
    repo, state_home = context(args)
    root, metadata = require_pair(repo, state_home, args.pair)
    target = args.target or str(pair_runtime_socket(state_home, repo, args.pair, "worker"))
    require_runtime_target(target)
    artifact = archive_inbox(root, args.pair, "peer-response")
    prompt = template("revision-prompt.md").safe_substitute(
        pair=args.pair,
        repo=str(repo),
        artifact_path=str(artifact),
        worker_output_path=str(root / "inbox" / "worker-handoff.md"),
        worker_template_path=str(template_path("worker-handoff.md")),
        policy_files=policy_text(repo, metadata),
    )
    inject_prompt(target, prompt)
    append_event(root, args.pair, "send-back", artifact=str(artifact), target=target)
    print(f"sent peer feedback to {target}")
    print(f"archived peer response: {artifact}")


def command_archive(args: argparse.Namespace, kind: str) -> None:
    repo, state_home = context(args)
    root, _ = require_pair(repo, state_home, args.pair)
    artifact = archive_inbox(root, args.pair, kind)
    print(f"archived {kind}: {artifact}")


def command_decision(args: argparse.Namespace) -> None:
    repo, state_home = context(args)
    root, _ = require_pair(repo, state_home, args.pair)
    note = args.note.strip() or "(no note)"
    with (root / "decisions.md").open("a", encoding="utf-8") as handle:
        handle.write(f"\n## {now_iso()} — {args.decision.upper()}\n\n{note}\n")
    append_event(root, args.pair, "coordination-decision", decision=args.decision, note=note)
    print(f"recorded local coordination decision: {args.decision.upper()}")
    print("note: this does not change repository governance or approval state")


def command_artifacts(args: argparse.Namespace) -> None:
    repo, state_home = context(args)
    root, _ = require_pair(repo, state_home, args.pair)
    files = sorted((root / "artifacts").glob("*.md"))
    if not files:
        print("no artifacts")
        return
    for path in files:
        print(path)


def command_show_latest(args: argparse.Namespace) -> None:
    repo, state_home = context(args)
    root, _ = require_pair(repo, state_home, args.pair)
    kind = KIND_ALIASES.get(args.kind) if args.kind else None
    files = latest_artifacts(root, kind=kind, limit=1)
    if not files:
        raise SystemExit("error: no matching archived artifact")
    path = files[0]
    print(f"--- {path} ---")
    print(path.read_text(encoding="utf-8"), end="")


def session_manager(args: argparse.Namespace) -> SessionManager:
    repo, state_home = context(args)
    return SessionManager(repo, state_home)


def record_value(record: SessionRecord) -> dict[str, Any]:
    value = record.to_dict()
    value["key"] = record.key
    value["runtime_lifecycle"] = runtime_lifecycle(record)
    return value


def print_session_table(records: list[SessionRecord]) -> None:
    if not records:
        print("no managed sessions")
        return
    print(f"{'#':>3}  {'state':<10} {'provider':<7} {'role':<6} {'topic':<28} name")
    for index, record in enumerate(records, 1):
        topic = record.topic if len(record.topic) <= 28 else f"{record.topic[:27]}…"
        print(
            f"{index:>3}  {runtime_lifecycle(record):<10} {record.provider:<7} "
            f"{record.role:<6} {topic:<28} {record.native_name}"
        )


def print_openable_session_table(
    records: list[SessionRecord], pair_records: list[PairRuntimeRecord]
) -> None:
    if not records and not pair_records:
        print("no sessions")
        return
    print(f"{'#':>3}  {'state':<10} {'kind':<7} {'provider':<7} {'role':<6} {'topic/pair':<28} name")
    index = 1
    for record in records:
        topic = record.topic if len(record.topic) <= 28 else f"{record.topic[:27]}…"
        print(
            f"{index:>3}  {runtime_lifecycle(record):<10} {'topic':<7} {record.provider:<7} "
            f"{record.role:<6} {topic:<28} {record.native_name}"
        )
        index += 1
    for record in pair_records:
        pair = record.pair if len(record.pair) <= 28 else f"{record.pair[:27]}…"
        print(
            f"{index:>3}  {record.state:<10} {'pair':<7} {record.provider:<7} "
            f"{record.role:<6} {pair:<28} {record.native_name}"
        )
        index += 1


def choose_openable_session(
    records: list[SessionRecord], pair_records: list[PairRuntimeRecord]
) -> SessionRecord | PairRuntimeRecord:
    choices: list[SessionRecord | PairRuntimeRecord] = [*records, *pair_records]
    if not choices:
        raise SessionManagerError("no matching sessions")
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise SessionManagerError("a session selector is required outside an interactive terminal")
    choice = input(f"Open [1-{len(choices)}, q]: ").strip()
    if choice.lower() == "q":
        raise SystemExit(0)
    try:
        index = int(choice)
    except ValueError as exc:
        raise SessionManagerError(f"invalid selection: {choice}") from exc
    if index < 1 or index > len(choices):
        raise SessionManagerError(f"selection out of range: {index}")
    return choices[index - 1]


def choose_record(
    records: list[SessionRecord], prompt: str = "Select session", *, show_table: bool = True
) -> SessionRecord:
    if not records:
        raise SessionManagerError("no matching managed sessions")
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise SessionManagerError("a session selector is required outside an interactive terminal")
    if show_table:
        print_session_table(records)
    choice = input(f"{prompt} [1-{len(records)}, q]: ").strip()
    if choice.lower() == "q":
        raise SystemExit(0)
    try:
        index = int(choice)
    except ValueError as exc:
        raise SessionManagerError(f"invalid selection: {choice}") from exc
    if index < 1 or index > len(records):
        raise SessionManagerError(f"selection out of range: {index}")
    return records[index - 1]


def resolve_managed_session(
    manager: SessionManager,
    selector: str | None,
    *,
    include_archived: bool = True,
) -> SessionRecord:
    if selector:
        return manager.registry.resolve(
            selector,
            repo=manager.repo,
            include_archived=include_archived,
        )
    records = manager.registry.find(repo=manager.repo, include_archived=include_archived)
    return choose_record(records)


def command_sessions(args: argparse.Namespace) -> None:
    manager = session_manager(args)
    repo_filter = None if args.global_scope else manager.repo
    records = manager.registry.find(
        repo=repo_filter,
        provider=args.provider,
        role=args.role,
        include_archived=args.all,
    )
    if args.json:
        print(json.dumps([record_value(record) for record in records], ensure_ascii=False, indent=2))
        return
    fixed_pairs = pair_runtime_records(
        manager.state_home,
        repo=repo_filter,
        provider=args.provider,
        role=args.role,
    )
    print_openable_session_table(records, fixed_pairs)
    if args.no_select or (not records and not fixed_pairs) or not (
        sys.stdin.isatty() and sys.stdout.isatty()
    ):
        return
    selected = choose_openable_session(records, fixed_pairs)
    if isinstance(selected, PairRuntimeRecord):
        if selected.state == "running":
            if selected.provider == "codex":
                native_id = pair_native_session_id(manager.state_home, selected.pair, selected.role)
                if native_id and codex_rollout_is_idle(native_id) and replay_idle_codex_runtime(
                    selected.runtime_socket
                ):
                    print("replaying recent Codex transcript", flush=True)
                    command_session(
                        argparse.Namespace(
                            repo=str(selected.repo),
                            state_home=str(manager.state_home),
                            pair=selected.pair,
                            role=selected.role,
                            command=None,
                            fresh=False,
                            recent_context=True,
                        )
                    )
                    return
                # A live runtime is already showing the native Codex TUI.  Do
                # not inject Ctrl+T here: that opens the transcript browser
                # and leaves ff r in a view that requires q to return.
                attach_runtime(selected.runtime_socket)
            else:
                attach_runtime(selected.runtime_socket)
            return
        print(f"opening fixed pair {selected.pair} {selected.role}", flush=True)
        command_session(
            argparse.Namespace(
                repo=str(selected.repo),
                state_home=str(manager.state_home),
                pair=selected.pair,
                role=selected.role,
                command=None,
                fresh=False,
                recent_context=True,
            )
        )
        return
    state = runtime_lifecycle(selected)
    actions = ["attach"] if state == "running" else []
    if selected.lifecycle == "archived":
        actions.extend(["unarchive", "inspect"])
    else:
        if state != "running":
            actions.append("resume")
            actions.extend(["inspect", "finish", "rename", "archive"])
        else:
            actions.extend(["inspect", "rename"])
    actions = list(dict.fromkeys(actions))
    print("  ".join(f"{index}. {action}" for index, action in enumerate(actions, 1)))
    choice = input("Action [number, q]: ").strip()
    if choice.lower() == "q":
        return
    try:
        action_index = int(choice)
    except ValueError as exc:
        raise SessionManagerError(f"invalid action: {choice}") from exc
    if action_index < 1 or action_index > len(actions):
        raise SessionManagerError(f"invalid action: {choice}")
    action = actions[action_index - 1]
    if action == "attach":
        if not selected.runtime_socket:
            raise SessionManagerError("session has no protected runtime")
        if selected.provider == "codex":
            if codex_rollout_is_idle(selected.native_session_id) and replay_idle_codex_runtime(
                selected.runtime_socket
            ):
                print("replaying recent Codex transcript", flush=True)
                manager.resume(selected)
                return
            # A live runtime is already showing the native Codex TUI.  Do not
            # inject Ctrl+T here; attach directly and preserve the active view.
            attach_runtime(selected.runtime_socket)
        else:
            attach_runtime(selected.runtime_socket)
    if action == "resume":
        print(f"resuming {selected.key} with disconnect protection", flush=True)
        manager.resume(selected)
    if action == "inspect":
        print(json.dumps(record_value(selected), ensure_ascii=False, indent=2))
    if action == "finish":
        outcome = input("Outcome (optional): ").strip()
        manager.finish(selected, outcome=outcome)
        print(f"completed {selected.key}")
    if action == "rename":
        name = input("New native name: ").strip()
        renamed = manager.rename(selected, name)
        print(f"renamed {renamed.key} -> {renamed.native_name}")
    if action == "archive":
        archived = manager.archive(selected)
        print(f"archived {archived.key}")
    if action == "unarchive":
        restored = manager.unarchive(selected)
        print(f"unarchived {restored.key}")


def required_or_prompt(value: str | None, label: str) -> str:
    if value:
        return value
    if sys.stdin.isatty() and sys.stdout.isatty():
        result = input(f"{label}: ").strip()
        if result:
            return result
    raise SessionManagerError(f"{label.lower()} is required")


def command_session_new(args: argparse.Namespace) -> None:
    manager = session_manager(args)
    provider = required_or_prompt(args.provider, "Provider (codex/claude)")
    topic = required_or_prompt(args.topic, "Topic")
    role = required_or_prompt(args.role, "Role (worker/peer)")
    print(f"starting protected {provider} {role} session", flush=True)
    manager.new(
        provider=provider,
        topic=topic,
        role=role,
        native_name=args.name,
    )


def command_session_resume(args: argparse.Namespace) -> None:
    manager = session_manager(args)
    record = resolve_managed_session(manager, args.selector, include_archived=False)
    print(f"opening {record.key} with disconnect protection", flush=True)
    manager.resume(record)


def command_session_attach(args: argparse.Namespace) -> None:
    manager = session_manager(args)
    record = resolve_managed_session(manager, args.selector, include_archived=False)
    if not record.runtime_socket:
        raise SessionManagerError("session has no protected runtime")
    attach_runtime(record.runtime_socket)


def command_session_finish(args: argparse.Namespace) -> None:
    manager = session_manager(args)
    record = resolve_managed_session(manager, args.selector, include_archived=False)
    completed = manager.finish(record, outcome=args.outcome)
    print(f"completed {completed.key}")


def command_session_rename(args: argparse.Namespace) -> None:
    manager = session_manager(args)
    record = resolve_managed_session(manager, args.selector)
    renamed = manager.rename(record, args.name)
    print(f"renamed {renamed.key} -> {renamed.native_name}")


def command_session_inspect(args: argparse.Namespace) -> None:
    manager = session_manager(args)
    record = resolve_managed_session(manager, args.selector)
    value = record_value(record)
    if args.native and record.provider == "codex":
        value["native"] = manager.codex.read(record.native_session_id)
    print(json.dumps(value, ensure_ascii=False, indent=2))


def command_session_archive(args: argparse.Namespace) -> None:
    manager = session_manager(args)
    record = resolve_managed_session(manager, args.selector, include_archived=False)
    archived = manager.archive(record)
    qualifier = "native + index" if record.provider == "codex" else "index view"
    print(f"archived {archived.key} ({qualifier})")


def command_session_unarchive(args: argparse.Namespace) -> None:
    manager = session_manager(args)
    record = resolve_managed_session(manager, args.selector)
    restored = manager.unarchive(record)
    print(f"unarchived {restored.key}")


def command_hook_claude(args: argparse.Namespace) -> None:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        raise SessionManagerError(f"invalid Claude hook payload: {exc}") from exc
    if not isinstance(payload, dict):
        raise SessionManagerError("Claude hook payload must be a JSON object")
    handle_claude_hook(payload, dict(os.environ))


def command_hook_install_claude(args: argparse.Namespace) -> None:
    settings = Path(args.settings).expanduser().resolve()
    changed = install_claude_hooks(settings, retention_days=args.retention_days)
    print(f"{'installed' if changed else 'already installed'} Claude session hooks: {settings}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agents-owl",
        description="Disconnect-safe native agent sessions and optional worker/peer collaboration",
    )
    parser.add_argument("--repo", help="repository root; defaults to AGENTS_OWL_REPO or current Git root")
    parser.add_argument("--state-home", help="runtime state root; defaults to AGENTS_OWL_STATE_HOME or XDG state")
    sub = parser.add_subparsers(dest="command_name", required=True)

    solo = sub.add_parser("solo", help="start or reconnect an independent agent in the current directory")
    solo.add_argument("provider", choices=("claude", "codex"))
    solo.add_argument("--name", default="default", help="independent task slot; defaults to default")
    solo.add_argument(
        "--fresh",
        action="store_true",
        help="start a new provider conversation instead of resuming this slot's previous one",
    )
    solo.set_defaults(func=command_solo)

    impl = sub.add_parser("impl", help="start a Claude worker session")
    impl.add_argument("topic", nargs="?")
    impl.add_argument("--name", help="provider-native session name")
    impl.set_defaults(func=command_session_new, provider="claude", role="worker")

    review = sub.add_parser("review", help="start a Codex peer session")
    review.add_argument("topic", nargs="?")
    review.add_argument("--name", help="provider-native session name")
    review.set_defaults(func=command_session_new, provider="codex", role="peer")

    workers = sub.add_parser("i", help="list and select worker sessions")
    add_session_list_arguments(workers)
    workers.set_defaults(func=command_sessions, role="worker")

    peers = sub.add_parser("r", help="list and select peer sessions")
    add_session_list_arguments(peers)
    peers.set_defaults(func=command_sessions, role="peer")

    init = sub.add_parser("init", help="initialize or refresh a pair")
    init.add_argument("pair")
    init.set_defaults(func=command_init)

    status = sub.add_parser("status", help="show pair state without creating it")
    status.add_argument("pair", nargs="?", help="pair name; omit to choose interactively")
    status.set_defaults(func=command_status)

    sessions = sub.add_parser("sessions", help="list and select native Codex/Claude sessions")
    add_session_list_arguments(sessions, include_role=True)
    sessions.set_defaults(func=command_sessions)

    managed_session = sub.add_parser("session", help="manage provider-native sessions")
    managed_sub = managed_session.add_subparsers(dest="session_action", required=True)

    managed_new = managed_sub.add_parser("new", help="start a named native session")
    managed_new.add_argument("--provider", choices=("codex", "claude"))
    managed_new.add_argument("--topic")
    managed_new.add_argument("--role", choices=("worker", "peer"))
    managed_new.add_argument("--name", help="provider-native session name")
    managed_new.set_defaults(func=command_session_new)

    managed_resume = managed_sub.add_parser("resume", help="resume by native id, name, or index key")
    managed_resume.add_argument("selector", nargs="?")
    managed_resume.set_defaults(func=command_session_resume)

    managed_attach = managed_sub.add_parser("attach", help="attach to a still-running protected session")
    managed_attach.add_argument("selector", nargs="?")
    managed_attach.set_defaults(func=command_session_attach)

    managed_finish = managed_sub.add_parser("finish", help="mark a topic session completed")
    managed_finish.add_argument("selector", nargs="?")
    managed_finish.add_argument("--outcome", default="")
    managed_finish.set_defaults(func=command_session_finish)

    managed_rename = managed_sub.add_parser("rename", help="rename the provider-native session")
    managed_rename.add_argument("selector")
    managed_rename.add_argument("name")
    managed_rename.set_defaults(func=command_session_rename)

    managed_inspect = managed_sub.add_parser("inspect", help="show indexed session metadata")
    managed_inspect.add_argument("selector", nargs="?")
    managed_inspect.add_argument("--native", action="store_true", help="also read Codex native metadata")
    managed_inspect.set_defaults(func=command_session_inspect)

    managed_archive = managed_sub.add_parser("archive", help="hide a session and archive Codex natively")
    managed_archive.add_argument("selector", nargs="?")
    managed_archive.set_defaults(func=command_session_archive)

    managed_unarchive = managed_sub.add_parser("unarchive", help="restore an archived session")
    managed_unarchive.add_argument("selector", nargs="?")
    managed_unarchive.set_defaults(func=command_session_unarchive)

    legacy_session = sub.add_parser(
        "pair-session",
        help="compatibility command: create or attach a fixed protected worker/peer session",
    )
    legacy_session.add_argument("pair")
    legacy_session.add_argument("role", choices=sorted(ROLE_ALIASES))
    legacy_session.add_argument("--command", help="agent command; defaults to claude for worker and codex for peer")
    legacy_session.add_argument(
        "--fresh",
        action="store_true",
        help="start a new provider conversation instead of resuming the pair role's previous one",
    )
    legacy_session.set_defaults(func=command_session)

    hook = sub.add_parser("hook", help="Claude lifecycle hook integration")
    hook_sub = hook.add_subparsers(dest="hook_action", required=True)
    hook_claude = hook_sub.add_parser("claude", help=argparse.SUPPRESS)
    hook_claude.set_defaults(func=command_hook_claude)
    hook_install = hook_sub.add_parser("install-claude", help="install Claude lifecycle hooks")
    hook_install.add_argument("--settings", default=str(Path.home() / ".claude" / "settings.json"))
    hook_install.add_argument(
        "--retention-days",
        type=int,
        help="also set Claude cleanupPeriodDays; use 3650 for long-lived history",
    )
    hook_install.set_defaults(func=command_hook_install_claude)

    for command_name in ("send-peer", "send-review"):
        send_peer = sub.add_parser(command_name, help="archive worker handoff and request optional peer feedback")
        send_peer.add_argument("pair")
        send_peer.add_argument("--target", help="runtime socket; defaults to the pair peer runtime")
        send_peer.add_argument("--focus", help="review questions or requirements; answer these before generic findings")
        send_peer.set_defaults(func=command_send_peer)

    send_back = sub.add_parser("send-back", help="archive peer response and send bounded feedback to worker")
    send_back.add_argument("pair")
    send_back.add_argument("--target", help="runtime socket; defaults to the pair worker runtime")
    send_back.set_defaults(func=command_send_back)

    for command_name, kind in (
        ("archive-worker", "worker-handoff"),
        ("archive-implementer", "worker-handoff"),
        ("archive-peer", "peer-response"),
        ("archive-review", "peer-response"),
    ):
        archive = sub.add_parser(command_name, help=f"move inbox/{kind}.md into timestamped artifacts")
        archive.add_argument("pair")
        archive.set_defaults(func=lambda args, selected=kind: command_archive(args, selected))

    decision = sub.add_parser("decision", help="append a local coordination note; never a project approval")
    decision.add_argument("pair")
    decision.add_argument("decision", choices=sorted(DECISIONS))
    decision.add_argument("--note", default="")
    decision.set_defaults(func=command_decision)

    artifacts = sub.add_parser("artifacts", help="list archived handoffs and peer responses")
    artifacts.add_argument("pair")
    artifacts.set_defaults(func=command_artifacts)

    latest = sub.add_parser("show-latest", help="print the newest archived artifact")
    latest.add_argument("pair")
    latest.add_argument("--kind", choices=sorted(KIND_ALIASES))
    latest.set_defaults(func=command_show_latest)
    return parser


def add_session_list_arguments(parser: argparse.ArgumentParser, *, include_role: bool = False) -> None:
    parser.add_argument("--provider", choices=("codex", "claude"))
    if include_role:
        parser.add_argument("--role", choices=("worker", "peer"))
    parser.add_argument("--all", action="store_true", help="include archived sessions")
    parser.add_argument("--global", dest="global_scope", action="store_true", help="include all repositories")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-select", action="store_true", help="only print the list")


SESSION_ACTIONS = {"new", "resume", "attach", "finish", "rename", "inspect", "archive", "unarchive"}


def normalize_argv(argv: list[str]) -> list[str]:
    """Preserve the v0.1 `session PAIR ROLE` spelling."""
    result = list(argv)
    if not result:
        return ["sessions"]
    try:
        index = result.index("session")
    except ValueError:
        return result
    if (
        index + 1 < len(result)
        and not result[index + 1].startswith("-")
        and result[index + 1] not in SESSION_ACTIONS
    ):
        result[index] = "pair-session"
    return result


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    selected_argv = sys.argv[1:] if argv is None else argv
    args = parser.parse_args(normalize_argv(selected_argv))
    try:
        args.func(args)
    except (ProviderError, RegistryError, SessionManagerError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    sys.exit(main())
