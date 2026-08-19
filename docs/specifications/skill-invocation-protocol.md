# Skill 调用与适配协议

状态：Accepted  
版本：1.0  
机器契约：[Skill Manifest](../../schemas/skill/skill-manifest-1.0.schema.json) · [Skill Binding](../../schemas/skill/skill-binding-1.0.schema.json) · [Skill Invocation](../../schemas/skill/skill-invocation-1.0.schema.json) · [Skill Result](../../schemas/skill/skill-result-1.0.schema.json)

## 1. 调用边界

Workflow JSON 只声明 Capability Contract。SkillResolver 产生固定 SkillBinding；WorkflowRuntime 通过统一 SkillAdapter 调用实现。业务代码不得绕过 Adapter 直接导入 Skill 实现。

```text
NodeSpec(capability)
  → SkillResolver
  → SkillBinding(skill/version/digest)
  → SkillInvocation
  → SkillAdapter.invoke
  → SkillResult | SkillError
```

Skill 不得创建 Workflow、签发 Grant、修改 Policy/Registry/WorldModel，也不得直接写 BIA 领域数据库。持久化通过返回结构化结果或受控领域 Repository Capability；领域事件由 Runtime 在事实事务提交后通过 Outbox 发布。

## 2. 调用对象

### SkillInvocation

必须包含 invocation/run/node/task IDs、固定 binding、capability/version、input、deadline、idempotency key、attempt、允许权限、数据分类、预算和 correlation/causation ID。输入在调用前按 Capability input Schema 校验并计算 digest。

### SkillContext

Adapter 只向 Skill 暴露最小上下文：只读 Clock、结构化 Logger、CancellationToken、Artifact writer、获准 Secret reference 和 ResourceBudget。不得暴露 EventBus、数据库连接、Registry 或全局容器。

### SkillResult

包含 status、output 或 artifact reference、output digest、usage、started/finished time 和 provider operation ID。Runtime 在接受前验证 output Schema、体积、数据分类和预算。

### SkillError

映射统一 Error Schema，必须给出稳定 code、明确 retryable boolean、阶段和已脱敏 details。第三方异常、HTTP body、SQL、Prompt 和密钥不得直接透传。

## 3. 标准 Adapter 接口

```python
class SkillAdapter(Protocol):
    async def invoke(self, invocation: SkillInvocation, context: SkillContext) -> SkillResult: ...
    async def health(self) -> SkillHealth: ...
    async def cancel(self, invocation_id: str) -> CancelResult: ...
    async def query_result(self, idempotency_key: str, provider_operation_id: str | None) -> RecoveryResult: ...
```

`query_result` 对 QUERYABLE 必须实现；其他恢复类型可以返回 NOT_SUPPORTED。health 只影响新 Binding，不得导致运行中任务偷偷换 Skill。

## 4. 超时、取消和重试

- deadline 是绝对 UTC 时间；Runtime 传递剩余预算，Skill 不得延长。
- timeout 表示未在期限内得到可信终态；cancel 是尽力停止请求，两者不能混同。
- cancel 成功前不得假设外部副作用未发生；IDEMPOTENT/QUERYABLE 使用原 idempotency key 恢复。
- NON_REPLAYABLE 超时或进程崩溃一律进入 `REQUIRES_REVIEW`。
- 重试由 Runtime 决定，Skill 内部只允许 Manifest 声明的传输级有限重试，并计入总 attempt/预算。
- 取消不响应的进程内 Skill 使 Adapter 熔断；阻塞或不可信实现必须放到隔离进程/外部 Worker。

## 5. 恢复语义

| 类型 | 前提 | 崩溃/未知结果 |
|---|---|---|
| PURE | 无外部可观察副作用 | 可使用同一输入安全重放 |
| IDEMPOTENT | 相同键只产生一次效果 | 使用原键有限重放 |
| QUERYABLE | 提供稳定外部 operation ID/查询 | 先查询 SUCCESS/FAILED/UNKNOWN，再决定 |
| NON_REPLAYABLE | 无幂等与查询保证 | 禁止自动重放，人工复核 |

Manifest 声明必须与 Capability Contract 相同或更严格。Resolver 拒绝副作用更强、权限更多或恢复能力更弱的候选。

## 6. 并发、资源与数据

每次调用同时受全局、Workflow、Skill 三层并发和速率限制；统一获取顺序为 global → workflow → skill，反序释放。Invocation 排队时间计入 deadline。模型 Token、费用、网络字节、Artifact 和 CPU/内存配额都计入 Grant 预算。

输入输出默认限制 1 MiB，超过后必须使用 Artifact reference；事件、日志只记录 digest 和摘要。数据分类控制是否允许进入模型上下文、外部网络和持久化。

## 7. 注册、启用和固定绑定

安装 → digest/来源检查 → Manifest Schema → Capability 契约测试 → 权限审批 → health check → Enable。Resolver 按 Schema、权限、数据区域、健康、熔断、成本、延迟和稳定排序选择 Skill，并把版本和 digest 写入 Grant。运行开始后 Registry 变化不影响当前 Binding。

## 8. 错误码

至少定义：`SKILL_NOT_FOUND`、`SKILL_BINDING_NOT_FOUND`、`SKILL_SCHEMA_INCOMPATIBLE`、`SKILL_PERMISSION_DENIED`、`SKILL_UNHEALTHY`、`SKILL_TIMEOUT`、`SKILL_CANCEL_FAILED`、`SKILL_OUTPUT_INVALID`、`SKILL_RECOVERY_UNKNOWN`。调用方只能依据 code/category/retryable 分支。

## 9. 契约测试

每个 Skill 必须通过：合法输入/输出、非法输入零调用、超时、取消、错误映射、并发限制、预算、敏感数据脱敏、幂等重放或结果查询、Artifact 大输出、health 降级和固定 digest 测试。Fake Skill 与真实 Skill 使用同一 Capability 测试套件。
