# BIA 文档中心

## 文档状态标记

- `Draft`：草案，允许较大调整。
- `Review`：等待评审，核心结构基本稳定。
- `Accepted`：已接受，修改需同步评估影响。
- `Deprecated`：已废弃，仅保留历史参考。

各文档以页首状态为准：核心产品文档处于 `Review`，技术规范多数处于 `Draft`，部分基础 ADR 为 `Accepted for initial design`。

## 产品文档

- [产品愿景与范围](product/vision-and-scope.md)：为什么做、为谁做、首版做什么。
- [产品需求文档 PRD](product/prd.md)：场景、功能、非功能需求和验收指标。

## 技术文档

- [系统架构](architecture/system-architecture.md)：BrainAgent Engine 类脑底座、Loop 调度层、脑区边界、Workflow/Skill、量化场景、运行模型、约束与演进总览。
- [架构视图地图](architecture/architecture-views.md)：六层总体逻辑架构、运行恢复、DNA 演化、底座能力和部署数据流的统一入口。
- [DNA 技术架构](architecture/dna-architecture.md)：Organization/Agent/Workflow 三层 DNA、执行身份、持久化控制面和受治理演化闭环。
- [平台与领域应用分层](architecture/platform-domain-separation.md)：Kernel、通用平台、Domain SDK 与 Quant App 的依赖和扩展边界。
- [事件协议](specifications/event-protocol.md)：EventBus 及消息契约。
- [Workflow 规范](specifications/workflow-spec.md)：声明式任务格式和运行语义。
- [Plan、Task 与 Error 契约](specifications/plan-task-error-spec.md)：规划、执行状态和统一错误模型。
- [运行时数据模型与事务规范](specifications/runtime-data-and-transactions.md)：SQLite 事实表、Inbox/Outbox、事务边界和崩溃恢复。
- [Skill 调用与适配协议](specifications/skill-invocation-protocol.md)：Invocation、Result、取消、恢复、资源与适配边界。
- [记忆系统](architecture/memory-system.md)：工作、情景、语义、程序与策略记忆。
- [可进化 Workflow 与 Skill 架构](architecture/evolvable-workflow-skill-architecture.md)：能力契约、动态绑定与双向进化。
- [Loop Engineering 因子发现架构](architecture/factor-discovery-loop-architecture.md)：检查点、生成-审查-验证闭环、FSA、自适应搜索与 Hooks。
- [安全与治理](architecture/safety-and-governance.md)：权限、风险门、预算和审计。
- [可观测性与运维](operations/observability-and-operations.md)：日志、指标、追踪、恢复与值守。
- [MVP 运维 Runbook](operations/mvp-runbook.md)：队列、SQLite、Skill、费用、副作用、崩溃循环和 SAFE_MODE 处置步骤。
- [测试与验收](quality/test-and-acceptance.md)：测试层次、故障场景与发布门槛。
- [MVP P0 验收标准](quality/p0-acceptance-criteria.md)：逐条可自动化验收标准。
- [Markdown 渲染测试](quality/markdown-rendering-test.md)：表格、代码、图片、公式、脚注、提示框等兼容性测试。
- [MVP 端到端场景](scenarios/mvp-end-to-end-scenarios.md)：市场摘要与夜间复盘的可执行场景。

## 交付与决策

- [实施路线图](delivery/roadmap.md)：从文档冻结到 MVP 和量化扩展的阶段计划。
- [开发任务规划](delivery/development-plan.md)：任务分组、依赖、Sprint 顺序和完成定义。
- [开放问题](delivery/open-questions.md)：尚未决定且会影响实现的事项。
- [阶段 0 冻结就绪报告](delivery/stage-0-freeze-readiness.md)：准入核对、配置基线和冻结动作。
- [T06 发布验收记录](delivery/t06-release-validation.md)：虚拟 30 天结果与真实 24 小时 soak 状态。
- [MVP 需求评审记录（2026-08-16）](reviews/requirements-review-2026-08-16.md)：评审结论、分级问题、追踪矩阵和冻结条件。
- [技术架构复查（2026-08-16）](reviews/technical-architecture-review-2026-08-16.md)：认知闭环、运行语义与冻结前优化建议。
- [术语表](glossary.md)：统一概念和命名。
- [ADR-0001：控制平面与 LLM 的边界](decisions/ADR-0001-deterministic-control-plane.md)
- [ADR-0002：进程与事件循环模型](decisions/ADR-0002-runtime-model.md)
- [ADR-0003：MVP 产品基线](decisions/ADR-0003-mvp-product-baseline.md)
- [ADR-0004：基于能力契约的可进化 Workflow](decisions/ADR-0004-capability-bound-evolvable-workflows.md)

## 文档维护规则

1. PRD 描述“需要什么”，架构文档描述“如何满足”，避免相互混写。
2. 影响系统边界、数据契约或安全模型的决定必须新增 ADR。
3. 协议发生不兼容变化时升级主版本，并提供迁移说明。
4. 实现与文档不一致时，视为缺陷；不能用代码事实替代设计决定。
5. 每个里程碑结束时更新路线图、开放问题与验收结果。
