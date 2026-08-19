# 通用平台与领域应用分层规范

状态：Accepted  
版本：1.0

## 1. 目标

BIA 当前用于量化研究，但底层必须支持任意主动式 Agent 项目。平台不认识股票、交易日、因子、回测或 Qlib；量化应用只能通过公开扩展点使用底层。

## 2. 代码分层

```text
packages/
  brain_kernel/              # 类脑协议与生命周期，零领域依赖
  active_agent_platform/     # Loop/Workflow/Skill/Event/Memory 的通用实现
  domain_sdk/                # 应用扩展接口、测试工具和插件装载规范
apps/
  quant_agent/               # 当前量化应用
    workflows/
    skills/
    capabilities/
    policies/
    schemas/
    scenarios/
```

MVP 可以先采用一个 Python distribution，但源码边界必须保持上述包结构，未来可以无业务重写地拆包发布。

## 3. 各层职责

| 层 | 可以包含 | 禁止包含 |
|---|---|---|
| `brain_kernel` | Area/Message/Memory/Planner/Executor 端口、生命周期、通用状态机 | JSON 业务结构、数据库实现、行情、模型供应商 |
| `active_agent_platform` | LoopEngine、EventBus、WorkflowRuntime、SkillResolver、Grant、Trace、SQLite adapter | 量化字段、交易时间、因子阈值、Qlib/easy-tdx |
| `domain_sdk` | Plugin/Capability/Skill/Workflow 注册 API、contract test kit | 任一具体领域业务规则 |
| `quant_agent` | 量化 Workflow、市场日历、数据 Skill、回测、因子 Policy 和报告 | 修改底层私有状态、绕过 Grant/Resolver |

## 4. 稳定扩展点

上层只通过以下公开端口接入：

- `AreaPlugin`：注册领域感知或评价服务；
- `CapabilityContract`：声明领域能力的输入输出和副作用；
- `SkillManifest/SkillAdapter`：实现领域能力；
- `WorkflowSpec`：用 JSON 组合能力；
- `LoopProfile`：声明领域迭代状态、触发、checkpoint 和终止条件；
- `PolicyProvider`：提供版本化领域规则，不获得系统权限；
- `SchemaProvider`：注册领域事件和数据 Schema；
- `OutcomeEvaluatorPlugin`：追加领域评价，不能覆盖平台执行事实；
- `HookSubscriber`：订阅已登记事件，不能隐藏控制流。

插件由 Composition Root 在启动时装配。平台不得用 if/else 判断 `quant`、`finance` 等领域名称。

## 5. 依赖与数据规则

- Kernel 不依赖 Platform、SDK 或 App；Platform 只依赖 Kernel；App 只依赖公开 SDK/Platform API。
- 跨层对象使用版本化 DTO/Schema，不传递 ORM 实体、数据库连接或内部容器。
- 应用不能直接访问平台 SQLite 表；使用 Repository/Query Port。
- 应用 Skill 不能直接发布内部事件；结果由 Runtime 事务提交后写 Outbox。
- 通用错误码位于平台；领域错误码使用命名空间并映射统一 Error。
- 配置分为 platform 和 application 两棵树，应用不能覆盖平台安全上限。

## 6. 可移植性验收

必须建立一个非量化 `hello_research` 示例插件，证明：不修改 Kernel/Platform 即可注册一个 Capability、两个 Skill、一个 Workflow、一个 Loop Profile 和一个 OutcomeEvaluator。CI 增加依赖边界测试：底层源码出现 `quant/market/stock/factor/backtest/qlib/tdx` import 或领域常量即失败。

## 7. 发布与版本

- Kernel、Platform、Domain SDK 和 Quant App 分别维护语义版本；
- Platform 公共端口不兼容变化升级主版本并提供迁移说明；
- Quant Workflow/Skill 可独立发版，不推动 Kernel 发版；
- Active 插件版本固定 digest，运行中 Task 不随应用升级漂移；
- 领域应用故障可禁用插件，不能导致平台诊断与恢复能力不可用。

## 8. 与当前量化项目的关系

量化是首个、也是最完整的参考应用，用于验证平台能力，不是平台内核的一部分。`FactorDiscoveryLoop`、交易日历、行情和回测都属于 `apps/quant_agent`；LoopEngine、WorkflowRuntime、checkpoint、FSA 所依赖的通用 Loop Profile 机制属于 Platform。FSA 的表达式树统计规则属于量化应用，通用失败模式与多样性接口属于 Platform。
