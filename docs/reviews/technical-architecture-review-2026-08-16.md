# 类脑主动式 Agent 技术架构复查

评审日期：2026-08-16  
评审结论：**架构方向正确，建议完成 4 项冻结前修正，并将 6 项认知增强按 MVP/v1.5 分层实施。**

## 1. 当前底层思路

当前系统本质上是“确定性控制平面包围概率性认知核心”的事件驱动自治架构：

```text
外部世界
  → 感知与注意力
  → 受限规划（LLM/规则）
  → 确定性校验与风险门
  → 声明式 Workflow 执行
  → 情景记忆与夜间整理
```

运行底座采用模块化单体、单一 asyncio 控制事件循环、进程内发布订阅、SQLite 事实存储和 Outbox；阻塞与 CPU 密集任务移出事件循环。其核心价值不是“模仿脑区名称”，而是持续感知、选择性注意、目标驱动、动作执行、结果反馈和记忆巩固。

这一路线比“LLM 无限循环 + 工具调用”可靠，且适合从模拟量化场景逐步扩展。

## 2. 已经做对的技术选择

1. **主动性不依赖无限 LLM 循环。** 时间、显著变化、目标和命令共同产生刺激。
2. **LLM 没有最终执行权。** PlanValidator 与 RiskGate 保持确定性。
3. **脑区和 Workflow 分离。** 常驻服务与一次性任务不会混为一谈。
4. **状态维度正交。** 市场阶段、忙闲和健康模式不再塞进单一三态。
5. **执行按副作用类型恢复。** PURE/IDEMPOTENT/QUERYABLE/NON_REPLAYABLE 是正确基础。
6. **记忆不是聊天记录仓库。** Episode、候选经验和已验证知识分层。
7. **先模块化单体。** 当前不需要承担微服务、分布式事务和远程消息系统成本。

## 3. 冻结前应修正的问题

### TA-B01：WorldState 只有名字，没有正式组件

架构数据流写了 `update WorldState`，但职责表、数据契约和持久化策略中没有 WorldModel。若不补齐，Attention、Prefrontal 和 WorkingMemory 会分别维护“当前世界”，产生状态漂移。

建议增加 `WorldModel`（世界模型/黑板）：

- 将原始观测归一化为当前事实；
- 保存每个事实的来源、观测时间、置信度和新鲜度；
- 处理乱序、重复、修订和冲突；
- 生成不可变的 `WorldSnapshot` 供一次认知周期使用；
- 不负责决定任务，也不保存全部历史。

MVP 可用内存投影 + SQLite checkpoint，不需要 LLM。

### TA-B02：缺少认知周期仲裁，可能产生并发计划风暴

多个显著事件、定时器、目标和外部命令可能同时触发 Prefrontal。单一 asyncio 循环只保证代码不并行执行，不保证业务上只有一个一致的决策上下文。

建议增加轻量 `CognitiveCoordinator`：

- 在短窗口内合并刺激；
- 从 WorldModel 获取同一版本 Snapshot；
- 选择本轮焦点和固定目标；
- 分配 `cognitive_cycle_id`；
- 限制同时规划数；
- 抑制与当前任务冲突或重复的计划；
- 规划结束后再评估积压刺激。

它不是第二个事件循环，也不是另一个无限大循环，而是事件驱动的认知事务协调器。

### TA-B03：执行结果没有独立评价，闭环尚未真正闭合

`task.finished` 只能说明工具执行完成，不能说明目标是否达成、信号是否正确或经验是否值得记住。目前 `Evaluation / consolidation` 只存在于数据流文字中。

建议增加 `OutcomeEvaluator`：

- 区分 execution success、goal success 和 decision quality；
- 对即时结果做规则评价，对延迟结果登记待评价项；
- 输出 `outcome.evaluated`，包含指标、证据、评价窗口和 evaluator version；
- 只有评价结果才能推动 Goal 状态和候选经验置信度；
- Prefrontal 订阅的是评价反馈，而不只是 TaskResult。

这是从“自动化执行器”升级为“会根据结果调整的 Agent”的关键。

### TA-B04：消息与 Plan 的语义需要收紧

当前 EventBus 声明“进程内至少一次投递”，但普通 `asyncio.Queue` 没有持久 ack；若消费者取出后崩溃，消息会丢失。准确语义应为：

- 易失感知事件：进程内 best-effort/可合并；
- 事实型业务事件：先写数据库 Outbox，再至少一次派发；
- 消费幂等由 Inbox/processed-message 表保证；
- 不宣称内存消息具备崩溃后的至少一次语义。

此外，Plan 当前用同一对象从 CANDIDATE 变为 APPROVED，和“获批后不可变”容易冲突。建议拆分为追加事实：

```text
CandidatePlan（不可变）
  + PlanDecision(APPROVED/REJECTED，策略版本与理由)
  → ExecutionGrant（执行层只消费这个）
```

Task 继续作为状态投影，并保留追加式 TaskTransition 历史。

## 4. 优化后的认知闭环

```text
Clock/Scheduler ───────────────┐
Sensory Adapters → Normalizer  │
                    ↓          │
                 WorldModel    │
                    ↓          │
                 Attention ◄───┘
                    ↓ salient stimuli
        GoalPolicy + CognitiveCoordinator
                    ↓ world snapshot + focus
                 Prefrontal
                    ↓ CandidatePlan
        Validator → RiskGate → ExecutionGrant
                    ↓
             MotorExec / Workflow
                    ↓ TaskResult
             OutcomeEvaluator
              ↓             ↓
        Goal progress    Episodic Memory
              ↓             ↓
        next cognition   Rest/Consolidation
```

这里的“类脑”对应关系更实质：

| 认知能力 | 工程组件 |
|---|---|
| 感觉输入 | Sensory Adapter + Normalizer |
| 当前信念 | WorldModel / WorldSnapshot |
| 选择性注意 | Attention |
| 内稳态与动机 | 固定 GoalPolicy、预算、期限、风险阈值 |
| 全局工作空间 | CognitiveCoordinator 形成的本轮上下文 |
| 计划和抑制 | Prefrontal + Validator/RiskGate |
| 动作 | MotorExec + Workflow/Tool |
| 奖励与误差信号 | OutcomeEvaluator |
| 情景与知识 | Episodic/Semantic Memory |
| 睡眠巩固 | RestRepair/Consolidation |

## 5. MVP 应做和不应做

### MVP 现在补上

- 确定性 WorldModel 和版本化 WorldSnapshot；
- 轻量 CognitiveCoordinator，仅做合并、快照、互斥和预算；
- OutcomeEvaluator 的规则版本，至少判断目标完成/失败；
- Scheduler 与 Sensory 分离：时间触发由 Scheduler，Sensory 只采集；
- Outbox/Inbox 双端幂等语义；
- CandidatePlan、PlanDecision、ExecutionGrant 分离。

### v1.5 再做

- LLM 动态目标拆解；
- 学习型 Attention；
- 基于预测误差的主动感知频率调整；
- 延迟奖励、策略质量评分；
- 语义记忆和向量检索；
- 多模型路由和模型竞争；
- Agent 自我反思提议，但策略变更仍需审批。

### 暂时不要做

- 多 Agent 社会模拟；
- 每个“脑区”都配置一个 LLM；
- LLM 自行改 Workflow 或风险策略；
- 全量事件溯源重建所有实时行情状态；
- 为追求类脑而引入神经科学名词对应的空模块；
- 在没有评价数据前训练所谓自我进化策略。

## 6. 进一步的工程优化

### 控制面与数据面分开

全市场行情等大数据不要复制进 EventBus 消息。事件只携带摘要和 artifact reference；大对象通过 MarketDataStore/ObjectStore 读取。否则多订阅者 fan-out 会迅速放大内存。

### 常驻协程数量保持克制

脑区是逻辑边界，不必每个类都是一个永久 `while True`。只有需要独立消费、计时或生命周期隔离的组件才常驻；Validator、RiskGate、WorldModel 更新器可作为受控服务方法或事件处理器。

### 避免优先级饥饿

单纯 priority queue 会让低优先级任务长期饿死。调度器应采用优先级 + aging，并为系统健康、任务结果和关闭信号保留容量。

### 时间语义统一

至少区分 `occurred_at`、`observed_at`、`published_at` 和 `processed_at`。市场事件以 event time 判断窗口，系统 SLA 以 processing time 判断，所有规则使用注入 Clock。

### Snapshot 保证可复现

CandidatePlan 必须记录 `world_snapshot_id`、goal version、memory query/result IDs、model/prompt version 和 registry digest，才能真正复现一次决策。

## 7. 推荐的新核心不变量

1. 一个认知周期只读取一个不可变 WorldSnapshot。
2. 同一冲突域同时最多一个获准计划；非冲突计划可并发。
3. Task 成功不自动等于 Goal 成功。
4. 未经 OutcomeEvaluator 评价的 Episode 不提高策略或记忆置信度。
5. 内存 EventBus 不承担事实持久化责任。
6. 所有事实型消息先持久化，后派发；所有消费者按 message ID 幂等。
7. Prefrontal 无权直接修改 WorldModel、GoalPolicy、Policy Memory 和 Workflow Registry。
8. WorldSnapshot、Plan、Decision、ExecutionGrant 均追加且不可变；Task 当前状态是可重建投影。

## 8. 最终判断

当前设计不是错误，而是已经完成了“神经系统骨架”和“受控执行系统”，尚缺明确的“当前信念、认知仲裁和结果评价”。补齐 WorldModel、CognitiveCoordinator、OutcomeEvaluator，并修正消息/Plan 语义后，架构才能稳定形成：

```text
感知 → 信念更新 → 注意 → 目标驱动 → 规划与抑制
    → 行动 → 结果评价 → 记忆与目标更新 → 下一轮认知
```

建议把这 4 项作为阶段 0 的技术修订，而不是留到编码中临时决定。它们会略增设计量，但能显著减少 Prefrontal 膨胀、并发计划冲突、错误学习和恢复歧义。
