#!/usr/bin/env python3
"""Terminal-first controller for optional worker/peer collaboration."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from string import Template
from typing import Any

from .providers import ProviderError
from .registry import RegistryError, SessionRecord
from .session_manager import (
    SessionManager,
    SessionManagerError,
    attach_tmux,
    handle_claude_hook,
    install_claude_hooks,
    make_tmux_name,
    runtime_lifecycle,
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


def tmux_has_session(session: str) -> bool:
    result = subprocess.run(
        ["tmux", "has-session", "-t", session],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def require_tmux_target(target: str) -> None:
    try:
        result = subprocess.run(
            ["tmux", "display-message", "-p", "-t", target, "#{pane_id}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except FileNotFoundError as exc:
        raise SystemExit("error: tmux is not installed or not on PATH") from exc
    if result.returncode != 0:
        raise SystemExit(f"error: tmux target does not exist: {target}")


def inject_prompt(target: str, prompt: str) -> None:
    require_tmux_target(target)
    try:
        subprocess.run(["tmux", "set-buffer", "--", prompt], check=True)
        subprocess.run(["tmux", "paste-buffer", "-t", target], check=True)
        subprocess.run(["tmux", "send-keys", "-t", target, "C-m"], check=True)
    except subprocess.CalledProcessError as exc:
        raise SystemExit(f"error: tmux prompt injection failed for {target}") from exc


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
    root, metadata = require_pair(repo, state_home, args.pair)
    print(f"pair: {args.pair}")
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
    print("\ntmux sessions:")
    for role in ("worker", "peer"):
        name = session_name(args.pair, role)
        print(f"  {name}: {'exists' if tmux_has_session(name) else 'missing'}")
    print(f"\nevents: {root / 'events.jsonl'}")
    print(f"decisions: {root / 'decisions.md'}")


def command_session(args: argparse.Namespace) -> None:
    repo, state_home = context(args)
    root = initialize_pair(repo, state_home, args.pair)
    role = ROLE_ALIASES[args.role]
    session = session_name(args.pair, role)
    if tmux_has_session(session):
        os.execvp("tmux", ["tmux", "attach-session", "-t", session])
    default_cmd = "claude" if role == "worker" else "codex"
    legacy_env = "IMPLEMENTER_CMD" if role == "worker" else "REVIEWER_CMD"
    owl_env = "AGENTS_OWL_WORKER_CMD" if role == "worker" else "AGENTS_OWL_PEER_CMD"
    agent_cmd = args.command or os.environ.get(owl_env) or os.environ.get(legacy_env) or default_cmd
    output_name = "worker-handoff.md" if role == "worker" else "peer-response.md"
    output_path = root / "inbox" / output_name
    role_template = template_path(output_name)
    startup = (
        "printf '\\n=== AgentsOwl ===\\n'; "
        f"printf 'pair: %s\\n' {shlex.quote(args.pair)}; "
        f"printf 'role: %s\\n' {shlex.quote(role)}; "
        f"printf 'repo: %s\\n' {shlex.quote(str(repo))}; "
        f"printf 'session: %s\\n' {shlex.quote(session)}; "
        f"printf 'deliverable: %s\\n' {shlex.quote(str(output_path))}; "
        f"printf 'handoff template: %s\\n\\n' {shlex.quote(str(role_template))}; "
        f"exec {agent_cmd}"
    )
    try:
        subprocess.run(["tmux", "new-session", "-d", "-s", session, "-c", str(repo), startup], check=True)
    except FileNotFoundError as exc:
        raise SystemExit("error: tmux is not installed or not on PATH") from exc
    except subprocess.CalledProcessError as exc:
        raise SystemExit(f"error: could not create tmux session {session}") from exc
    append_event(root, args.pair, "session-start", role=role, session=session, command=agent_cmd)
    os.execvp("tmux", ["tmux", "attach-session", "-t", session])


def command_send_peer(args: argparse.Namespace) -> None:
    repo, state_home = context(args)
    root, metadata = require_pair(repo, state_home, args.pair)
    target = args.target or session_name(args.pair, "peer")
    require_tmux_target(target)
    artifact = archive_inbox(root, args.pair, "worker-handoff")
    prompt = template("peer-prompt.md").safe_substitute(
        pair=args.pair,
        repo=str(repo),
        artifact_path=str(artifact),
        peer_output_path=str(root / "inbox" / "peer-response.md"),
        peer_template_path=str(template_path("peer-response.md")),
        policy_files=policy_text(repo, metadata),
    )
    inject_prompt(target, prompt)
    append_event(root, args.pair, "send-peer", artifact=str(artifact), target=target)
    print(f"sent optional peer request to {target}")
    print(f"archived worker handoff: {artifact}")


def command_send_back(args: argparse.Namespace) -> None:
    repo, state_home = context(args)
    root, metadata = require_pair(repo, state_home, args.pair)
    target = args.target or session_name(args.pair, "worker")
    require_tmux_target(target)
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
    print_session_table(records)
    if args.no_select or not records or not (sys.stdin.isatty() and sys.stdout.isatty()):
        return
    selected = choose_record(records, prompt="Open", show_table=False)
    state = runtime_lifecycle(selected)
    actions = ["attach"] if state == "running" else []
    if selected.lifecycle == "archived":
        actions.extend(["unarchive", "inspect"])
    else:
        if state != "running":
            actions.extend(["resume-direct", "resume-tmux"])
        actions.extend(["inspect", "finish", "rename", "archive"])
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
        if not selected.tmux_session:
            raise SessionManagerError("session has no tmux runtime")
        attach_tmux(selected.tmux_session)
    if action == "resume-direct":
        print(f"resuming {selected.key} directly", flush=True)
        manager.resume(selected, use_tmux=False)
    if action == "resume-tmux":
        resumed = manager.resume(selected, use_tmux=True)
        if not resumed.tmux_session:
            raise SessionManagerError("session has no tmux runtime")
        attach_tmux(resumed.tmux_session)
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
    if args.no_attach and not args.tmux:
        raise SessionManagerError("--no-attach is only valid together with --tmux")
    provider = required_or_prompt(args.provider, "Provider (codex/claude)")
    topic = required_or_prompt(args.topic, "Topic")
    role = required_or_prompt(args.role, "Role (worker/peer)")
    if not args.tmux:
        manager.require_direct_terminal()
    mode = "persistent tmux" if args.tmux else "direct native UI"
    print(f"starting {provider} {role} in {mode}", flush=True)
    record = manager.new(
        provider=provider,
        topic=topic,
        role=role,
        native_name=args.name,
        use_tmux=args.tmux,
    )
    if not args.tmux:
        return
    if record is None:
        tmux_name = make_tmux_name(manager.repo, topic, role)
        print(f"started Claude session in {tmux_name}; native id registration is pending")
        if not args.no_attach:
            attach_tmux(tmux_name)
        return
    print(f"started {record.key}: {record.native_name}")
    if not args.no_attach and record.tmux_session:
        attach_tmux(record.tmux_session)


def command_session_resume(args: argparse.Namespace) -> None:
    manager = session_manager(args)
    if args.no_attach and not args.tmux:
        raise SessionManagerError("--no-attach is only valid together with --tmux")
    record = resolve_managed_session(manager, args.selector, include_archived=False)
    if not args.tmux:
        manager.require_direct_terminal()
    mode = "persistent tmux" if args.tmux else "direct native UI"
    print(f"resuming {record.key} in {mode}", flush=True)
    resumed = manager.resume(record, use_tmux=args.tmux)
    if not args.tmux:
        return
    print(f"resumed {resumed.key}")
    if not args.no_attach and resumed.tmux_session:
        attach_tmux(resumed.tmux_session)


def command_session_finish(args: argparse.Namespace) -> None:
    manager = session_manager(args)
    record = resolve_managed_session(manager, args.selector, include_archived=False)
    completed = manager.finish(record, outcome=args.outcome, stop=args.stop)
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
        description="Optional worker/peer collaboration without a mandatory review gate",
    )
    parser.add_argument("--repo", help="repository root; defaults to AGENTS_OWL_REPO or current Git root")
    parser.add_argument("--state-home", help="runtime state root; defaults to AGENTS_OWL_STATE_HOME or XDG state")
    sub = parser.add_subparsers(dest="command_name", required=True)

    init = sub.add_parser("init", help="initialize or refresh a pair")
    init.add_argument("pair")
    init.set_defaults(func=command_init)

    status = sub.add_parser("status", help="show pair state without creating it")
    status.add_argument("pair")
    status.set_defaults(func=command_status)

    sessions = sub.add_parser("sessions", help="list and select native Codex/Claude sessions")
    sessions.add_argument("--provider", choices=("codex", "claude"))
    sessions.add_argument("--role", choices=("worker", "peer"))
    sessions.add_argument("--all", action="store_true", help="include archived sessions")
    sessions.add_argument("--global", dest="global_scope", action="store_true", help="include all repositories")
    sessions.add_argument("--json", action="store_true")
    sessions.add_argument("--no-select", action="store_true", help="only print the list")
    sessions.set_defaults(func=command_sessions)

    managed_session = sub.add_parser("session", help="manage provider-native sessions")
    managed_sub = managed_session.add_subparsers(dest="session_action", required=True)

    managed_new = managed_sub.add_parser("new", help="start a named native session")
    managed_new.add_argument("--provider", choices=("codex", "claude"))
    managed_new.add_argument("--topic")
    managed_new.add_argument("--role", choices=("worker", "peer"))
    managed_new.add_argument("--name", help="provider-native session name")
    managed_new.add_argument(
        "--tmux",
        action="store_true",
        help="run in persistent tmux instead of the direct native terminal",
    )
    managed_new.add_argument(
        "--no-attach",
        action="store_true",
        help="with --tmux, leave the persistent runtime detached",
    )
    managed_new.set_defaults(func=command_session_new)

    managed_resume = managed_sub.add_parser("resume", help="resume by native id, name, or index key")
    managed_resume.add_argument("selector", nargs="?")
    managed_resume.add_argument(
        "--tmux",
        action="store_true",
        help="run in persistent tmux instead of the direct native terminal",
    )
    managed_resume.add_argument(
        "--no-attach",
        action="store_true",
        help="with --tmux, leave the persistent runtime detached",
    )
    managed_resume.set_defaults(func=command_session_resume)

    managed_finish = managed_sub.add_parser("finish", help="mark a topic session completed")
    managed_finish.add_argument("selector", nargs="?")
    managed_finish.add_argument("--outcome", default="")
    managed_finish.add_argument("--stop", action="store_true", help="also stop its tmux runtime")
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
        help="compatibility command: create or attach a fixed worker/peer tmux session",
    )
    legacy_session.add_argument("pair")
    legacy_session.add_argument("role", choices=sorted(ROLE_ALIASES))
    legacy_session.add_argument("--command", help="agent command; defaults to claude for worker and codex for peer")
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
        send_peer.add_argument("--target", help="tmux target; defaults to owl-<pair>-peer")
        send_peer.set_defaults(func=command_send_peer)

    send_back = sub.add_parser("send-back", help="archive peer response and send bounded feedback to worker")
    send_back.add_argument("pair")
    send_back.add_argument("--target", help="tmux target; defaults to owl-<pair>-worker")
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


SESSION_ACTIONS = {"new", "resume", "finish", "rename", "inspect", "archive", "unarchive"}


def normalize_argv(argv: list[str]) -> list[str]:
    """Preserve the v0.1 `session PAIR ROLE` spelling."""
    result = list(argv)
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
