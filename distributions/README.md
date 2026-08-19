# Independent distributions

The monorepo development package remains available for compatibility. Release artifacts are split
along the enforced dependency direction:

```text
brainagent-domain-sdk -> brainagent-platform -> brainagent-kernel
```

Build each directory with `python -m build distributions/<name>`. Application distributions, such
as `brainagent-quant`, depend on these artifacts and are not dependencies of any foundation layer.
