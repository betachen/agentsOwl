# AgentsOwl

AgentsOwl 是一个 terminal-first 的双 AI 协作工具：一个 AI 负责完成任务
（worker），另一个 AI 可选地提供独立复核（peer）。它保存交接证据并在
tmux 会话之间传递 prompt，但不把 peer 变成审批者，也不替代项目自己的
规则、测试、human 裁决或发布流程。

## 原则

- 双 AI 是可选协作方式，不是强制交叉验证 gate。
- 项目仓库的 `AGENTS.md`、架构文档和研究规则始终拥有更高权威。
- peer 区分硬错误、项目约束违规、建议和未知，不用直觉否决研究假设。
- worker 可以接受、推迟或有证据地拒绝可选建议；human-only 决策仍由
  human 作出。
- runtime artifact 默认位于 `~/.local/state/agents-owl/`，不污染业务仓库。

完整语义见 [docs/WORKFLOW.md](docs/WORKFLOW.md)，从旧 Research Gate v0
迁移见 [docs/MIGRATION.md](docs/MIGRATION.md)。

## 依赖

- Python 3.10+
- tmux
- 至少一个可在终端运行的 agent；默认 worker=`claude`、peer=`codex`

工具只使用 Python 标准库。

## 快速开始

```bash
git clone git@github.com:betachen/agentsOwl.git ~/workspace/agentsOwl
chmod +x ~/workspace/agentsOwl/bin/agents-owl
export PATH="$HOME/workspace/agentsOwl/bin:$PATH"

cd /path/to/project
agents-owl init my-project
agents-owl session my-project worker
```

在 worker 会话中给出原始任务，并明确要求它读取启动横幅显示的 handoff
template，完成后把交接写到横幅显示的 deliverable 路径。AgentsOwl 不会替
human 自动生成任务内容。

在另一个终端：

```bash
cd /path/to/project
agents-owl session my-project peer
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
```

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```
