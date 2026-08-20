# 类脑 Agent 命令面 MVP 专家审查

日期：2026-08-19  
审查对象：`command-surface-mvp-plan.md`  
结论：有条件通过（Approve with conditions）

## 1. 总体意见

计划覆盖了文档定义但尚不可操作的 Brain、Loop、Event、Plan/Task、Memory、Catalog、Schedule、DNA、Evolution 和 Insight 能力，命令树按用户职责分层，范围没有引入真实交易或未来量化 Adapter。将 Query、Command、Governance 分开是正确的安全边界。

发布前必须满足三个前置条件：量化 Runtime 归并到唯一 LoopEngine；市场摘要和日复盘接入三层 DNA 执行身份；Shell/CLI 只调用共享应用服务。若缺少任一项，`/loop`、`/dna` 或交互命令会成为与真实执行脱节的旁路界面。

## 2. 分领域审查

### 2.1 架构审查

判定：有条件通过。

- U03 是架构纠偏，不是普通查询页；必须先消除 QuantRuntimeService 自有顶层控制循环。
- U11 必须在 U12 之前完成，否则 DNA 查询无法证明产品执行使用了所展示的 Active DNA。
- Command Service、Query Service、Governance Service 应作为应用端口，Prompt Shell 只负责解析和渲染。
- LoopProfile 不能仅作为 Catalog 元数据；需有持久实例、checkpoint、终止条件和 Supervisor 归属后才可显示为“运行中”。

### 2.2 安全与治理审查

判定：通过，但 U05/U09/U10/U13/U15 为高风险实现项。

- cancel、retry、trigger、enable、activate、promote、rollback 都是写操作，不得直接 UPDATE 表。
- `retry` 必须复用原 Binding、幂等键和 correlation；NON_REPLAYABLE 永远返回拒绝。
- DNA transition 必须要求 version、expected revision 和 reason；activate/promote/rollback 要求交互确认，非 TTY 使用显式 `--yes`。
- SAFE 模式只开放 Query 和白名单恢复动作，不增加 `force-normal`。
- 查询输出统一脱敏，不回显 Prompt、密钥、原始异常堆栈或任意 Artifact 路径。

### 2.3 产品与交互审查

判定：通过。

- 一级命令按心智模型组织，优于把底层类名全部平铺到 `/`。
- `/brain` 是面向用户的摘要；`/system` 是运维事实；二者不能输出同一堆 JSON。
- `/tasks` 应取代 `/commands` 成为执行观察主入口，但 `/commands` 保留用于外部请求收据。
- `/help COMMAND`、候选菜单描述、示例和错误后的 next action 属于 MVP DoD。
- 列表命令统一支持 `--limit`、`--cursor`、`--status`、`--since` 和 JSON 输出；交互终端可以表格化。

### 2.4 运维与可靠性审查

判定：有条件通过。

- `/loop services` 必须来自 Supervisor 快照，不能从表是否存在推断健康。
- `/events` 和 `/tasks` 默认限制结果数量，避免终端查询拖慢同一 SQLite 写进程。
- cancel/retry/trigger 在确认前崩溃与确认后响应丢失场景均需故障注入。
- Q08 的 100 命令门应扩展为混合命令门：读查询、合法写、非法写、重复写、SIGKILL 恢复。
- 发布报告须分别统计业务副作用、治理 transition 和只读查询，不能只统计 Task 总数。

## 3. 主要风险

| 等级 | 风险 | 控制 |
|---|---|---|
| Critical | Shell 直接修改 Registry/Task，绕过治理 | 禁止 Shell SQL；架构边界测试扫描应用端口 |
| High | 显示的 DNA 与真实执行 DNA 不一致 | U11 前置；每次执行持久化并校验完整 dna_context |
| High | Quant 自有 Loop 与 LoopEngine 双重生命周期 | U03 单 Loop 黑盒与孤立协程检查 |
| High | retry/trigger 产生重复副作用 | 原幂等键、固定 Binding、Inbox 去重、故障注入 |
| High | DNA activate/promote 并发覆盖 | CAS revision、唯一 Active 索引、事务 transition |
| Medium | 命令过多导致用户不可发现 | 分层帮助、实时补全、角色化示例、别名迁移 |
| Medium | 大列表查询阻塞 Runtime | cursor、limit、busy timeout、只读应用服务 |
| Medium | 内部对象泄露破坏兼容 | 稳定 DTO/Schema，不直接序列化 ORM/SQLite Row |

## 4. 必须修改/确认项

计划实施时必须补充：

1. 为每个命令族冻结 JSON 1.0 输出契约和稳定退出码。
2. 建立权限矩阵：Query、Operator、Governance；单用户 MVP 仍保留角色语义，为未来认证做边界。
3. 为 Governance 操作规定 `--reason`、`--expected-revision`、`--yes`。
4. 明确 `/memory search` MVP 仅结构化/FTS，不暗示向量检索。
5. 明确 `/schedules trigger` 只产生受控事件，不同步调用 Workflow。
6. 明确 `/evolution replay` 使用 Sandbox/Fake Adapter，禁止生产副作用。
7. 同步修正文档中已过期的测试数、命令清单和“Quant 使用 LoopEngine/DNA”的表述。

## 5. 最终判定

任务拆分、依赖和验收方向可执行，建议进入开发。U03、U11、U17 是发布阻断项；U05、U09、U13、U15 必须由 TL/安全审查；任何为了快速展示而新增的直接数据库写入口均判定为不通过。

