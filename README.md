# BIA：类脑主动式 AI Agent

BIA（Brain-inspired Initiative Agent）是一个持续运行、事件驱动、可审计的主动式 Agent 项目。当前已冻结 MVP 核心设计，并按开发计划进入实现。

## 项目原则

- 主动性来自事件、目标和调度，而不是无限调用大模型。
- LLM 只生成候选计划；确定性规则负责校验、授权和风险控制。
- 脑区是常驻服务，Workflow 是一次性任务定义。
- 业务脑区通过事件协作；日志、配置、时钟等基础设施通过接口注入。
- 第一版只做研究、监控、回测与通知，不连接真实交易。
- 所有关键决定、输入、输出与副作用均可追踪、可恢复、可复现。

## 文档入口

完整导航见 [docs/README.md](docs/README.md)。建议按以下顺序阅读：

在线浏览：启动 `npm run docs:serve` 后打开 [http://localhost:4173/docs-site/](http://localhost:4173/docs-site/)。页面左侧是文档树，右侧是正文和本页目录。

1. [产品愿景与范围](docs/product/vision-and-scope.md)
2. [产品需求文档 PRD](docs/product/prd.md)
3. [系统架构](docs/architecture/system-architecture.md)
4. [事件协议](docs/specifications/event-protocol.md)
5. [Workflow 规范](docs/specifications/workflow-spec.md)
6. [实施路线图](docs/delivery/roadmap.md)

## 工程分层

```text
brain_kernel → active_agent_platform → domain_sdk → apps/quant_agent
```

当前已完成 Kernel、Platform、Domain SDK、量化 Fake Skills、DNA 演化链和可运行量化闭环。外部 CLI 命令经 Outbox/Inbox、Planner、RiskGate、Grant、Workflow Runtime、Outcome 和 Insight 投影处理；重启保持幂等和完整 Trace。当前不接真实交易，行情、摘要和通知默认使用本地 Fake Adapter。

## 本地开发

项目使用 `uv` 管理 Python 3.11、虚拟环境、依赖和锁文件，不使用系统 `pip`：

```bash
uv sync --dev
uv run ruff check .
uv run mypy
uv run pytest
uv run python -m compileall -q brain_kernel active_agent_platform domain_sdk apps
```

运行非量化可移植装配示例：

```bash
uv run python -m apps.hello_research
```

该入口会通过 Domain SDK 装配 Capability、两个可替换 Skill、Workflow、LoopProfile 和 OutcomeEvaluator，初始化 SQLite 与 LoopEngine 后干净退出并输出注册摘要。

## 启动量化闭环

推荐先安装本地命令，然后直接进入交互终端；不带子命令执行 `bia` 会自动启动 Runtime，数据默认保存在 `~/.local/state/bia/bia.db`：

```bash
uv tool install --editable .
bia
```

进入后使用 Slash Command：

```text
/market INDEX.TEST,INDEX.DEMO --title "今日市场摘要"
/commands
/insights
/health
/loop status
/loop services
/loop lag
/loop checkpoints
/help
/exit
```

在真实终端中输入 `/` 时会立即出现 Slash Command 菜单；继续输入 `/h`，菜单会实时过滤为 `/health`、`/help`。可以用上下键选择、回车执行，也可以继续输入完整命令。

下面的非交互方式继续保留，适合脚本、systemd 和自动化调用。

终端一启动常驻 Runtime：

```bash
export BIA_DB="$HOME/.local/state/bia/bia.db"
bia --database "$BIA_DB" run
```

默认在 `Asia/Shanghai` 的交易日 18:00 执行日复盘。时间、时区、触发窗口和停机错过策略均可配置，例如：

```bash
bia --database "$BIA_DB" run --daily-review-at 18:30 \
  --daily-review-timezone Asia/Shanghai \
  --daily-review-missed-policy FIRE_ONCE
```

终端二提交并查询市场摘要：

```bash
bia --database "$BIA_DB" market summary --symbols INDEX.TEST,INDEX.DEMO
bia --database "$BIA_DB" commands
bia --database "$BIA_DB" insights latest
```

`market summary` 返回 message ID；使用 `bia commands MESSAGE_ID` 查看 `ACCEPTED/RUNNING/SUCCEEDED/FAILED`，成功后可用 `insights show/explain` 查看证据与完整 correlation 链。
