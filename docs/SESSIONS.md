# 原生会话管理

## 边界

AgentsOwl 管理的是索引，不是 transcript：

```text
topic
├── worker → (claude, native session_id)
└── peer   → (codex, native thread/session id)
```

唯一键是 `(provider, native_session_id)`。`native_name`、topic、role、repo、
lifecycle、可选 tmux 名称和关联会话只是索引字段。默认运行完全不经过 tmux；
恢复时直接将原生 ID 交给 `claude --resume` 或 `codex resume`。

索引默认写在：

```text
~/.local/state/agents-owl/sessions/index.json
```

文件写操作使用进程锁，并通过同目录临时文件 + `fsync` + 原子替换提交。
AgentsOwl 不扫描或解析
Claude transcript JSONL、Codex rollout 文件，也不复制对话正文。

## 首次设置

Codex 不需要额外配置，AgentsOwl 使用官方 app-server 的 `thread/start`、
`thread/name/set`、`thread/read`、`thread/archive` 和 `thread/unarchive`。

Claude 需要一次性安装官方 `SessionStart`/`SessionEnd` hooks：

```bash
agents-owl hook install-claude --retention-days 3650
```

安装命令原子合并 `~/.claude/settings.json`，保留已有字段和 hooks，并且可以
重复运行。hook 对普通 Claude 会话是 no-op；只有 AgentsOwl 启动并设置
`AGENTS_OWL_MANAGED_SESSION=1` 的会话才进入索引。

Claude Code 默认会清理超过 30 天的 session。需要长期回顾时显式传
`--retention-days 3650`；这会设置官方 `cleanupPeriodDays`。AgentsOwl 不使用
无效的 `0`，也不会在未传该参数时擅自改变 retention。

## 日常流程

新主题新会话：

```bash
agents-owl session new \
  --provider claude \
  --role worker \
  --topic 'E-021 post-impulse scan'
```

参数缺失且当前是交互终端时会逐项询问。默认原生名称是
`<topic> [<role>]`；可以用 `--name` 指定。默认直接进入 provider 原生 TUI，
因此滚动、选取和剪贴板行为与直接运行 `claude`/`codex` 相同。
命令必须从 tmux 外的普通 shell 启动；若环境已有 `$TMUX`，工具会拒绝伪 direct。

只有需要让正在执行的 agent 在 SSH/terminal 异常断开后继续运行时使用：

```bash
agents-owl session new \
  --provider claude \
  --role worker \
  --topic 'long-running topic' \
  --tmux
```

`--tmux` 创建持久 runtime 并 attach。若明确要只在后台启动，加
`--tmux --no-attach`。重新查看后台 TUI 时仍需 attach tmux，这是进程位于
tmux PTY 中以抵抗断线的必要代价。

需要可选 peer 时使用完全相同的 topic：

```bash
agents-owl session new \
  --provider codex \
  --role peer \
  --topic 'E-021 post-impulse scan'
```

同 repo、同 topic 的所有原生会话会自动写入彼此的
`related_session_keys`。这只是关联，不引入审批或强制交叉验证。

浏览与选择：

```bash
agents-owl sessions
```

在 TTY 中，它显示编号列表并继续询问 session 与 action；pipe/脚本环境只
输出列表。其他形式：

```bash
agents-owl sessions --no-select
agents-owl sessions --all
agents-owl sessions --provider codex
agents-owl sessions --role worker
agents-owl sessions --global
agents-owl sessions --json
```

大部分命令中的 `SESSION` 可以是完整索引键、原生 ID、唯一 ID 前缀或唯一
原生名称；省略时打开编号选择器：

```bash
agents-owl session resume [SESSION]
agents-owl session inspect [SESSION]
agents-owl session finish [SESSION] --outcome '完成 E-021；测试通过'
```

`resume` 默认也是 direct；需要持久 runtime 时显式加 `--tmux`。交互列表会
分别显示 `resume-direct` 和 `resume-tmux`。若同一原生 session 已经在 tmux
中运行，AgentsOwl 会拒绝并行 direct resume，避免两个 TUI 同时占用它。

`finish` 默认只标记主题完成，不突然终止 TUI。明确希望同时关闭其 tmux 时：

```bash
agents-owl session finish SESSION --stop
```

## 生命周期

索引存储 `active/completed/archived/missing`。显示层把 active 细分为：

- `running`：对应 tmux 仍存在；
- `suspended`：tmux 不存在，可以用原生 ID resume；
- `direct`：该 session 使用原生终端模式；工具不冒充进程监控器，无法从外部
  判断 direct TUI 此刻是否仍打开；
- `completed`：主题任务已完成，仍可重新 resume；
- `archived`：默认列表隐藏；
- `missing`：已知原生对象在 provider 侧不可用。

`finish` 会保存 outcome、完成时间和当前 repo 的 Git HEAD（如果存在）。v0.3
不提供 delete；历史记录默认可恢复，避免误删 provider transcript。

## Rename 与 archive 的 provider 差异

```bash
agents-owl session rename SESSION '新名称'
agents-owl session archive SESSION
agents-owl session unarchive SESSION
```

- Codex rename/archive/unarchive 直接调用官方 app-server，随后更新索引。
- Claude direct session 请在原生 TUI 中使用 `/rename`；CLI 的外部 rename
  仅在 managed tmux 正在运行时可注入并同步索引。
- Claude Code 没有与 Codex app-server 对称的外部 archive/unarchive API；
  因此 Claude archive 是 AgentsOwl 的列表视图，不触碰 Claude transcript。

若需要查看 Codex provider 返回的最新 native metadata：

```bash
agents-owl session inspect SESSION --native
```

## Provider 事实来源

- [Codex app-server](https://developers.openai.com/codex/app-server/)
- [Codex CLI reference](https://developers.openai.com/codex/cli/reference/)
- [Claude Code sessions](https://code.claude.com/docs/en/sessions)
- [Claude Code hooks](https://code.claude.com/docs/en/hooks)
