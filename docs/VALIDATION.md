# 实际验证记录

验证日期：2026-11-20  
环境：Python 3.11.15、NumPy 2.4.6、pytest 9.2.0  
样本种子：`20261120`  
当前快照：版本 `2`，校验和 `sha256:89fd632f26749fcea73e5fe9e47d17cce3c368fbc8c771b449358fe53ff11880`

## 自动化测试

```text
$ .venv/bin/python -m pytest -q
..................................                                       [100%]
```

共 34 项测试通过，覆盖：

- 未知/截断版本头，竖线字段数，必填字段，空 pattern，重复 query_id
- IR 字节确定性、节点源行追溯
- 权重/深度/槽位类型配置错误
- 码点偏移、条件/引用展开、确定性排序
- v1->v2、v2->v1、v2->v3、v3->v1 丢弃策略
- 快照整体替换、回退、校验和损坏、槽位类型退休
- 三方言等价、v3 高级语义漂移
- 验证器对候选、分数和补全追溯的篡改检测
- 槽位能力差异触发 `E_SEMANTIC_DRIFT` 的槽位类别
- 样本生成确定性、CLI JSON、交互方言切换、Windows 入口文件

## 核心三方言结果

`python -m qrw demo --output artifacts/demo_report.json`：

```text
ok: true
snapshot.version: 2
core equivalence: EQUIVALENT
candidate sets equal: true
candidate order equal: true
offset map equal: true
validation: true
v1/v2/v3 IR deterministic: true
```

样本查询包括普通推荐、预订、意图槽位和区域槽位。三方言在所有 5 条查询上候选集合、顺序、分数明细和偏移映射一致。

## 迁移结果

- `v1->v2`：22 个补全项；候选等价 `true`
- `v2->v1`：16 个显式丢弃项；候选等价 `true`
- `v2->v3`：8 个升级补全项；候选等价 `true`
- 高级 v3->v1：条件分支规则、引用规则和 fragment 均显式丢弃

pattern/replacement 文本在迁移测试中逐字符保持。

## 注入异常

| 注入 | 结果 | 定位 |
| --- | --- | --- |
| 非法版本头 | `E_VERSION_UNSUPPORTED` | line 1, field `header` |
| 竖线字段数不符 | `E_FORMAT_CORRUPT` | line 2, field `segment[3]`, actual_count=2 |
| 空 pattern | `E_PATTERN_EMPTY` | line 2, field `pattern` |
| 重复 query_id | `E_DUP_QUERY` | line 2, field `query_id`, first_line=1 |
| 槽位类型越界 | `E_CONFIG_INVALID` | field `rules.bad_geo.slots.city.type`, reason=`slot_type_out_of_bounds` |
| 展开深度超限 | `E_CONFIG_INVALID` | field `rules.deep_root.max_depth`, required_depth=2, max_depth=1 |
| 校验和不符 | `E_SNAPSHOT_CORRUPT` | field `checksum` |
| 快照回退 | `E_SNAPSHOT_ROLLBACK` | current_version=2, attempted_version=1 |
| 槽位类型退休 | `E_SLOT_TYPE_RETIRED` | rule `R_area`, slot `area`, type `AREA` |

## 语义漂移演示

`samples/advanced/v3.json` 使用：

- `property=酒店/民宿` 条件分支
- `brand_suffix` 嵌套引用

对应 v1/v2 只能写成固定 replacement。裁决结果：

```text
status: DRIFT
code: E_SEMANTIC_DRIFT
unrecoverable_categories: ["条件分支", "引用"]
first_difference.query_id: q003
first_difference.field: candidate
```

这验证了“可还原时给出等价结论，v1 表达能力不足时返回漂移类别”的裁决路径。

## 产物

- 完整 JSON：`artifacts/demo_report.json`
- 独立验证摘要：`artifacts/validation_report.json`
- 环境记录：`artifacts/environment_report.json`
- 固定样本：`samples/`
- 技术设计：`docs/DESIGN.md`

