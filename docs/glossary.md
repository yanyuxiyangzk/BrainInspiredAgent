# 术语表

| 术语 | 定义 |
|---|---|
| ActiveBrain | 应用主入口和总体运行容器 |
| Supervisor | 管理常驻服务生命周期、健康状态与恢复的控制组件 |
| Brain Area / 脑区 | 具有单一职责的常驻异步服务 |
| Thalamus / EventBus | 脑区业务事件的发布订阅通道 |
| Perception | 未经决策解释的环境观测 |
| Attention | 对事件进行去重、合并、显著性评分和优先级调整的过程 |
| Prefrontal | 根据事件、目标和记忆生成候选计划的决策脑区 |
| Goal | 带完成条件、期限、优先级和预算的持续意图 |
| TaskPlan | 一组经过结构化表达、尚待验证的 Workflow 执行请求 |
| MotorExec | 只负责调度和执行已授权计划的脑区 |
| Workflow | 版本化、一次性、声明式任务定义 |
| Capability | Workflow 节点声明的版本化能力契约，不包含具体实现 |
| Skill | 实现一个或多个 Capability 的可版本化、可替换能力包 |
| SkillBinding | 一次 Task 内固定 Capability 到具体 Skill 版本和 digest 的绑定 |
| Trace | 一次事件—决策—执行链路的可审计记录 |
| Working Memory | 当前任务所需、容量有限的内存上下文 |
| Episodic Memory | 具体事件和执行经历的结构化历史 |
| Semantic Memory | 从经历中提炼并验证过的知识 |
| RiskGate | 独立于 LLM 的权限、风险、预算和副作用控制点 |
| Idempotency Key | 用于保证同一逻辑动作不会产生重复副作用的稳定键 |
| Correlation ID | 关联一次完整业务链路的标识 |
