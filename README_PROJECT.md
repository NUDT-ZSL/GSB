# sensitrace — 离线敏感信息流追踪工作台

在离线 Windows / Linux 工作站上，对多版本确定性 JSON 文档快照重建字段级敏感信息
血缘，校验编辑是否破坏脱敏约定与派生一致性，并输出可复现的 JSON 诊断报告与流转图。
仅依赖 Python 3.10+ 标准库；测试使用 pytest。

## 快速开始

```bash
# 1. 生成确定性样本（固定种子，纯本地，无网络）
python -m sensitrace.cli gen-samples --outdir samples

# 2. 导入并追踪（单文档失败不影响同批其他文档）
python -m sensitrace.cli import --workdir wd samples/sample_*.json

# 3. 查看血缘、传播等级与违规
python -m sensitrace.cli track --workdir wd

# 4. 校验一次编辑（不修改已持久化状态）
python -m sensitrace.cli edit-validate --workdir wd --document edited.json

# 5. 原子导出诊断报告与流转图
python -m sensitrace.cli export --workdir wd --outdir report
```

## 退出码

| 码 | 含义 |
| --- | --- |
| 0 | 成功且无业务诊断 |
| 1 | 部分文档失败或存在规则违规（其余照常处理） |
| 2 | 命令行参数错误 |

## 业务错误码

| 码 | 触发条件 |
| --- | --- |
| `E_FIELD` | 非法寻址、数组越界、字段/端点不存在 |
| `E_VERSION` | 派生声明版本无法归入 v1/v2 |
| `E_MASK_BYPASS` | 敏感源的直接下游被赋予明文标记 |
| `E_DANGLING_DERIVE` | 派生引用指向已删除字段 |
| `E_LOCK` | 工作目录检查点锁被其他进程持有 |
| `E_CORRUPT_CHECKPOINT` | 检查点损坏或结构/规则版本不一致，触发整体重建 |

## 运行测试

```bash
pytest
```

设计细节与语义决策见 `docs/DESIGN.md`。
