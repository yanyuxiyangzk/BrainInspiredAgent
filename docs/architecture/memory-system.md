# 记忆系统设计

状态：Accepted

## 1. 目标

记忆系统为决策提供有限、相关、可追溯的上下文，而不是无差别保存并塞回所有历史。任何长期结论都必须能够回到来源 Episode。

## 2. 记忆类型

| 类型 | 内容 | MVP 存储 | 生命周期 |
|---|---|---|---|
| Working | 当前事件、计划和运行状态 | 有界内存 | 分钟至小时 |
| Episodic | 感知—决策—执行的具体经历 | SQLite | 按保留策略 |
| Semantic | 经提炼与验证的知识 | MVP 后期可仍用 SQLite；以后接向量库 | 带有效期 |
| Procedural | Workflow、工具说明及成功经验 | 版本库 + 数据库索引 | 随版本管理 |
| Policy | 权限、风险与禁止事项 | 只读配置/策略库 | 人工审批变更 |

## 3. 写入流程

```text
raw event/trace
  → redact
  → persist episode
  → cluster/summarize
  → candidate insight
  → evidence validation
  → semantic memory
```

任务完成不等于结论正确。未经验证的摘要只能标记为候选经验，不能作为确定事实使用。

## 4. 检索流程

Prefrontal 提交结构化查询，包括当前目标、事件类别、时间范围、市场阶段和最大 Token 预算。MemoryService 组合：

1. 精确过滤；
2. 最近事件；
3. 语义相似性；
4. 置信度与新鲜度重排；
5. 去重和预算裁剪。

返回条目必须携带 memory ID、来源、置信度、有效期和摘要，禁止只返回脱离来源的文本块。

## 5. Semantic Memory 字段

- `memory_id`
- `statement`
- `evidence_episode_ids`
- `scope`
- `conditions`
- `confidence`
- `validation_method`
- `data_version`
- `created_at`
- `valid_until`
- `contradicted_by`
- `status`: candidate/validated/expired/rejected

## 6. 压缩与遗忘

- Working Memory 按容量、时间和重要度淘汰。
- 高频相似 Episode 合并摘要，但原始审计链按保留策略保存。
- 低价值原始大对象移至低成本存储或删除。
- 过期语义记忆不参与默认检索。
- 新证据与旧结论冲突时记录矛盾，不直接静默覆盖。

## 7. MVP 边界

MVP 先使用 SQLite 完成 Working/Episodic 和结构化摘要，不立即引入向量数据库。只有当语义检索场景和评估集明确后再选择 Chroma、pgvector 或 Milvus。
