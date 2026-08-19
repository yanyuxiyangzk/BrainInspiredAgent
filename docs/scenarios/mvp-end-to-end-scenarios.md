# MVP 端到端场景

状态：Review  
版本：1.0-rc1

本文固定 MVP 的两个业务闭环。测试必须使用虚拟时钟、确定性模拟行情、Fake LLM 和本地 NotificationSink，不依赖真实日期或外部服务。

## 1. 通用约定

- 时区：`Asia/Shanghai`；持久化时间使用 UTC，业务日按上海时间计算。
- 交易日：测试日 `2026-08-17`，测试日历明确标记为交易日。
- 模拟数据：JSONL，每行一个符合 Event Schema 1.0 的观测事件。
- 运行实例：单进程、单实例、SQLite、本地对象目录。
- 通知：只写本地 NotificationSink，使用 idempotency key 去重。
- 模型：Fake LLM 根据固定输入返回固定结构化结果。
- 工作流版本：场景引用精确的 `(workflow_id, version)`，禁止自动漂移至最新版。

## 2. 场景一：主动市场摘要

### 2.1 业务目标

在竞价观察窗口接收模拟行情，当确定性注意力规则识别到显著变化时，主动生成一次市场摘要，记录本地通知和完整 Episode。无需用户 Prompt。

### 2.2 输入

测试数据至少包含：

```json
{"instrument":"INDEX.TEST","event_time":"2026-08-17T09:24:50+08:00","price":1000.0,"volume":100000,"source_seq":1}
{"instrument":"INDEX.TEST","event_time":"2026-08-17T09:25:05+08:00","price":1012.0,"volume":180000,"source_seq":2}
```

注意力规则 `auction.price_change.v1`：在 `AUCTION` 阶段，同一标的相对最近有效基线价格变化绝对值不低于 1%，且数据年龄不超过 15 秒，生成一个显著事件。规则输出必须包含 `rule_id`、`score`、基线、当前值和证据事件 ID。

### 2.3 主路径

```text
09:24:50 virtual clock
  → Sensory publishes baseline perception
09:25:05 virtual clock
  → Sensory publishes changed perception
  → Attention matches auction.price_change.v1
  → CandidatePlan(market_summary@1.0.0)
  → PlanValidator validates schema/registry/expiry
  → RiskGate validates capabilities/budget/freshness
  → PlanDecision persisted → SkillBindings pinned → ExecutionGrant dispatched
  → MotorExec runs market_summary@1.0.0
  → local.notification@1.0.0 writes one record
  → TaskResult(SUCCEEDED)
  → Episode persisted
```

### 2.4 Given/When/Then

#### E2E-MARKET-001：正常主动触发

Given 系统处于 `NORMAL`，市场阶段为 `AUCTION`，依赖健康且当日预算充足；When 第二条行情进入并满足规则；Then 10 秒内产生一个获批计划并成功执行 `market_summary@1.0.0`，本地通知恰好一条，完整链路共享同一 correlation ID。

#### E2E-MARKET-002：重复事件去重

Given `source_seq=2` 已处理；When 相同事件被重复投递三次；Then 不产生第二个计划或通知，Trace 记录重复丢弃原因。

#### E2E-MARKET-003：同窗口冷却

Given 当日竞价摘要已成功；When 冷却窗口内出现另一条满足阈值的事件；Then Attention 可记录显著事件，但 Planner 因业务幂等键 `market_summary:2026-08-17:auction` 不创建第二次执行。

#### E2E-MARKET-004：陈旧数据拒绝

Given 当前虚拟时间为 09:25:30；When 收到 event_time 为 09:25:05 的实时分析事件；Then RiskGate 以 `DATA_STALE` 拒绝，不执行 Workflow。

#### E2E-MARKET-005：非法模型输出

Given Fake LLM 返回未知 Workflow 或不符合 Plan Schema；When Prefrontal 创建候选计划；Then PlanValidator 以 `PLAN_SCHEMA_INVALID` 或 `WORKFLOW_NOT_ALLOWED` 拒绝，工具调用数为零。

#### E2E-MARKET-006：副作用前后崩溃

Given 在本地通知提交前或提交后注入进程崩溃；When 系统重启恢复；Then最终通知恰好一条，任务进入 `SUCCEEDED` 或可解释的 `REQUIRES_REVIEW`，不得静默重复。

#### E2E-MARKET-007：预算耗尽

Given 每日模型预算已耗尽；When 事件达到阈值；Then 计划被 `BUDGET_EXCEEDED` 拒绝或执行配置的确定性降级摘要，且不得调用模型。

### 2.5 输出

- 一条 `TaskResult`；
- 一条本地通知或明确拒绝记录；
- 一条包含观测、规则证据、候选计划、审批和节点 Trace 的 Episode；
- 指标：触发耗时、执行耗时、Token/费用、去重数和最终状态。

## 3. 场景二：收盘后夜间复盘

### 3.1 业务目标

市场关闭后系统不退出，切换到 `REVIEW`，对当日 Episode 生成结构化复盘，识别失败与异常，产出候选经验但不自动将其升级为已验证知识。

### 3.2 触发条件

- 测试日历为交易日；
- 虚拟时间越过 15:30；
- `market_phase=CLOSED`；
- 当日复盘业务幂等键尚未成功；
- Memory 可写且系统不处于 `SAFE`。

### 3.3 主路径

```text
15:30:00 virtual clock
  → StateController emits CLOSED / REVIEW
  → RestRepair requests daily_review@1.0.0
  → Validator and RiskGate approve
  → Workflow queries today's episodes and metrics
  → deterministic aggregation
  → Fake LLM produces structured review
  → review artifact and candidate insights persisted
  → TaskResult(SUCCEEDED)
  → system remains alive in REVIEW
```

### 3.4 Given/When/Then

#### E2E-REVIEW-001：正常复盘

Given 当日至少有一个成功 Episode 和一个失败 Episode；When 时间越过 15:30；Then 60 秒内启动 `daily_review@1.0.0`，产出包含统计、失败分类、证据 ID 和候选经验的复盘记录。

#### E2E-REVIEW-002：无事件日

Given 当日没有业务 Episode；When 进入 REVIEW；Then 生成 `NO_ACTIVITY` 复盘记录，不调用不必要的深度推理，也不报任务失败。

#### E2E-REVIEW-003：重启幂等

Given 复盘成功后进程重启；When 恢复扫描执行；Then 依据 `daily_review:2026-08-17` 不重复生成复盘。

#### E2E-REVIEW-004：数据库暂时不可写

Given Memory 写入失败；When 复盘准备启动；Then 系统进入 `DEGRADED`，不声称复盘完成；依赖恢复后在截止时间前有限重试。

#### E2E-REVIEW-005：候选经验隔离

Given 复盘模型生成一条经验；When 结果写入 Memory；Then 状态只能是 `candidate`，必须带 evidence episode IDs、置信度、适用范围和有效期，不能直接变成 `validated`。

#### E2E-REVIEW-006：外部命令唤醒

Given 系统处于 REVIEW；When CLI 注入允许的只读状态查询；Then 立即响应且不改变 market phase；若注入未授权副作用命令，则 RiskGate 拒绝。

#### E2E-REVIEW-007：进程保持在线

Given 复盘执行完毕；When 虚拟时钟继续推进 6 小时；Then Supervisor 和允许的脑区保持健康，实时行情扫描保持关闭，低频健康感知继续运行。

### 3.5 输出

- 每交易日最多一条成功复盘；
- 聚合统计及失败分类；
- 带证据的候选经验列表；
- 复盘 Workflow 和节点 Trace；
- 系统继续处于可响应状态。

## 4. 场景性能边界

- 测试事件 JSON 编码后不超过 4 KiB；
- 基线环境为 2 vCPU、4 GiB RAM、本地 SSD；
- 无外部依赖延迟时，进程内事件投递 P95 小于 100 ms；
- 显著事件到计划获批 P95 小于 2 秒（Fake LLM）；
- 市场摘要端到端目标小于 10 秒；
- 日复盘启动目标小于 60 秒；
- `delay` 节点在 MVP 中最长 60 秒，更长等待必须转换为持久调度。

## 5. 场景数据产物

实现阶段必须提供：

- `fixtures/calendar.json`
- `fixtures/market_day.jsonl`
- Fake LLM 响应集合
- 两个 Workflow 1.0.0
- 预期事件与 TaskResult golden files
- 崩溃注入点配置
