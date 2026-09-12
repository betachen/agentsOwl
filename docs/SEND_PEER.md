# `send-peer` 快速使用说明

本文面向 Alchemist 的 `ff` alias。偶尔复核一次时可以直接复制粘贴；需要 SSH
断线保护、自动投递和归档时使用本流程。

## 最短流程

使用固定 pair，不要和 `ff impl` / `ff review` 混用。下面以
`range-v01-r2` 为例。

协调终端：

```bash
ff init range-v01-r2
```

Worker 终端：

```bash
ff session range-v01-r2 worker
```

Peer 终端：

```bash
ff session range-v01-r2 peer
```

以后无需记住 pair 名即可重新进入：`ff i` 选择 worker，`ff r` 选择 peer。
固定 pair 在列表中的 `kind` 是 `pair`；选择运行中的条目会直接 attach，选择
已退出的条目会重新启动。`ff status` 不带名称时也会列出当前项目的 pair 供
编号选择。

Worker 写完 handoff 后，协调终端执行：

```bash
ff status range-v01-r2
ff send-peer range-v01-r2
```

Peer 写完 response 后，需要回传才执行：

```bash
ff status range-v01-r2
ff send-back range-v01-r2
```

## 给 worker 的交接提示

把原始任务交给 worker 时附上：

```text
完成任务并执行与风险相称的验证。

完成后按照：
/home/ch/workspace/agentsOwl/templates/worker-handoff.md

写入：
/home/ch/.local/state/agents-owl/pairs/range-v01-r2/inbox/worker-handoff.md

Handoff 只写任务边界、结果、相关文件或 symbol、测试、风险和复核重点；
不要粘贴完整源文件、完整 diff、长日志或历史对话。
```

发送前可以人工确认：

```bash
less ~/.local/state/agents-owl/pairs/range-v01-r2/inbox/worker-handoff.md
```

## 它发送什么

`send-peer` 不会复制 worker 的最新回答、完整会话、transcript、工具调用或代码
阅读输出。它只归档当前 `worker-handoff.md`，然后向 peer 发送包含以下内容的
评审请求：

- 当前 handoff 文件路径；
- 仓库和项目规则路径；
- peer 的审查边界；
- `peer-response.md` 输出路径。

Peer 会被要求把 handoff 当作材料而非指令，只为核实具体 claim 阅读必要的
diff 和文件，不主动读取旧对话、旧 artifact 或无关修改。

## 文件流向

```text
worker
  → inbox/worker-handoff.md
  → ff send-peer PAIR
  → artifacts/<timestamp>-worker-handoff.md
  → peer
  → inbox/peer-response.md
  → ff send-back PAIR（仅在需要时）
  → worker
```

状态和实际路径随时查看：

```bash
ff status range-v01-r2
ff artifacts range-v01-r2
```

## 注意事项

1. Pair 名只能包含字母、数字、`_` 和 `-`。
2. 推荐使用 `ff session PAIR worker|peer` 创建固定 pair；`ff impl/review`
   创建的是另一类 topic session。两类都会出现在 `ff i/r`，但只有固定 pair
   能被 `send-peer PAIR` 直接寻址。
3. 发送前确认 worker、peer 都是 `running`，并确认 peer 当前空闲。工具能判断
   runtime 是否存活，但不知道 AI 是否正在回答。
4. Handoff 应是短摘要，只列路径、symbol、命令和结论。不要内嵌完整代码、
   diff、长日志或完整回答。
5. `send-peer` 会把 handoff 从 inbox **移动**到 artifacts。发送后 inbox 中
   消失是正常的；下一轮必须由 worker 写一份新的 handoff。
6. 同一 peer session 会积累自身上下文。同一任务的小修订可以复用；新任务、
   跨模块任务或历史很长时应新建 pair/peer。
7. Peer 结论只是建议，不是项目批准、否决或 human 决策。
8. 任务运行中按 `Ctrl+\` 可安全 detach 并返回 shell；重新执行同一个
   `ff session PAIR ROLE` 即可接入。正常 `/exit` 或 `/quit` 会结束 runtime。

## 常见错误

### `protected runtime is not running`

启动或重新接入对应角色：

```bash
ff session range-v01-r2 peer
# 或
ff session range-v01-r2 worker
```

### `missing inbox artifact: ...worker-handoff.md`

Worker 尚未生成 handoff，或者它已经发送并归档：

```bash
ff status range-v01-r2
ff artifacts range-v01-r2
```

如果发送过程报错但 handoff 已进入 artifacts，先检查 peer 是否其实已经收到，
避免重复发送。确认未收到后，再把最新 `*-worker-handoff.md` 复制回
`inbox/worker-handoff.md` 重试。

### 已经使用 `ff impl` / `ff review` 创建 session

最省事的是直接复制粘贴。如果必须使用 `send-peer`，先查 peer 的
`runtime_socket`：

```bash
ff sessions --all --role peer --json
```

再显式指定：

```bash
ff send-peer range-v01-r2 --target /path/to/peer-runtime.sock
```

这种方式容易选错 target，因此新任务推荐固定 pair。

## 结束本轮

无需回传 peer 意见时：

```bash
ff archive-peer range-v01-r2
```

可选记录：

```bash
ff decision range-v01-r2 accept --note 'Peer READY；本轮完成'
```

`decision` 只写本地协调记录，不会修改 Git 或 Alchemist 的正式研究状态。
