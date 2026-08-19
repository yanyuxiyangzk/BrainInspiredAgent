# 安全与治理

状态：Accepted

## 1. 信任边界

以下内容均视为不可信输入：用户命令、行情/新闻、网页内容、LLM 输出、Workflow 参数和第三方工具返回。来自这些来源的文本不能改变系统策略或取得额外权限。

## 2. 执行授权链

```text
CandidatePlan
  → Schema validation
  → Workflow/Capability allowlist
  → Parameter constraints
  → data freshness check
  → budget and rate limit
  → capability policy
  → RiskGate
  → PlanDecision + ExecutionGrant
```

任何环节拒绝都生成结构化原因并写入审计记录。

## 3. 能力分级

| 级别 | 能力 | MVP |
|---|---|---|
| L0 | 读取公开/模拟数据、查询状态 | 开放 |
| L1 | 分析、回测、生成报告 | 开放 |
| L2 | 发送通知、写业务数据 | 限制并审计 |
| L3 | 模拟交易 | MVP 后评估 |
| L4 | 真实资金与交易 | 禁止 |

脑区、Workflow 和 Skill Manifest 分别声明所需 capability，ExecutionGrant 取策略允许权限的交集，不允许由 LLM 临时提升。

## 4. 风险控制

- 全局、每日、每计划和每节点费用预算；
- 全局、Workflow 与 Skill 级并发、频率和超时限制；
- 外部数据新鲜度和完整性检查；
- 副作用 Skill 强制声明恢复类型和幂等键；
- 连续失败熔断；
- `SAFE_MODE` 和独立 kill switch；
- 高风险能力需人工审批，审批不可由模型模拟；
- Policy Memory 只允许通过受审查配置变更。

## 5. Prompt 注入防护

- 外部文本以数据字段传递，不拼接为系统指令；
- 系统策略、工具描述与外部内容分层；
- Skill 调用由 Capability 和结构化 Schema 约束；
- 检索内容保留来源和信任等级；
- 外部内容中声称的“忽略规则”“调用工具”不具备授权效力；
- 敏感 Skill 不可仅凭自然语言理由获得权限。

## 6. 密钥与隐私

- 密钥来自环境或专用 Secret Manager；
- 日志、Trace、事件和 Prompt 中统一脱敏；
- Capability 输入输出 Schema 标注敏感字段；
- 调试导出默认排除原始凭据和个人数据；
- 数据保留、导出和删除操作均有审计记录。

## 7. 审计要求

至少记录：谁/什么触发、采用哪些数据、模型与 Prompt 版本、候选计划、PlanDecision、ExecutionGrant、Workflow/Skill 版本与 digest、节点结果、副作用标识、费用和最终状态。

审计记录为追加式。更正通过新记录表达，不静默覆盖历史。

## 8. 量化专项约束

- 研究结论与交易指令严格分离；
- 回测必须保存数据范围、基准、成本、滑点和代码版本；
- 因子进入候选库前完成样本外验证；
- 禁止未来函数和数据泄漏；
- 报告明确标注模拟、研究用途及数据延迟。
