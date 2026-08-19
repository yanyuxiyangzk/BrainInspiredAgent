# Insight 查询与用户交付规范

状态：Accepted for MVP 0.1  
目标版本：MVP 0.1

## 1. 分层边界

Insight 是 `apps.quant_agent` 的产品 Read Model，不进入 Kernel 或 Platform。平台提供 Outcome、Evidence、Artifact、Trace、Repository 和可靠投递；应用层解释这些事实对量化研究者的意义。查询接口只读投影，用户命令必须经 Command Adapter、Planner、RiskGate、Resolver 和 Grant，二者不得共用绕过治理的写入口。

## 2. MarketInsight 1.0

每条洞察至少包含：`insight_id`、`type`、`title`、`summary`、`as_of`、`freshness`、`confidence`、`evidence[]`、`risk_notes[]`、`symbols[]`、`workflow_version`、`skill_bindings[]`、`data_digest`、`correlation_id`、`created_at` 和 `read_state`。Insight 正文是可重建投影；Outcome、Trace 和 Artifact 仍是权威事实。

`freshness` 只允许 `FRESH/STALE/UNKNOWN`，`read_state` 只允许 `UNREAD/READ`。任何展示必须明确数据时间和风险，不把模型文本包装成确定事实，不在 MVP 输出自动交易指令。

## 3. 查询服务

应用服务公开稳定的只读端口：

```python
class InsightQueryService(Protocol):
    async def latest(self, query: InsightQuery) -> tuple[MarketInsight, ...]: ...
    async def get(self, insight_id: str) -> MarketInsight | None: ...
    async def explain(self, insight_id: str) -> InsightExplanation | None: ...
```

`InsightQuery` 支持 type、symbol、from/to、freshness、limit 和 cursor；默认稳定排序为 `as_of DESC, insight_id ASC`，limit 最大 100。`explain` 返回证据、风险、数据/Workflow/Skill 版本和 correlation 链，不重新运行 Skill。读取采用 snapshot/version，分页 cursor 不接受任意 SQL、表达式或文件路径。

预留的认证 HTTP API 属于 P1，若实现则与 CLI 复用同一应用服务和 JSON Schema；MVP 不开放网络监听。

## 4. CLI

平台命令：`bia start/status/stop/health`。量化应用命令：

```text
bia market summary
bia insights latest [--type TYPE] [--symbol SYMBOL] [--format json|markdown]
bia insights show INSIGHT_ID [--format json|markdown]
bia insights explain INSIGHT_ID [--format json|markdown]
bia subscriptions add SUBSCRIPTION_ID [--minimum-level LEVEL] [--hourly-limit N]
bia subscriptions list SUBSCRIPTION_ID
bia subscriptions read DELIVERY_ID
```

成功输出 stdout，诊断输出 stderr；退出码稳定且机器可读。JSON 是兼容性接口，Markdown 是展示视图。`market summary` 是命令，必须走治理链；其余 insights 操作为只读查询。本地无常驻消费者时，`market summary` 返回 `PUBLISHED` 和 message ID，表示命令已可靠进入 Outbox，而不是伪装成摘要已经生成。

## 5. 交付渠道

MVP 支持终端和 LocalNotification。通知以 `(subscription_id, insight_id, channel)` 为稳定幂等键，支持主题订阅、最低严重度、静默时间、每小时上限和已读状态。投递失败由 Outbox 重试，不重新生成 Insight；重启不得重复已确认通知。Webhook、邮件和即时消息仅作为后续 Delivery Adapter，不改变 Insight 契约。

## 6. 验收

- 同一 Outcome 重投只生成一个 Insight；同一订阅只投递一次。
- latest 分页稳定，show/explain 不产生 Skill 调用或业务副作用。
- JSON/Markdown 表达同一 insight ID、时间、证据、风险和版本。
- 陈旧或未知 freshness 醒目标记；敏感字段按统一脱敏规则处理。
- 用户可以从 Insight correlation ID 追溯至触发事件、计划、Grant、Task、节点和结果。
