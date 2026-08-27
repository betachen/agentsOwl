# AgentsOwl

AgentsOwl 是一个 disconnect-safe 的 Codex/Claude Code 会话入口，也保留可选
的双 AI 协作工具。它用透明的 `dtach` PTY 保护正在运行的 agent，避免 SSH 或
终端异常断开杀死进程；同时直接索引 provider 自己的 session ID 和名称，不
复制 transcript，不创建另一套会话身份。

## 原则

- 双 AI 是可选协作方式，不是强制交叉验证 gate。
- 项目仓库的 `AGENTS.md`、架构文档和研究规则始终拥有更高权威。
- peer 区分硬错误、项目约束违规、建议和未知，不用直觉否决研究假设。
- worker 可以接受、推迟或有证据地拒绝可选建议；human-only 决策仍由
  human 作出。
- runtime artifact 默认位于 `~/.local/state/agents-owl/`，不污染业务仓库。
- 所有 AgentsOwl 会话默认获得断线保护；`dtach` 只转发原始终端字节，不接管
  provider TUI，也不是会话身份。
- 正常退出完全遵循 Codex/Claude 原生语义；AgentsOwl 只处理异常断线后的
  进程存活与重新连接。

会话语义与完整命令见 [docs/SESSIONS.md](docs/SESSIONS.md)，可选双 AI 语义见
[docs/WORKFLOW.md](docs/WORKFLOW.md)，从旧 Research Gate v0 迁移见
[docs/MIGRATION.md](docs/MIGRATION.md)。

## 依赖

- Python 3.10+
- dtach 0.9+
- Codex CLI 和/或 Claude Code

工具只使用 Python 标准库。

## 快速开始

```bash
git clone git@github.com:betachen/agentsOwl.git ~/workspace/agentsOwl
chmod +x ~/workspace/agentsOwl/bin/agents-owl
export PATH="$HOME/workspace/agentsOwl/bin:$PATH"
sudo apt install dtach

cd /path/to/project
agents-owl hook install-claude --retention-days 3650  # 只需一次；保留已有 settings
agents-owl session new --provider claude --role worker --topic '实现 E-021'
```

AgentsOwl 默认立即进入 Claude/Codex 原生 TUI。外层 `dtach` 没有窗口、状态栏
或终端模拟层；它只在 SSH/terminal 突然消失时保住 agent：

```bash
agents-owl session new --provider claude --role worker --topic '长任务'
```

正常使用时无需 detach 命令。若连接异常中断，重新登录后运行 `agents-owl
sessions`，选择状态为 `running` 的会话并执行 `attach`。如果在 provider 内
正常退出（Claude `/exit`、Codex `/quit`），agent 进程与保护 runtime 一起
结束，原生 session 历史仍由 provider 管理。

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
agents-owl session attach SESSION  # 只连接仍在运行的进程
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
agents-owl session attach [SESSION]
agents-owl session finish [SESSION] [--outcome TEXT]
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
AGENTS_OWL_DTACH_CMD
```

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```
