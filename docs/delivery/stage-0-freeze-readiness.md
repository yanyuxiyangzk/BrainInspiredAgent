# 阶段 0 冻结就绪报告

状态：Accepted / Frozen  
检查日期：2026-08-17

最终技术复盘要求的六项 P0 已于 2026-08-17 补齐：事件协议对齐、PlanDecision/ExecutionGrant 授权闭环、运行时数据与事务规范、Workflow 控制节点强 Schema、Skill 调用协议、正式文档 Skill 术语统一。机器 Schema 语法及引用校验已通过；运行时契约测试列入 Sprint 0。

可进化 Workflow、Capability Contract、SkillResolver 与双向晋级管线已形成[专项架构](../architecture/evolvable-workflow-skill-architecture.md)和 [ADR-0004](../decisions/ADR-0004-capability-bound-evolvable-workflows.md)。具体实施顺序见[开发任务规划](development-plan.md)。

## 1. 总体结论

阶段 0 已由产品与技术负责人确认，正式进入 **Accepted / Frozen**。后续开发允许基于实现证据修订文档，但必须通过变更记录、兼容性评估和回归验收，不得静默改动冻结契约。

阶段 0 签字不等同于运行时实现完成：Schema 契约测试、Python 代码、E2E 和长稳测试均属于后续里程碑。

## 2. 准入核对

| 准入项 | 状态 | 证据 |
|---|---|---|
| 产品负责人确认四项产品决策 | 完成 | [ADR-0003](../decisions/ADR-0003-mvp-product-baseline.md) |
| 两条端到端场景完成详细设计 | 完成 | [MVP 端到端场景](../scenarios/mvp-end-to-end-scenarios.md) |
| 全部 P0 有唯一、可自动化验收标准 | 完成 | [P0 验收标准](../quality/p0-acceptance-criteria.md) |
| Event 1.0 契约候选 | 完成 | [事件协议](../specifications/event-protocol.md) |
| Workflow 1.0 契约候选 | 完成 | [Workflow 规范](../specifications/workflow-spec.md) |
| Plan/Task/Error 1.0 机器契约 | 完成 | [契约说明](../specifications/plan-task-error-spec.md) |
| Workflow/Node/Capability/Skill/Grant/Patch 机器契约 | 完成 | `schemas/` 目录；语法已校验，契约测试待 Sprint 0 |
| LoopEngine 应用级职责与调度边界 | 完成 | [系统架构](../architecture/system-architecture.md)第 2 节 |
| 单一 Skill 能力节点模型 | 完成 | [Workflow 规范](../specifications/workflow-spec.md)第 4 节 |
| 事件发布/订阅/持久化闭环 | 完成 | [事件协议](../specifications/event-protocol.md) |
| PlanDecision → ExecutionGrant → Task 闭环 | 完成 | [Plan/Task/Error 契约](../specifications/plan-task-error-spec.md) |
| SQLite 事实模型与 T1～T6 事务边界 | 完成 | [运行时数据与事务](../specifications/runtime-data-and-transactions.md) |
| Skill Invocation/Result/恢复协议 | 完成 | [Skill 调用协议](../specifications/skill-invocation-protocol.md) |
| Skill 恢复矩阵 | 完成 | Plan/Task 规范第 3 节 |
| 性能基线 | 完成 | 端到端场景第 4 节 |
| 费用预算默认值 | 完成 | 本报告第 3 节 |
| 数据保留和大对象策略 | 完成 | 本报告第 3 节及 AC-011-03 |
| 阶段 1 不接 LLM | 完成 | [路线图](roadmap.md) |

## 3. MVP 配置基线

这些是可配置默认值，不是写死的业务常量：

| 项目 | 默认值 |
|---|---|
| 部署 | Linux/Docker，单实例 |
| 资源基线 | 2 vCPU、4 GiB RAM、本地 SSD |
| 时区 | Asia/Shanghai；存储 UTC |
| 模拟数据 | 单事件不超过 4 KiB 的 JSONL |
| SQLite Trace 保留 | 90 天 |
| 本地大对象保留 | 30 天 |
| 聚合复盘保留 | 长期，后续按年归档 |
| Working Memory | 默认 1000 条，同时受 TTL 和体积限制 |
| 每日模型 Token 上限 | 100,000 tokens |
| 每日模型费用上限 | USD 5.00（500 cents） |
| 单计划默认总时限 | 300 秒 |
| 单 Workflow 默认并发 | 4 |
| `delay` 最大值 | 60 秒 |
| 优雅关闭宽限期 | 30 秒 |
| 感知间隔 | ACTIVE 15s、IDLE 40s、REVIEW 300s |

生产实现必须允许更严格的环境覆盖；达到预算 80% 告警，达到 100% 拒绝新模型调用。

## 4. 已关闭的需求评审问题

| 原问题 | 关闭方式 |
|---|---|
| RR-B01 首要用户不唯一 | ADR-0003 固定单人量化研究者 |
| RR-B02 缺少确定的业务闭环 | 两个 E2E 场景已定义 |
| RR-B03 P0 不可量化 | 每条 P0 已分配 AC 编号 |
| RR-B04 恢复语义不清 | 四类 Skill 恢复矩阵已定义 |
| RR-B05 缺 Plan/Task 契约 | JSON Schema 1.0-rc1 已提交 |
| RR-M01 外部命令优先级冲突 | CLI 拆为 FR-016 P0，认证 API 保持 P1 |
| RR-M02 夜间复盘优先级过低 | FR-012 已提升 P0 |
| RR-M03 性能条件不完整 | 事件大小、硬件和时延均已定义 |
| RR-M04 99% 分母不清 | 冻结定义见第 5 节 |
| RR-M05 七天反馈过慢 | 增加虚拟 30 天与 24h 分层门槛 |
| RR-M06 Attention 不可解释 | 场景和 AC 要求 rule_id/score/evidence |
| RR-M07 GoalManager 范围过大 | MVP 只支持固定系统目标 |
| RR-M08 数据保留未决 | 配置基线已定义 |

## 5. 指标口径冻结候选

- 计划触发成功率：分母为“依赖健康、预算充足、在有效窗口内且通过输入 Schema 的应触发实例”；分子为窗口 SLA 内创建获批计划的实例。策略拒绝、无效输入和演练故障不计入分母，但单独计数。
- 重复副作用：相同业务幂等作用域内，外部可观察副作用超过一次即计一次事故，目标为零。
- 恢复成功率：可自动恢复类别中，在恢复期限内进入正确终态且无重复副作用的任务数/全部可自动恢复任务数。
- 有意义事件与 LLM 调用比率：产生模型调用的显著事件数/全部模型调用数；同一显著事件重试单列，不用该指标掩盖重试。
- Workflow 成功率：SUCCEEDED / 全部进入 DISPATCHED 的非演练任务；取消、过期和策略拒绝分别报告。

## 6. 最终冻结评审动作

冻结会议只审以下内容：

1. 是否接受 ADR-0003 和两个 MVP 场景；
2. 是否接受 P0 AC 作为开发完成定义；
3. 是否接受 Event/Workflow/Plan/Task/Error 1.0-rc1；
4. 是否接受配置基线和指标口径；
5. 是否存在必须在阶段 1 前解决的新 Blocker。

签字结论：全部接受。相关核心文档状态更新为 `Accepted`，机器 Schema 使用 `1.0`，立即开始 Sprint 0 工程骨架。任何新增功能进入后续版本，不再塞入 MVP 冻结范围。

## 7. 签字记录

| 角色 | 决定 | 日期 | 说明 |
|---|---|---|---|
| 产品/技术负责人 | Accepted / Frozen | 2026-08-17 | 同意按最新 MVP 里程碑开工；实现发现问题时走变更评审 |
