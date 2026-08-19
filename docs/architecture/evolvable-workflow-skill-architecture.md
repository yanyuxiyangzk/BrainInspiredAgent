# 可进化 Workflow 与 Skill 架构

状态：Accepted  
版本：1.0-rc1

## 1. 设计意图

BIA 的业务能力不固化在脑区代码中，而由版本化 Workflow JSON 描述。Workflow 节点不绑定某个具体实现，而声明需要的能力契约；Skill 提供该能力的一个可替换实现。

系统可从两个方向改进 Workflow：

- 自上而下：目标、环境和规划结果提出新的编排或参数结构；
- 自下而上：执行 Trace、结果评价、失败与成本数据提出替换节点、调整依赖或更换 Skill 的建议。

“自动进化”指自动提出、验证和晋级候选版本，不允许原地修改正在运行或已经发布的 JSON。

## 2. 单一 Loop 与 Workflow 的关系

全局只有一个应用级 LoopEngine，底层只有一个 asyncio 事件循环，但需要区分四个概念：

| 概念 | 生命周期 | 职责 |
|---|---|---|
| LoopEngine | 进程级，唯一 | 常驻服务、刺激、认知周期、Workflow Run 的全局调度和恢复 |
| asyncio event loop | 进程级，唯一 | 承载协程和异步 I/O，不包含业务调度策略 |
| Cognitive Cycle | 事件触发、短生命周期 | 固定世界快照、选择焦点、产生候选计划 |
| Workflow Run | 一次性 | 按 JSON 执行获准任务，结束后销毁 |

Workflow 与唯一 Loop 结合的准确含义是：LoopEngine 接纳并全局调度获批运行，Workflow Runtime 将每个运行实例编译为协程/DAG Task，全部挂载到同一个 asyncio 事件循环，并在等待 Skill、定时器或子 Workflow 时主动 `await` 让出控制权。

Workflow JSON 不得定义自己的永久 `while true` 主循环。周期任务由 Scheduler 产生新 Workflow Run；事件任务由 EventBus 产生新 Workflow Run。这样既保持一个大脑心跳，又避免某个 Workflow 变成第二套不可治理的循环。

## 3. 四层解耦模型

```text
WorkflowSpec
  声明业务步骤、依赖、条件、预算和能力需求
        ↓ compile
NodeSpec
  声明 capability + input/output schema + side-effect constraints
        ↓ resolve
BindingSpec
  在当前环境将 capability 绑定到一个具体 Skill 版本
        ↓ invoke
Skill
  实现能力，负责真正调用代码、模型或外部系统
```

### WorkflowSpec

描述“先做什么、后做什么、什么条件下做”，不包含 Python 类名、模块路径、密钥或供应商私有连接信息。

### NodeSpec

节点引用稳定能力，例如 `market.auction.read`、`analysis.rps.calculate`、`notification.send`，而不是直接引用 `easy_tdx_fetch_auction` 的实现函数。

### BindingSpec

绑定配置根据环境、策略、健康、成本和数据区域选择 Skill，例如：

```text
market.auction.read@1
  ├─ dev  → mock-market-auction@1.2.0
  ├─ prod → easy-tdx-auction@2.1.0
  └─ fallback → cached-auction-reader@1.0.3
```

### Skill

Skill 是带 Manifest 的可调用能力包，不等同于 Workflow 节点。一个 Skill 可服务多个 Workflow；一个能力也可由多个 Skill 实现。

## 4. Workflow 节点示例

```json
{
  "node_id": "fetch_auction",
  "type": "skill",
  "capability": "market.auction.read",
  "capability_version": "1.0",
  "input": {
    "trade_date": "$.params.trade_date"
  },
  "constraints": {
    "max_latency_ms": 5000,
    "freshness_seconds": 15,
    "side_effect": "PURE",
    "required_data_class": "MARKET_PUBLIC"
  },
  "output_schema_ref": "schema://market/auction-snapshot/1.0"
}
```

运行前，SkillResolver 将它解析为固定执行绑定：

```json
{
  "node_id": "fetch_auction",
  "skill_id": "easy-tdx-auction",
  "skill_version": "2.1.0",
  "skill_digest": "sha256:...",
  "binding_policy_version": "prod-cn-market-3",
  "resolved_at": "2026-08-16T10:00:00Z"
}
```

该绑定被写入 ExecutionGrant 和 Trace。本次运行开始后不得因注册表变化而切换 Skill。

## 5. Skill Manifest

每个 Skill 至少声明：

```json
{
  "skill_id": "easy-tdx-auction",
  "version": "2.1.0",
  "digest": "sha256:...",
  "provides": [
    {
      "capability": "market.auction.read",
      "capability_version": "1.0",
      "input_schema_ref": "schema://market/auction-query/1.0",
      "output_schema_ref": "schema://market/auction-snapshot/1.0"
    }
  ],
  "side_effect": "PURE",
  "required_permissions": ["network.market.read"],
  "runtime": "python",
  "entrypoint": "adapter:invoke",
  "timeout_seconds": 10,
  "concurrency_limit": 2,
  "supports_cancel": true,
  "healthcheck": "adapter:health"
}
```

还应声明：

- 可重试错误类型；
- 幂等或结果查询能力；
- 数据分类和敏感字段；
- 资源上限；
- 运行隔离级别；
- 许可证与来源；
- 兼容的操作系统/架构；
- 是否允许进入 LLM 上下文。

Manifest 是声明，不是信任证明。Skill 安装和启用前仍需静态检查、契约测试和权限审批。

## 6. Skill 可插拔条件

两个 Skill 只有同时满足以下条件才可无感替换：

1. 提供相同 capability 主版本；
2. 输入 Schema 兼容；
3. 输出 Schema 兼容；
4. side-effect 类型相同或更严格；
5. 权限不超过节点和 Workflow 授权；
6. 延迟、成本、新鲜度满足约束；
7. 错误和取消语义可被 Runtime 理解；
8. 通过该能力的契约测试套件。

只因自然语言描述相似，不能判定两个 Skill 可替换。

## 7. SkillResolver

SkillResolver 是确定性控制平面的一部分，不由 LLM 直接决定最终绑定。解析过程为：

```text
capability requirement
  → registry candidates
  → schema compatibility
  → permission/policy filter
  → health and circuit state
  → latency/cost/data-location constraints
  → deterministic ranking
  → pinned skill binding
```

LLM 可以建议“优先低成本实现”或“尝试候选 Skill”，但不能绕过 Registry、Policy 和 RiskGate。

解析失败返回 `SKILL_BINDING_NOT_FOUND`；存在多个同分候选时使用稳定排序，不得随机漂移。

## 8. 双向进化模型

### 自上而下：Goal-to-Workflow

```text
Goal / WorldSnapshot
  → Prefrontal 识别能力需求
  → WorkflowDesigner 生成 WorkflowCandidate 或 WorkflowPatch
  → 静态和策略验证
  → 仿真/历史重放
  → 候选版本
```

允许修改：节点增删、依赖关系、条件、参数映射、预算和 capability 选择。不得生成任意代码、扩大权限或直接启用新 Skill。

### 自下而上：Outcome-to-Workflow

```text
Trace + OutcomeEvaluation + cost/latency/failure metrics
  → PatternMiner
  → EvolutionProposal
  → 归因分析
  → WorkflowPatch / BindingPolicyPatch
  → 同一验证与晋级管线
```

例如：某 Skill 连续超时，可提议替换绑定；某节点对目标结果无贡献，可提议删除；某步骤并行后成本更低，可提议修改 DAG。结果相关不等于因果，提议仍必须通过对照评估。

## 9. 不可变版本与自动晋级

Workflow 生命周期：

```text
DRAFT
  → VALIDATED
  → REPLAYED
  → SHADOW
  → CANARY
  → ACTIVE
  → DEPRECATED / ROLLED_BACK
```

关键规则：

- ACTIVE JSON 永远不可原地修改；
- 每次改变产生新语义版本和内容 digest；
- 正在运行的任务固定旧版本直至结束；
- 只有 ACTIVE 版本可被普通计划选择；
- 每个版本保留父版本、Patch、提议来源和验证证据；
- 自动晋级上限由风险等级决定；
- 任意版本可一键回滚到上一稳定版本。

### 自动化等级

| 等级 | 允许行为 |
|---|---|
| E0 | 只生成改进建议 |
| E1 | 自动生成候选 JSON 并静态验证 |
| E2 | 自动历史重放和影子运行 |
| E3 | 低风险 Workflow 自动小流量 Canary |
| E4 | 达标后自动晋级低风险无副作用 Workflow |
| E5 | 涉及通知、写入、交易等副作用，必须人工审批 |

MVP 只实现 E0/E1 的数据结构和验证接口；v1.5 再实现 E2/E3。真实交易相关能力永远不因自进化自动获得权限。

## 10. 演化评价函数

不能用单一“成功率”优化，否则系统可能通过少做事提高成功率。候选版本采用多目标约束：

- 目标完成率；
- 决策质量或业务评价；
- 错误率和恢复率；
- P95 时延；
- Token、费用和算力；
- 数据新鲜度；
- 副作用和风险事件；
- 结果稳定性；
- 相对基线的统计置信度。

安全、权限、审计和数据质量是硬约束，不参与收益权衡。只有满足硬约束后才比较软指标。

## 11. WorkflowPatch

进化过程保存结构化 Patch，而不只保存完整新 JSON：

```json
{
  "proposal_id": "uuid",
  "base": {"workflow_id": "market_summary", "version": "1.0.0", "digest": "sha256:..."},
  "source": "OUTCOME_EVALUATION",
  "hypothesis": "替换行情 Skill 可降低超时率",
  "operations": [
    {
      "op": "replace_constraint",
      "node_id": "fetch_auction",
      "path": "constraints.max_latency_ms",
      "value": 3000
    }
  ],
  "required_evidence": ["replay:30-trading-days", "shadow:100-runs"],
  "requested_capabilities": []
}
```

Patch 操作采用白名单语义，不直接接受任意 JSON Patch 修改权限、Schema 引用或安全策略。

## 12. 数据模型与审计

至少保存：

- workflow_definition/version/digest/status；
- workflow_parent 和 evolution lineage；
- evolution_proposal/hypothesis/source；
- workflow_patch；
- validation/replay/shadow/canary result；
- skill_manifest/version/digest/status；
- capability_contract/version；
- binding_policy/version；
- resolved_skill_binding；
- promotion/rollback decision；
- OutcomeEvaluator 版本和评价数据窗口。

这样系统不仅知道“当前怎么做”，还知道“为什么从旧版本变成现在这样”。

## 13. 对核心认知闭环的影响

```text
WorldSnapshot + Goal
  → CognitiveCoordinator
  → Prefrontal 选择 ACTIVE Workflow 或提出 Candidate
  → Validator/RiskGate
  → SkillResolver 固定节点实现
  → ExecutionGrant
  → Workflow Runtime on single asyncio loop
  → TaskResult
  → OutcomeEvaluator
  → EvolutionProposal
  → candidate validation/promotion
```

因此，Workflow JSON 是可进化的“程序性记忆”，Skill 是可替换的“能力器官”，OutcomeEvaluator 提供改进信号，确定性晋级管线则相当于抑制和免疫机制。

Workflow/Skill 的能力绑定是 Workflow DNA 的执行基础；Organization/Agent/Workflow 三层身份、Registry 和演化发布闭环见 [DNA 技术架构](dna-architecture.md)。完整的四平面逻辑架构、消息/查询/调度三种连接语义和事实投影关系，以[系统架构](system-architecture.md)为准。
