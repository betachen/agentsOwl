# AgentsOwl 协作工作流

## 定位

AgentsOwl 的可选双 AI 层只解决三件事：保持两个独立 agent 会话、传递有
结构的交接材料、保留本地证据链。基础会话索引见 `SESSIONS.md`。两者都不
定义项目方法论，也不建立第二套审批或状态系统。

```text
human task → worker → verified deliverable
                       └─ optional peer look
                            ├─ no actionable issue → finish
                            └─ bounded feedback → worker follow-up
```

项目规则永远先于 AgentsOwl prompt。若项目只要求单 agent 完成低风险任务，
可以完全跳过 peer。若任务涉及复杂 diff、跨层约束、高代价错误或 human 希望
获得独立视角，再启用 peer。

## 角色

### Worker

Worker 对任务结果负责：读取项目规则、实现或研究、执行与风险相称的验证，
并如实记录已检查和未检查的部分。Worker 不应为了让 handoff 看起来完整而
制造证据，也不能把需要 human 决定的事项伪装成已批准。

### Advisory peer

Peer 独立查看代码、diff、测试和证据，优先发现：

- 明确 correctness 或 safety bug；
- 违反仓库显式约束；
- 声称与证据不一致；
- 关键检查实际没有运行；
- 在项目定义的阶段中使用了错误的判定标准。

Peer 不拥有 veto。它不能冻结路线、批准发布、扩大任务，或因个人偏好要求
重做。输出必须把硬错误、可选建议和未知分开。

## 一轮协作

1. `agents-owl init PAIR` 初始化外部 state，并读取 `.agents-owl.json`。
2. `agents-owl session PAIR worker` 启动或接入 worker。
3. human 向 worker 给出原始任务；worker 完成并写 `worker-handoff.md`。
4. human 决定是否需要 peer：
   - 不需要：`archive-worker`，继续项目自己的完成流程；
   - 需要：启动 peer，然后 `send-peer`。
5. peer 独立核对并写 `peer-response.md`。
6. human/worker 解释 peer 建议：
   - `HARD_ERROR` 或确认的项目约束违规：做有界修复；
   - `SUGGEST_CHANGES`：按任务价值选择接受、推迟或拒绝；
   - `READY`：结束本轮。
7. 需要回传时运行 `send-back`；否则运行 `archive-peer`。

`send-peer` 和 `send-back` 会把 inbox 文件移动到带时间戳的 artifacts，确保
下一轮必须生成新交接，不会误发陈旧文件。

## Recommendation 语义

- `READY`：已检查范围内没有值得回传的实质问题，不表示正式批准。
- `SUGGEST_CHANGES`：存在有价值的有界建议，由 worker/human 选择。
- `HARD_ERROR`：peer 发现可复现的 correctness/safety 错误或明确规则违规；
  必须给出证据，不能用“不够完善”代替。

## 决策与证据边界

`agents-owl decision` 只写本地 coordination note。例如它可以记录 human
选择跳过 peer 或接受某项建议，但不会：

- 修改 Git、merge PR 或发布；
- 写项目 decision log；
- 冻结/解冻研究路线；
- 授权使用受保护数据；
- 把 peer 意见升级成 human 裁决。

所有正式状态变化仍使用项目自己的权威机制。

## tmux 与并发

v0.1 兼容 pair session 名为：

```text
owl-<pair>-worker
owl-<pair>-peer
```

兼容命令 `session PAIR ROLE` 在会话不存在时创建，存在时直接 attach。用
`Ctrl-b d` detach。新任务推荐使用默认 direct 的 `session new`；只有显式
`--tmux` 时才创建包含 repo、topic、role 和稳定 hash 的持久 runtime。无论
是否使用 tmux，原生 session ID 才是身份。
向 agent 注入 prompt 前，`send-peer`/`send-back` 会先验证 tmux target；可用
`--target session:window.pane` 显式指定 pane。
