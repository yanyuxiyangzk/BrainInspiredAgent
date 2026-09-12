# BIA：类脑主动式 AI Agent

BIA（Brain-inspired Initiative Agent）是一个持续运行、事件驱动、可审计的主动式 Agent 项目。当前已冻结 MVP 核心设计，并按开发计划进入实现。

> **分支说明**：当前分支 `generic-core` 只包含领域中性的运行时（Kernel / Platform / Domain SDK 机制 / `brainagent` CLI / `hello_research` 样例）。量化应用（`apps/quant_agent`、`bia` 命令、市场摘要/日复盘/交互终端、因子回测与验收回放）位于 `main` 分支。

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
brain_kernel → active_agent_platform → domain_sdk → apps
```

`apps` 在本分支提供领域中性的 `brainagent` CLI（start/status/health/diagnose/metrics/
trace/migrations/evolution auto-plan/run，`--plugin module:PluginClass` 装配任意领域插件）与
`hello_research` 可移植装配示例。外部命令经 Outbox/Inbox、Planner、RiskGate、
Grant、Workflow Runtime、Outcome 处理；重启保持幂等和完整 Trace。

## 本地开发

项目使用 `uv` 管理 Python 3.11、虚拟环境、依赖和锁文件，不使用系统 `pip`：

```bash
uv sync --dev
uv run ruff check .
uv run mypy
uv run pytest
uv run python -m compileall -q brain_kernel active_agent_platform domain_sdk apps
```

## 上手：领域中性的运行时

初始化并检查运行时（SQLite 事实库默认在当前目录或 `--database` 指定）：

```bash
brainagent --database bia.db start
brainagent --database bia.db --plugin apps.hello_research.plugin:HelloResearchPlugin status
brainagent --database bia.db health
brainagent --database bia.db metrics
```

装配任意领域：实现 `DomainPlugin.contribute()`（Capability / Skill / Workflow /
LoopProfile / OutcomeEvaluator），用 `--plugin module:PluginClass` 注入，
无需修改 Kernel 或 Platform。

JSON 驱动的 Workflow 与 DNA 自动演化演示（真实模块、临时数据库）：

```bash
uv run python scripts/workflow_demo.py        # 五类节点全链执行
uv run python scripts/evolution_demo.py       # 弱点→候选→回放→选择→晋级
```

量化闭环（`bia` 命令、市场摘要、日复盘、交互终端）见 `main` 分支 README。
