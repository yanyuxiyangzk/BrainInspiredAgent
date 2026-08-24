# 类脑 Agent 命令面 MVP 开发计划

状态：In Development  
版本：MVP 0.3 / Command Surface 1.0  
日期：2026-08-20

## 1. 目标与边界

本计划补充各任务的命令树、DoD 和验收；任务状态以 `development-plan.md` 为唯一基线。它把已经实现的类脑运行、事实、记忆、能力目录和 DNA 治理能力转化为稳定的 CLI 与交互式 Slash Command。所有入口同时提供机器可读 CLI 和人类可用交互别名，共用应用服务，不在 Shell 中直接访问 SQLite 私有表。

MVP 只开放 `market_summary`、`daily_review` 和已有 Fake Skills。`auction_monitor`、Qlib、RD-Agent、真实行情、Webhook、真实交易、自动 DNA 激活和强制解除 SAFE 不在本轮。

命令分为三类：

| 类型 | 语义 | 约束 |
|---|---|---|
| Query | 读取权威事实或带版本投影 | 不调用 Skill、不产生业务副作用 |
| Command | 取消、重试、启停、触发或状态迁移 | 经 Command Adapter、白名单、RiskGate、幂等键和审计 |
| Governance | DNA、Workflow、Skill 生命周期变化 | 显式 ID/version/revision、合法状态机、digest 重算、追加式 transition；高风险操作要求确认 |

## 2. 统一命令树

```text
/brain          state · areas · cycles
/loop           status · services · lag · checkpoints
/events         recent · show · correlation · inbox · outbox · dead-letter
/attention      recent · explain · metrics
/goals          active · show · history
/plans          recent · show · rejected · explain
/tasks          list · running · failed · show · trace · cancel · retry
/memory         working · episodes · semantic · search · candidates · consolidate
/catalog        capabilities · skills · workflows
/skills         list · show · health · bindings · enable · disable
/workflows      list · active · show · validate · runs · activate · deprecate
/schedules      list · show · history · trigger
/dna            list · active · show · lineage · explain · executions · transition
/evolution      candidates · fitness · datasets · replay · compare · campaigns · promote · rollback
/market         summary
/insights       latest · show · explain
/subscriptions  add · show · list · read · enable · disable
/system         status · health · diagnose · metrics · logs · migrations
/help           [COMMAND]
/exit
```

原有非交互命令保持兼容；`/status`、`/health`、`/trace`、`/commands`、`/subscribe` 和 `/deliveries` 在 1.x 期间作为别名保留并显示规范命令提示。

## 3. 可派工 Backlog

| ID | 状态 | 优先级 | 负责人 | 依赖 | 估算 | 交付物与完成标准 | 验收 |
|---|---|---:|---|---|---:|---|---|
| U01 | `✅ 已开发已测试` | P0 | APP/BE | Q08 | 2 | `CommandSpec` 命令描述模型、分层帮助、实时补全、别名；CLI/Shell 共用规范路由 | `/help COMMAND`、补全、未知命令、TTY/非 TTY 契约测试通过 |
| U02 | `✅ 已开发已测试` | P0 | BE/OPS | U01,I02,I03 | 3 | `/system` 与 `/brain` 查询；BrainState 未持久化部分明确标注 derived | 查询零副作用；健康、迁移、脑区负载和最近 Cycle 契约测试通过 |
| U03 | `✅ 已开发已测试` | P0 | BE/TL | U01,A03,Q07 | 4 | Quant 常驻服务由唯一 LoopEngine/Supervisor 托管；`/loop status/services/lag/checkpoints` | CLI/Shell 无手工服务生命周期；排空/恢复回归；运行态来自 Supervisor，进程外明确 UNKNOWN |
| U04 | `✅ 已开发已测试` | P0 | BE/OPS | U01,B01～B03,G01 | 3 | `/events` 查询 Outbox/Inbox/死信/详情/correlation；有界输出不回显 envelope payload | 顺序、ID、状态和 correlation 保真 |
| U05 | `✅ 已开发已测试` | P0 | APP/BE | U01,F01～F06,G01 | 4 | `/plans`、`/tasks` 查询完成；cancel/retry 经持久治理入口执行 | cancel 必须命中 RUNNING 活句柄；retry 仅接受可恢复终态并复用固定 Binding/权限和 SINGLE_TASK_MULTI_ATTEMPT Grant |
| U06 | `✅ 已开发已测试` | P1 | AI/BE | U02,E02～E05 | 3 | `/attention`、`/goals` 查询与解释；证据、Goal 条件/预算/策略上下文 | 同一事实解释确定；查询不创建认知周期；动态 Goal 修改不在 MVP |
| U07 | `✅ 已开发已测试` | P1 | BE/AI | U01,E06,G01,G04,G05 | 4 | `/memory` 查询 Working/Episode/Semantic/Candidate；search；受控 consolidate | Working 标注非权威；Candidate 经证据/矛盾/有效期校验和显式确认晋级 |
| U08 | `✅ 已开发已测试` | P0 | APP/BE | U01,A07,C01,D01 | 3 | Runtime 幂等持久化校验目录；`/catalog`、`/skills`、`/workflows` 查询 | 真实 Schema/digest/status，未暴露 Adapter 实现 |
| U09 | `✅ 已开发已测试` | P1 | BE/TL | U08,D04,C01 | 4 | Skill enable/disable、Workflow validate/activate/deprecate 治理命令 | 显式版本/revision；定义和活动 Run 检查；全部写 append-only transition/audit |
| U10 | `✅ 已开发已测试` | P1 | APP/BE | U03,B06,Q04 | 3 | `/schedules` 查询配置、checkpoint、触发历史；受控一次性 trigger | 交易日/时区可解释；trigger 使用稳定 occurrence key；重复执行为 0 |
| U11 | `✅ 已开发已测试` | P0 | BE/TL | U03,H01.1～H12 | 5 | Quant 默认 Organization/Agent/Workflow DNA 已幂等装配并接入真实执行 | Market summary 的 Plan→Outcome 持有三层 DNA context；无 Outcome 的 review 不写伪归因 |
| U12 | `✅ 已开发已测试` | P0 | APP/BE | U11 | 4 | `/dna` list/active/show/lineage/explain/executions 查询已提供 | 查询零副作用；执行归因读取 append-only context |
| U13 | `✅ 已开发已测试` | P1 | BE/TL/QA | U12 | 5 | DNA 合法 transition：validate/shadow/canary/activate/deprecate/retire | CAS/reason/yes 已接入；复用底层状态机和审计 |
| U14 | `✅ 已开发已测试` | P1 | AI/BE | U12,H06～H12 | 5 | `/evolution` 查询、Explain、Replay cases 和 Fitness Compare | 读取真实 append-only evidence，不生成伪回放结果 |
| U15 | `✅ 已开发已测试` | P1 | TL/QA | U13,U14 | 4 | promote/rollback/kill 接入 Promotion Controller | 样本/稳定性/风险门、CAS revision、reason/yes 与自动回滚生效；禁止绕门 Active |
| U16 | `✅ 已开发已测试` | P0 | APP/BE | I04,I05,U01 | 4 | cursor/stale/symbol/time/type 过滤；Subscription quiet hours/list/enable/disable | 查询零副作用；时间区间校验 |
| U17 | `✅ 已开发已测试` | P0 | QA/OPS | U02～U16 | 5 | Ruff/Mypy、507 项全量测试、95.03% 覆盖率、黑盒/故障注入及发布报告通过 | Query 0 副作用；Command 100% 经治理；强杀恢复；Critical/High=0 |

关键路径：`U01 → U03 → U02 → U11 → U12 → U13 → U17`。  
并行路径：`U04/U05/U08/U16` 可在 U01 后并行；`U06/U07/U10` 在对应查询端口完成后并行；`U14/U15` 依赖 DNA 执行归因。

## 4. 使用方式

### 4.1 研究者

```text
/market INDEX.TEST,INDEX.DEMO --title "今日市场摘要"
/tasks running
/insights latest --symbol INDEX.TEST --freshness FRESH
/insights explain INSIGHT_ID
/subscriptions add me --quiet 22:00-08:00
```

### 4.2 值守人员

```text
/system health
/brain state
/loop services
/events dead-letter
/tasks failed
/tasks trace TASK_ID
```

### 4.3 能力开发者

```text
/catalog capabilities
/skills health
/workflows validate workflow.json
/workflows show market_summary 1.0.0
```

### 4.4 DNA 治理者

```text
/dna active --kind workflow
/dna lineage quant.market_summary 1.1.0
/evolution compare PARENT CANDIDATE
/dna transition CANDIDATE_ID 1.1.0 --to SHADOW --revision 3
```

## 5. 发布门

1. Query 命令在数据库前后快照中不新增业务事实；访问日志除外。
2. 所有写命令产生 `command.received`、稳定幂等键、PlanDecision 或治理 transition 和 correlation trace。
3. Shell、JSON CLI 与 Markdown 只共享应用服务，不各自实现 SQL。
4. Quant Runtime 只有一个 LoopEngine；没有应用自建永久 `while` 控制循环。
5. 市场摘要与日复盘的三层 DNA identity 可从请求追溯至 Outcome。
6. ACTIVE DNA、Workflow、Skill 不允许原地修改；状态变更使用 CAS revision。
7. SAFE 模式无强制解除命令；NON_REPLAYABLE 无 retry 命令；真实交易无入口。
8. 真实子进程黑盒覆盖查询、写命令、权限拒绝、SIGKILL 恢复和重复提交。
