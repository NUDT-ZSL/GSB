# 查询改写模板编译与等价验证工具

本项目从零实现一条面向搜索/推荐前链路的本地确定性管线：读取 v1/v2/v3 三种改写模板方言，编译为统一 IR，执行条件展开、候选打分、跨方言等价裁决，并通过版本化快照管理模板集演进。

全程不访问外部服务。运行时只读取本地 `samples/`，输出可审计 JSON 报告。

## 环境

实际验证环境：

- Python `3.11.15`
- NumPy `2.4.6`
- pytest `9.2.0`
- 项目虚拟环境：`.venv/`

重新准备环境：

```bash
uv python install 3.11
uv venv --python 3.11 .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m pip install -e .
python scripts/generate_samples.py
```

Windows:

```bat
py -3.11 -m venv .venv
.venv\Scripts\activate
python -m pip install numpy pytest
python -m pip install -e .
python scripts\generate_samples.py
run_demo.cmd
```

## 快速运行

POSIX:

```bash
./run_demo.sh
python -m qrw validate
python -m qrw interactive
```

Windows 命令入口：

```bat
qrw.cmd demo --output artifacts\demo_report.json
qrw.cmd validate
qrw.cmd interactive
```

`qrw.cmd` 与 PowerShell 脚本均使用脚本自身目录定位 `.venv` 和 `src`，不依赖当前工作目录。

## 数据与方言

固定种子脚本 `scripts/generate_samples.py` 生成：

- `samples/queries.jsonl`：5 条查询，字段为 `query_id/query_text/locale/template_set_version`
- `samples/v1.txt`：无字段名、三竖线分列，规则行格式 `pattern|replacement|order`
- `samples/v2.txt`：键值行，含 `slots/weight/enabled/max_depth/order`
- `samples/v3.json`：结构化规则组，支持槽位类型、条件分支、嵌套引用和展开优先级
- `samples/slot_types.json`：受控槽位类型与值集合
- `samples/versions.json`：严格递增的快照版本表
- `samples/advanced/`：含条件分支与引用的 v3 高级样例，用于语义漂移演示
- `samples/anomalies/`：非法版本头、竖线字段数错误、空 pattern、重复 query_id、槽位类型越界、展开深度超限

v1 示例：

```text
#V1
{city} {property} 推荐|{city} {property} 榜单|10
```

v2 示例：

```text
#V2
id="R_rank"; pattern="{city} {property} 推荐"; replacement="{city} {property} 榜单"; slots="city:GEO,property:PROPERTY"; weight=1.0; enabled=true; max_depth=1; order=10
```

v3 示例：

```json
{
  "version": "V3",
  "rules": [
    {
      "id": "R_rank",
      "pattern": "{city} {property} 推荐",
      "slots": [{"name": "city", "type": "GEO"}],
      "replacement": [{"text": "{city} {property} 榜单"}]
    }
  ]
}
```

## 命令

```bash
python -m qrw demo --output artifacts/demo_report.json
python -m qrw validate
python -m qrw migrate --from v1 --to v2 --input samples/v1.txt --output artifacts/migrated_v2.txt
python -m qrw migrate --from v2 --to v1 --input samples/v2.txt
python -m qrw migrate --from v2 --to v3 --input samples/v2.txt
python -m qrw interactive
```

`demo` 写完整报告，并在标准输出打印便于查看的 JSON 摘要。报告中包含原文、完整 IR、每个查询的三方言候选、打分明细、偏移映射、迁移补全/丢弃项、等价裁决、快照操作与异常注入结果。

## 交互命令

```text
queries
snapshot
view v1 | v2 | v3
ir [v1|v2|v3] [rule_id]
candidates <query_id> [v1|v2|v3|all]
equivalence
advanced
migrate v1->v2 | v2->v1 | v2->v3
anomalies
demo
errors
quit
```

交互提示符持续显示当前模板版本、当前方言视图和最近业务错误码。

## 错误码

| 错误码 | 含义 |
| --- | --- |
| `E_VERSION_UNSUPPORTED` | 未知版本头或方言 |
| `E_FORMAT_CORRUPT` | 头截断、字段数/必填字段/JSON 结构错误 |
| `E_DUP_QUERY` | JSONL 中 query_id 重复 |
| `E_PATTERN_EMPTY` | pattern 为空 |
| `E_CONFIG_INVALID` | 权重非正、深度越界、槽位类型越界等配置错误 |
| `E_SEMANTIC_DRIFT` | v1 无法还原条件/引用/槽位语义或候选不一致 |
| `E_SNAPSHOT_ROLLBACK` | 尝试用旧版本覆盖新版本 |
| `E_SNAPSHOT_CORRUPT` | 快照清单损坏或内容校验和不符 |
| `E_SLOT_TYPE_RETIRED` | 新快照删除已编译规则仍使用的槽位类型 |
| `E_INCONSISTENT` | 独立验证器发现候选、顺序、偏移、分数或补全追溯不一致 |

## 验证

```bash
python -m pytest -q
python -m qrw demo --output artifacts/demo_report.json
python -m qrw validate > artifacts/validation_report.json
```

当前结果：34 项测试通过；核心 v1/v2/v3 候选集合与顺序一致；高级 v3 条件/引用样例按预期返回 `E_SEMANTIC_DRIFT`；四类指定异常和快照异常均返回预期错误码。

详细设计与实际验证记录见 `docs/DESIGN.md` 和 `docs/VALIDATION.md`。

