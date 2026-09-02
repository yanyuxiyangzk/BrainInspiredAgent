# 实施路线图

状态：Accepted / Frozen baseline

阶段 0 当前状态：`Accepted / Frozen`，详见[冻结就绪报告](stage-0-freeze-readiness.md)。

具体任务、依赖与 Sprint 顺序见[开发任务规划](development-plan.md)。

## 阶段 0：需求与设计冻结

目标：在编码前消除会导致大规模返工的歧义。

交付物：

- 愿景、PRD、术语和系统边界；
- 消息与 Workflow 1.0 草案；
- 安全模型、测试策略和 ADR；
- MVP 场景时间线和模拟数据约定；
- 开放问题评审结果。

退出条件：P0 范围、状态模型、投递语义、恢复语义和能力分级被接受。

## 阶段 1：确定性运行内核

实现 Supervisor、EventBus、Inbox/Outbox、Clock、Scheduler、状态模型、SQLite、基础 Trace 和优雅停机。先不接 LLM；同步建立 WorldModel、Workflow Registry 和 Capability Contract 的最小接口。

退出条件：虚拟一天时间线可稳定重放，进程重启后状态一致。

## 阶段 2：Workflow JSON 与 Skill 系统

实现 Workflow Registry、Schema 校验、DAG 调度、Skill Registry/Resolver、核心节点、超时重试、并发限制、Fake Skill 和幂等副作用。

退出条件：市场摘要与夜间复盘两个示例 Workflow 通过故障注入测试。

## 阶段 3：受限智能决策

实现 Attention、GoalPolicy、WorldSnapshot、CognitiveCoordinator、LLM Adapter、CandidatePlan、PlanDecision、ExecutionGrant、PlanValidator、RiskGate 和调用预算。

退出条件：非法输出和越权计划全部被拦截；无显著事件时不持续调用模型；同一认知周期不产生冲突计划。

## 阶段 4：MVP 管理能力

实现 OutcomeEvaluator、夜间复盘、健康、状态、任务、Trace 和 CLI；完善指标、告警、恢复和 Runbook。

退出条件：模拟环境连续运行 7 天并满足 PRD 指标。

## 阶段 5：经验学习

建立 WorkflowPatch、候选版本、历史重放、Outcome 评价、语义记忆评估集、候选经验验证、矛盾处理和过期机制，再决定向量数据库。

退出条件：离线评估证明加入记忆后决策质量提升，且错误召回在可接受范围。

## 阶段 6：量化适配

按顺序接入 easy-tdx、RPS/板块轮动、Qlib、每日回测，最后评估 RD-Agent。每种能力以 Skill Manifest 注册，并通过 capability contract 与 Workflow 解耦。

退出条件：数据版本、时间对齐、样本外验证及复现实验均通过专项验收。

> 2026-09-02 调整：经验学习提前到量化适配之前——先在 Fake Market 数据上闭环
> 记忆与经验验证，再接入真实数据源，避免数据适配与学习闭环两条风险线叠加。

## 近期建议顺序

1. 完成阶段 0 最终签字，冻结 Event、Workflow、Skill、PlanDecision、ExecutionGrant、Task 和 Error 契约。
2. 按开发计划领取 `A01 → A02 → A03/A04/A05 → M0-02/T01`。
3. 从无 LLM 的确定性 LoopEngine、SQLite 和可靠事件内核开始实现。
4. 每个 Sprint 按开发计划更新任务状态与测试证据。
5. MVP 发布后再启动 Workflow 双向进化的回放和影子阶段。
