# Alchemist 接入说明

Alchemist 是 AgentsOwl 从 Research Gate v0 迁出的起点，但 AgentsOwl 不
拥有 Alchemist 的研究方法论。每轮 worker 和 peer 必须按仓库 `AGENTS.md`
规定的顺序读取四份核心文档：

1. `docs/ARCHITECTURE.md`
2. `docs/EXPERIMENT_RULES.md`
3. `docs/strategy_catalog.md`
4. `docs/experiment_ledger.md`

代码审查另读 `docs/review/README.md`；挂起项仅在任务相关或 human 提升时读
`docs/parking_lot.md`。

## 对旧双 AI 流程的修正

- 不恢复 E-017 已撤销的强制 `dual_ai_workflow`。
- E-020 将保留形式明确为可选 worker + advisory peer，并把工具迁出项目。
- peer 是可选的独立第二视角，不是 Pruner、批准者或路线冻结者。
- 研究假设可以发散；生产架构仍按 `ARCHITECTURE.md` 收敛。
- AI 脑内否决仅限硬错误：未来泄漏、无法成交、成本模型错误、逻辑自相
  矛盾。经济性由廉价、统一、可失败的实验判断。
- peer 必须匹配 S0-S4 阶段。不能用 S3/S4 的确认强度阻断 S1 扫描，也不能
  用孤立正点绕过后续验证。
- 负结果必须声明 operationalization、死亡机制、外推边界和仍存活假设。
- 冻结、解冻、reserved/lockbox 消费与候选状态推进只由 human 裁决；
  AgentsOwl 的本地 decision note 不具备这些权力。

Alchemist 根目录的 `.agents-owl.json` 只列出权威文件，不复制规则，避免
工具仓库与项目仓库形成两份会漂移的事实源。

## 当前 shell 入口

新 shell（或 `source ~/.bashrc`）中：

```text
pair       列出全部 Alchemist 会话，编号选择后 resume/inspect/finish 等
rgimpl     用原生 TUI 新建 Claude worker；交互输入本次 topic
rgreview   用原生 TUI 新建 Codex peer；输入与 worker 完全相同的 topic 即关联
rgi        只列 worker 会话并进入编号选择
rgr        只列 peer 会话并进入编号选择
```

一个 topic 完成后在 `pair` 中选择 `finish`，后续主题使用 `rgimpl` 新建会话。
需要直接命令操作时，session selector 可使用原生 ID、唯一名称或
`provider:native_session_id`。

这五个入口默认不使用 tmux。预计连接可能中断且当前推理必须继续时，可直接：

```bash
rgimpl --tmux
rgreview --tmux
```

再加 `--no-attach` 则只在后台创建持久 runtime。
