# T06 发布验收记录

状态：进行中（真实 24 小时 soak 尚未结束）

## 已完成

虚拟 30 天验收已由 `scripts/run_t06_validation.py` 实际执行，报告位于 `reports/release/t06-virtual-30d.json`：

| 指标 | 结果 |
|---|---:|
| 虚拟天数/checkpoint | 30 |
| readiness failures | 0 |
| 持久通知记录 | 30 |
| 重复投递尝试 | 30，全部返回 `DUPLICATE` |
| 错误 | 0 |
| 状态 | `PASSED` |

全量发布前质量门也已通过：测试、覆盖率、Ruff、strict Mypy、Compileall、文档构建和文档树检查均通过。市场摘要和 daily review 两条 E2E、T1～T6 故障注入、恢复矩阵、健康诊断和 CLI 交付测试均包含在全量门中。

## 真实 24 小时

真实 soak 已启动为独立进程，不能用虚拟时钟替代：

```bash
ps -p 319950 -o pid=,stat=,etime=,cmd=
cat reports/release/t06-real-24h.json
```

报告路径：`reports/release/t06-real-24h.json`。进程每 60 秒写入原子 checkpoint，完成后必须看到 `status=PASSED`、`finished_at` 非空、`readiness_failures=0`，并由发布负责人签署。

真实报告结束前，T06 不得标记为完成，也不得发布 MVP 0.1。若进程退出或报告进入 `FAILED`，保留数据库和报告，按 Runbook 的 SQLite/崩溃循环章节处理并重新开始完整 24 小时窗口。
