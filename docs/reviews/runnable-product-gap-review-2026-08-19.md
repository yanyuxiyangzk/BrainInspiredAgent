# 可运行产品闭环缺口评审

日期：2026-08-19  
结论：有条件通过，按 Q01～Q08 实施后重新做发布验收

## 1. 评审证据

现场启动 `brainagent` 与 `hello_research` 插件成功：SQLite/Outbox liveness 和 readiness 均为 HEALTHY，22 项迁移完整，目录中可见 1 个 Capability、2 个 Skill 和 1 个 Workflow。`bia market summary` 返回 `PUBLISHED`，但随后 Task、WorkflowRun、Episode 和 LocalNotification 仍为 0。

代码检查确认 CLI 的 `SQLiteEventSink` 只把 `command.received` 写入 Outbox；`MarketSummaryApp`、`DailyReviewApp`、Workflow Runtime、RiskGate、MotorExec、Outcome、Insight Query 和 Delivery 已存在并有组件/E2E 测试，但 `RuntimeBuilder` 只运行插件显式贡献的 `ManagedService`。当前没有 QuantDomainPlugin，也没有服务消费该 Outbox 并连接上述应用服务。

另一个现场问题是数据库父目录不存在时 SQLite 直接报 `unable to open database file`，错误未被 CLI 转成稳定结构化响应。

## 2. 架构判定

底座内核的执行、治理、恢复和观测能力不需要重写。缺口位于量化应用 Composition Root 和进程生命周期，属于产品化连接问题。但它是发布阻断级缺口：测试中手工装配成功不能证明用户提交的命令会被正在运行的系统处理。

必须保持以下边界：

```text
CLI / Scheduler
  → durable Outbox
  → Relay + Inbox dedup
  → CognitiveCycle
  → Planner / Validator / RiskGate / Grant
  → MotorExec / Workflow / fixed SkillBinding
  → Outcome / Evidence / Trace
  → MarketInsight projection
  → LocalNotification delivery
```

禁止让 CLI 直接调用 `MarketSummaryApp.execute()`；这会把命令提交和长任务执行绑在一个短进程中，破坏背压、确认前崩溃恢复、异步状态和统一监管。禁止在 Kernel 或 Platform 硬编码 `market.summary`，量化注册和事件映射必须留在 `apps.quant_agent` 插件中。

## 3. 任务与优先级评审

Q01～Q03 是 P0：没有它们就没有真实的命令执行闭环。Q06、Q07 是 P0：没有可查询结果、稳定终态和可运维入口，用户仍无法判断系统是否产生价值。Q04 是 P1，可在手工市场摘要闭环后接入；Q05 工作量小但应在黑盒验收前完成。Q08 是发布门，不可被现有 T04/T05 的进程内 E2E 替代。

Q02 是风险最高的任务。Outbox 的“发布确认”不能早于 Inbox 业务提交；重复消息必须沿用原 dedup/correlation，不能生成新幂等键。Q03 次高风险，必须冻结 Active DNA、Workflow digest 和 SkillBinding 后才签 Grant，运行中不可重新解析最新版本。Q06 必须从权威 Outcome/Trace 重建 Insight，不能把终端输出当事实源。

## 4. 范围控制

Q01～Q08 只形成 Fake Skills 的开箱即用闭环。真实行情、LLM Provider、邮件/Webhook、认证、多租户、Web UI 和真实交易不纳入本轮。真实 Adapter 应在闭环稳定后以 Domain SDK 插件加入，并继续经过相同 Capability、RiskGate、Grant 和恢复边界。

## 5. 验收意见

现有 T06 证明其测试负载下的 24 小时稳定性，没有覆盖“外部 CLI 子进程写入 → 常驻量化 Runtime 消费 → 用户查询和交付”的连接点。因此 T06 报告仍有效，但不能替代 Q08；产品对外表述应为“底座 MVP 已通过稳定性验收，开箱即用量化闭环待 Q01～Q08 完成”。

评审通过条件：任务依赖和完成定义按开发计划执行；Q08 必须使用独立子进程、同一个持久数据库、至少一次强杀恢复，并验证零丢失、零重复副作用和完整 correlation Trace。
