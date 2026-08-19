# P08 独立发布验收

状态：P08 验收通过；T06 真实 24 小时报告通过并已人工签署，MVP 0.1 可发布。

P08 不执行 `pip install`，不修改当前 Python 环境。验收脚本只检查发行配置、源码边界，并在独立 `PYTHONPATH` 中运行 Research Agent。

## 验收报告

报告：`reports/release/p08-release.json`

| 项目 | 结果 |
|---|---:|
| Kernel/Platform/Domain SDK 包边界 | 通过 |
| 真实安装 | 未执行（按要求） |
| Research 虚拟 checkpoint | 30 |
| Research 真实 smoke checkpoint | 10 |
| readiness failures | 0 |
| PURE Skill 重放一致次数 | 30 |
| 错误 | 0 |
| P08 状态 | `PASSED` |
| MVP 发布决策 | `RELEASABLE`，T06 为 `PASSED` 且已人工签署 |

执行：

```bash
python scripts/run_p08_validation.py \
  --database reports/release/p08-research.db \
  --output reports/release/p08-release.json \
  --t06-report reports/release/t06-real-24h.json
```

## 发布门

P08 只证明独立发行边界和跨领域稳定性。最终 MVP 发布要求 P08 `PASSED`，且 T06 真实 24 小时报告为 `PASSED`、无错误、无 readiness failure，并完成人工签署。上述条件现已满足，MVP 0.1 的发布决策为 `RELEASABLE`。
