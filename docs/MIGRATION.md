# 从 Research Gate v0 迁移

AgentsOwl 保留旧工具的断线保护、handoff、artifact 和事件日志能力，移除了
“双 AI 必须严格交叉验证并形成 gate”的治理假设。

## 概念映射

| Research Gate v0 | AgentsOwl |
|---|---|
| implementer | worker |
| reviewer / auditor | advisory peer |
| `implementer-done.md` | `worker-handoff.md` |
| `reviewer-done.md` | `peer-response.md` |
| PASS / NEEDS_REVISION / REJECT | READY / SUGGEST_CHANGES / HARD_ERROR |
| project-local `.agent-console/pairs/` | external XDG state |
| reviewer protocol as gate | repository policy as authority |

兼容命令仍可用：

```text
send-review          → send-peer
archive-implementer  → archive-worker
archive-review       → archive-peer
impl/implementer     → worker
review/reviewer      → peer
```

## Shell alias 迁移

五个旧别名可以保留名称，并切换为原生 session 列表/选择器：

```bash
AGENTS_OWL_BIN="$HOME/workspace/agentsOwl/bin/agents-owl"
PROJECT_REPO="$HOME/workspace/example"

alias rgimpl="cd $PROJECT_REPO && $AGENTS_OWL_BIN session new --provider claude --role worker"
alias rgreview="cd $PROJECT_REPO && $AGENTS_OWL_BIN session new --provider codex --role peer"
alias pair="cd $PROJECT_REPO && $AGENTS_OWL_BIN sessions"
alias rgi="cd $PROJECT_REPO && $AGENTS_OWL_BIN sessions --role worker"
alias rgr="cd $PROJECT_REPO && $AGENTS_OWL_BIN sessions --role peer"
```

实际 `.bashrc` 中应把 `$PROJECT_REPO` 和 `$AGENTS_OWL_BIN` 展开成完整路径，
避免 alias 执行时变量不存在。v0.1 的 `session PAIR ROLE` 仍兼容，但只用于
固定 pair runtime，不进入新的原生 session index。

这些 alias 默认进入由透明 PTY 保护的 Claude/Codex 原生 TUI。SSH 异常断开
后重新执行 `pair`，选择 `running` 会话并 attach；不需要额外启动参数。

## 旧数据

旧 `.agent-console` 数据不会自动删除。若其中有需要保留的历史，先复制到
安全位置；新任务使用 `agents-owl status PAIR` 显示的 state 路径。旧的
project-local reviewer protocol 不应复制成新项目权威规则。

旧 `pair decision` 的内容只可作为历史协调记录，不应自动迁入项目正式
decision log。
