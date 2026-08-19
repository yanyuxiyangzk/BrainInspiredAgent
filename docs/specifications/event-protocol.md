# 事件协议规范

状态：Accepted  
Schema 版本：1.0  
机器契约：[Event Envelope](../../schemas/event/event-envelope-1.0.schema.json) · [Core Event Payload](../../schemas/event/core-event-payload-1.0.schema.json)

## 1. 定位与投递模型

Thalamus EventBus 是进程内发布/订阅通道，不是事实数据库，也不是所有消费者争抢同一个 Queue。每个订阅者拥有独立有界队列；发布时按消息类型和过滤器投递不可变消息引用。

MVP 采用进程内至少一次投递：同一 source 保持发布顺序，不保证跨 source 全局顺序；消费者必须通过 Inbox 幂等；业务事实先与 Outbox 在同一事务持久化，再异步发布。高频可替代快照可以合并，计划、授权、任务结果和评价事实不可静默丢弃。

## 2. 标准链路

```text
Sensory / Scheduler / CommandAdapter
  → perception.snapshot / schedule.triggered / command.received
  → WorldModel / Attention
  → world.snapshot_created / attention.salient_event
  → CognitiveCoordinator
  → Prefrontal
  → plan.candidate_created
  → PlanValidator + RiskGate
  → plan.decided
  → SkillResolver
  → execution.granted
  → MotorExec / WorkflowRuntime / Skill
  → task.started / task.finished / task.failed
  → OutcomeEvaluator
  → outcome.evaluated
  → EpisodicMemory / GoalPolicy / EvolutionPipeline
```

外部命令不得直达 Prefrontal 或 Skill。Task 只能从未过期、未撤销且与当前任务匹配的 ExecutionGrant 创建。

## 3. 消息信封

```json
{
  "schema_version": "1.0",
  "msg_id": "018f0000-0000-7000-8000-000000000001",
  "msg_type": "perception.snapshot",
  "source": "sensory.market",
  "target": null,
  "occurred_at": "2026-08-15T01:25:20Z",
  "published_at": "2026-08-15T01:25:20.032Z",
  "priority": 50,
  "correlation_id": "018f0000-0000-7000-8000-000000000002",
  "causation_id": null,
  "dedup_key": "market-snapshot:20260815:092520",
  "expires_at": "2026-08-15T01:25:35Z",
  "trace_context": {},
  "payload_schema": "schema://event/core-event-payload/1.0",
  "payload": {"event_type": "perception.snapshot", "stimulus_id": "market:20260815:092520", "data": {}, "data_quality": "VALID", "source_sequence": 100}
}
```

规则：ID 使用 UUIDv7 或等价可排序唯一 ID；存储时间统一 UTC；`priority` 为 0～100 且越大越优先；`causation_id` 指向直接原因；`dedup_key` 表达业务唯一性；payload 必须在入队前通过注册 Schema；密钥和非必要个人信息禁止进入消息。

## 4. 事件类型注册表

| 类型 | 发布者 | 订阅者 | 持久化/过期策略 |
|---|---|---|---|
| `perception.snapshot` | Sensory | WorldModel、Attention | 可合并；按 freshness 过期 |
| `command.received` | CommandAdapter | Attention、Coordinator | 命令记录持久化；过载明确拒绝 |
| `schedule.triggered` | Scheduler | Coordinator、RestRepair | 以 schedule/window 去重；过窗即过期 |
| `brain.state_changed` | StateController | 相关脑区 | Outbox 事实；不可丢 |
| `world.snapshot_created` | WorldModel | Coordinator、Memory | 保存 snapshot 引用；不可变 |
| `attention.salient_event` | Attention | Coordinator、WorkingMemory | 持久化证据引用；可设置期限 |
| `goal.changed` | GoalPolicy/Evaluator | Coordinator、Memory | Outbox 事实；不可丢 |
| `plan.candidate_created` | Prefrontal | PlanValidator | Plan 先持久化；按 expires_at 过期 |
| `plan.decided` | Validator/RiskGate | Resolver、Memory | PlanDecision 事实；不可丢 |
| `execution.granted` | SkillResolver/GrantIssuer | MotorExec | Grant 事实；按 expires_at 过期 |
| `task.started/finished/failed` | MotorExec | Evaluator、Memory、Prefrontal | TaskTransition 与 Outbox 同事务 |
| `outcome.evaluated` | OutcomeEvaluator | GoalPolicy、Memory、Evolution | Evaluation 事实；不可丢 |
| `memory.consolidated` | Memory/RestRepair | Prefrontal | 保存批次和证据窗口 |
| `evolution.proposed` | Designer/Miner | EvolutionValidator | 只产生候选；不可直接激活 |
| `system.health_changed` | Supervisor | Operations、Coordinator | 状态变化不可丢；重复可合并 |

事件类型必须先登记发布者、消费者、payload schema、优先级、保留级别和过期策略，禁止运行时临时创造未注册业务类型。核心 Payload 使用带 `event_type` 判别字段的机器契约，领域校验器必须断言它与 Envelope 的 `msg_type` 完全一致。

## 5. 背压与消费

| 类别 | 队列满时策略 |
|---|---|
| 高频行情快照 | 按 instrument/window 合并，仅保留最新并计数 |
| 状态变化 | 有限等待；失败进入 Outbox 重投并告警 |
| 外部命令 | 拒绝并返回 `EVENT_QUEUE_FULL` |
| Plan/Grant/Task/Outcome | 先持久化，经 Outbox 重投，不允许丢弃 |
| 指标和调试事件 | 允许采样或丢弃 |

消费者顺序为：校验信封与 payload → 检查期限 → Inbox 去重 → 执行业务事务 → 写消费结果和 Outbox → ack。可重试异常采用有上限的指数退避和抖动；不可重试异常进入死信记录并告警；消费者失败不得中止其他订阅者。

## 6. 顺序、去重和因果

- source 必须维护单调递增 source sequence（若上游提供）；同一消费者观察同 source 顺序不得倒退。
- `msg_id` 用于投递实例去重，`dedup_key` 用于业务作用域去重，两者不可互换。
- 重投保持原 `msg_id`、correlation 和 causation，只增加投递 attempt 元数据。
- 同一认知周期固定 World/Goal/Memory Snapshot；晚到事件进入下一周期。
- 过期消息保存拒绝事实，不继续触发业务动作。

## 7. 版本兼容

新增可选字段属于向后兼容；删除、重命名、改变字段语义或收紧已有取值必须升级主版本。消费者至少支持当前版本和前一个兼容版本。代码模型与 JSON Schema 必须通过契约测试防漂移；未知主版本进入死信，不允许猜测解析。
