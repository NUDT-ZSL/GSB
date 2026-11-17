# 实际验证记录

验证日期：2026-11-18

## 环境

- 运行平台：本地 Ubuntu 22.04 工作区
- Python：CPython 3.11.15（项目 `.venv`）
- pytest：8.4.2
- 容器：未使用 Docker 或其他容器
- 运行期数据：全部由项目内确定性生成器产生，无网络、账号或外部数据集

## 自动化测试

```bash
.venv/bin/python -m pytest -q
```

实际结果：

```text
44 passed in 2.25s
```

覆盖内容包括：

- 点路径/数组下标路径规范化与非法路径拒绝；
- 24 种记录排列下结论全等；
- `v1` 单跳、`v2` 真值外延、`v2` 假值单跳；
- 明文 > 脱敏 > 不可见固定优先级；
- 1、2、5、12 节点环与自环；
- 版本混用冲突及冲突来源键；
- 降级、泄露及来源诊断；
- 16 层传播链真值来源；
- 重复路径、非法状态/版本、缺失来源和级联拒绝；
- 真实计时超时、字段上限；
- 并发度 1/4 的语义与报告顺序一致性；
- CLI 退出码 0/1/2；
- 原子替换失败保留旧报告；
- 校验前后描述集对象和源文件字节不变。
- 显式注入文件构造环、降级、版本混用和深链。

## 端到端验收

```bash
.venv/bin/python scripts/acceptance.py
```

关键实际输出：

```json
{
  "exit_codes": {
    "clean": 0,
    "mixed": 1,
    "concurrent": 1,
    "parameter_error": 2
  },
  "dataset_count": 9,
  "code_counts": {
    "E_FIELDS": 1,
    "E_TIMEOUT": 1,
    "E_CYCLE": 2,
    "E_VERSION_CONFLICT": 1,
    "E_DOWNGRADE": 5,
    "E_LEAK": 3
  },
  "observations": {
    "consistent": 52,
    "inconsistent": 0
  },
  "force_cycle_path": [
    "cycle[0].value",
    "cycle[3].value",
    "cycle[2].value",
    "cycle[1].value",
    "cycle[0].value"
  ],
  "concurrency_semantics_equal": true,
  "input_unchanged": true,
  "reports_parseable": true,
  "failures": []
}
```

验收产物：

- `artifacts/acceptance/samples/batch.json`
- `artifacts/acceptance/reports/clean.json`
- `artifacts/acceptance/reports/serial.json`
- `artifacts/acceptance/reports/concurrent.json`
- `artifacts/acceptance/acceptance-summary.json`

结论：顺序无关传播、环注入复现、资源限制、并发一致性和退出码均通过本地确定性样本验收。
