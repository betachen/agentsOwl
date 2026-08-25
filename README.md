# AgentsOwl

AgentsOwl 是一个 terminal-first 的 Codex/Claude Code 会话管理器，也保留
可选的双 AI 协作工具。它直接索引 provider 自己的 session ID 和名称，把同
一 topic 的 worker/peer 关联起来；不复制 transcript，不创建另一套会话身份。

## 原则

- 双 AI 是可选协作方式，不是强制交叉验证 gate。
- 项目仓库的 `AGENTS.md`、架构文档和研究规则始终拥有更高权威。
- peer 区分硬错误、项目约束违规、建议和未知，不用直觉否决研究假设。
- worker 可以接受、推迟或有证据地拒绝可选建议；human-only 决策仍由
  human 作出。
- runtime artifact 默认位于 `~/.local/state/agents-owl/`，不污染业务仓库。
- tmux 只是运行载体，不是会话身份；detach 或 tmux 消失后仍可按原生 ID resume。

会话语义与完整命令见 [docs/SESSIONS.md](docs/SESSIONS.md)，可选双 AI 语义见
[docs/WORKFLOW.md](docs/WORKFLOW.md)，从旧 Research Gate v0 迁移见
[docs/MIGRATION.md](docs/MIGRATION.md)。

## 依赖

- Python 3.10+
- tmux
- Codex CLI 和/或 Claude Code

工具只使用 Python 标准库。

## 快速开始

```bash
git clone git@github.com:betachen/agentsOwl.git ~/workspace/agentsOwl
chmod +x ~/workspace/agentsOwl/bin/agents-owl
export PATH="$HOME/workspace/agentsOwl/bin:$PATH"

cd /path/to/project
agents-owl hook install-claude --retention-days 3650  # 只需一次；保留已有 settings
agents-owl session new --provider claude --role worker --topic '实现 E-021'
```

一个 topic 完成后标记完成；新 topic 新开原生会话：

```bash
agents-owl sessions                 # 列表 → 编号选择 → action
agents-owl session finish SESSION --outcome 'E-021 完成'
agents-owl session new --provider codex --role worker --topic '实现 E-022'
```

同一 topic 需要第二视角时，启动另一个 provider/role。索引会自动建立关联：

```bash
agents-owl session new --provider codex --role peer --topic '实现 E-021'
```

Claude 的 `session_id` 由 `SessionStart` hook 回报；Codex 会话通过官方
app-server 创建和命名。恢复始终使用原生 ID：

```bash
agents-owl session resume SESSION
agents-owl session inspect SESSION --native  # --native 目前适用于 Codex
agents-owl session archive SESSION
agents-owl session unarchive SESSION
```

Codex archive/unarchive 会同步原生状态；Claude 没有对称的外部 archive API，
所以 Claude archive 只影响 AgentsOwl 列表，不删除 provider transcript。

## 可选 worker/peer 交接

原 v0.1 的固定 pair 命令仍兼容。例如：

```bash
agents-owl init my-project
agents-owl session my-project worker  # 兼容写法，等价于内部 pair-session
```

worker 完成后写入 `agents-owl status my-project` 显示的
`inbox/worker-handoff.md`，然后协调端执行：

```bash
agents-owl send-peer my-project
```

peer 的输出写入 `inbox/peer-response.md`。需要把建议送回 worker 时：

```bash
agents-owl send-back my-project
```

peer 不是必经步骤。无需复核时可以直接归档 worker handoff：

```bash
agents-owl archive-worker my-project
agents-owl decision my-project skip-peer --note 'Low-risk local change; project checks passed'
```

## 项目接入

项目可以在仓库根目录添加 `.agents-owl.json`：

```json
{
  "policy_files": [
    "AGENTS.md",
    "docs/ARCHITECTURE.md"
  ],
  "collaboration_note": "Peer feedback is advisory; human retains project decisions."
}
```

运行 `agents-owl init <pair>` 会校验这些文件并把它们写入 pair metadata。
配置内容变化后重新执行一次 `init` 即可刷新。

## 常用命令

```text
agents-owl sessions [--all] [--provider codex|claude] [--role worker|peer] [--json]
agents-owl session new --provider PROVIDER --role ROLE --topic TOPIC [--name NAME]
agents-owl session resume [SESSION]
agents-owl session finish [SESSION] [--outcome TEXT] [--stop]
agents-owl session rename SESSION NAME
agents-owl session inspect [SESSION] [--native]
agents-owl session archive [SESSION]
agents-owl session unarchive [SESSION]
agents-owl hook install-claude

agents-owl init PAIR
agents-owl status PAIR
agents-owl session PAIR worker|peer
agents-owl send-peer PAIR
agents-owl send-back PAIR
agents-owl archive-worker PAIR
agents-owl archive-peer PAIR
agents-owl artifacts PAIR
agents-owl show-latest PAIR [--kind worker|peer]
agents-owl decision PAIR accept|revise|reject|fork|skip-peer [--note TEXT]
```

旧名称 `send-review`、`archive-implementer`、`archive-review` 仍是兼容别名，
但新文档统一使用 worker/peer，避免把 advisory peer 误解为强制审批者。

全局选项必须放在子命令之前：

```bash
agents-owl --repo /path/to/project --state-home /path/to/state status my-project
```

也可以设置：

```text
AGENTS_OWL_REPO
AGENTS_OWL_STATE_HOME
AGENTS_OWL_WORKER_CMD
AGENTS_OWL_PEER_CMD
AGENTS_OWL_CODEX_CMD
AGENTS_OWL_CLAUDE_CMD
```

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```
