# ADR-0004：基于能力契约的可进化 Workflow 与 Skill 绑定

状态：Accepted
日期：2026-08-16

## 背景

项目希望通过自动修改 Workflow JSON 改进任务编排，并允许每个节点随时脱离当前 Skill、绑定到兼容 Skill。若节点直接引用具体 Tool/Skill，实现替换会污染 Workflow；若允许系统原地修改 ACTIVE JSON，则运行不可复现且难以回滚。

## 决定候选

1. Workflow 节点引用版本化 capability contract，不直接引用实现类。
2. Skill 通过 Manifest 声明其提供的 capability、Schema、副作用、权限和运行约束。
3. SkillResolver 以确定性规则将节点解析为固定 Skill 版本，结果写入 ExecutionGrant。
4. Workflow 和 Skill 发布版本不可变；任何修改产生新版本和 digest。
5. 双向进化只产生 WorkflowCandidate/WorkflowPatch 或 BindingPolicyPatch，并经过验证、重放、影子、Canary 和晋级管线。
6. MVP 只实现 E0/E1：生成建议/候选和静态验证，不自动替换 ACTIVE 版本。
7. Workflow Run 作为协程/DAG Task 挂载于唯一 asyncio 控制事件循环，不允许 Workflow 定义永久主循环。

## 结果

收益：编排与能力实现解耦、Skill 可替换、执行可复现、演化可审计且可回滚。代价：需要维护 capability contract、Skill Manifest、Resolver、兼容性测试和版本晋级管线。

## 未选择方案

- 节点直接写 `tool_name`：实现与业务编排耦合，难以稳定替换。
- LLM 直接生成并启用 Workflow：无法保证权限、兼容性和回滚。
- 原地修改 Workflow JSON：正在执行与历史 Trace 无法复现。
- 为每个 Workflow 建独立永久循环：破坏单一控制内核和统一治理。
