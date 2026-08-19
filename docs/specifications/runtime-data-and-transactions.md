# 运行时数据模型与事务规范

状态：Accepted  
版本：1.0

## 1. 目标与原则

本规范定义 MVP SQLite 的事实模型、投影和事务边界。数据库是可恢复业务事实的唯一来源；EventBus、WorkingMemory 和状态缓存都只是可重建投影。

原则：追加式事实优先；状态变化与 Outbox 同事务；Inbox 去重与消费副作用同事务；Active 定义不可原地修改；大对象只保存 Artifact 引用；所有业务表带 UTC 时间和 correlation ID。

## 2. 数据分区

| 分区 | 核心表 | 写入者 |
|---|---|---|
| 消息可靠性 | `inbox_message`、`outbox_event`、`dead_letter` | EventBus adapter/消费者事务 |
| 规划授权 | `plan`、`plan_decision`、`execution_grant` | Prefrontal、Validator/RiskGate、GrantIssuer |
| 执行事实 | `task`、`task_transition`、`workflow_run`、`node_run`、`skill_binding` | MotorExec、WorkflowRuntime、SkillResolver |
| 结果记忆 | `episode`、`outcome_evaluation`、`memory_entry` | Evaluator、MemoryService |
| 程序与进化 | `workflow_definition`、`skill_manifest`、`capability_contract`、`evolution_lineage` | Registry、EvolutionPipeline |
| 对象与运维 | `artifact`、`schema_migration`、`audit_record` | ArtifactStore、迁移器、审计服务 |

## 3. 关键表与不变量

### 3.1 Inbox/Outbox

- `inbox_message`：主键 `(consumer_id,msg_id)`；保存 dedup key、状态、attempt、processed_at 和 error_id。
- `outbox_event`：event ID 唯一；保存 envelope JSON、publish state、attempt、next_attempt_at。
- 业务消费者必须在同一事务中插入 Inbox、更新领域事实并写 Outbox；唯一键冲突代表已经处理。
- Outbox 发布成功只更新投递状态，不改写领域事实；重投保持原 msg ID。

### 3.2 Plan/Decision/Grant

- `plan` 保存不可变 CandidatePlan、digest、expires_at；状态是事件投影而非授权凭证。
- `plan_decision` 追加保存 Validator/RiskGate 决定；一个 Plan 只能存在一个最终有效决定，更正需新 Plan。
- `execution_grant` 固定 decision、Workflow digest、World/Memory Snapshot、Policy 和所有 SkillBinding。
- Task 创建必须引用 APPROVED decision 和有效 Grant；`task_id` 必须与 Grant 一致。
- Grant 采用 `SINGLE_TASK_MULTI_ATTEMPT`：只能授权一个逻辑 Task，但允许同一恢复策略下的有限 attempt；不得用于创建第二个 Task。
- Grant 在 Task 开始前过期则拒绝；Task 已合法 RUNNING 后，Grant 过期不强制杀死任务，Task 自身 deadline 仍生效。SAFE/kill switch 可撤销未开始 Grant并取消允许取消的运行任务。

### 3.3 Task/WorkflowRun/NodeRun

- `task_transition` 追加记录 from/to、reason、attempt 和 event ID；`task` 是可重建当前投影。
- `workflow_run` 固定 Workflow ID/version/digest、input digest、deadline、父子 run 关系。
- `node_run` 主键 `(run_id,node_id,attempt)`；保存固定 SkillBinding、状态、输入输出 Artifact、错误与资源用量。
- 状态转换使用乐观版本号 compare-and-swap；冲突返回 `TASK_STATE_TRANSITION_INVALID`。
- Node 完成与对应 Task/Node 事实事件写入同一事务。

### 3.4 Registry 与进化

- `(kind,id,version)` 唯一且内容 digest 不可变化。
- 同一 Workflow ID 同时最多一个 Active 版本；切换 Active 必须事务化写 promotion audit。
- EvolutionLineage 保存 base digest、Patch、假设、证据和晋级决定；候选不能直接覆盖 Active。

## 4. 权威事务边界

```text
T1 消费消息：Inbox insert + 领域更新 + Outbox append → commit → ack
T2 计划决定：PlanDecision insert + plan.decided Outbox → commit
T3 签发授权：ExecutionGrant + SkillBindings + execution.granted Outbox → commit
T4 创建任务：校验 Grant + Task/WorkflowRun insert + transition + Outbox → commit
T5 节点完成：NodeRun update + Artifact refs + transition/result Outbox → commit
T6 结果评价：OutcomeEvaluation + Episode linkage + outcome.evaluated Outbox → commit
```

任何事务提交前崩溃都必须整体不可见；提交后、发布前崩溃由 Outbox 恢复。禁止“先发布事件、后提交事实”。外部副作用无法与 SQLite 做原子事务，必须依赖稳定 idempotency key、结果查询或人工复核。

## 5. 恢复顺序

启动后依次：完成迁移 → 校验 Registry digest → 恢复 Outbox → 重建 Task/State/World 投影 → 扫描未终态任务 → 按 Skill recovery type 决策 → 启动消费者 → 开放新认知周期。恢复期间 readiness 为 false；只允许诊断和恢复命令。

未终态节点处理：PURE 重放；IDEMPOTENT 使用原键重放；QUERYABLE 先调用 recovery query；NON_REPLAYABLE 进入 `REQUIRES_REVIEW`。超过 Task deadline 直接 EXPIRED/TIMED_OUT，不补执行时间敏感动作。

## 6. SQLite 实现约束

- 开启 WAL、foreign keys 和 busy timeout；写事务保持短小。
- 迁移只能前向、带 checksum，启动时发现未知或被修改迁移立即失败。
- JSON 正文入库前通过对应 Schema；常用 ID、状态和时间建立普通列索引。
- 单条事件正文不超过 64 KiB，单节点内联输出不超过 1 MiB；更大内容进入 ArtifactStore。
- 数据清理以批次执行并写删除审计，不在主消费事务中大批量删除。

## 7. 一致性验收

至少覆盖：T1～T6 每个提交点前后崩溃；重复消息；Outbox 重投；并发状态更新；Grant 重复消费；Artifact 写成但事务回滚后的垃圾回收；QUERYABLE 未知结果；迁移 checksum 不一致。所有测试必须断言不存在“状态成功但无事实事件”或“事件已发但事实未提交”的半状态。
