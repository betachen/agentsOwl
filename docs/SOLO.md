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

同一个“目录 + provider + 名称”对应一个运行中的会话。默认名称是 `default`。
想另开一份上下文时给一个新名称：

```bash
ff solo claude --name quick-fix
ff solo claude --name explore
ff solo codex --name quick-fix
```

每条命令都是独立会话，重连时使用相同命令。正常退出 agent 会结束对应 runtime；
下次使用该命令会开启新对话，不自动 resume 历史。

solo 会话通过 `ff solo ...` 接回，不出现在 worker/peer 的 `ff i/r` 列表。

## 工作目录与上下文

- 默认使用调用命令时的确切目录，子目录不会被替换成 Git 根目录，非 Git 目录也能用。
- 不采用继承的 `AGENTS_OWL_REPO`；可以明确使用 `ff --repo /path/to/task solo claude`。
- 不读取 AgentsOwl 的项目配置，不加载 Alchemist 的材料或交接模板，不设置 worker/peer 角色。
- 启动时移除继承的 `AGENTS_OWL_*` 协作变量与旧 worker/peer 命令变量，避免误登记为 managed session。
- 正常保留 provider 的全局设置、认证环境及适用于当前目录的项目规则。solo 不是配置隔离或文件访问沙箱。
- runtime socket 位于 AgentsOwl state 目录，默认是 `~/.local/state/agents-owl/runtimes/`，不在任务目录生成额外文件。

`ff` 的 alias 应直接指向工具，不要带固定项目的 `cd`：

```bash
alias ff='/home/ch/workspace/agentsOwl/bin/agents-owl'
```
