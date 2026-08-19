# Workflow 规范

状态：Accepted  
规范版本：1.0  
机器契约：[Workflow](../../schemas/workflow/workflow-1.0.schema.json) · [Workflow Node](../../schemas/workflow/workflow-node-1.0.schema.json)

## 1. 定位

Workflow 是版本化、一次性、声明式任务定义，也是业务编排的唯一事实源。它不能创建常驻循环，不能绕过 Skill 治理层，不能修改权限或风险策略。能力节点只引用稳定的 Capability Contract，由 SkillResolver 在执行前绑定具体 Skill；JSON 不得引用实现类、函数、模块路径或 `tool_name`。

Workflow、Skill 解耦及双向进化机制见[可进化 Workflow 与 Skill 架构](../architecture/evolvable-workflow-skill-architecture.md)。

## 2. 顶层结构

```json
{
  "spec_version": "1.0",
  "workflow_id": "market_summary",
  "version": "1.0.0",
  "name": "市场摘要",
  "description": "读取模拟行情并生成摘要",
  "input_schema": {
    "type": "object",
    "required": ["trade_date"]
  },
  "policy": {
    "timeout_seconds": 120,
    "max_parallelism": 3,
    "required_capabilities": ["market.read", "llm.reason"]
  },
  "nodes": [],
  "output_mapping": {}
}
```

工作流由 `(workflow_id, version)` 唯一确定。已发布版本不可原地修改。

## 3. 节点通用字段

```json
{
  "node_id": "fetch_market",
  "type": "skill",
  "depends_on": [],
  "timeout_seconds": 20,
  "retry": {
    "max_attempts": 2,
    "backoff_seconds": 1,
    "retry_on": ["TEMPORARY_UNAVAILABLE"]
  },
  "capability": "market.snapshot.read",
  "capability_version": "1.0",
  "input": {},
  "idempotency_key": "${workflow.run_id}:fetch_market"
}
```

节点 ID 在工作流版本内唯一。Runtime 在执行前必须验证依赖图无环。

## 4. 节点类型

正式 1.0 节点分为两类：

| 类别 | 节点 | 职责 |
|---|---|---|
| 能力节点 | `skill` | 通过 Capability Contract 调用动态适配的 Skill |
| 控制节点 | `condition`、`parallel`、`delay`、`sub_workflow` | 表达 DAG 控制流，不直接提供业务能力 |

### skill

声明 `capability`、版本、输入输出 Schema 和副作用约束，由 SkillResolver 绑定具体 Skill。绑定结果在一次运行中固定。

模型推理、行情读取、数据库受控访问、通知、Qlib 和 RD-Agent 均作为 Skill 实现。模型类 Skill 还必须声明结构化输出 Schema、Token/费用上限、超时和允许的数据分类。

### sub_workflow

调用固定 ID 和版本的子 Workflow。默认最大嵌套深度 8；加载时检测直接和间接递归。失败策略为 `propagate`、`continue` 或 `compensate`；补偿必须显式引用节点。

### condition

使用受限表达式语言选择 `then` 或 `else` 节点集合。未选分支进入 SKIPPED；SKIPPED 依赖默认不阻止已选路径，跨分支依赖在编译期拒绝。禁止 `eval`、任意函数调用和文件访问。

### parallel

并发执行若干节点分支，并受系统、Workflow、Skill 三级并发上限约束。必须声明失败策略：`fail_fast`、`collect_all` 或 `min_success`；输出按声明的 branch 顺序返回，每个分支携带状态、结果或 Error。`fail_fast` 取消尚未完成分支，但不假装回滚已发生副作用。

### delay

异步等待 `duration_seconds` 或绝对 UTC `until`，二者只能选一个。延迟不得超过任务总期限且 MVP 最长 60 秒；更长等待转为持久化 Scheduler 事件。

## 5. 引用表达式

MVP 使用受限 JSONPath 子集：

- `$.params.trade_date`
- `$.nodes.fetch_market.output`
- `$.context.correlation_id`

不支持脚本、过滤执行、动态函数和跨运行访问。引用不存在时默认失败；只有显式配置默认值才可继续。

## 6. 执行状态

```text
PENDING → READY → RUNNING → SUCCEEDED
                         ├→ FAILED
                         ├→ TIMED_OUT
                         ├→ CANCELLED
                         └→ SKIPPED
```

Workflow 的最终状态由节点状态和声明的失败策略确定。取消向下传播，但已发生的外部副作用不会假装回滚；如需补偿，必须显式定义补偿节点。

Node ID 在整个 Workflow 版本（含控制分支引用）中同一作用域唯一。`depends_on` 与 condition/parallel 分支引用共同编译成一张 DAG；不存在的节点、跨分支非法依赖、环和不可达节点在发布前拒绝。子 Workflow 拥有独立 node ID 作用域，通过输入和输出映射与父运行交互。

失败传播：普通 Skill 失败使依赖节点 SKIPPED 并使 Workflow FAILED；condition 仅调度选中分支；parallel 按其失败策略聚合；sub_workflow 默认传播子流程终态。取消从 Task 向子 Workflow 和运行节点传播，补偿节点只在显式声明且仍有执行授权时按逆副作用顺序运行。

## 7. 执行保护

默认限制：

- 嵌套深度：8；
- 节点数：100；
- 单节点输出：1 MiB，超出部分转对象存储或摘要；
- 默认总超时：5 分钟；
- 默认并发：4；
- 单节点重试：最多 3 次；
- 所有副作用 Skill 必须声明幂等和恢复语义。

禁止：

- 任意 Shell/Python 执行；
- 未参数化 SQL；
- 动态导入模块；
- Workflow 自行注册或安装 Skill；
- 从模型输出动态生成未经审核的可执行节点。

## 8. Trace

每个节点记录：

- workflow/run/node ID；
- 输入输出摘要和大对象引用；
- 状态及时间；
- 重试次数和错误分类；
- Capability、Skill、模型和 Prompt 版本；
- Token、费用和资源使用；
- idempotency key；
- correlation/causation ID。

敏感字段必须按 Schema 标记并在日志中脱敏。

## 9. 发布流程

`Draft → Validated → Replayed → Shadow → Canary → Active → Deprecated/RolledBack`

上线前必须通过 Schema、DAG、权限、资源上限、单元、契约和场景测试。生产计划只能引用 `Active` 版本。

Active 版本不可原地修改。自动进化只能生成带父版本、假设、Patch 和验证证据的新候选版本；MVP 不允许候选自动替换 Active。
