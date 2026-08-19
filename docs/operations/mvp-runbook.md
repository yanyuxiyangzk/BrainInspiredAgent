# MVP 运维 Runbook

状态：Accepted

适用版本：MVP 0.1，本地单进程、SQLite、Fake Skills/LocalNotification。本文不授权直接修改数据库事实，也不授权绕过 RiskGate 重放副作用。

## 1. 值守入口和安全原则

设定数据库路径后，所有只读检查使用同一参数：

```bash
export BIA_DB=/absolute/path/to/bia.db
python -m apps.quant_agent --database "$BIA_DB" health
python -m apps.quant_agent --database "$BIA_DB" diagnose --limit 20
python -m apps.quant_agent --database "$BIA_DB" metrics
```

底座或非量化领域统一使用领域无关入口；插件参数只在启动 Runtime 时需要，以下命令直接读取通用事实库：

```bash
brainagent --database "$BIA_DB" health
brainagent --database "$BIA_DB" diagnose --limit 20
brainagent --database "$BIA_DB" metrics --prometheus
brainagent --database "$BIA_DB" migrations
brainagent --database "$BIA_DB" trace CORRELATION_ID
```

判定顺序：先保护副作用，再保存证据，然后恢复依赖，最后恢复执行。禁止删除 WAL、直接把 Task 改成成功、清空 Outbox、修改 Grant、换 SkillBinding 或以新幂等键重放原动作。`stop` 当前只返回前台运行时的停止请求语义，不管理后台 daemon；进程管理仍由启动它的终端或服务管理器负责。

每次事件记录：开始/结束时间、操作者、版本、数据库备份位置、health/diagnose 输出、correlation ID、采取的动作和验证结果。凭据、Prompt 和未裁剪行情不得进入记录。

## 2. 通用分级与恢复完成条件

| 等级 | 条件 | 首次响应 | 行为 |
|---|---|---:|---|
| Critical | SQLite 不可用、重复外部副作用、持续 Loop lag | 立即 | 停止新增有副作用工作，保存证据并升级 |
| High | Outbox 持续增长、崩溃熔断、SAFE_MODE | 15 分钟 | 保持事实库，隔离故障依赖 |
| Medium | 费用达到 80%、模型/Skill 降级 | 1 小时 | 禁止扩大预算，切确定性降级路径 |

恢复完成必须同时满足：`readiness=HEALTHY`；两次间隔五分钟的指标不继续恶化；无新增重复副作用；逾期 Task 已按恢复矩阵进入正确终态；相关 correlation 可由 `replay` 完整解释。

## 3. 队列堆积

检测：

```bash
python -m apps.quant_agent --database "$BIA_DB" metrics --prometheus
python -m apps.quant_agent --database "$BIA_DB" diagnose --limit 50
```

`outbox_pending >= 100` 时 readiness 降为 `DEGRADED`。持续增长或最老工作已过 deadline 升为 High。

处置：停止新的命令注入；确认 SQLite 健康；检查最近错误和消费者崩溃；让原 OutboxRelay 使用原 event ID 恢复投递。不得清表或手工标记 `PUBLISHED`。发布端可能已收到而确认未落库时允许安全重投，下游必须按 event ID 去重。

验证：积压连续下降；死信不继续增加；抽取 correlation 执行：

```bash
python -m apps.quant_agent --database "$BIA_DB" replay CORRELATION_ID
```

## 4. SQLite 不可用或损坏

检测：`health` 中 `dependencies.sqlite=UNHEALTHY`，CLI 返回退出码 5，或启动因 migration checksum 不一致而停止。

止损：停止新增工作和所有新副作用；保留数据库、`-wal`、`-shm` 三个文件；记录文件系统剩余空间和权限。不得删除 WAL、执行未经验证的修复 SQL，或跳过 migration checksum。

恢复：先恢复磁盘/权限；确认没有写进程后对数据库及 WAL/SHM 做一致性备份；在副本上验证启动和 `health`；migration checksum 不一致必须回到匹配的程序/迁移版本，不得改 checksum。只有副本验证通过后才能按变更流程恢复服务。

验证：`quick_check` 由 health 探针返回健康；迁移列表完整；`diagnose` 可读；Outbox 恢复且无半状态。无法在副本通过检查则保持停止并升级 Critical。

## 5. Skill 或模型不可用

检测：诊断中的失败 Node、Task error ID 和 correlation；模型请求失败或结构化输出失败率异常。

```bash
python -m apps.quant_agent --database "$BIA_DB" diagnose --limit 50
python -m apps.quant_agent --database "$BIA_DB" replay CORRELATION_ID
```

处置必须遵循固定 Binding 和恢复矩阵：`PURE` 可重放；`IDEMPOTENT` 只能使用原 key；`QUERYABLE` 先查询提供方；`NON_REPLAYABLE` 转人工复核。不得在同一次 Grant 中解析替代 Skill。市场摘要模型不可用时保持 RulePlanner/FakeSummary 确定性降级；不扩大权限和预算。

验证：原 correlation 下 Binding digest 未变化；Task 达到明确终态；QUERYABLE 有提供方证据；无新增重复副作用。

## 6. 费用超限

检测：

```bash
python -m apps.quant_agent --database "$BIA_DB" metrics
```

单日上限候选为 500 minor units；达到 80% 为 Medium，达到 100% 禁止新模型调用。处置时不提高限额；保留无模型规则和 Fake Skill 路径；检查异常重试、缓存未命中和单个 correlation 的 Token。预算变更只能走配置评审，不通过数据库更新 Grant。

验证：模型请求和费用不再增加；确定性查询、health 和已生成 Insight 仍可用。

## 7. 重复通知或其他副作用

重复外部可观察动作一律为 Critical。立即停止新的同类动作，保存 idempotency key、notification/provider operation ID、task/run/correlation，不删除 delivery 事实。

使用 `replay` 确认是否为确认前崩溃；同 key 同 payload 应返回既有结果，同 key 不同 payload 必须报 `IDEMPOTENCY_CONFLICT`。不得用新 key “再试一次”。确认影响范围、通知用户并记录事故；只有幂等存储和提供方状态核对完成后才恢复。

## 8. 脑区崩溃循环

连续崩溃达到 Supervisor 窗口阈值后为 High；关键服务失败进入 SAFE。收集结构化日志、health、diagnose 和最近 correlation。隔离非关键依赖，不反复无限重启；三次相同失败后保持熔断。SQLite 或事实一致性不明时按第 4 节处理。

验证：服务状态回到 READY；五分钟无同类崩溃；Loop lag 与队列恢复；恢复扫描完成。

## 9. SAFE_MODE 解除

SAFE_MODE 是保护状态，不是可直接清除的告警。先确认触发原因已消失：关键依赖健康、无重复副作用、预算未超限、队列稳定、恢复任务分类完成。操作者不得直接写 brain mode 或 Task 状态。

由 StateController 的受控状态转换解除 SAFE；当前 MVP 没有“强制解除”CLI。若无法通过合法转换恢复，保持 SAFE 并升级。解除后先运行只读 health/Insight 查询，再允许低风险 PURE 工作，最后恢复幂等副作用；NON_REPLAYABLE 工作需单独审批。

## 10. 演练记录与签字

| 日期 | 演练 | 自动化证据 | 结果 | 工程/QA 签字 |
|---|---|---|---|---|
| 2026-08-18 | T1～T6 commit 前崩溃 | `tests/test_t02_transaction_fault_injection.py` | PASS | automated quality gate |
| 2026-08-18 | 四类 Skill 恢复 | `tests/test_skill_recovery.py` | PASS | automated quality gate |
| 2026-08-18 | SQLite/Outbox/SAFE 健康诊断 | `tests/test_diagnostics.py` | PASS | automated quality gate |
| 2026-08-18 | 通知重启幂等 | `tests/test_fake_skills.py` | PASS | automated quality gate |
| 2026-08-18 | P07 Research 跨领域运维 | `tests/test_p07_cross_domain_operations.py` | PASS | automated quality gate |

T06 发布前，发布负责人还必须在真实 24 小时报告中签署：无 Critical/High 未关闭项、无重复副作用、备份恢复演练通过、SAFE_MODE 解除路径已现场复核。
