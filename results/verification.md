# 验收结果

执行命令：

```bash
python -m pytest -q
python scripts/run_acceptance.py --output results/verification.json
```

实际结果：

- pytest：`27 passed`
- 端到端验收：`12/12 passed`
- 强杀退出码：`91`（2 个分片已提交、指针为空）
- 清单写入中强杀退出码：`92`（1 个无引用临时清单文件）
- v1 清单：`bb3281a1e511651950d1c2f8c51e9a9e7c90000f34056b8dd5815bdea4be3304`
- v2 清单（增量与独立全量逐字节一致）：`e80899cb35457b414c85500e0779a6fccf0fbd7859ced04345b27df37d33e058`
- v2 恢复链摘要：`b0f48203233d9983e6fefd57a6feb2e904c62143241f268616649f3e6052325e`

| 检查 | 结果 |
|---|---|
| 非法输入业务码且不落盘 | 通过 |
| 4 分片并行保存 | 通过 |
| 增量命中 3、写入 1 | 通过 |
| 增量/全量清单字节一致 | 通过 |
| 拼接与逐层独立重建一致 | 通过 |
| 摘要与层校验 | 通过 |
| status 稳定排序 | 通过 |
| 强杀后恢复清单一致 | 通过 |
| 半截清单临时文件清理 | 通过 |
| 预算未启动与 resume | 通过 |
| 双进程同版本幂等提交 | 通过 |
| Flask 只读诊断 | 通过 |

环境：WSL2 Ubuntu、Python `3.10.12`、NumPy `1.26.4`、Flask `3.0.3`、pytest `8.3.3`。项目声明 Python `>=3.11`；当前容器没有 3.11，Windows 分支需在目标 Windows 机器上复跑一次。
