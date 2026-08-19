# BIA 架构视图地图

状态：Accepted  
版本：1.0  
日期：2026-08-19

本文以系统架构中的原始 Markdown 总图为入口，并提供四张正式专题图和一张实验性六层补充图。每张专题图只回答一组问题，避免把静态分层、动态执行、DNA 演化和部署数据流压进同一张“蜘蛛网”。Archify 图使用英文技术标签以保证 SVG 跨平台显示；本页提供中文阅读说明。

## 1. 阅读顺序

| 顺序 | 视图 | 回答的问题 | 主要读者 |
|---:|---|---|---|
| 1 | 原始 Markdown 总体逻辑架构 | BIA 的四级稳定分层和平台内部主链路是什么 | 所有人 |
| 2 | Agent 单次运行与恢复 | 一个目标如何变成结果，失败如何恢复 | 开发、测试、运维 |
| 3 | DNA 执行与演化 | DNA 如何驱动执行，Candidate 如何受控晋级 | 架构、算法、治理 |
| 4 | 技术底座能力地图 | 哪些能力可以独立复用于其他领域项目 | 平台负责人、集成方 |
| 5 | 部署与数据流 | 进程、事实库、制品、外部 Skill 如何连接 | 开发、运维、安全 |

## 2. 总体逻辑架构主参考

[查看系统架构中的原始 Markdown 总图](system-architecture.md#22-原始-markdown-总体逻辑架构主参考)。该文本图是当前正式主参考，因为它比六层 Archify 图更紧凑，也更清楚地体现了 Quant Application、Domain SDK、Active Agent Platform 与 BrainAgent Kernel 的上下关系。

### 六层与三条主线补充视图

六层图降级为实验性参考，不在文档入口直接展示：

- [查看 SVG](../assets/archify/bia-logical-architecture.architecture.svg)
- [打开 Archify 交互版](../assets/archify/bia-logical-architecture.architecture.html)

六层从用户价值向稳定底座逐层收敛：

1. 用户价值与交付层：CLI、Query API、MarketInsight、通知和报告；
2. 领域应用与接入层：Quant Agent、Domain SDK、Adapter 和领域 Skill；
3. Agent 组织与行为编码层：Organization、Agent、Workflow DNA 与固定 SkillBinding；
4. 治理与执行平台层：Planner、RiskGate、ExecutionGrant、MotorExec、Runtime 和 Recovery；
5. 类脑内核层：生命周期、LoopEngine、认知周期、状态机、Memory 和 Clock；
6. 事实、事件与可观测层：SQLite、Artifact Store、EventBus、Outbox、Outcome 和 Trace。

右侧三条主线用于横向检查系统完整性：

- 执行主线：`Goal → Plan → Grant → Workflow → Skill → Outcome`；
- DNA 主线：`Organization → Agent → Workflow → Frozen Identity`；
- 事实主线：`Plan/Task/Run → Evidence/Trace → Insight/Delivery`。

## 3. Agent 单次运行与恢复

[![BIA Agent 运行与恢复](../assets/archify/bia-runtime-lifecycle.architecture.svg)](../assets/archify/bia-runtime-lifecycle.architecture.html)

成功路径固定为“入口归一化 → Active DNA 解析 → 身份冻结 → 认知周期 → Plan/RiskGate → Grant-only Runtime → Outcome/Evidence → Insight Delivery”。失败路径不依赖调用方猜测，而由统一错误分类、恢复矩阵和持久化事实决定重试、恢复、补偿或 RestRepair。

不可破坏的不变量：

- 没有 ExecutionGrant 就不能发生副作用；
- 重试沿用冻结的 DNA identity，不重新漂移选择；
- 事实先于外部交付持久化；
- NON_REPLAYABLE 工作禁止盲目重试；
- Task 成功不等于目标成功，最终价值由 Outcome 和 Evidence 判定。

## 4. DNA 执行与受治理演化

[![BIA DNA 执行与演化](../assets/archify/bia-dna-evolution.architecture.svg)](../assets/archify/bia-dna-evolution.architecture.html)

上半环是执行闭环：三层 Active DNA 组成冻结身份，通过治理执行产生可归因 Evidence。下半环是演化闭环：Fitness、Experience Dataset、Candidate、Sandbox Replay、Population Selection、Shadow/Canary 和 Registry 构成逐级晋级门。

自动化只能产生 Candidate；Active DNA 不能原地修改，也不能绕过 RiskGate、权限、预算、Replay 和 Promotion Gate。所有生成、选择、晋级和回滚均进入谱系与追加式审计。

## 5. 技术底座能力地图

[![BIA 技术底座能力地图](../assets/archify/bia-platform-capability-map.architecture.svg)](../assets/archify/bia-platform-capability-map.architecture.html)

底座是否完整，不由 Quant 功能数量证明，而由九类领域无关能力及其公共契约证明：Kernel、Runtime、Reliability、Governance、Intelligence Loop、DNA Control Plane、Observability、Integration 和 Delivery。量化只是通过 Domain SDK 接入的第一个领域，不能反向污染 Kernel 和 Runtime。

## 6. 部署与数据流

[![BIA 部署与数据流](../assets/archify/bia-deployment-dataflow.architecture.svg)](../assets/archify/bia-deployment-dataflow.architecture.html)

部署图区分三种数据：

- 事实源：Runtime Facts、DNA Registry/Audit、Inbox/Outbox，不允许用内存状态替代；
- 可重建投影：MarketInsight、Health 和 Diagnostics Read Model；
- 外部制品与副作用：Artifact、领域 Skill、外部数据和通知渠道，必须经过固定 Binding、Grant 或事务 Outbox。

## 7. 视图边界

原始 Markdown 总图承担总体逻辑架构主参考职责；`bia-logical-architecture` 仅作为六层检查视图；`bia-system.architecture` 是系统上下文图。字段、状态机、事务和 API 的精确语义仍以专项规范为准，任何架构图都不是机器契约。
