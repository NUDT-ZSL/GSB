# 校园对象存储 · 负查询加速内核（Negative Query Gateway）

仅使用 Python 3 标准库实现的计数布隆过滤器前置拦截内核：负查询在网关层直接
拦截，正/疑似查询再放行到后端分片索引核对；支持删除残留消解、容量超限换代重建
与原子切换，并对三个历史 schema 版本做统一归一与逐查询核对。

无第三方依赖、无网络、无真实对象存储；样本由固定种子的确定性生成器产出。

## 目录结构

```text
negative-query-gateway/
├── nqg-demo              # 可执行入口（demo/verify/trace/query/shell）
├── nqg/
│   ├── schemas.py        # 三版本字段/种子/键编码归一与跨版本差异表
│   ├── hashing.py        # 规范哈希口径：BLAKE2b(seed||key) mod m
│   ├── bloom.py          # 计数布隆内核（逐位计数、置位/删除/成员判定）
│   ├── reference_bloom.py# 独立参考位数组（不复用主实现位计算路径）
│   ├── generator.py      # 固定种子确定性事件生成器
│   ├── engine.py         # 网关引擎：拦截/放行/墓碑/换代/后端索引
│   ├── statistics.py     # 每代、两课题组、残留与复算统计
│   ├── demo.py           # 三版本编排与跨版本一致性
│   └── report.py         # 文本报告与 JSON/CSV 产物
└── reports/              # 运行后生成的机器可读产物
```

## 运行

```bash
cd negative-query-gateway

./nqg-demo demo            # 完整文本报告（报告 + 整数退出码）
./nqg-demo demo --full-trace
./nqg-demo verify          # 八项验收，全部通过退出码为 0
./nqg-demo trace --key obj-0014 --limit 6
./nqg-demo query unknown/never-written-00003.bin
./nqg-demo shell           # 交互式 get/put/del/stats
```

## 统一口径

- 键：三版本均归一为 `bytes`；v1 为原始字节，v2/v3 为十六进制串，解码并
  重新编码后必须与原字节一致。
- 种子：v1 整数列表、v2 十进制字符串、v3 `0x` 十六进制字符串，归一为同一
  整数向量 `(17, 29, 41, 53)`。
- 位位置：`index = int(BLAKE2b(seed.to_bytes(4) || key)) mod 位阵列规模`，
  同一键在三版本上映射到完全相同的位位置。

## 删除与换代

- 删除同时做两件事：计数布隆逐位计数递减，并写入持久代际墓碑。墓碑跨代生效，
  保证被删键即使位仍被其他键置位，也不会再被判定为存在；重新插入才解除。
- 当代插入量达到声明上限时，以后端存活键集重建后继代（重建只置位、不计入本代
  插入数），单次指针赋值完成原子切换；旧代记录停用时刻后不再参与判定。

## 独立参考实现

`reference_bloom.ReferenceCountingBitArray` 不导入 `hashing`/`bloom`，自行调用
`hashlib` 重算位位置，并用整数集合维护置位状态。每条查询都会核对原始位数组裁决
与置位计数；发现首个不一致即报告查询序号、逻辑时刻、键、位位置与参考位状态，
并以退出码 2 终止。

## 退出码

| 码 | 含义 |
|----|------|
| 0 | 全部裁决与核对通过 |
| 2 | 独立参考实现与主实现不一致 |
| 3 | 三版本归一后裁决/统计不一致 |
| 4 | 统计复算、删除残留或换代容量不达标 |
| 5 | schema 归一或键编码往返失败 |

## 验收要求对照

1. 三版本归一与位位置一致 —— `verify` 检查 1–3，`schemas.py`
2. 逐代占用率/插入数/置位计数 —— 报告第 2 节、`generation_stats.csv`
3. 逐查询拦截/放行/后端核对/误判 —— 报告第 9 节、`per_query_verdicts.csv`
4. 删除残留与墓碑、残留计数清单 —— 报告第 4 节、`verify` 检查 6
5. 超限重建与原子切换 —— 报告第 3 节、`verify` 检查 7–8
6. 每代误判率/拦截率与两课题组汇总 —— 报告第 2、5 节
7. 三版本结果一致与差异清单 —— 报告第 6 节、`verify` 检查 3
8. 独立参考实现逐查询核对 —— 报告第 7 节、`verify` 检查 4
