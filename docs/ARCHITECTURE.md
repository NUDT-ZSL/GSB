# 全景分割掩码互操作管线 — 架构与格式规范

## 1. 模块总览

```
panseg/
  errors.py        业务码、整数退出码、PipelineError
  models.py        统一中间表示 InstanceRecord、PipelineConfig、失败记录
  rle.py           LEB128 varint、两档行程编码、位图/行程互转、内容指纹
  v1_package.py    v1 二进制行程包读写（PAN1 魔数）
  v2_package.py    v2 base64 逐像素 JSON 包读写（含置信度/crowd/调色板）
  converters.py    双格式解析到 IR、元数据补全、v1<->v2 双向转换与损失清单
  fingerprints.py  实例缓存键、融合依赖指纹、配置哈希
  fusion.py        跨格式融合、IoU 匹配、crowd 规则、冲突裁决、边界诊断
  scheduler.py     线程池调度、稳定排序键、单实例异常隔离
  snapshot.py      临时目录 + 原子整体替换、manifest、崩溃恢复
  pipeline.py      端到端编排、增量复用/失效、硬崩溃注入
  report.py        原子 JSON 验证报告
  cli.py           信号处理与整数退出码入口
scripts/
  generate_samples.py    固定种子确定性样本（含边界翻转与损坏包）
  consistency_matrix.py  增量/全量一致性矩阵
  run_acceptance.py      一键验收
tests/                   31 个单元/集成/OS 信号测试
```

## 2. 两种交换格式

### v1 二进制行程包（`.v1.bin`）
小端、LEB128 varint。布局：魔数 `PAN1`、版本 uint8、flags uint8（bit0=压缩档）、
宽、高、实例数，随后每条记录为 `id_len+id、class_len+class、payload_len+RLE payload`。
RLE 两档均可逆：

- 档位 0 `pairs`：每个 1-行程显式 `(start, length)` varint 对；
- 档位 1 `packed`：COCO 风格 0/1 交替计数，首项恒为 0-行程计数（可为 0）。

v1 只表达实例 ID、语义类别与掩码，不含置信度、crowd、调色板。

### v2 JSON 掩码包（`.v2.json`）
`format_version=2`，含宽高、命名 `palette`（RGB）、实例数组；每实例为
base64 编码的逐像素 0/1 字节串、`confidence`、`is_crowd`、`palette_name`。

## 3. 统一中间表示与信息保留

- v1 升 v2：缺失置信度取 `default_confidence`、crowd 取 `default_crowd`，
  按语义类别确定性合成调色板；推定字段写入 `source_flags` 并记入升级损失。
- v2 降 v1：置信度、crowd、调色板无法表达，逐条写入降级损失清单
  （`downgrade_losses`），绝不静默丢弃；行程与实例 ID 无损。
- 压缩档切换仅改变字节编码，`(start,length)` 行程与实例 ID 完全可逆。

## 4. 融合与裁决

- 仅在 v1/v2 同语义类别、非 crowd 实例间按 IoU 阈值匹配；
- 匹配成功按置信度加权合并，平局按实例 ID 字典序；
- 候选冲突决断顺序：IoU 降序 -> 置信度降序 -> 实例 ID 字典序；
- crowd 只参与像素覆盖，不进入匹配竞争；像素认领时非 crowd 优先于 crowd；
- 每个像素归属唯一，未覆盖区域为背景 0，输出融合实例清单与冲突裁决记录。

## 5. 指纹、失效与快照

- 实例缓存键 = 行程内容指纹 + 语义类别 + 元数据指纹 + 格式版本号；
- 融合依赖指纹 = 参与实例缓存键集合 + IoU 阈值 + 保留策略；
- 元数据替换/单实例掩码变更只失效该实例及其所在场景融合，其余直接复用且不重复解码；
- 快照先写 `.snap-tmp-*` 临时目录，再 `os.replace` 整体切换，`current.json`
  原子指向当前版本；manifest 记录全局版本、格式版本、配置哈希、实例与融合版本；
- 配置哈希不匹配则重建；硬崩溃只允许从已完整替换的快照恢复，不承诺退出报告。

## 6. 边界诊断（旁路，不改变主结果）

- 阈值敏感：以 Python 整数先算后除的 f64 参考值对比 f32 实际判定；
- 策略敏感：覆盖率 f64 参考值对比 f32 实际值与 f64 下限比较；
- 两者判定不一致记 `E_BOUNDARY_SENSITIVE`，报告区分
  `threshold_sensitive` 与 `policy_sensitive`，结果可复现。

确定性翻转样本：
- IoU：inter=1677727, union=2396753，阈值 0.7，f64 判 False、f32 判 True；
- 覆盖率：1/100，下限 0.01，f64 判 True、f32 判 False。

## 7. 并发与异常隔离

线程池最大并发取 `workers`；任务携带 `instance_id::stage` 稳定排序键，
结果在返回前重排，单线程与多线程的掩码顺序、投票顺序、失败清单顺序完全一致。
单实例任意阶段抛异常只生成失败记录（实例 ID、阶段、业务码、异常类型），
其余实例继续执行。异常通过独立 `fault_injection` 配置注入，不改源码。

## 8. 整数退出码约定

| 退出码 | 含义 |
| --- | --- |
| 0 | 成功，或收到可处理信号后完成当前产物与 manifest 原子提交 |
| 2 | 管线完成但存在业务失败清单（解析/转换/融合阶段） |
| 3 | 致命错误（输入/快照不可用等），已尽力写报告 |
| 137 | 模拟强制终止（硬崩溃），仅写崩溃标记，不承诺退出报告 |
