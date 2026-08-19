# 技术底座独立交付验收

P01～P04 将“源码可复用”提升为“可独立装配和交付”：

| 任务 | 当前实现 | 验收证据 |
|---|---|---|
| P01 独立运行时 | `brainagent` 通用 CLI、`RuntimeBuilder`、插件加载、run/health/status/diagnose | `tests/test_platform_productization.py` |
| P02 公共 API | `domain_sdk` 导出稳定契约和 `RuntimeBuilder`；应用不需要导入量化模块 | SDK 导出和独立插件测试 |
| P03 发行包 | `distributions/kernel`、`platform`、`domain-sdk` 三个 wheel manifest | 三个 wheel 构建成功；依赖方向为 SDK → Platform → Kernel |
| P04 外部领域 | `examples/research_agent` 只依赖公开 SDK/Platform 包，独立注册 Capability/Skill/Workflow | 外部消费者 status E2E |
| P05 通用闭环 | Research Agent 运行认知、计划、RiskGate、Grant、Workflow、Skill、Outcome、Trace | 独立领域 E2E、取消和事务崩溃回滚；底座超时/恢复矩阵全量回归 |
| P06 兼容升级 | 公共 API 1.0 清单、插件 API 主版本门、Schema 向后兼容检查、SQLite 前向迁移 | 旧插件继续运行；未来主版本启动前拒绝；旧库升级后事实不丢 |
| P07 独立运维 | `PlatformOperations` 和通用 CLI 的 health/diagnose/metrics/trace/migrations | Research 事实库积压诊断、指标、迁移和完整 Trace 查询 |
| P08 发布验收 | 不安装的发行边界检查、Research 虚拟 30 天与真实 smoke、最终发布门聚合 | P08 PASS；T06 未 PASS 时发布决策保持 BLOCKED |

独立性边界：Kernel/Platform/SDK 不得依赖 `apps.quant_agent`。量化应用属于上层领域产品。

构建发行包：

```bash
python -m build --wheel distributions/kernel
python -m build --wheel distributions/platform
python -m build --wheel distributions/domain-sdk
```

T06 真实 24 小时 soak 仍是 MVP 发布门槛，与 P01～P04 的开发和测试相互独立。

P05 同时新增 `GovernedCognitiveApp`，把此前量化应用内的闭环装配提升为领域无关平台能力；
`DomainSkillBridge` 将 SDK 的最小 Skill 接口接入平台的受治理调用协议。

## P06 兼容规则

- 公共 API、插件 API 和 Schema 使用主版本表达破坏性边界；当前支持 `1.x`。
- 同一主版本只允许向后兼容扩展，例如新增可选字段；新增必填字段、收窄 enum、改变类型或禁止此前允许的额外字段会被拒绝。
- 插件在 Catalog 构建阶段校验 `sdk_api_version`，不兼容插件不会进入 Runtime。
- SQLite 迁移只允许前向、顺序、checksum 不变；旧库升级保留既有事实，未知版本或漂移 checksum 阻止启动。
- `public_api_manifest()` 是机器可读的 API 版本和稳定符号清单。

## P07 领域无关运维

`PlatformOperations` 只读取平台事实，不导入任何领域应用。通用 `brainagent` CLI 可直接对任意领域数据库执行健康、诊断、指标、迁移和 correlation Trace 查询。100 条 Research Outbox 积压会稳定产生 `DEGRADED` readiness 和明确原因，证明告警语义不依赖量化字段。
