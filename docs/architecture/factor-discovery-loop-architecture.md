# Loop Engineering 因子发现架构

状态：Accepted as v1.5 extension  
版本：1.0  
适用范围：量价因子自动发现、后续另类数据和高频因子研究

## 1. 定位

因子发现把一次性模型调用升级为“触发 → 生成 → 审查 → 硬验证 → 记录 → 调整”的可恢复闭环。它运行在 BrainAgent Kernel + Active Agent Platform 的类脑底座中，由 LoopEngine 调度。它不是新的顶层事件循环，也不是 Workflow 自己的永久 `while true`；FactorDiscoveryLoop 是 Loop 调度层托管的持久化领域 Loop Profile，每一轮都是一次有限的 `factor_discovery_iteration` Workflow Run。

```text
BrainAgent Kernel + Active Agent Platform（类脑底座与运行平台）
  → LoopEngine（统一调度层 / 唯一顶层心跳）
  → Scheduler 每 5 分钟触发 iteration
  → FactorDiscoveryLoop 加载 checkpoint
  → 生成候选 → 规则/LLM 审查 → 硬编码回测验证
  → 原子提交结果和 checkpoint
  → Hook 发布摘要与下一轮预算
```

## 2. 四层职责

| 层级 | 职责 | 生命周期 |
|---|---|---|
| BrainAgent Kernel + Platform | 脑区、EventBus、Memory、Skill、Policy 和 Trace 运行环境 | 进程级底座 |
| LoopEngine | 全局服务、时间刺激、预算、并发、恢复和退出 | 进程级唯一调度层 |
| FactorDiscoveryLoop | 研究目标、迭代计数、checkpoint、终止条件和策略预算 | 持久化 Profile |
| Factor Iteration Workflow | 一轮生成/审查/验证 DAG | 一次性 |
| Skill/Sub-agent | 生成、审查、回测、去重等能力实现 | 单次调用 |

FactorDiscoveryLoop 不得创建新的 asyncio loop，只能向 LoopEngine 请求下一轮 Run。它可以读取类脑底座提供的 Memory/World/Skill 端口，但不得绕过 EventBus、Policy 和 Grant。中断时恢复 Profile 和未完成 Run，不从头开始。

FactorDiscoveryLoop 的每 5 分钟触发只产生待准入请求，不保证立即执行。CorticalSchedulingPolicy 根据市场阶段、实时任务、deadline、CPU/模型预算和 aging 决定 `ADMIT/DEFER/REJECT`：交易时段默认降权，REVIEW/HOLIDAY 获取后台配额，SAFE 时停止新迭代。这样领域 Loop 不会挤占实时类脑任务。

## 3. 生成—审查—验证闭环

### 3.1 Generate

生成器根据 `FactorSearchState` 和预算返回结构化候选，不执行回测、不写因子库。默认配额：

| 策略 | 比例 | 作用 |
|---|---:|---|
| `mutate` | 25% | 替换父本表达式树叶节点或子树 |
| `crossover` | 25% | 交换两个父本的子树分支 |
| `parameter_perturb` | 15% | 依据动量和自适应步长微调窗口 |
| `random_explore` | 15% | 从字段/算子分布独立探索 |
| `llm_mechanism` | 20% | 按经济学机制族提出假设并生成表达式 |

冷启动以随机和机制探索建立覆盖；积累父本后动态平衡探索与利用。上一轮入库率、重复率、机制覆盖、失败模式和停滞程度可调整下一轮比例，但不得突破 Profile Policy 上下限。

### 3.2 Review

- 未知字段/算子、跨量纲、非法窗口和数据越界直接拒绝；
- 表达式规范化、恒等式化简、重复哈希、复杂度和树深度过滤；
- 模型审查边界条件、缺失值、极值和经济学含义；
- 生成 Agent 与审查 Agent 使用不同 SkillBinding、模型配置和上下文；
- LLM 只返回结构化 verdict，不能绕过硬门槛或触发回测。

### 3.3 Validate

验证是确定性硬门槛，不依赖模型自我评价：固定数据版本、样本区间、换仓频率、成本、滑点和基准；计算 IC、年度稳定性、风险调整收益、近期持续性、换手和独立性；执行多项联合过滤和 IC 相关性去重；通过后入库，否则进入失败模式库。每次结果保存回测参数、代码/Skill digest 和 Artifact。

## 4. Checkpoint 与断点续跑

每轮结束以临时文件写入、fsync、原子 rename 提交 checkpoint，至少包含：

- Profile、迭代计数、最后完成时间和下一触发时间；
- 已测试候选哈希及算法版本；
- 入库因子定义、验证结果、IC Artifact 和 digest；
- 父本池、机制覆盖、失败模式和 FSA 统计；
- 五类策略实际配额、动量和自适应步长；
- 数据、算子、Skill、Workflow 版本和错误状态。

先在 SQLite 事务中提交候选/验证事实与 Outbox，再提交 checkpoint 指针。恢复时交叉校验事实与 checkpoint digest；不一致进入 `REQUIRES_REVIEW`，禁止盲目覆盖。

## 5. FSA 频繁子树规避

FSA 定期统计表达式子树。默认某骨架占比超过 15% 且长期无增量价值时进入版本化禁止列表；同骨架参数变体设上限；列表记录原因、统计窗口和解除条件。生成后的确定性审查负责拦截，LLM 不得绕过。FSA 只影响新搜索，不改写历史和已入库因子。

## 6. Hooks 与 Sub-agent

Hooks 是 Loop 事件订阅者，不是隐藏控制流：`iteration.completed` 输出摘要，`iteration.failed` 记录失败，`checkpoint.committed` 校验备份，`factor.accepted` 更新覆盖，`profile.stalled` 请求调整。

生成与审查 Sub-agent 都是独立 Skill 调用，具有独立 Schema、预算、模型版本和权限。它们不能写 checkpoint、Registry 或因子库，结果必须返回父 Workflow 接受硬校验和事务提交。

## 7. 终止、恢复与治理

Profile 必须配置最大迭代、每日预算、连续失败阈值、停滞窗口、最大候选数和人工暂停开关。达到条件进入 `COMPLETED`、`PAUSED` 或 `REQUIRES_REVIEW`，不得无限运行。恢复从最近一致 checkpoint 继续，已测试哈希不重复回测。

只允许研究/回测 Capability，不得生成下单能力；数据泄漏、未来函数、样本外越界和生存偏差是硬拒绝；LLM 表达式必须经过 AST/Schema/数据范围校验；结果必须标注研究用途。

## 8. 多目标评价与版本进化

评价同时考虑通过率、年度稳定性、风险调整收益、近期表现、独立性、换手、成本、重复率、机制覆盖和失败率。安全、数据质量和防泄漏是硬约束。Profile、Workflow 或预算策略改变必须产生新版本/WorkflowPatch，Active 不原地修改。

因子发现属于 v1.5。MVP 只提供 BrainAgent Kernel、Active Agent Platform、通用 LoopEngine、checkpoint、Workflow/Skill 和两条基础 E2E，不承诺真实因子收益。
