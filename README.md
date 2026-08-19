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

当前已完成 A01～A07 工程基线、B01～B06 可靠事件/契约/状态/时间链路、C01～C04 Workflow 静态定义内核及 E01～E06 主动认知输入链路：基础设施、LoopEngine/Supervisor、Error、SQLite、确定性皮层调度、Domain SDK、Composition Root、EventBus、事务型 Inbox、可确认恢复重投的 Outbox、Event Envelope/Payload 1.0 契约、三维 StateController、持久化 Scheduler、JSONL Sensory、白名单 CommandAdapter、WorldModel、确定性 Attention、固定 GoalPolicy、CognitiveCoordinator、有界 WorkingMemory、Workflow Registry、DAG 校验和受限表达式。WorkflowRuntime 与完整 Skill Runtime 按 [MVP 开发计划](docs/delivery/development-plan.md)继续开发。

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
