# Solo：当前目录里的独立 agent

```bash
cd /path/to/your/task
ff solo claude
# 或
ff solo codex
```

无需 `init`、pair、topic、peer 或交接文件。直接启动 PATH 中的 `claude` /
`codex`，由 dtach 保持后台运行，保留 agent 原生交互界面。

## 离开与返回

按 `Ctrl+\` 返回 shell，任务继续。SSH 断开也不会结束后台 agent。
回到同一目录，执行原命令即可重连，例如 `ff solo claude`。

重新 attach 后，原生 TUI 通常只显示当前视图，不会把整段旧对话重新绘制到主
界面。这不代表上下文丢失。Codex 中按 `Ctrl+T` 打开完整 transcript，用方向键
或 PageUp/PageDown 滚动，按 `q` 返回主界面。

同一个“目录 + provider + 名称”对应一个运行中的会话。默认名称是 `default`。
想另开一份上下文时给一个新名称：

```bash
ff solo claude --name quick-fix
ff solo claude --name explore
ff solo codex --name quick-fix
```

每条命令都是独立会话，重连时使用相同命令。正常退出 agent（`Ctrl+D`、
`/exit`、`/quit`）会结束对应 runtime；下次使用同一命令会恢复同一个 provider
原生对话（Claude `--resume <id>`、Codex `resume <id>`），上下文保持不变。
如果上次没有产生任何对话，Claude 沿用同一 ID 重新开始。

需要为同一名称开启全新对话时：

```bash
ff solo claude --fresh
```

`--fresh` 只在 runtime 未运行时生效；运行中的会话仍直接重连。

solo 会话通过 `ff solo ...` 接回，不出现在 worker/peer 的 `ff i/r` 列表。

## 工作目录与上下文

- 默认使用调用命令时的确切目录，子目录不会被替换成 Git 根目录，非 Git 目录也能用。
- 不采用继承的 `AGENTS_OWL_REPO`；可以明确使用 `ff --repo /path/to/task solo claude`。
- 不读取 AgentsOwl 的项目配置，不加载 Alchemist 的材料或交接模板，不设置 worker/peer 角色。
- 启动时移除继承的 `AGENTS_OWL_*` 协作变量与旧 worker/peer 命令变量，避免误登记为 managed session。
- 正常保留 provider 的全局设置、认证环境及适用于当前目录的项目规则。solo 不是配置隔离或文件访问沙箱。
- runtime socket 位于 AgentsOwl state 目录，默认是 `~/.local/state/agents-owl/runtimes/`，不在任务目录生成额外文件。
- Codex 首次启动由原生 TUI 创建 rollout；产生 rollout 后，“目录 + provider + 名称”
  与原生会话 ID 的绑定保存在 `~/.local/state/agents-owl/solo/native-sessions.json`。

`ff` 的 alias 应直接指向工具，不要带固定项目的 `cd`：

```bash
alias ff='/home/ch/workspace/agentsOwl/bin/agents-owl'
```
