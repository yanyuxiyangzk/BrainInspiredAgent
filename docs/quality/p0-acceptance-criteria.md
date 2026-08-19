# MVP P0 验收标准

状态：Review  
版本：1.0-rc1

## 1. 统一测试规则

- 自动化测试使用虚拟时钟，禁止等待真实交易时刻。
- 外部依赖使用符合相同 Capability Contract 的 Fake Skill。
- 所有测试可在 2 vCPU、4 GiB RAM、Linux、本地 SSD 基线上复现。
- “10 秒内”等时限同时提供虚拟时间语义；性能测试才使用真实墙钟。
- 每项标准必须留下测试报告、相关 Trace 和稳定错误码。
- 所有 P0 标准通过后才允许 MVP 发布；不得以人工观察替代可自动化断言。

## 2. FR-001 Supervisor 生命周期

### AC-001-01 正常启动

Given 配置和 SQLite 可用；When 启动应用；Then 5 秒内所有 P0 脑区进入 `READY`，健康接口/CLI 返回唯一实例 ID 和各脑区状态。

### AC-001-02 异常隔离与重启

Given 任一非关键脑区处理消息时抛出未捕获异常；When Supervisor 检测到退出；Then EventBus 和其他脑区继续推进，失败脑区按配置退避重启，并记录 `AREA_CRASHED`。

### AC-001-03 熔断

Given 同一脑区在 60 秒内连续崩溃达到 3 次；When 第三次退出；Then Supervisor 停止自动重启该脑区，系统进入 `DEGRADED`，产生一条熔断告警。

### AC-001-04 优雅关闭

Given 有运行中任务；When 收到关闭信号；Then 停止接收新计划，等待最多 30 秒，持久化可恢复状态，关闭后没有孤立协程。

### AC-001-05 可解释皮层调度

Given 同时存在实时刺激、恢复任务和后台 Run；When CorticalSchedulingPolicy 排序；Then 每项产生 `ADMIT/DEFER/REJECT`、score、rule IDs、原因和预算快照；实时/恢复任务按策略优先，后台任务通过 aging 在资源允许时最终获得准入，策略不得调用 LLM。

## 3. FR-002 EventBus

### AC-002-01 发布订阅

Given 两个订阅者订阅同一消息类型；When 发布一条合法消息；Then 两者各收到一次，且 msg/correlation ID 不变。

### AC-002-02 顺序边界

Given 同一 source 连续发布序号 1～100；When 单个订阅者消费；Then观察顺序严格递增。不同 source 不要求全局顺序。

### AC-002-03 背压

Given 行情订阅队列已满；When 同一 instrument/window 再发布 100 条快照；Then队列不超过配置容量，只保留合并后的最新快照，并累计合并指标。

### AC-002-04 消费隔离

Given 一个消费者持续抛错；When 发布目标消息；Then其他消费者正常处理，失败消息按策略重试并最终进入死信记录。

### AC-002-05 性能基线

Given 4 KiB 内的轻量事件；When 以 1000 条/分钟持续输入 10 分钟；Then无任务结果类消息丢失，队列恢复至稳态，进程内投递延迟 P95 小于 100 ms。

## 4. FR-003 Sensory 与状态感知

### AC-003-01 虚拟日历

Given 注入测试日历和虚拟时钟；When 依次跨越盘前、竞价、交易和收盘边界；Then产生且只产生对应 `brain.state_changed` 事件。

### AC-003-02 感知频率

Given `NORMAL/TRADING`、`NORMAL/IDLE`、`REVIEW/CLOSED`；When各运行一个配置周期；Then默认感知间隔分别为 15、40、300 秒，允许误差不超过调度 tick。

### AC-003-03 数据源序号与时间

Given乱序、重复和未来时间事件；When Sensory 接收；Then保留原始 source sequence，重复事件可识别，超过允许时钟偏差的事件以稳定错误码拒绝。

## 5. FR-004 Attention

### AC-004-01 显著性证据

Given价格变化达到规则阈值；When Attention 处理；Then输出包含 rule_id、score、baseline、current value 和 evidence msg IDs。

### AC-004-02 未达阈值

Given变化低于阈值；When处理 100 条合法事件；Then不产生 salient event，不调用 LLM，并记录聚合指标而非逐条高等级日志。

### AC-004-03 去重和冷却

Given同一 dedup key 重复投递或处于规则冷却期；When处理；Then不产生重复计划，去重原因可查询。

## 6. FR-005 Prefrontal

### AC-005-01 结构化候选计划

Given一个合法 salient event 和允许的固定目标；When Prefrontal 规划；Then只发布符合 Plan Schema 1.0 的 `CANDIDATE` 计划，包含理由、证据、预算请求和固定 Workflow 版本。

### AC-005-02 无幻觉能力

Given模型返回未知 Workflow/Capability 或自由文本；When解析；Then不得修补成可执行调用，以 `PLAN_SCHEMA_INVALID`、`WORKFLOW_NOT_FOUND` 或 `SKILL_BINDING_NOT_FOUND` 结束候选流程。

### AC-005-03 无模型降级

Given模型不可用；When固定时间计划到达；Then确定性计划仍可生成；需要语义推理的计划明确标记降级或失败，不阻塞事件循环。

## 7. FR-006 PlanValidator 与 RiskGate

### AC-006-01 Schema 和注册表

Given缺字段、未知版本、未知 Workflow 或参数不合法的计划；When验证；Then全部拒绝，工具调用数为零并返回对应错误码。

### AC-006-02 过期与陈旧

Given plan 已过 expires_at 或输入数据超过 freshness 限制；When验证；Then分别以 `PLAN_EXPIRED`、`DATA_STALE` 拒绝。

### AC-006-03 权限和模式

Given Workflow 请求未授予 capability，或系统处于 SAFE；When验证；Then拒绝所有非只读/恢复白名单动作。

### AC-006-04 预算

Given计划、每日或模型预算任一不足；When验证；Then以 `BUDGET_EXCEEDED` 拒绝，实际费用不增加。

### AC-006-05 事实不可变

Given计划获批并完成 Skill 解析；When签发 ExecutionGrant；Then plan/decision ID、Workflow digest、SkillBindings、快照、参数摘要、权限和预算不可被执行层修改。

## 8. FR-007 MotorExec

### AC-007-01 状态转换

Given合法且未过期的 ExecutionGrant；When执行成功；Then只创建一个逻辑 Task，并按允许状态机从 `PENDING` 到 `SUCCEEDED`，每次转换有时间和原因。

### AC-007-02 并发限制

Given提交超过全局和 Workflow 限制的任务；When调度；Then同时 RUNNING 数永不超过配置，其余保持 READY/PENDING。

### AC-007-03 超时取消

Given Skill 永不返回；When达到节点或任务 deadline；Then任务进入 `TIMED_OUT`，子任务收到取消，事件循环继续响应。

### AC-007-04 有限重试

Given前两次返回可重试错误、第三次成功；When执行；Then仅按配置重试并成功。不可重试错误不重试。

### AC-007-05 崩溃恢复

Given在 PURE、IDEMPOTENT、QUERYABLE 和 NON_REPLAYABLE 节点分别崩溃；When重启；Then按恢复矩阵重试、查询或转 `REQUIRES_REVIEW`，不盲目重放。

## 9. FR-008 Workflow Runtime

### AC-008-01 核心节点

Given通过发布验证的测试 Workflow；When分别执行 skill、condition、parallel、delay、sub_workflow；Then输出符合 Schema，引用解析和声明的失败传播正确。

### AC-008-02 图和嵌套保护

Given循环 DAG、直接/间接递归、深度 9 或节点数 101；When发布验证；Then全部在运行前拒绝。

### AC-008-03 受限表达式

Given条件中含任意代码、函数或文件访问；When解析；Then以 `EXPRESSION_NOT_ALLOWED` 拒绝，不执行表达式副作用。

### AC-008-04 资源上限

Given节点输出超过 1 MiB；When完成；Then正文不进入事件或日志，输出转本地对象存储并返回带校验和的引用。

### AC-008-05 版本固定

Given存在 1.0.0 和 1.1.0；When计划引用 1.0.0；Then只执行 1.0.0，运行期间注册表变化不改变本次执行。

## 10. FR-009 Trace

### AC-009-01 完整链路

Given任一结束的 E2E 场景；When按 correlation ID 查询；Then可获得源事件、显著性证据、候选计划、审批、Task、节点和最终结果。

### AC-009-02 版本和成本

Given含模型或外部系统 Skill 节点；When执行；Then Trace 保存 Capability、Skill、模型、Prompt、Workflow、数据版本及 Token/费用。

### AC-009-03 脱敏

Given输入含标记为 secret 的测试值；When运行并导出日志/Trace；Then原值匹配数为零，只出现脱敏占位符。

### AC-009-04 追加审计

Given已有状态记录；When发生更正；Then新增更正记录并引用原记录，不原地静默覆盖审计事实。

## 11. FR-010 Working Memory

### AC-010-01 容量

Given容量 100；When写入 101 个同优先级条目；Then容量不超过 100，按策略淘汰最旧条目。

### AC-010-02 TTL 与重要度

Given过期和高重要度条目并存；When执行回收；Then过期条目删除，高重要度未过期条目不被普通条目优先挤出。

### AC-010-03 重启语义

Given进程重启；When重新启动；Then Working Memory 为空或由持久事实重建，不把旧内存快照冒充当前事实。

## 12. FR-011 Episodic Memory

### AC-011-01 持久化

Given一个成功和一个失败任务；When重启后查询；Then Episode 及关联 Trace 完整可用。

### AC-011-02 事务一致性

Given任务状态与 Outbox 在事务提交前注入崩溃；When恢复；Then二者要么都不可见，要么都可见，不出现已成功但无结果事件的半状态。

### AC-011-03 保留策略

Given超过 90 天 Trace 和超过 30 天大对象；When执行清理；Then按策略删除/归档，长期摘要及审计删除记录保留。

## 13. FR-012 最小夜间复盘

### AC-012-01 自动进入 REVIEW

Given测试交易日；When时钟越过 15:30；Then系统进入 CLOSED/REVIEW，停止实时扫描并在 60 秒内请求当日复盘。

### AC-012-02 每日幂等

Given当日复盘已成功；When重复状态事件或重启；Then不生成第二条成功复盘。

### AC-012-03 经验隔离

Given复盘产出经验；When持久化；Then只能写 `candidate`，证据、置信度、范围和有效期齐全。

### AC-012-04 保持在线

Given复盘完成；When虚拟时间推进 6 小时；Then进程和健康感知保持运行，可响应允许的 CLI 查询。

## 14. FR-016 最小 CLI

### AC-016-01 状态查询

Given实例运行；When执行状态命令；Then返回实例、三维状态、脑区健康、队列深度和活动任务，不经 LLM。

### AC-016-02 命令统一治理

Given注入“立即生成摘要”命令；When接收；Then形成 `command.received` 并走 Coordinator、PlanValidator、RiskGate、SkillResolver 和 GrantIssuer，不允许 CLI 直接调用 Skill。

### AC-016-03 非法命令

Given未知或越权命令；When执行；Then以非零退出码和稳定错误码拒绝，不产生业务副作用。
