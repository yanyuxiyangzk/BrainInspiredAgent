# Markdown 渲染能力测试

状态：维护测试页

本页用于验证在线文档浏览器的 Markdown 兼容能力。

## 基础文本

普通文本、**粗体**、*斜体*、~~删除线~~、`inline_code` 和 [内部链接](../architecture/system-architecture.md)。

自动链接：https://example.com

---

## 列表与任务

- 无序列表
  - 二级列表
  - 另一项
- 最后一项

1. 有序列表
2. 第二项

- [x] 已完成任务
- [ ] 未完成任务

## 表格

| 能力 | 状态 | 说明 |
|---|:---:|---|
| GFM 表格 | 支持 | 左中右对齐 |
| 横向滚动 | 支持 | 小屏不挤压内容 |

## 引用和提示框

> 普通 Markdown 引用块。

> [!NOTE]
> 这是 GitHub 风格 Note 提示。

> [!TIP]
> 这是 Tip 提示。

> [!WARNING]
> 这是 Warning 提示。

## 代码块和高亮

```python
async def heartbeat(interval: float) -> None:
    while True:
        await event_bus.publish("brain.heartbeat")
        await asyncio.sleep(interval)
```

```json
{
  "workflow_id": "market_summary",
  "version": "1.0.0"
}
```

## 图片

![BIA Markdown 图片测试](../assets/markdown-test.svg)

## 数学公式

行内公式：$P(A|B) = \frac{P(B|A)P(A)}{P(B)}$。

块级公式：

$$
RPS_i = \frac{rank(r_i)}{N} \times 100
$$

## Mermaid

```mermaid
flowchart LR
    A[感知] --> B[决策]
    B --> C[执行]
    C --> D[评价]
    D --> A
```

## 脚注

Workflow Active 版本不可原地修改[^immutable]，所有修改产生新版本。

[^immutable]: 这样可以保证运行复现、审计和安全回滚。

## 原生 HTML

<details>
<summary>展开详情</summary>

这是受信任本地文档中的 HTML details 元素。

</details>

## 转义字符

\*这不是斜体\*，\# 这不是标题。
