# 断线安全的原生会话

## 边界

AgentsOwl 只补 provider 原生 CLI 缺少的 SSH 断线保护，不替代其会话系统：

```text
terminal ⇄ dtach PTY ⇄ Claude/Codex native TUI
                         └─ provider-owned session ID + transcript
```

`dtach` master 持有 PTY。SSH 或终端异常消失时，只有前端连接断开，正在推理、
调用工具或写代码的 agent 继续运行。它不解释终端数据流，因此没有窗口、状态
栏、copy mode 或额外 UI。

AgentsOwl 索引的唯一键是 `(provider, native_session_id)`。`native_name`、
topic、role、repo、lifecycle、runtime socket 和关联会话都是外部索引字段；
transcript 始终归 Claude/Codex 所有。

索引默认写在：

```text
~/.local/state/agents-owl/sessions/index.json
```

runtime socket 默认写在：

```text
~/.local/state/agents-owl/runtimes/<hash>.sock
```

索引写操作使用进程锁，并通过同目录临时文件、`fsync` 和原子替换提交。

## 首次设置

安装透明 PTY 保护器：

```bash
sudo apt install dtach
```

如果可执行文件不叫 `dtach`，可以设置 `AGENTS_OWL_DTACH_CMD`。

Codex 无需额外配置。AgentsOwl 使用 app-server 创建并命名原生 thread，然后
在受保护 PTY 中执行 `codex resume <native-id>`。

Claude 需要一次性安装 `SessionStart`/`SessionEnd` hooks：

```bash
agents-owl hook install-claude --retention-days 3650
```

安装命令会原子合并 `~/.claude/settings.json`，保留已有字段和 hooks，并可
重复运行。普通 Claude 会话不会进入索引；只有 AgentsOwl 设置了
`AGENTS_OWL_MANAGED_SESSION=1` 的会话才会登记 provider 原生 ID。

## 日常流程

如果为项目配置了 `ff` alias，最短入口是：

```bash
ff                 # 全部会话
ff i               # worker 列表
ff r               # peer 列表
ff impl [TOPIC]    # 新建 Claude worker
ff review [TOPIC]  # 新建 Codex peer
```

这些只是 AgentsOwl 内置子命令的短入口，不是 shell 中五套独立逻辑。

新主题新会话：

```bash
agents-owl session new \
  --provider claude \
  --role worker \
  --topic 'E-021 post-impulse scan'
```

参数缺失且当前是交互终端时会逐项询问。默认原生名称是
`<topic> [<role>]`，也可以用 `--name` 指定。

该命令直接 attach 到受保护的原生 TUI。`dtach` 的 detach 和 suspend 按键
处理已禁用，键盘输入原样交给 provider；日常无需学习另一套快捷键。

连接异常中断后：

```bash
agents-owl sessions
# 选择 running 会话 → attach
```

也可以直接指定：

```bash
agents-owl session attach SESSION
```

`SESSION` 可以是完整索引键、原生 ID、唯一 ID 前缀或唯一原生名称。只有仍在
运行的进程可以 attach。重新连接时 AgentsOwl 会发送一次 `Ctrl-L`，强制
Claude/Codex 重绘全屏 TUI；这不会提交输入或改变会话内容。

如果 agent 已经正常退出，runtime socket 会自动消失，会话显示为 `exited`。
此时使用 provider 原生 ID 恢复，但仍由 AgentsOwl 重新加上断线保护：

```bash
agents-owl session resume SESSION
```

`resume` 遇到仍在运行的 runtime 时会直接 attach，不会启动第二个 TUI。

同一 topic 需要可选 peer 时使用完全相同的 topic：

```bash
agents-owl session new \
  --provider codex \
  --role peer \
  --topic 'E-021 post-impulse scan'
```

同 repo、同 topic 的原生会话自动写入彼此的 `related_session_keys`。这只是
索引关联，不引入审批或强制交叉验证。

## 列表与生命周期

```bash
agents-owl sessions
agents-owl sessions --no-select
agents-owl sessions --all
agents-owl sessions --provider codex
agents-owl sessions --role worker
agents-owl sessions --global
agents-owl sessions --json
```

交互终端中会显示编号列表并询问 action；pipe 或脚本环境只输出列表。

索引存储 `active/completed/archived/missing`。显示层把 active 细分为：

- `running`：runtime socket 可连接，agent 进程仍然存活；
- `exited`：保护 runtime 已正常结束，可以用原生 ID resume；
- `orphaned`：socket 仍存在但 master 不可连接；resume 会清理该 stale socket
  并重新启动；
- `completed`：主题任务已标记完成；
- `archived`：默认列表隐藏；
- `missing`：已知原生对象在 provider 侧不可用。

## 正常退出与完成

正常退出仍使用 provider 自己的命令（Claude `/exit`、Codex `/quit`）。退出后
agent 进程结束，`dtach` 自动移除 runtime socket；AgentsOwl 不改变、复制或
删除原生历史。

主题完成后再标记：

```bash
agents-owl session finish SESSION --outcome '完成 E-021；测试通过'
```

如果进程仍在运行，`finish` 会拒绝操作，避免把活跃任务从列表中隐藏。它会
保存 outcome、完成时间和当前 repo 的 Git HEAD（如果存在）。

## Rename 与 archive

```bash
agents-owl session rename SESSION '新名称'
agents-owl session archive SESSION
agents-owl session unarchive SESSION
```

- Codex rename/archive/unarchive 调用 app-server，然后更新索引。
- Claude rename 会向仍在运行的保护 runtime 发送 `/rename`；进程未运行时，
  先 resume 后在原生 TUI 中操作。
- Claude 没有与 Codex app-server 对称的外部 archive API，因此 archive 只影响
  AgentsOwl 列表，不触碰 provider transcript。
- 运行中的 session 不能 archive。

查看 Codex provider 返回的 native metadata：

```bash
agents-owl session inspect SESSION --native
```

## 为什么不用 provider 后台命令

Claude Code 有自己的后台 agent 功能，但 Codex 没有完全对称的本地 TUI
迁移机制。AgentsOwl 选择在两者之外提供同一层 PTY 生存语义：不依赖 provider
特性，不改变原生 session 身份，也不会把 Claude/Codex 的 transcript 复制到
工具自己的数据库。

## Provider 事实来源

- [dtach manual](https://manpages.debian.org/testing/dtach/dtach.1.en.html)
- [Codex app-server](https://developers.openai.com/codex/app-server/)
- [Codex CLI reference](https://developers.openai.com/codex/cli/reference/)
- [Claude Code sessions](https://code.claude.com/docs/en/sessions)
- [Claude Code hooks](https://code.claude.com/docs/en/hooks)
