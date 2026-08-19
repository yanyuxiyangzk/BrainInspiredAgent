# DNA MVP 计划

## 定义

DNA 是驱动 Agent 执行的版本化 JSON 编码。它描述目标、Workflow 组织、Skill/Capability 引用、输入输出和演化元数据；WorkflowRuntime 是 DNA 解释器，Skill 插件是具体能力实现。现有 Workflow JSON 是 DNA 的执行内核，首阶段采用 Envelope 包装，不改变执行协议。

```text
DNA JSON → WorkflowRuntime → Skill Plugin → Outcome/Trace
```

DNA 只能产生候选变化，不能绕过 Schema、RiskGate、Grant、权限、预算或恢复边界。Active 版本追加而非原地修改。

## MVP 顺序

| 任务 | 状态 | 内容 | 验收 |
|---|---|---|---|
| H01 | `🧪 原型已完成` | DNA Envelope、Workflow 包装、版本、digest 和不可变模型 | 旧 Workflow 可包装；digest 稳定；正式 Schema 尚待 H01.1 |
| H01.1 | `✅ 已开发已测试` | DNA 1.0 JSON Schema、规范化和双 digest | T01 正反例；content/envelope digest 含义固定；旧 Workflow 适配 |
| H02 | `🧪 内存原型已完成` | DNA Registry、状态和 Active 版本切换 | 单进程显式激活；持久化和严格状态机尚待 H02.1 |
| H02.1 | `✅ 已开发已测试` | SQLite Registry、CAS 状态机、审计、回滚和谱系校验 | 原子激活；重启不丢；非法跃迁/循环父链拒绝；Active 唯一 |
| H03 | `✅ 已开发已测试` | DNA Composer | 顺序/并行/子 DNA 组合；Schema、Capability、预算和深度边界 |
| H04 | `✅ 已开发已测试` | DNA Fitness | 将 Outcome、Evidence、成本、延迟、风险投影到 DNA 版本 |
| H05 | `✅ 已开发已测试` | Experience Dataset | 从 Episode/Trace 构造可归因训练样本 |
| H06 | `✅ 已开发已测试` | Mutation/Crossover Candidate | 参数、节点、Skill Binding 和 DNA 组合候选生成 |
| H07 | `✅ 已开发已测试` | Sandbox Replay | FakeClock、历史重放、故障注入和新旧 DNA 对照 |
| H08 | `✅ 已开发已测试` | Population Selection | 适应度、Pareto、多样性、淘汰和去重 |
| H09 | `✅ 已开发已测试` | Shadow/Canary Promotion | Shadow → Canary → Active，自动回滚 |
| H10 | `✅ 已开发已测试` | Lineage/Explain | 父 DNA、变异、证据、fitness delta 和回滚解释 |
| H11 | `✅ 已开发已测试` | Agent DNA | Goal/Attention/Memory/Evaluation 策略纳入 DNA |
| H12 | `✅ 已开发已测试` | Organization DNA | 多 Agent DNA 协作、仲裁和组织级预算 |
| T07.1 | `✅ 已开发已测试` | DNA Execution Identity | Organization/Agent/Workflow 身份贯穿 Plan、Decision、Grant、Task、Run、Episode、Outcome 与 Trace |
| T07.2 | `✅ 已开发已测试` | Organization Governed Entry | responsibility 委派、fallback、预算和三层 digest 校验后进入既有治理执行链 |

## 当前首阶段

H01/H02 的内存原型保留于 `domain_sdk.dna`，H02.1 已通过 `PersistentDnaRegistry` 和 012 迁移形成持久化 DNA 控制面。`DnaDefinition` 仍负责包装和验证现有 Workflow；生产式版本注册、晋级、激活和回滚由 SQLite Registry 管理。H03 已允许受静态治理的显式组合；H06 之后才会自动产生候选；H09 之前不会自动上线。

## H02.1 持久化控制面

- `dna_definition` 保存不可变版本文档、双 digest、当前状态和 CAS `revision`；数据库 partial unique index 保证每个 `dna_id` 最多一个 `ACTIVE`。
- `dna_parent` 保存结构化父引用；注册时校验父版本存在、content digest 匹配、最多四个父、祖先深度不超过 32，并拒绝直接或祖先子图循环。
- `dna_transition` 保存注册和每次状态变化；event ID 唯一，数据库触发器禁止更新或删除，形成追加式审计链。
- 严格路径为 `CANDIDATE → VALIDATED → SHADOW → CANARY → ACTIVE → DEPRECATED → RETIRED`；各阶段也允许显式退役，但不允许跳级。
- 激活会在同一事务中弃用旧 Active 并晋级 Canary；回滚会以双方 revision 做 CAS，在同一事务中切换当前和目标版本。任一步失败，状态和审计记录全部原子回滚。
- Registry 重启后从 SQLite 和 DNA 文档恢复；读取时重算双 digest，持久化文档被篡改会拒绝加载。

## H03 DNA Composer

- `DnaComposer` 将一至四个已通过验证门的 Workflow DNA 组合成新的 `CANDIDATE`，不修改、不激活任何来源版本；成员以 `dna_id/version/content_digest` 固定为父谱系。
- 顺序模式生成 `sub_workflow` 依赖链；并行模式生成 `parallel` fan-out gate。生成物仍是 Workflow 1.0，可由现有 `WorkflowValidator`、`WorkflowRegistry` 和 `WorkflowRuntime` 直接解释，无需新的执行协议。
- `DnaComponent` 为每个成员声明别名、输入映射和输出 Object Schema。Composer 对父输入及前序成员输出执行保守类型检查，拒绝缺少必填字段、未知字段、未来节点引用和类型不兼容。
- `CompositionLimits` 固定父数量、展开节点数、子 DNA 深度、超时、并发、Capability 白名单、Permission 白名单和最大 SideEffect 等级。顺序超时按子预算求和，并行超时取最大值；顺序并发取最大值，并行并发求和，调用方不能用较小外层预算掩盖真实子预算。
- Composer 递归解析所有 `sub_workflow` 引用，拒绝缺失版本、循环、超深和组合展开爆炸；仅允许 `VALIDATED/SHADOW/CANARY/ACTIVE/DEPRECATED` 成员，禁止未经验证的 Candidate 或已 Retired DNA 参与组合。
- 当前 Schema 检查是 Workflow 边界的保守 Object Schema 检查，不推断 Skill 实现的动态输出；每个组件必须显式提供输出契约。H04/H05 的评价数据不会反向放宽这些静态安全边界。

## H04 DNA Fitness

- `DnaFitnessProjector` 将已持久化 Outcome 与成本、延迟、稳定性、风险观察投影到固定的 `dna_id/version/content_digest`；归因前同时验证 DNA Registry、Outcome task/correlation/document 和实际 WorkflowRun 的 workflow ID/version/digest，拒绝调用方伪造归因。
- 013 迁移新增追加式 `dna_fitness_observation` 和可重建 `dna_fitness_snapshot`。每个 Outcome 只能归因一次；相同 payload 重试幂等，不同归因冲突拒绝；数据库触发器禁止修改或删除原始观察。
- Fitness 保存多维向量：样本数、成功率、Wilson 成功率置信下界、证据分、用户价值分、平均成本、平均延迟、P95 延迟、稳定率和风险事件率，不提供可掩盖风险的单一综合分。
- `DnaFitnessPolicy` 固定评价策略版本、窗口 ID、起止时间、最小样本量、最大风险率和置信参数。窗口外事实拒绝混入；窗口关闭后通过 `refresh` 形成最终投影。
- readiness 分为 `COLLECTING/OBSERVING/READY/RISK_BLOCKED`。样本不足不能就绪，观察窗未结束不能就绪；达到样本量后只要风险率超过硬阈值，即使成功率和用户价值很高仍然 `RISK_BLOCKED`。
- H04 只形成可审计评价投影，不自动选择、变异或晋级 DNA；H05 固定经验数据集，H08 才在硬约束下做 Pareto 选择。

## H05 Experience Dataset

- `ExperienceDatasetBuilder` 在一个 SQLite 事务中，从 H04 Observation 连接 Outcome、Episode 和同 correlation 的 Plan/Decision/Grant/Task/WorkflowRun/NodeRun/Audit，复制成自包含的 Experience Sample；训练与重放不依赖之后可能变化的在线查询结果。
- `ExperienceDatasetSpec` 固定 dataset ID/version、builder version、Fitness window、起止时间、baseline content digest、candidate content digests、每个 DNA 最小样本量，以及严格按时间排序的 Train/Validation/Test 切分边界，防止未来事实泄漏到训练集。
- 每个 Sample 固定 DNA ID/version/content digest、baseline/candidate cohort、split、Observation/Evaluation/Episode/Correlation 来源 ID、Observation digest、Outcome、Evidence、Fitness 向量和完整 Trace，并计算规范化 `sample_digest`。
- Dataset manifest 由规范文档和有序 sample digest 列表计算；014 迁移将 manifest 和 sample 封存为不可更新、不可删除的事实。同一版本同一规范重试幂等，新增在线事实不会被既有版本吸收；同一版本更换规范会拒绝。
- `replay` 重新计算全部 sample digest 和 manifest digest，任何样本内容、顺序或 manifest 篡改都会失败。窗口外记录、缺少 baseline/candidate 样本或任一 DNA 低于最小样本量时不得构建。
- H05 只提供固定、可归因、可重放的数据资产；H06 才能使用它生成 mutation/crossover Candidate，且不得直接修改 Active DNA。

## H06 Mutation/Crossover Candidate

- `DnaCandidateGenerator` 读取已封存 Dataset manifest，并验证每个父 DNA 的注册版本、content digest 和 Dataset 样本存在性；它只生成新的 `CANDIDATE` 提案，不修改来源 DNA、不注册为 Active，也不绕过 H02 状态机。
- 操作不是任意 JSON Patch，而是六类有语义白名单：`SET_INPUT`、`SET_CONSTRAINT`、`SET_CAPABILITY_VERSION`、`ADD_SKILL_NODE`、`REMOVE_NODE`、`REPLACE_FROM_DONOR`。Mutation 使用单亲；Crossover 必须固定双亲并至少执行一次 donor 节点替换。
- `CandidatePolicy` 固定 policy version、`mutable_paths`、Capability/Capability-Version Binding 白名单、Permission 白名单、最大 SideEffect、最大操作数和节点数。Schema 主版本、Workflow 身份、输入输出 Schema、policy/硬预算、SideEffect 和权限声明均为不可变边界。
- 新增或替换 Skill 节点会再次检查字段白名单、约束数值、Capability 声明、Binding、Permission 和 SideEffect；最终 Workflow 必须重新通过 `WorkflowValidator`，无效依赖、输出引用、循环和版本格式都会拒绝。
- 015 迁移追加保存 proposal ID、模式、假设、操作及解析路径、Dataset manifest、策略版本、父 DNA、完整 Candidate 文档和 proposal digest。相同 proposal 重试幂等；同 ID 不同内容、重复 Candidate content、审计更新或删除均拒绝。
- Candidate generator 记录可复现的变更提案，但当前不自行上线，也不根据单条样本自由发明操作。H07 必须先做 Sandbox Replay；H08 负责群体选择；H09 才允许受门控的 Shadow/Canary 晋级。

## H07 Sandbox Replay

- `DnaSandboxReplay` 只选择 H05 封存数据集的 Validation/Test 样本，明确禁止 Train split 进入候选验收，防止在训练数据上自证改进。每个 Parent/Candidate case 使用相同历史 `virtual_time`、确定性 seed 和故障场景。
- `SandboxExecutor` 是隔离执行协议，不调用生产 Skill；默认拒绝含 `NON_REPLAYABLE` 节点的 DNA，只有显式配置了模拟器策略才能进入 Sandbox。支持 `TIMEOUT/SKILL_FAILURE/CORRUPT_OUTPUT/CANCELLED` 故障注入。
- 每个 DNA 对同一 case 至少重复执行两次，完整 Measurement（成功、证据、价值、成本、延迟、稳定性、风险和输出 digest）必须一致；Candidate 非确定性属于硬失败。
- Parent/Candidate 分别形成成功率、证据、价值、平均成本、平均/P95 延迟、稳定率和风险率向量，并计算相对 delta。成功、证据、价值、稳定性、成本和延迟阈值由版本化 `ReplayPolicy` 固定；Candidate 风险超过硬阈值直接失败，不参与平均收益抵消。
- Replay 会验证持久化 Proposal、Candidate 文档、父 DNA、Dataset manifest 和每个 Sample digest。016 迁移追加保存 run、每个 case、故障、seed、Measurement、确定性标志和双层 digest；同 replay ID 同请求幂等，不同请求冲突，记录不可修改或删除。
- H07 的 `PASSED` 只表示离线 Sandbox 门通过，不代表 Candidate 可上线。H08 还要进行群体/Pareto 选择，H09 还要经过 Shadow/Canary 和自动回滚门。

## H08 Population Selection

- `DnaPopulationSelector` 只读取已持久化且 digest 一致的 H06 Proposal 与 H07 Replay，Replay 失败、带失败原因或存储事实被篡改的 Candidate 会被硬淘汰，不能靠其他维度的高分抵消。
- 每个 Candidate 使用 H07 的多维向量参与选择：成功率、证据、用户价值、稳定性最大化，成本、平均/P95 延迟和风险最小化；只有不存在其他 Candidate 在全部维度不差且至少一维更优时，才进入 Pareto 前沿。
- 前沿内部以 H06 操作签名的 Jaccard 距离衡量多样性，并按版本化策略的最小 novelty 和最大 survivor 数量截断；结果明确记录 `SELECTED/DOMINATED/DUPLICATE/HARD_REJECTED/CAPACITY`，不会把淘汰误写成晋级。
- 017 迁移追加保存 Selection run、每个成员的 disposition、Pareto rank、novelty、完整向量、原因及双层 digest；同 selection ID 同请求幂等，不同请求冲突，run/member 均不可修改或删除。
- H08 只输出离线 survivor 集合，不注册、不改变 DNA 状态、更不会激活版本。H09 才能把 survivor 送入受门控的 Shadow/Canary，并在异常时自动回滚。

## H09 Shadow/Canary Promotion

- `DnaPromotionController` 只接纳 H08 中 disposition 为 `SELECTED` 且 Proposal/content digest 与存储一致的 Candidate；控制器负责注册 Candidate，并沿 Registry 严格状态机进入 `VALIDATED → SHADOW`，未选中成员不能启动 campaign。
- Shadow 使用镜像路由，不替代基线输出；Canary 使用确定性 routing key 哈希和版本化流量比例，只把稳定的一小部分请求交给 Candidate；通过样本数、最短观察窗、成功率、稳定率和风险率门后才允许 `SHADOW → CANARY → ACTIVE`。
- 重复副作用、权限扩大、恢复失败或任意风险违规属于立即停止条件，不等待平均指标。Shadow/Canary 阶段会自动 Retire Candidate；Active 阶段会通过 Registry CAS 将新版本降级并原子恢复之前的 Deprecated baseline。
- 提供独立 kill switch；终态重复调用幂等。每个 observation 固定其所属 stage 和 digest，同 observation ID 不同 payload 冲突；campaign 使用 revision CAS，promotion event 与 observation 均为追加式审计事实。
- 018 迁移新增 campaign、observation 和 event 表。H09 完成的是受控晋级控制面与确定性路由决策，不直接实现基础设施负载均衡器；领域/部署适配器根据 `PromotionRoute` 执行镜像或分流。

## H10 Lineage/Explain

- `DnaLineageExplainer` 从目标 DNA 沿结构化 parent 引用递归构造最多 32 层的谱系，逐个重建 DNA 文档并核对 content digest、父引用和 Registry 存储；缺失、循环、错误 digest 或被篡改文档均拒绝解释。
- 对 H06 生成物，解释包含 mutation/crossover 模式、假设、操作、策略版本和 H05 Dataset manifest，并重新计算 Proposal digest。治理状态变化不会误判为执行内容变化，解释身份固定在 content digest。
- 决策证据串联 H04 Fitness snapshots、H07 Replay 状态/delta、H08 disposition/Pareto rank/novelty，以及 H09 Campaign、Observation 和 Event。Replay、Selection member/report 与 Promotion observation 的 digest 都会在生成解释前复核。
- `why` 提供稳定的机器可读原因序列，例如生成假设、Replay 结果、Selection disposition、最终 Promotion stage 和最后一次晋级/停止/回滚原因；完整结构化 document 可由 CLI、查询接口或未来可视化层转换为面向用户的说明。
- 019 迁移把一次解释保存为追加式、防篡改快照；同 explanation ID 对同一 DNA 幂等，对不同目标冲突。解释是历史证据快照，不修改 DNA、Selection 或 Promotion 状态。

## H11 Agent DNA

- Agent DNA 使用独立 `kind=AGENT`、独立 `dna_id/version` 和双 digest，不把 Agent 身份继续绑定到某个 Workflow ID。它通过带 role 的强引用组合一至 32 个 Workflow DNA，执行仍由既有 WorkflowRuntime 和 Skill 完成。
- `AgentPolicyProfile` 固定五类认知策略：Goal 的允许类型/活动数量/默认优先级，Attention 的显著性权重/焦点容量/切换阈值，Planning 的策略/时间视野/任务上限，Memory 的工作容量/情景保留/语义候选上限，以及 Evaluation 的证据/价值阈值和复盘周期。
- Agent DNA 策略是受 Schema 约束的参数，不允许注入代码、自由表达式、权限或新的 Runtime 语义。策略、Workflow 引用或版本变化会改变 content digest；Candidate 到 Active 的治理状态变化只改变 envelope digest。
- `PersistentAgentDnaRegistry` 在注册时验证每个 Workflow DNA 已存在、content digest 匹配且不是 Candidate/Retired；独立使用 CAS revision、唯一 Active、严格 `CANDIDATE → VALIDATED → ACTIVE → DEPRECATED → RETIRED` 路径和追加式 transition 审计。
- 020 迁移新增 Agent DNA definition、Workflow role reference 和 transition 表；`agent-dna-1.0.schema.json` 已进入 T01 全量 Schema 契约。H11 不改变现有 Workflow DNA 1.0，因此旧领域插件和 H01～H10 链路保持兼容。

## H12 Organization DNA

- Organization DNA 使用独立 `kind=ORGANIZATION`、身份、版本和双 digest，以 2～64 个带唯一 role 的 Agent DNA 成员构成组织；每个成员固定职责、优先级和 Agent content digest，不复制 Agent 或 Workflow 执行内容。
- `OrganizationPolicyProfile` 固定通信 channel、消息大小和 hop 上限，职责/优先级委派，并提供 `PRIORITY/QUORUM/UNANIMOUS` 仲裁、组织级 token/成本/时间/并发预算，以及成员失败次数、隔离时间和 fallback role。
- 委派是确定性的：优先选择声明对应 responsibility 的可用成员，并按 priority/role 稳定排序；没有匹配者时只允许显式 fallback。仲裁拒绝未知角色和空投票，预算请求任何维度越界都会整体拒绝。
- `PersistentOrganizationDnaRegistry` 注册时验证全部 Agent DNA 已存在、content digest 匹配且状态为 Validated/Active/Deprecated；使用 CAS revision、唯一 Active、旧 Active 自动 Deprecated、严格状态机和追加式 transition 审计。
- 021 迁移新增 Organization definition、不可变 member reference 和 transition 表；`organization-dna-1.0.schema.json` 已加入 T01 全量契约。Organization DNA 只负责治理协作，实际执行仍沿 `Organization → Agent DNA → Workflow DNA → Skill` 下沉。

## T07 DNA 全链路执行连接点

- `DnaExecutionIdentity` 固定 Organization、委派 role、Agent 和 Workflow 的 `dna_id/version/content_digest`；执行入口从 SQLite Registry 重建三层引用，验证 Organization member、Agent Workflow role、Active 状态和实际 Workflow digest，调用方不能用字符串伪造身份。
- `GovernedCognitiveApp.execute` 保持旧三参数调用兼容；完整 DNA 入口额外要求 identity 与 responsibility，并把同一 `dna_context` 固定进 Plan、PlanDecision 和 ExecutionGrant。Task、WorkflowRun、Episode、Outcome 通过 022 迁移的 `dna_execution_context` 关联到同一 context digest，Trace 查询直接返回该上下文。
- `OrganizationGovernedApp` 是新的组织执行入口：加载唯一 Active Organization DNA，先检查组织 token/成本/时间/并发预算，再按 responsibility 和 unavailable roles 确定委派；随后加载确切 Agent 和 Workflow role。委派在进入 Plan 前冻结，重试沿原 Grant/Task 上下文执行，不在执行期间重新路由。
- Organization 预算是 RiskGate 之前的附加上限，不替代 RiskGate；任一预算越界、无可用 fallback、Agent/Workflow 非 Active、引用缺失或 digest 漂移都会在 Skill 调用前失败。
- `dna-execution-context-1.0.schema.json` 是可选兼容扩展，因此旧 Plan/Decision/Grant 文档仍合法；新组织入口必须产生完整上下文。`dna_execution_context` 为追加式事实，禁止更新与删除。

## 可变与不可变边界

可变：Workflow 节点结构、参数、分支、Skill Binding、DNA 组合和评价策略。

不可变：Kernel/Runtime 语义、权限上限、RiskGate、预算上限、Schema 主版本、事务/恢复语义、历史事实。

## 专家评审结论（2026-08-18）

结论：采用“DNA Envelope 包装现有 Workflow，Runtime 和 Skill 保持稳定”的方向成立，能够增量演进；当前实现适合作为概念证明，但存在以下发布阻断项。

### 必须补齐

1. **DNA 身份不能永久等同于 workflow_id。** Workflow DNA 可以默认映射，Agent DNA 和 Organization DNA 必须拥有独立 `dna_id`，并引用一个或多个 Workflow DNA。
2. **需要两个 digest。** `content_digest` 只覆盖可执行内容和不可变策略，用于复现；`envelope_digest` 覆盖谱系、生成器和声明元数据，用于审计。状态、运行统计不得改变 content digest。
3. **DNA Schema 已由 H01.1 补齐。** DNA 1.0 Schema、T01 正反例和旧 Workflow 包装已完成；未来 Agent/Organization DNA 必须使用新的 kind 版本扩展，不能悄悄放宽 Workflow DNA 1.0。
4. **Registry 必须持久化和事务化。** 当前字典 Registry 重启丢失；激活必须使用 SQLite 事务、唯一 Active 约束、CAS、追加式转换历史和审计。
5. **状态必须是严格状态机。** 禁止注册时伪造 `ACTIVE`；推荐路径为 `CANDIDATE → VALIDATED → SHADOW → CANARY → ACTIVE → DEPRECATED → RETIRED`，跳级需要有版本化策略和人工授权证据。
6. **谱系必须结构化。** 父引用采用 `dna_id/version/content_digest`，校验父存在、digest 匹配、无循环、最大祖先深度和最大父数量。
7. **执行必须可归因。** Plan、Grant、Task、WorkflowRun、Episode、Outcome、Trace 必须固定记录 DNA ID/version/content digest；否则 Fitness 无法判断改进来自哪个 DNA。
8. **组合必须先类型检查。** H03 必须验证输入输出 Schema、Capability、权限、SideEffect、预算、deadline、节点/递归深度和组合爆炸上限。
9. **Fitness 不能只有单一分数。** 保存成功、证据质量、用户价值、成本、延迟、稳定性和风险向量；选择时采用约束 + Pareto，禁止高风险通过平均分掩盖。
10. **经验数据必须防止错误归因。** 固定数据集版本、时间切分、最小样本量、置信区间、基线 DNA、反事实或 A/B 标识，避免把市场环境变化误判为 DNA 进化。
11. **晋级必须具备停止条件。** Shadow/Canary 需要样本量、观察窗、回退阈值、kill switch、最大风险预算；出现重复副作用、权限扩大或恢复异常立即回滚。
12. **自动变异范围必须白名单化。** `mutable_paths` 可变；权限、预算硬上限、NON_REPLAYABLE 规则、Schema 主版本和 Kernel 安全语义属于 `immutable_paths`。

### 模型分层

```text
Workflow DNA      执行结构，可直接交给现有 WorkflowRuntime
Agent DNA         Goal/Attention/Planning/Memory/Evaluation + Workflow DNA 引用
Organization DNA  多 Agent DNA 的角色、通信、仲裁和组织级预算
```

H03 只实现 Workflow DNA 组合，不提前混入 Agent/Organization 语义。H11/H12 分别扩展更高层，避免首版 Envelope 过度泛化。

### 数据闭环

```text
DNA content digest
  → Plan/Grant 固定
  → Workflow/Skill 执行
  → Episode/Outcome/Evidence
  → Fitness vector + 数据集版本
  → Candidate mutation/crossover
  → Replay/Shadow/Canary
  → 晋级或淘汰
```

训练数据数量增长本身不会触发进化；只有达到最小有效样本、证据完整、评价窗口结束且相对基线有统计上可信的改善，才允许产生或晋级候选。

## 修订后的开发门

- H03 开始前：完成 H01.1 的 Schema/digest 设计，至少冻结组合接口。
- H04/H05 开始前：DNA 身份必须进入 Trace/Outcome，定义数据集版本和评价窗口。
- H06 开始前：完成 mutable/immutable path、资源上限和候选去重。
- H09 开始前：完成持久化 Registry、CAS、回滚、kill switch 和副作用安全验证。
- H11/H12 开始前：Workflow DNA 在两个独立领域完成长期对照，证明组合抽象稳定。

## H01.1 digest 规范

`content_digest` 的规范输入为：`dna_spec_version`、独立 `dna_id`、DNA `version`、`kind` 和规范化后的 Workflow。JSON 使用键排序、UTF-8 和紧凑分隔符；它是 Plan/Grant/Run 将要固定的执行身份。

`envelope_digest` 在上述内容上加入 `status`、`content_digest`、结构化父 DNA 和 generator 元数据；状态晋级或谱系/生成器变化会产生新 envelope digest，但不会改变 content digest。旧 `digest` 属性暂时作为 `content_digest` 的只读兼容别名。
