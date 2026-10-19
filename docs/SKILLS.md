# 在 Claude 和 Codex 中使用 AgentsOwl skill

仓库中的 `skills/agents-owl/SKILL.md` 保存一份共享流程：识别当前 pair 和角色、
完成任务后写入最新 handoff、独立复核、按要求发送和回传。它不包含任何
Alchemist 特定的研究条件，目标项目的规则仍由该项目提供。

安装到当前用户的两个客户端：

```bash
cd /path/to/agentsOwl
python3 scripts/install_skills.py
```

安装器创建符号链接：

- Codex：`~/.agents/skills/agents-owl`
- Claude Code：`~/.claude/skills/agents-owl`

两个链接都指向当前仓库中的 `skills/agents-owl`，更新仓库就会更新 skill。
因此请保留仓库路径。安装器可以重复执行；同名目录或指向其他位置的链接会
报错并保留原内容。如果客户端使用自定义目录，可指定：

```bash
python3 scripts/install_skills.py \
  --codex-skills-dir /custom/codex/skills \
  --claude-skills-dir /custom/claude/skills
```

旧版 Codex 若只扫描 `~/.codex/skills`，可以通过 `--codex-skills-dir` 指定该目录。
不要在同一个客户端的多个扫描目录重复安装同名 skill。

## 如何使用

客户端可根据 AgentsOwl / ff / pair 等语境自动选择 skill。想确保本次加载：

Codex：

```text
$agents-owl 修复这个问题，完成后写交接。
```

Claude Code：

```text
/agents-owl 修复这个问题，完成后写交接。
```

新启动的固定 pair 会话会带上 `AGENTS_OWL_PAIR`、`AGENTS_OWL_ROLE`、
仓库、state 和输出路径，skill 可直接识别，不必再重复说明交接文件在哪里。
skill 按当前角色操作，不按 Claude/Codex 推断角色。

现有运行中的 session 不会获得新增的环境变量。无需中断任务：首次加载时说明
一次，例如 `worker explore-alpha：完成本轮并写交接`，之后沿用当前会话上下文。
如果 skill 暂时没有出现在客户端的 skill 列表里，在任务结束后重启客户端再加载。

自动选择由客户端模型决定，不能保证任意一句话都触发。安装 skill 也不等于恢复
项目历史；新任务仍需说明目标。skill 复用的是 AgentsOwl 操作背景。

## 交接行为

Worker 完成本轮任务后，将当前结果写入 `inbox/worker-handoff.md`；peer 完成
复核后写入 `inbox/peer-response.md`。在请求发送时才执行 `send-peer/send-back`，
不会自行启动循环复核，也不会把 peer 的建议当成开发 gate。

Skill 采用 `skill-creator` 的按场景加载方式，将细节交给仓库已有模板和文档，
避免把整份手册加入每次任务上下文。`solo` 和未选择 pair 的普通任务不承担交接义务。

客户端的发现和调用方式参考 [Codex skill 文档](https://learn.chatgpt.com/docs/build-skills)
及 [Claude Code skill 文档](https://code.claude.com/docs/en/skills)。
