# Verification Record

本记录来自本地实际执行，未访问外部数据集或网络。

## Environment

- Python: `3.11.18`（由 `uv` 安装到项目 `.venv`）
- NumPy: `2.4.6`
- pytest: `9.2.0`
- OS: `Linux-6.18.33.2-microsoft-standard-WSL2-x86_64-with-glibc2.35`

## Commands

```bash
.venv/bin/python -m pytest
.venv/bin/python scripts/run_demo.py
```

测试结果：`35 passed`。

## Sample And Matrix

- 样本：`data/samples.jsonl`，24 行
- 样本 SHA-256：`21d132c0dc007289f210200255d90f527176be0306e3d9fbe20524d92492c635`
- 矩阵形状：`24 x 38`
- 编码矩阵版本：`05e9a222be9f9235aa224f6cf95b61527f3779433fe8c2a7132dd2a80fbac654`
- 保留特征（8）：`num__income`, `num__tenure`, `num__age`, `cat__region__target`, `cat__channel__web`, `cat__channel__store`, `cat__color__red`, `cat__channel__partner`

## Reproducibility

同一输入、配置、种子、全新缓存目录分别运行两次，以下文件逐字节相同：

| Artifact | SHA-256 |
| --- | --- |
| `matrix.npy` | `7b078f48ba9d98fc3562369693355370dba3ac3d7cfa648980a7ec1d7dd74bef` |
| `columns.json` | `00e6d380b83f3285cdf7eb8090c5497caae06fe5bf63b52219ae2e78912e73e8` |
| `encoding.json` | `d831ad399e4e41ce38908c1dd6d0aaaa0135b7eb8d3e1427bdf28a6a9fa56d54` |
| `standardization.json` | `1ed8a6379352efcd66136f7888b5a4edb7e2424ca94abab1e391fd0503d2bb27` |
| `cross.json` | `e6cc6e17e9a365ef9d8799110201627bdf450053bd57e9fe2311beff8c37faba` |
| `selection.json` | `f22332d42deb47c2186e691c79152c067ff7f5399ea5f8e8465108653ce98b7e` |

同一缓存目录第二次运行：`9` 个缓存原语全部命中，重算数为 `0`。

## Injection Attribution

`config/injections.json` 的四阶段错误：

| Stage | Code | Feature | Row |
| --- | --- | --- | --- |
| encode | `E_ENCODE_VALUE_RANGE` | `color` | `row-0007` |
| standardize | `E_STANDARDIZE_VALUE_INVALID` | `income` | `row-0003` |
| cross | `E_SLOT_REF_INVALID` | `region_channel` | null |
| select | `E_CONFIG_INVALID` | `selector.threshold` | null |

降级包括：跳过失败编码/标准化特征、跳过无效交叉槽、跳过依赖失败编码的交叉槽、使用可用矩阵、回退默认方差阈值。成功处理的 `cat__region__target`、`num__age`、`num__tenure` 在正常与注入运行中逐元素相同。

未归因注入探测结果为 `E_UNCLASSIFIED`，并进入默认方差选择降级。

## Incremental Invalidation

实际观测的重算与复用集合均无交集：

| 场景 | 重算阶段 | 重算特征 | 复用特征 |
| --- | --- | --- | --- |
| 单行 `age` 变更 | standardize, select | `standardize:age`, `select:_selection` | 全部编码、两个交叉槽、其它数值列 |
| `color` 编码方式变为 target | encode, select | `encode:color`, `select:_selection` | 全部交叉槽、其它编码/标准化 |
| 新增 `color_channel` 槽 | cross, select | `cross:color_channel`, `select:_selection` | 基础编码/标准化、原两个交叉槽 |
| 选择器改为 variance | select | `select:_selection` | 全部上游特征 |
| 单行 label 变更 | encode, select | `encode:region`（target）, `select:_selection` | one-hot 编码、数值标准化、全部交叉槽 |
| `config_version` 提升 | 全部 | 全部缓存原语 | 无 |

## Snapshots

- 新版本写入：`created` / `replaced_new_version`，`replaced_entirely=true`
- 同版本不同校验和：整体替换，不保留旧 payload 字段
- 低版本覆盖尝试：`E_SNAPSHOT_ROLLBACK`
- 修改 payload 后校验和失配：`E_SNAPSHOT_CORRUPT`
- 缓存对象或索引键组成被篡改：`E_CACHE_CORRUPT`，记录具体特征名并修复后恢复命中

完整机器可读证据见 `artifacts/demo/demo_report.json`，摘要见 `artifacts/demo/verification.md`。
