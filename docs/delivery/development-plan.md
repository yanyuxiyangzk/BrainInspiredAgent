# MVP 开发实施计划

状态：In development（A01～A07、B01～B06、C01～C04、E01～E06 已完成）  
计划版本：MVP 0.1 / Plan 1.0-rc1  
最后核验：2026-08-19

本文是 MVP 唯一派工与进度基线。架构文档描述设计，本文只记录做什么、谁负责、依赖什么、如何验收以及是否真实完成。

## 1. 当前基线

| 标记 | 判定标准 |
|---|---|
| `⬜ 未开始` | 无实现产物 |
| `📝 文档完成` | 设计完成，不代表代码完成 |
| `🚧 进行中` | 已开始但未达到 DoD |
| `🛠 已开发` | 实现已提交，约定测试未全部通过 |
| `✅ 已开发已测试` | 实现与约定自动化测试均通过 |
| `⛔ 阻塞` | 存在明确外部阻塞，必须记录原因和解除条件 |

仓库现状：产品、架构、场景、P0 和技术契约文档已完成；在线文档站已开发并通过检查；15 份 JSON Schema 已开发并通过 Draft 2020-12 元规范及引用检查；A01～A07 工程基线、B01～B06 可靠事件/契约/状态/时间链路，C01～C04 Workflow 静态定义内核，以及 E01～E06 感知、世界模型、确定性注意力、固定目标策略、认知周期协调与工作记忆已完成并通过测试。Workflow Runtime、Skill Runtime、规划授权、完整 Agent 契约测试和业务 E2E 尚未开始。

状态只能依据代码、测试报告或可检查产物更新。部分 Schema 已有不代表对应运行时任务完成。

### 1.1 当前自动化验证基线

截至 2026-08-17，A01～A07、B01～B06、C01～C04、E01～E06 共执行 198 项自动化测试，全部通过；四层代码行/分支综合覆盖率为 95.41%。`uv run pytest` 已内置 95% 覆盖率失败门槛，CI 同步执行 Ruff、Mypy strict、Pytest/Coverage 和 Compileall。

当前用例进一步覆盖 SQLite WAL/外键/迁移 checksum/事务回滚/约束/并发串行化，皮层 ADMIT/DEFER/REJECT、aging、SAFE、期限、预算和解释字段，插件装配，EventBus 独立订阅、过滤、顺序、优先级、四类背压及 10,000 条持续投递性能基线，Inbox 去重/恢复/有限重试/死信，Outbox 原子提交/重投/确认/退避/恢复，Event Envelope/Payload 1.0 round-trip/注册/校验，StateController 三维状态，Scheduler 窗口/cooldown/missed trigger/过期/checkpoint，JSONL Sensory 的解析归一化、source sequence、重复/乱序/未来时间、数据质量和 CommandAdapter 白名单治理，WorldModel 事实投影/事件时间冲突/新鲜度/不可变快照，Attention 阈值/证据/去重/冷却/聚合指标，GoalPolicy 完成条件/期限/禁用状态/冲突域/预算契约/确定性选择，CognitiveCoordinator 窗口合并/焦点选择/快照固定/cycle ID/并发与冲突域锁/晚到事件隔离，WorkingMemory 容量/TTL/重要度淘汰/深度不可变快照/空启动与带事实来源重建，以及 Workflow Registry/版本 digest/Active 不可变、Workflow/Node/IO 静态校验、DAG 拓扑/环/分支/递归检查和受限 JSONPath/条件表达式。

## 2. MVP 范围

### 2.1 必须交付

- 单进程、一个应用级 LoopEngine、一个底层 asyncio event loop；
- EventBus、Inbox/Outbox、SQLite 事实模型和崩溃恢复；
- JSON 驱动 WorkflowRuntime 与五类节点；
- Capability Contract、Skill Registry/Resolver、固定 SkillBinding；
- Sensory、WorldModel、Attention、Coordinator、Prefrontal、RiskGate、MotorExec；
- Working/Episodic Memory、OutcomeEvaluator、RestRepair；
- CLI、健康、指标、Trace 和 Runbook；
- `market_summary` 与 `daily_review` 两条 E2E；
- 全部 P0 AC、虚拟 30 天、真实 24 小时稳定性验证。

### 2.2 明确不进入 MVP

真实交易、模拟下单、WebUI、Redis/Kafka/PostgreSQL、多实例、向量数据库、easy-tdx 实盘源、Qlib、RD-Agent、自动 Shadow/Canary、Workflow 自动激活、动态长期目标和通用 Saga 均不进入 MVP 0.1。

## 3. 角色与估算

| 缩写 | 责任角色 |
|---|---|
| TL | 技术负责人，架构与跨模块验收 |
| BE | Python/运行时开发 |
| AI | 认知、模型与评价开发 |
| QA | 自动化、故障注入与发布验收 |
| OPS | 构建、观测、运行和恢复 |

估算单位为人日，包含实现、单元测试和代码评审，不包含等待时间。单人顺序开发基线约 70～90 人日；2 名开发 + 兼职 QA 的目标周期为 8～10 周。估算在 Sprint 0 结束后根据实际速度校准，不作为完成证明。

## 4. 依赖与关键路径

```text
M0 契约冻结
 → M1 工程/LoopEngine/SQLite
 → M2 EventBus + Inbox/Outbox + Scheduler
 → M3 WorkflowRuntime + Skill Runtime
 → M4 PlanDecision/Grant + MotorExec
 → M5 认知闭环 + Outcome/Memory
 → M6 两条 E2E + P0 + 稳定性发布
```

关键路径：`A01 → A03 → A06 → B01 → B03 → C02 → C05 → D03 → D05 → F02 → F05 → E05 → G02 → T04 → T06`。任何关键路径任务延期必须在计划页记录影响，不通过隐藏并行工作压缩测试周期。

## 5. 可派工 Backlog

### 5.1 M0 契约与工程基线

| ID | 状态 | 负责人 | 依赖 | 估算 | 交付物/完成标准 | 验收 |
|---|---|---|---|---:|---|---|
| M0-01 | `📝 文档完成` | TL | - | 0 | 系统架构、事件、Workflow、授权、事务、Skill 协议冻结候选 | 阶段 0 报告 |
| M0-02 | `🛠 已开发` | TL/QA | M0-01 | 1 | 15 份 Schema；增加正例、反例和跨字段语义测试后完成 | AC-005-01、006-01、008-01 |
| A01 | `✅ 已开发已测试` | BE/OPS | M0-01 | 1.5 | `uv` 项目、锁文件、四层包布局、CI 和 lint/type/test/build 命令 | 本地同构 CI 全绿、AST 依赖方向及领域泄漏检查通过 |
| A02 | `✅ 已开发已测试` | BE | A01 | 1.5 | 不可变 Settings；UTC/单调/异步 Clock 与可推进 FakeClock；单调 UUIDv7 与 Fake；结构化 Logger；显式依赖容器 | 正常/异常/边界/Fake 注入测试及全套质量检查通过 |
| A03 | `✅ 已开发已测试` | BE/TL | A02 | 3 | 单次运行 LoopEngine、TaskGroup 故障隔离、顺序启动/反序关闭、虚拟时钟退避、60 秒崩溃窗口、关键服务 SAFE、限时排空/checkpoint/取消 | AC-001-01～04；并发竞争与故障注入测试通过 |
| A04 | `✅ 已开发已测试` | BE | A01,M0-02 | 1 | Error 1.0 不可变模型、核心/Skill 错误目录、目录扩展、ErrorFactory、5 层 cause、异常安全映射、details/Logger 统一递归脱敏 | JSON Schema 契约与泄密反例通过；A01～A04 共 50 项、覆盖率 97.75% |
| A05 | `✅ 已开发已测试` | BE/OPS | A02 | 2.5 | SQLite WAL/外键/busy timeout、前向 checksum 迁移、全部事实表和索引、显式事务、Repository 基类 | 迁移漂移/未知版本/原子回滚/约束/并发串行化测试通过 |
| A06 | `✅ 已开发已测试` | BE/TL | A02,A03 | 2.5 | DeterministicCorticalPolicy、稳定评分、ADMIT/DEFER/REJECT、aging、预算快照、规则/原因/下次评估 | AC-001-05；SAFE/期限/恢复/后台公平性和预算测试通过 |
| A07 | `✅ 已开发已测试` | BE/TL | A01,A05 | 2 | Composition Root、Domain SDK、不可变插件目录、跨引用校验、`hello_research` 五类扩展示例和模块入口 | 不修改 Kernel/Platform 完成装配；81 项全绿、综合覆盖率 98.63% |

里程碑出口：工程可安装；Kernel/Platform/SDK/App 包边界可检查；hello_research 可装配；所有 Schema 正反例通过；Fake Clock 可控；数据库可重复初始化；LoopEngine 启停无孤立协程；皮层策略对同一输入产生稳定、可解释结果。

### 5.2 M1 可靠事件与时间内核

| ID | 状态 | 负责人 | 依赖 | 估算 | 交付物/完成标准 | 验收 |
|---|---|---|---|---:|---|---|
| B01 | `✅ 已开发已测试` | BE | A03,M0-02 | 3 | Thalamus EventBus、独立有界订阅、类型/谓词过滤、source 内 FIFO/source 间优先级、WAIT/REJECT/DROP/COALESCE、生命周期与指标 | AC-002-01～03/05 及 AC-002-04 传输隔离通过；重试/死信由 B02 完成；101 项全绿、综合覆盖率 98.86% |
| B02 | `✅ 已开发已测试` | BE | A05,B01 | 2 | 事务型 Inbox、`msg_id`/业务 `dedup_key` 去重、持久化 attempt、可注入有限指数退避、终态幂等、死信和消费事务模板 | AC-002-04、011-02；111 项全绿、综合覆盖率 98.22%，Ruff/Mypy strict/构建通过 |
| B03 | `✅ 已开发已测试` | BE | A05,B01 | 2.5 | 事务型 Outbox Writer、原 envelope/ID 重投、发布确认、无限保留的封顶指数退避、稳定批次与启动恢复 Relay、ManagedService 生命周期 | AC-011-02；确认前崩溃按至少一次语义重投并由 Inbox 去重；123 项全绿、综合覆盖率 97.66% |
| B04 | `✅ 已开发已测试` | BE/QA | M0-02,B01 | 1.5 | Event Envelope/Payload 1.0 不可变代码模型、JSON Schema 注册表、事件类型/优先级元数据、Bus 与 Outbox 入队前校验和未知类型拒绝 | AC-002-01、003-03；130 项全绿、综合覆盖率 96.12%，Ruff/Mypy strict/Compileall/文档检查通过 |
| B05 | `✅ 已开发已测试` | BE | A02,B01 | 2 | 三维 StateController：phase（交易适配的 PRE_OPEN/AUCTION/TRADING/CLOSED/HOLIDAY）、workload、brain_mode；注入 Clock/TradingCalendar，幂等状态变更、健康/SAFE/关闭控制和可观察 StateChange | AC-003-01；134 项全绿、综合覆盖率 95.90%，Ruff/Mypy strict/Compileall/文档检查通过 |
| B06 | `✅ 已开发已测试` | BE | A02,B03 | 2.5 | 持久化 Scheduler、时间窗口、cooldown、SKIP/FIRE_ONCE missed trigger、过期、交易日历适配、Outbox 原子触发、checkpoint 恢复和生命周期 | AC-003-01～02、012-01；003 迁移、142 项全绿、综合覆盖率 95.75%，Ruff/Mypy strict/构建/文档检查通过 |
| E01 | `✅ 已开发已测试` | BE | B01,B04 | 2 | JSONL Sensory、ISO event-time 归一化、source sequence 保序/重复识别/乱序拒绝、freshness 数据质量、未来时间拒绝，以及白名单 CommandAdapter 的 `command.received` 受控注入 | AC-003-02～03、016-02/03；149 项全绿、综合覆盖率 95.76%，Ruff/Mypy strict/构建/文档检查通过 |
| I01 | `✅ 已开发已测试` | BE/OPS | A03,B05 | 2 | CLI start/status/stop/inject/replay/log，机器可读退出码 | 受控 `inject` 经 CommandAdapter 写入 command.received/Outbox；market summary 使用同一治理入口；后台 daemon 管理仍由外部服务管理器负责 |

里程碑出口：不接 LLM；可用虚拟时钟重放完整交易日；重启后 Outbox 不丢事实、Inbox 不重复处理；状态和队列可从 CLI 查询。

### 5.3 M2 Workflow JSON 与 Skill 执行内核

| ID | 状态 | 负责人 | 依赖 | 估算 | 交付物/完成标准 | 验收 |
|---|---|---|---|---:|---|---|
| C01 | `✅ 已开发已测试` | BE | A05,M0-02 | 2 | Workflow Registry、version/digest/status、Active 不可变；同一 ID 同时仅一个 Active 版本 | AC-008-05；注册、digest、冻结和版本切换测试通过 |
| C02 | `✅ 已开发已测试` | BE/QA | C01,M0-02 | 2 | Workflow/Node/I/O 静态校验、能力声明、控制节点约束；非法定义在发布前拒绝且零执行 | AC-008-01～03；非法字段/节点/控制分支契约测试通过 |
| C03 | `✅ 已开发已测试` | BE | C02 | 2.5 | DAG 拓扑、分支依赖、未知引用、环和子 Workflow 直接递归检查；输出映射节点存在性校验 | AC-008-02；拓扑、分支、环和递归场景测试通过 |
| C04 | `✅ 已开发已测试` | BE | C02 | 2 | 受限 `$.path OP literal` JSONPath/条件比较器；禁止 eval、函数、数组访问、动态文件或模块访问 | AC-008-03；15 项 C01～C04 场景、198 项全量测试通过，综合覆盖率 95.41%，Ruff/Mypy strict/构建/文档检查通过 |
| C05 | `✅ 已开发已测试` | BE | C03,C04,A05 | 3 | 持久化 WorkflowRun/NodeRun 投影、严格状态机、逐 attempt、乐观版本 CAS、幂等 event ID、追加式转换历史及 004 迁移 | AC-007-01、009-04；229 项全绿、综合覆盖率不低于 95% |
| C06 | `✅ 已开发已测试` | BE | C05 | 4 | 确定性 WorkflowRuntime；`skill/condition/parallel/delay/sub_workflow` 五类节点；受限引用、分支 SKIPPED、fail_fast/collect_all/min_success 聚合、子流程 propagate/continue、内联/Artifact 输出和失败传播；实际并发/超时/取消由 C07 完成 | AC-008-01～03；242 项全绿，综合覆盖率不低于 95% |
| C07 | `✅ 已开发已测试` | BE | C05,A03 | 3 | Workflow/节点 deadline；global→workflow→skill 三级并发与实际 parallel 分支并发；Task/子 Workflow/活动 Skill 取消传播；按错误码最多 3 次持久化 attempt；显式 compensation 节点 | AC-007-02～04；249 项全绿，综合覆盖率 95.07% |
| C08 | `✅ 已开发已测试` | BE/OPS | A05 | 2 | 本地 content-addressed Artifact Store、原子落盘与事务登记、SHA-256/size 读取校验、1 MiB 自动转存、重复内容去重、带宽限期孤儿回收 | AC-008-04、011-03；237 项全绿，Artifact 模块覆盖率 98% |
| D01 | `✅ 已开发已测试` | BE | M0-02,A05 | 2 | Capability Registry、主版本、I/O Schema 和兼容判断 | AC-006-01 |
| D02 | `✅ 已开发已测试` | BE | D01 | 2 | Manifest 加载、digest、权限、资源、副作用和启用状态 | 契约测试 |
| D03 | `✅ 已开发已测试` | BE | D02,C08 | 3 | SkillAdapter invoke/health/cancel/query_result 与上下文隔离 | AC-007-03～05 |
| D04 | `✅ 已开发已测试` | BE | D02,A05 | 2 | Skill install/enable/disable/version/digest；未验证不可启用 | 集成测试 |
| D05 | `✅ 已开发已测试` | BE/TL | D01,D04 | 3 | Resolver 兼容/权限/健康/成本稳定排序及固定 Binding | AC-005-02、006-03、008-05 |
| D06 | `✅ 已开发已测试` | BE/QA | D03 | 2 | 确定性 SkillRecoveryManager：PURE 安全重放、IDEMPOTENT 原 key 有限重放、QUERYABLE 固定 Binding 查询后完成/失败/复核、NON_REPLAYABLE 禁止重放；deadline 与 attempt 上限保护 | AC-007-05；256 项全绿，综合覆盖率 95.08% |
| D07 | `✅ 已开发已测试` | BE/AI | D03 | 2 | 确定性 FakeMarketRead、无模型 FakeSummary、幂等可查询 LocalNotification；Capability/Manifest 一键注册 | 224 项全绿，综合覆盖率不低于 95%，E2E 前置完成 |

里程碑出口：一份合法 Workflow JSON 可在唯一 LoopEngine 上运行；替换兼容 Skill 不改 JSON；五类节点、超时、取消、幂等和崩溃恢复测试通过。

### 5.4 M3 规划授权与运动执行

| ID | 状态 | 负责人 | 依赖 | 估算 | 交付物/完成标准 | 验收 |
|---|---|---|---|---:|---|---|
| F01 | `✅ 已开发已测试` | AI/BE | M0-02 | 1.5 | 深度不可变 CandidatePlan、规范化 SHA-256 digest、事务型 PlanningRepository、内容校验及重复/篡改拒绝 | AC-005-01；纳入 262 项全量测试 |
| F02 | `✅ 已开发已测试` | BE/TL | F01,D05,A05 | 3 | 单一最终 PlanDecision；仅 APPROVED 可签发固定 ExecutionGrant；SINGLE_TASK_MULTI_ATTEMPT 顺序授权；Grant 撤销/过期及追加式历史；006 迁移 | AC-006-05、007-01；262 项全绿，综合覆盖率 95.11% |
| F03 | `✅ 已开发已测试` | AI | F01,E02,E05 | 3 | 规则 Planner、Fake LLM、结构化解析和无模型降级 | AC-005-01～03；281 项全绿，综合覆盖率 95.03% |
| F04 | `✅ 已开发已测试` | BE | F01,C01 | 2 | Plan Schema、Task DAG、Active Workflow Registry、计划/任务期限和 Workflow 参数 Schema 验证；拒绝发生在 Skill 调用前 | AC-006-01～02；正常/未知/过期/非法参数/依赖/环测试通过 |
| F05 | `✅ 已开发已测试` | BE/TL | F04,B05,D01 | 3 | Capability 白名单、SAFE 模式、新鲜度、每日/计划/节点时长预算 RiskGate；返回固定 policy/permissions/budget | AC-006-02～04；拒绝路径零费用零执行 |
| F06 | `✅ 已开发已测试` | BE | F02,C07,D06 | 4 | Grant-only MotorExec；Workflow/Binding/permission/deadline 不可扩权；持久化 Task 状态链；确定性优先级批调度；运行取消；消费 D06 COMPLETE/FAIL/REVIEW/TIME_OUT/REPLAY 恢复决策 | AC-007-01～05；单 Task 多 attempt 及恢复测试通过 |

里程碑出口：只有有效 ExecutionGrant 能创建 Task；执行层不能改变 Workflow/Skill/权限/预算；未知和越权计划产生零 Skill 调用。

### 5.5 M4 主动认知、结果与记忆

| ID | 状态 | 负责人 | 依赖 | 估算 | 交付物/完成标准 | 验收 |
|---|---|---|---|---:|---|---|
| E02 | `✅ 已开发已测试` | BE/AI | B04,E01 | 3 | WorldModel 事实投影、事件时间排序、重复/乱序/同时间冲突、新鲜度淘汰和不可变版本化 Snapshot | AC-003-03、005-01；正常/异常/边界及不可变性测试通过 |
| E03 | `✅ 已开发已测试` | AI | E02 | 2.5 | 无 LLM 的确定性规则 Attention、绝对/相对阈值、证据与解释字段、去重、聚合指标和冷却；输出 `attention.salient_event` 并传播关联/因果 ID | AC-004-01～03；156 项全绿、综合覆盖率 95.68%，Ruff/Mypy strict/Compileall/构建/文档检查通过 |
| E04 | `✅ 已开发已测试` | AI/BE | A02 | 1.5 | 固定 GoalPolicy、受限比较完成条件、绝对 UTC 期限、启用状态、冲突域确定性选择、Plan 兼容预算上限和不可变版本快照；不做 LLM 动态目标拆解或实际费用扣账 | 13 项正常/异常/边界场景；169 项全绿、综合覆盖率 95.94%，GoalPolicy 模块覆盖率 99% |
| E05 | `✅ 已开发已测试` | BE/AI | E02,E03,E04 | 3 | 有界 CognitiveCoordinator：合法刺激去重与容量治理、短窗口/批量合并、确定性焦点、固定 World/Goal/Memory Snapshot、cycle ID、规划并发上限、活动冲突域锁和晚到事件隔离；不调用 Planner/LLM | AC-005-01、016-02；6 项协调场景及 175 项全量测试通过，综合覆盖率 96.05%，Coordinator 模块覆盖率 98% |
| E06 | `✅ 已开发已测试` | BE | A02 | 2 | 有界 WorkingMemory、默认/逐条 TTL、先过期再按低重要度/最旧淘汰、深度不可变且兼容 Coordinator 的 MemoryContextSnapshot、空启动和仅允许带 `source_fact_id` 的显式重建；旧内存快照不作为权威事实 | AC-010-01～03；容量 101、TTL、重要度、重启/重建等 8 组场景及 183 项全量测试通过，综合覆盖率 96.16%，模块覆盖率 98% |
| G01 | `✅ 已开发已测试` | BE | A05,C05 | 2.5 | correlation ID 聚合 Plan/Decision/Grant/Task/WorkflowRun/NodeRun/Episode/Audit；Episode 写入、追加式审计链和只读 TraceQuery | AC-009-01～04、011-01；治理执行全链路查询通过；268 项全绿，综合覆盖率 95.07% |
| G02 | `✅ 已开发已测试` | AI/BE | F06,G01 | 3 | execution/goal/quality/evidence 四类 OutcomeEvaluator | 292 项全绿，综合覆盖率 95.15%；Episode/OutcomeEvaluation/Outbox 原子写入通过 |
| G03 | `✅ 已开发已测试` | BE/AI | G02 | 2 | 延迟评价窗口、evaluator version 和证据 Ledger | 296 项全绿；007 迁移、版本锁定、窗口边界与追加式证据测试通过 |
| G04 | `✅ 已开发已测试` | AI/BE | B06,G01,G02 | 3 | RestRepair、daily review、每日幂等和候选经验 | 302 项全绿，综合覆盖率 95.00%；008 迁移、无活动降级、重启幂等和候选隔离通过 |
| G05 | `✅ 已开发已测试` | AI/BE | G04,E06 | 2.5 | 摘要、证据、矛盾、过期和 candidate-only 晋级边界 | 308 项全绿，综合覆盖率 95.03%；009 迁移、证据校验、双向矛盾、过期和显式晋级测试通过 |
| G06 | `✅ 已开发已测试` | BE | D07,B03 | 1.5 | LocalNotification Skill、稳定幂等键和重启不重复 | 311 项全绿，综合覆盖率 95.02%；010 迁移、重启重放、payload 冲突和并发去重通过 |

里程碑出口：显著事件触发一次有界认知周期；TaskResult 经 OutcomeEvaluator 才写 Episode；市场摘要 E2E 完整可追踪；无显著事件不调用模型。

### 5.6 M5 观测、验收与发布

| ID | 状态 | 负责人 | 依赖 | 估算 | 交付物/完成标准 | 验收 |
|---|---|---|---|---:|---|---|
| I02 | `✅ 已开发已测试` | OPS/BE | A03,B01,F06 | 2.5 | Loop lag、队列、Task、Skill、模型、费用和副作用指标 | 持久事实 + 进程观测快照；JSON/Prometheus CLI；队列、执行、模型成本、通知与重复副作用测试通过 |
| I03 | `✅ 已开发已测试` | OPS/BE | I02 | 2 | liveness/readiness/dependency/brain health 与诊断快照 | SQLite/Outbox 分层探针、SAFE/DEGRADED brain、迁移/指标/逾期任务/错误快照及 health/diagnose CLI 通过 |
| I04 | `✅ 已开发已测试` | APP/BE | G01,G02,T04 | 2.5 | `MarketInsight` Read Model；latest/show/explain 查询服务；时间、证据、新鲜度、风险、版本和 correlation 投影 | 只读 latest/show/explain 已由 market_summary E2E 验证 |
| I05 | `✅ 已开发已测试` | APP/BE | I01,I04,G06 | 2.5 | Quant CLI：`market summary`、`insights latest/show/explain`；JSON/Markdown；通知订阅、限频、去重与已读状态 | market summary 治理事件、Insight 查询、JSON/Markdown、订阅/严重度/静默/限频/稳定去重/已读均通过 E2E；前台命令返回 accepted message ID，实际消费由运行时负责 |
| T01 | `✅ 已开发已测试` | QA/BE | M0-02 | 2 | 全部 Schema 正反例、版本和代码模型契约测试 | 15 份 Schema 全部编译；跨文件引用、正例、版本/必填/额外字段反例通过；并修正 SkillManifest 运行时字段契约 |
| T02 | `✅ 已开发已测试` | QA | B03,F06,D06 | 3 | T1～T6 提交点、四类恢复、重复副作用故障注入 | T1～T6 commit 前崩溃均无半状态；重试原子提交；PURE/IDEMPOTENT/QUERYABLE/NON_REPLAYABLE、Outbox 重投与通知幂等矩阵通过 |
| T03 | `✅ 已开发已测试` | QA | C06,D07,F06 | 2.5 | Workflow/Skill golden tests、非法表达式和资源上限 | 314 项全绿，综合覆盖率 95.02%；market_summary 三 Skill golden、非法引用和资源限制通过 |
| T04 | `✅ 已开发已测试` | QA/AI | E05,G02 | 3 | `market_summary` E2E、无模型降级和完整审计链 | CognitiveCycle → Rule fallback → RiskGate/Grant → 3 Skills → LocalNotification → Outcome/Episode/Trace E2E 通过 |
| T05 | `✅ 已开发已测试` | QA/AI | G04,G06 | 3 | `daily_review` E2E、每日幂等、候选经验和持续在线 | RestRepair → Validator/RiskGate/Grant → Workflow → CANDIDATE 经验；重启幂等、NO_ACTIVITY、虚拟 6h 在线通过 |
| T06 | `✅ 已开发已测试` | QA/OPS | T01～T05,I03,I04,I05 | 4 | 全部 P0 AC 自动化、虚拟 30 天、真实 24h 稳定性报告；CLI 可查询并解释测试装配生成的市场摘要 | 虚拟 30 天和真实 24h PASS；0 readiness failure、0 重复副作用、0 error；发布负责人已签署。外部 CLI 到常驻量化 Runtime 的连接点转由 Q01～Q08 验收 |
| I06 | `✅ 已开发已测试` | OPS/TL | T02,I03 | 2 | 队列、SQLite、Skill、费用、崩溃、SAFE_MODE Runbook | 可执行检测/止损/恢复/验证/升级步骤；T02、恢复矩阵、诊断与通知幂等自动演练签字；CLI/危险命令文档契约通过 |

## 技术底座独立交付（MVP 后续）

| 任务 | 状态 | 目标 | 验收 |
|---|---|---|---|
| P01 | `✅ 首版已开发已测试` | 领域无关 RuntimeBuilder 与 `brainagent` run/status/health/diagnose CLI | 插件加载和生命周期 smoke 通过 |
| P02 | `✅ 首版已开发已测试` | Domain SDK 公共契约和稳定运行时构建入口 | 外部插件不导入 quant 模块 |
| P03 | `✅ 首版已开发已测试` | Kernel、Platform、Domain SDK 独立 wheel manifest | 三个 wheel 构建成功，依赖单向 |
| P04 | `✅ 首版已开发已测试` | 独立 research_agent 外部领域消费者 | 不修改底座完成注册和运行时查询 |
| P05 | `✅ 已开发已测试` | 外部领域完整认知、治理、执行、评价和恢复 E2E | Research Agent 全 Trace；取消与事务崩溃回滚；全量超时/恢复矩阵回归 |
| P06 | `✅ 已开发已测试` | 公共 API、Schema、迁移和旧插件升级兼容 | API 1.0 清单；破坏性 Schema/未来插件拒绝；旧库前向升级无数据丢失 |
| P07 | `✅ 已开发已测试` | 独立运维、健康诊断、指标、Trace 和 Runbook | Research 跨领域积压诊断；通用 metrics/trace/migrations CLI；Runbook 演练 |
| P08 | `✅ 已开发已测试` | 不安装的发行边界、独立领域长稳和最终发布验收 | Research 虚拟 30 天/真实 smoke PASS；T06 未通过时发布门保持 BLOCKED |

P01～P08 已证明独立装配、通用闭环、兼容升级、跨领域运维和不安装发布验收；T06 真实 24 小时报告已通过并完成人工签署。

## 可运行产品闭环（MVP 0.1.1）

2026-08-19 的启动检查证明 Kernel、Platform、Domain SDK 和量化 E2E 组件可独立工作，但 `bia market summary` 只将 `command.received` 可靠写入 Outbox；当前常驻 `brainagent` 没有量化命令消费者，因此请求不会自动形成 Task、WorkflowRun、Insight 和通知。这是应用装配与运行链路缺口，不应通过 CLI 同步直调业务服务绕过治理。

关键路径：`Q01 → Q02 → Q03 → Q06 → Q07 → Q08`；Q04、Q05 可在主路径中并行。

| ID | 状态 | 负责人 | 依赖 | 估算 | 交付物/完成标准 | 验收 |
|---|---|---|---|---:|---|---|
| Q01 | `✅ 已开发已测试` | APP/BE | P01,P02,T04,T05 | 2 | `QuantDomainPlugin` 与唯一 Composition Root；注册 Fake Market/Summary/Notification、`market_summary`、`daily_review`、Evaluator 和所需常驻服务；CLI 与 Runtime 使用同一数据库和配置 | Quant 插件可发现 3 Capability、3 Skill、2 Workflow；平台包边界测试通过 |
| Q02 | `✅ 已开发已测试` | BE | Q01,B02,B03,E01 | 3 | 持久化 CommandConsumer：Outbox Relay → Transactional Inbox → 通用 `command_execution`；只消费白名单 `command.received`；保留原 msg/dedup/correlation | 先写后启动、重复投递、不支持命令死信和重启恢复测试通过 |
| Q03 | `✅ 已开发已测试` | APP/BE | Q02,E02～E05,F03～F06 | 4 | 将 `market.summary` 转换为固定 CognitiveCycle，固定 Workflow/SkillBinding，经 Planner/Validator/RiskGate/Grant/MotorExec 执行；失败持久化明确终态 | CLI 请求产生唯一 Plan/Decision/Grant/Task/Run/Episode，correlation 全链可查；失败不误报成功 |
| Q04 | `✅ 已开发已测试` | APP/BE | Q01,B06,G04,Q03 | 2.5 | Scheduler 驱动 `daily_review` 常驻服务；交易日/时区/错过窗口策略配置化；重启继续使用稳定 review key | 跨午夜、停机后恢复、重复触发只执行一次；无活动返回 NO_ACTIVITY |
| Q05 | `✅ 已开发已测试` | BE/OPS | Q01,A05 | 1 | 启动易用性：数据库/Artifact 父目录安全创建、路径和权限预检、启动失败结构化错误；文档路径不依赖当前工作目录 | 新目录一条命令启动；只读/非法路径退出码稳定且不留下半初始化文件 |
| Q06 | `✅ 已开发已测试` | APP/BE | Q03,I04,I05,G06 | 2.5 | Outcome → MarketInsight 投影与订阅自动交付；重建投影、freshness、证据、风险、版本和 read state；只在成功 Outcome 后交付 | latest/show/explain 可读真实运行结果；同业务 key 重放不重复 Insight/通知 |
| Q07 | `✅ 已开发已测试` | OPS/BE | Q03,Q06,I03 | 2 | 统一 `bia run` 前台入口、SIGINT/SIGTERM 排空、父目录创建及 `bia commands` 明确终态；进程内不自建 daemon/PID 管理 | 新目录冷启动、优雅停止、RUNNING 重置恢复、启动错误和机器可读查询测试通过 |
| Q08 | `✅ 已开发已测试` | QA/OPS | Q07 | 3 | 真实 CLI/Runtime 子进程黑盒 E2E、恢复、幂等和发布验收 | 100 次真实命令全部成功；100 Task/Run/Episode/通知；0 失败、0 重复副作用；476 项全量测试、95.04% 覆盖率通过；报告见 `reports/release/q08-release.json` |

阶段出口：用户在新目录执行一条前台启动命令后，另一个终端提交市场摘要请求，可以观察状态从 `ACCEPTED` 进入明确终态，并查询包含 Evidence、版本和 correlation 的 Insight；进程重启不丢请求、不重复执行或交付。此阶段仍使用 Fake Market/Fake Summary/LocalNotification，不宣称提供实时行情或生产外部通知。

## 类脑 Agent 命令面（MVP 0.2）

详细命令树、使用方式、DoD 和发布门见[类脑 Agent 命令面 MVP 计划](command-surface-mvp-plan.md)，专家结论见[命令面 MVP 专家审查](../reviews/command-surface-mvp-expert-review-2026-08-19.md)。本表是任务状态的唯一基线。

| ID | 状态 | 优先级 | 负责人 | 依赖 | 估算 | 目标 |
|---|---|---:|---|---|---:|---|
| U01 | `✅ 已开发已测试` | P0 | APP/BE | Q08 | 2 | 统一 `CommandSpec`、分层帮助、实时补全、别名和 Shell/CLI 路由；TTY/非 TTY 契约测试通过 |
| U02 | `✅ 已开发已测试` | P0 | BE/OPS | U01,I02,I03 | 3 | `/system`、`/brain` 有界只读入口；Brain 明确标注 derived，系统健康/迁移/指标来自权威事实 |
| U03 | `✅ 已开发已测试` | P0 | BE/TL | U01,A03,Q07 | 4 | Quant CLI/Shell 均由唯一 LoopEngine/Supervisor 托管；`/loop status/services/lag/checkpoints` 读取真实快照和权威事实 |
| U04 | `✅ 已开发已测试` | P0 | BE/OPS | U01,B01～B03,G01 | 3 | `/events` recent/show/correlation/inbox/outbox/dead-letter 有界脱敏查询 |
| U05 | `🟨 部分完成` | P0 | APP/BE | U01,F01～F06,G01 | 4 | `/plans`、`/tasks` 查询完成；cancel/retry 已经治理入口接收但安全拒绝，待 MotorExec 活句柄与新 Grant 语义完成 |
| U06 | `⬜ 未开始` | P1 | AI/BE | U02,E02～E05 | 3 | `/attention`、`/goals` 查询解释 |
| U07 | `⬜ 未开始` | P1 | BE/AI | U01,E06,G01,G04,G05 | 4 | `/memory` 查询、检索与受控巩固 |
| U08 | `✅ 已开发已测试` | P0 | APP/BE | U01,A07,C01,D01 | 3 | Runtime 幂等持久化校验后 Catalog；`/catalog`、`/skills`、`/workflows` 查询真实版本/digest/status |
| U09 | `⬜ 未开始` | P1 | BE/TL | U08,D04,C01 | 4 | Skill/Workflow 治理状态变更 |
| U10 | `⬜ 未开始` | P1 | APP/BE | U03,B06,Q04 | 3 | `/schedules` 查询和受控 trigger |
| U11 | `⬜ 未开始` | P0 | BE/TL | U03,H01.1～H12 | 5 | Quant 接入 OrganizationGovernedApp 和三层 DNA 身份 |
| U12 | `⬜ 未开始` | P0 | APP/BE | U11 | 4 | `/dna` 查询、谱系、解释和执行归因 |
| U13 | `⬜ 未开始` | P1 | BE/TL/QA | U12 | 5 | DNA 合法 transition 控制面 |
| U14 | `⬜ 未开始` | P1 | AI/BE | U12,H06～H12 | 5 | `/evolution` 查询、Replay、Compare 和 Explain |
| U15 | `⬜ 未开始` | P1 | TL/QA | U13,U14 | 4 | promote/rollback 与 kill switch |
| U16 | `🟨 部分完成` | P0 | APP/BE | I04,I05,U01 | 4 | Insight cursor/stale 分页和 Subscription quiet hours/list/enable/disable 已完成；symbol/time/type 过滤待补 |
| U17 | `⬜ 未开始` | P0 | QA/OPS | U02～U16 | 5 | 权限矩阵、故障注入、黑盒验收和发布报告 |

关键路径：`U01 → U03 → U02 → U11 → U12 → U13 → U17`。U11、U17 仍为发布阻断任务。

## DNA 演化 MVP（下一阶段）

DNA 将现有 Workflow JSON 作为执行编码，由 Runtime 解释、Skill 插件实现；自动变化只产生候选，必须经过验证、Shadow/Canary 和显式治理才能激活。详细计划见 [DNA MVP 计划](dna-mvp-plan.md)。H01.1～H12、T07.1/T07.2 已完成：Workflow DNA 演化闭环、Agent DNA 认知策略、Organization DNA 多 Agent 治理，以及三层身份归因和实际组织委派执行入口已经连成全链路。

里程碑出口：两条 E2E、全部 P0、故障恢复和审计链通过；用户可通过本地 CLI 以 JSON/Markdown 获取、查询和解释 MarketInsight；Critical/High 安全问题为零；虚拟 30 天和真实 24 小时报告达标后才可发布 MVP 0.1。7 天真实长稳作为 0.1 后续发布门槛，不阻塞首个内部 MVP。

## 6. Sprint 排期

以两名开发、兼职 QA、每 Sprint 两周为参考；单人开发保持依赖顺序，不照搬并行范围。

| Sprint | 目标 | 主要任务 | 计划出口 |
|---|---|---|---|
| S0 | 契约可执行、工程可运行 | M0-02、A01～A07、T01 起步 | CI、分层边界、Schema 契约、SQLite、LoopEngine 与皮层调度基线 |
| S1 | 可靠事件与虚拟一天 | B01～B06、E01、I01 | Inbox/Outbox、Scheduler、CLI、日线重放 |
| S2 | JSON + Skill 执行 | C01～C08、D01～D07、T03 | 五类节点与可替换 Fake Skill |
| S3 | 授权与运动执行 | F01～F06、T02 | Grant-only 执行、预算、取消和四类恢复 |
| S4 | 主动认知闭环 | E02～E06、G01～G03、F03、T04 | 市场摘要 E2E |
| S5 | 夜间复盘与发布 | G04～G06、I02～I06、T05～T06 | 日复盘 E2E、P0、24h MVP 报告 |

每个 Sprint 只允许在前置任务达到 DoD 后领取依赖任务。未完成任务回到 Backlog 重新估算，不把测试拆到无限期的“后续 Sprint”。

## 7. 需求到交付追踪

| 需求域 | 设计/契约 | 开发任务 | 主要验收 |
|---|---|---|---|
| 生命周期与唯一 Loop | 系统架构、ADR-0002 | A03、I03 | AC-001 |
| 事件与可靠事实 | 事件协议、事务规范 | B01～B04、A05、T02 | AC-002、011-02 |
| 时间与状态 | 系统架构状态模型 | A02、B05、B06、E01 | AC-003、012-01 |
| 主动认知 | 系统架构认知周期 | E02～E05、F03 | AC-004、005 |
| 确定性授权 | Plan/Task/Error、安全治理 | F01～F06、D05 | AC-006、007 |
| JSON Workflow | Workflow 规范 | C01～C08 | AC-008 |
| 动态 Skill | Skill 调用协议 | D01～D07 | AC-007-05、008-05 |
| Trace 与记忆 | 记忆系统、事务规范 | G01、E06、G05 | AC-009～011 |
| 夜间复盘 | E2E 场景 | G02～G06、T05 | AC-012 |
| 运维与 CLI | 运维规范 | I01～I06、T06 | AC-016、发布门槛 |

完整逐条断言以 [P0 验收标准](../quality/p0-acceptance-criteria.md)为准，两条业务链以 [MVP 端到端场景](../scenarios/mvp-end-to-end-scenarios.md)为准。

## 8. Definition of Ready

任务开工前必须具备：目标和非目标明确；依赖已完成；接口/Schema 有版本；正常、失败、取消和恢复路径清楚；测试数据/Fake 可获得；AC 或可自动化完成条件明确。否则任务保持未开始并回到设计修订，不以编码猜测契约。

## 9. Definition of Done

每个任务必须同时满足：实现提交；lint/type/unit 通过；正常和失败路径测试通过；契约未漂移；日志/Trace/Error/指标按风险接入；文档和状态更新；涉及副作用时幂等与恢复验证通过；评审问题关闭。只有开发与约定测试均满足才标记 `✅ 已开发已测试`。

## 10. 进度维护规则

- 每个 Sprint 开始时确认负责人、估算和依赖，结束时更新状态与测试证据链接。
- 状态变更必须带证据；不得一次性在项目结束后补状态。
- 阻塞超过一个工作日标记 `⛔`，记录阻塞原因、影响和解除责任人。
- 需求或契约变化先更新 ADR/Schema，再评估任务、测试和关键路径影响。
- MVP 发布后才建立 v1.5 进化 Backlog；H 系列不与 MVP 关键路径混排。

## 11. 第一批领取顺序

第一批只领取 `A01 → A02 → A03/A04/A05 → A06/A07 → M0-02/T01`。完成后再领取 B 系列。第一个技术垂直切片必须证明：合法 Event 通过 Schema，经皮层策略产生可解释准入决定，再通过 EventBus 和 Inbox 消费，在同一事务写事实与 Outbox，重启后重投不重复处理，并可由 CLI/Trace 查询；同时 hello_research 能在不修改 Kernel/Platform 的情况下装配运行。

## 12. v1.5 Loop Engineering 因子发现扩展

因子发现采用[Loop Engineering 因子发现架构](../architecture/factor-discovery-loop-architecture.md)，不创建第二个顶层事件循环。由 LoopEngine 的 Scheduler 每 5 分钟触发一个有限迭代 Workflow，并通过 checkpoint 恢复。

| ID | 状态 | 负责人 | 依赖 | 估算 | 交付物 | 验收 |
|---|---|---|---|---:|---|---|
| L-001 | `⬜ 未开始` | BE | MVP 发布 | 3 | FactorDiscoveryLoop Profile、5 分钟触发、终止/暂停状态机 | checkpoint/恢复测试 |
| L-002 | `⬜ 未开始` | BE | L-001,C08,A05 | 3 | 原子 checkpoint、候选哈希、因子库 digest、恢复一致性 | 中断续跑测试 |
| L-003 | `⬜ 未开始` | AI/BE | D03,C06 | 4 | 五类生成策略、配额、父本池、随机种子可复现 | 生成分布测试 |
| L-004 | `⬜ 未开始` | AI | L-003 | 2 | 动量追踪、自适应步长和探索/利用预算调整 | 多轮回放测试 |
| L-005 | `⬜ 未开始` | AI/BE | L-003,D01 | 3 | 规则审查、AST/量纲/复杂度/边界校验 | 非法候选零回测 |
| L-006 | `⬜ 未开始` | AI | L-005,D03 | 3 | 生成 Sub-agent 与审查 Sub-agent 独立 SkillBinding | 隔离与 Schema 测试 |
| L-007 | `⬜ 未开始` | AI/BE | L-005 | 4 | 硬编码回测、多维过滤、数据版本和样本外边界 | 防泄漏/回测 golden |
| L-008 | `⬜ 未开始` | AI/BE | L-007 | 2 | FSA 子树统计、禁止列表和解除条件 | 多样性测试 |
| L-009 | `⬜ 未开始` | BE/OPS | L-002,L-007 | 2 | iteration/failure/checkpoint/factor Hooks 与摘要 | Hook 幂等测试 |
| L-010 | `⬜ 未开始` | QA | L-001～L-009 | 4 | 581 轮模拟回放、故障注入、成本/覆盖/多样性报告 | v1.5 扩展验收 |

默认生成比例为 mutate 25%、crossover 25%、parameter perturb 15%、random 15%、LLM mechanism 20%；实际每轮配额必须记录到 checkpoint。该扩展不改变 MVP 的“无真实交易”边界。
