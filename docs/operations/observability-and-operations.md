# 可观测性与运维

状态：Accepted

## 1. 可观测性目标

维护者应能回答：系统是否健康、为何触发任务、任务卡在哪里、输出是否可信、花费多少、是否产生过副作用以及重启后如何处理。

## 2. 结构化日志

统一字段：

- timestamp、level、service/area；
- correlation_id、causation_id、msg_id；
- plan_id、task_id、workflow_id/version、node_id；
- capability/skill/model/prompt version；
- event、status、duration_ms、error_code；
- 数据分类和脱敏标记。

禁止记录密钥和未经裁剪的大模型上下文。

## 3. 指标

### 系统

- event loop lag；
- 各订阅队列长度、最老消息年龄、丢弃/合并量；
- 脑区健康、重启次数；
- 数据库延迟与错误率。

### 业务

- 显著事件率；
- 候选/批准/拒绝计划数；
- Workflow 和节点成功率、P50/P95 时延；
- 超时、重试、取消、过期数量；
- 重复副作用数量。

### AI 与成本

- 模型请求、Token、费用、缓存命中；
- 结构化输出校验失败率；
- 记忆检索采用率；
- 计划事后评价分布。

## 4. 分布式追踪

即使 MVP 是单进程，也使用 correlation/causation ID 形成链路。未来接入 OpenTelemetry 时无需改变领域协议。

## 5. 健康检查

- Liveness：主事件循环仍在推进。
- Readiness：EventBus、Memory 和必要配置可用。
- Dependency：LLM、行情、通知等依赖分别报告，不混为总体存活。
- Brain health：每个常驻脑区报告心跳和最后成功处理时间。

## 6. 告警建议

| 条件 | 等级 |
|---|---|
| event loop lag 持续超阈值 | Critical |
| 核心队列持续增长 | Critical |
| 持久化失败或重复副作用 | Critical |
| 脑区连续崩溃并熔断 | High |
| LLM 校验失败率异常 | Medium |
| 单日费用达到 80% | Medium |
| 非关键行情源短暂失败 | Low |

## 7. 启停与恢复

启动顺序：配置和数据库迁移 → Memory → EventBus → 状态控制 → 消费脑区 → Sensory/API。关闭时反向执行，停止接收新任务，等待宽限期，持久化状态，再取消剩余任务。

恢复扫描将未完成任务分类为：安全重试、状态查询后决定、过期终止、需要人工复核。

## 8. Runbook 最低清单

具体检测、止损、恢复、验证与升级步骤见 [MVP 运维 Runbook](mvp-runbook.md)，覆盖队列堆积、数据库不可用、模型/Skill 不可用、通知重复、费用超限、脑区崩溃循环和安全模式解除。
