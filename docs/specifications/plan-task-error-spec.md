# Plan、Task 与 Error 契约

状态：Accepted  
版本：1.0  
机器契约：[Plan](../../schemas/plan/plan-1.0.schema.json) · [PlanDecision](../../schemas/plan/plan-decision-1.0.schema.json) · [ExecutionGrant](../../schemas/execution/execution-grant-1.0.schema.json) · [Task](../../schemas/task/task-1.0.schema.json) · [Error](../../schemas/error/error-1.0.schema.json)

## 1. 契约关系

```text
Candidate Plan
  → PlanValidator + RiskGate
  → PlanDecision (approved/rejected/expired/cancelled fact)
  → SkillResolver pins all SkillBindings
  → GrantIssuer creates ExecutionGrant
  → MotorExec creates exactly one logical Task per Grant
  → Task state transitions
  → Task output or Error
```

Plan 描述“为什么以及请求做什么”；PlanDecision 是校验和策略决定事实；ExecutionGrant 是唯一执行授权，固定 Workflow、Skill、快照、权限和预算；Task 描述实际执行到了哪里；Error 是统一失败信封。Plan 本身不是执行凭证。

授权顺序固定为 Validator → RiskGate → SkillResolver → GrantIssuer。RiskGate 先确定允许的 capability/权限/预算上限，Resolver 只能在该上限内选实现；Binding 失败则不签发 Grant，并产生明确拒绝事实。Task 只能由未过期、未撤销且 task ID 匹配的 Grant 创建。

## 2. Plan 语义

### CandidatePlan 与 PlanDecision 状态

CandidatePlan 是不可变请求，机器 Schema 中 `status` 恒为 `CANDIDATE`。审批结果只写入独立 PlanDecision：

| Decision | 含义 | 后续 |
|---|---|---|
| APPROVED | Validator 与 RiskGate 均通过 | Resolver 绑定 Skill 后可签发 Grant |
| REJECTED | 校验、绑定或策略拒绝 | 终态，不可执行 |
| EXPIRED | 决策前超过 expires_at | 终态，不可执行 |
| CANCELLED | 授权前被明确取消 | 终态，不可执行 |

同一 Plan 只能存在一个有效最终 PlanDecision。不得回写或覆盖 CandidatePlan；需要更正请求时创建新 Plan 并通过 causation/lineage 关联。

### 不变量

- `expires_at > created_at`。
- 每个 task ID 在 Plan 内唯一，`depends_on` 只能引用同 Plan task，且图无环。
- Task deadline 不得晚于 Plan expiry 或批准预算的总期限。
- `(workflow_id, workflow_version)` 必须处于 Active 注册状态。
- `idempotency_key` 在业务作用域内稳定，不使用随机值掩盖重复动作。
- requested budget 是上限而非消费目标。
- APPROVED Plan 必须记录 policy version、决策者和理由。

## 3. Task 语义

### 状态机

```text
PENDING → READY → DISPATCHED → RUNNING → SUCCEEDED
                                  ├────→ FAILED
                                  ├────→ TIMED_OUT
                                  ├────→ CANCELLED
                                  └────→ REQUIRES_REVIEW
PENDING/READY/DISPATCHED ──────────────→ EXPIRED/CANCELLED
```

任何未列出的转换均以 `TASK_STATE_TRANSITION_INVALID` 拒绝。每次转换以追加式记录保存，Task 当前记录是投影，不取代状态历史。

### 终态约束

- SUCCEEDED：必须有 output，不得有终止 error。
- FAILED/TIMED_OUT/REQUIRES_REVIEW：必须有 error。
- CANCELLED/EXPIRED：应记录结构化原因，可无业务 output。
- finished_at 只在终态存在，且不早于 started_at/created_at。
- workflow definition digest 在一次运行内固定，防止同版本内容被替换。

### ExecutionGrant 语义

- 一个 Grant 只授权一个逻辑 Task，消费模式固定为 `SINGLE_TASK_MULTI_ATTEMPT`。
- 同一 Task 可在恢复策略允许时使用原 Grant 做有限 attempt，不得产生第二个 task ID。
- Grant 在 Task 开始前过期则 Task 进入 EXPIRED；已合法 RUNNING 后由 Task deadline 控制。
- SAFE/kill switch 可以撤销未开始 Grant；运行中任务按取消和副作用规则处理。
- Workflow digest、Policy version、World/Memory Snapshot 和全部 SkillBinding 在整个 Task 内不可变。
- 执行层不得扩大 permissions/budget、替换 Skill 或修改参数摘要。

### 恢复分类

Skill Manifest 与 Capability Contract 必须声明：

| 类型 | 崩溃恢复 |
|---|---|
| PURE | 安全重放 |
| IDEMPOTENT | 使用相同 idempotency key 重放 |
| QUERYABLE | 先查询外部操作结果，再决定完成或重放 |
| NON_REPLAYABLE | 不自动重放，进入 REQUIRES_REVIEW |

## 4. Error 语义

Error 面向机器判断；`message` 面向维护者，不用于分支逻辑。调用方只能依据 `code`、`category` 和 `retryable` 决定行为。

### 类别

| 类别 | 含义 | 默认重试 |
|---|---|---|
| VALIDATION | Schema、参数、表达式或状态无效 | 否 |
| POLICY | 权限、预算、模式或数据新鲜度拒绝 | 否 |
| DEPENDENCY | 外部服务或适配器失败 | 视 code |
| TIMEOUT | deadline 或调用超时 | 视幂等性 |
| CONFLICT | 幂等、版本、并发或状态冲突 | 通常否/先查询 |
| RESOURCE | 队列、磁盘、内存或配额不足 | 有限重试 |
| INTERNAL | 未分类系统缺陷 | 默认否并告警 |

### 核心错误码 1.0

| Code | Category | Retryable | 触发条件 |
|---|---|---:|---|
| SCHEMA_INVALID | VALIDATION | 否 | 通用契约不合法 |
| PLAN_SCHEMA_INVALID | VALIDATION | 否 | Plan 契约不合法 |
| PLAN_EXPIRED | POLICY | 否 | Plan 已过期 |
| WORKFLOW_NOT_FOUND | VALIDATION | 否 | ID/版本未注册 |
| WORKFLOW_NOT_ALLOWED | POLICY | 否 | 不在当前能力白名单 |
| WORKFLOW_GRAPH_INVALID | VALIDATION | 否 | DAG 有环或依赖非法 |
| WORKFLOW_RECURSION_LIMIT | VALIDATION | 否 | 子流程递归或深度超限 |
| EXPRESSION_NOT_ALLOWED | VALIDATION | 否 | 条件表达式超出安全子集 |
| PARAMETER_INVALID | VALIDATION | 否 | Workflow/Skill 参数不合法 |
| CAPABILITY_DENIED | POLICY | 否 | 权限不足 |
| BRAIN_MODE_DENIED | POLICY | 否 | 当前模式禁止动作 |
| DATA_STALE | POLICY | 否 | 实时输入超过新鲜度 |
| BUDGET_EXCEEDED | POLICY | 否 | Token、费用或时长预算不足 |
| IDEMPOTENCY_CONFLICT | CONFLICT | 否 | 相同键对应不同输入摘要 |
| TASK_STATE_TRANSITION_INVALID | CONFLICT | 否 | 非法状态转换 |
| TASK_DEADLINE_EXCEEDED | TIMEOUT | 视恢复类型 | 任务超时 |
| SKILL_BINDING_NOT_FOUND | VALIDATION | 否 | 无满足契约和策略的 Skill 实现 |
| SKILL_OUTPUT_INVALID | VALIDATION | 否 | Skill 输出不符合能力契约 |
| SKILL_TIMEOUT | TIMEOUT | 视恢复类型 | Skill 调用超时 |
| DEPENDENCY_UNAVAILABLE | DEPENDENCY | 是 | 依赖暂时不可用 |
| MEMORY_WRITE_FAILED | DEPENDENCY | 是 | 持久化失败 |
| EVENT_QUEUE_FULL | RESOURCE | 是 | 不可合并队列已满 |
| AREA_CRASHED | INTERNAL | 是 | 常驻脑区意外退出 |
| INTERNAL_ERROR | INTERNAL | 否 | 未分类缺陷 |

“视恢复类型”在产生实例 Error 时必须被解析成明确的 boolean，不允许输出第三种状态。

## 5. 错误传播和脱敏

- 低层错误可作为 `cause` 嵌套，但最大深度由实现限制为 5。
- 对用户/CLI 返回的 message 不含堆栈、SQL、路径、Prompt 或密钥。
- 完整堆栈只进入受控内部日志，并由 error ID 关联。
- details 仅保存结构化、已脱敏、小体积信息。
- 重试每次产生独立 error ID，并通过 Task attempt 和 causation ID 关联。

## 6. 版本策略

- Schema 使用 JSON Schema Draft 2020-12。
- 代码模型必须从同一契约验证，禁止维护语义不同的复制版。
- 新增可选字段属于兼容变更；删除、重命名、改变枚举语义需升级主版本。
- RC 评审通过后内容冻结为 1.0；任何不兼容修改走 ADR 和迁移说明。
