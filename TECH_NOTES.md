# 中文 NER 领域组件：增量依赖失效判定 — 技术说明

## 组件结构

- `ner_component/loaders.py` — 语料/模型/词典/配置/编辑加载与校验，非法项分别产出
  `E_CORPUS_INVALID`、`E_MODEL_SHAPE_MISMATCH`、`E_DICT_INVALID` 并进入失败清单，合法项继续处理。
- `ner_component/core.py` — 字级哈希特征（当前字、前后各一字、词表命中长度与位置）、
  对数域 BIESO 维特比解码（同分按标签字典序决断，numpy 点积打分）、实体切分与词典
  最长匹配后校验（冲突按词频→词长→位置→词条文本决断，记录被修正项）。
- `ner_component/fingerprint.py` — 词条/模型/句内容指纹与实体缓存键
  （`sha256(词条指纹集 + 模型指纹 + 句指纹 + 跨度/类型/文本)`）。
- `ner_component/snapshot.py` — 版本化快照：先写 `*.tmp` 目录再整体 rename 替换，
  旧目录先改名 `*.old` 后清理，无半替换残片。
- `ner_component/engine.py` — 全量/增量引擎：依赖失效判定、批处理、线程池并发解码、
  异常隔离、信号退出、原子报告写盘。
- `generate_samples.py` — 固定种子（47）生成语料、模型 v1/v2、词典 v1/v2、配置与编辑文件。
- `main.py` — CLI：`full`（空白工作区一次性全量）/ `incr`（分批增量）。
- `verify.py` — 验收脚本，23 项检查。

## 关键设计

- **精确失效判定**：句对词条的依赖按"词条文本是否为句子内容的子串"判定，与当前词典
  状态无关，因此单条增删、整体替换（按词条指纹 diff）都能精确圈定重算集；模型参数
  变更使全部句子失效。未受影响句子直接复用缓存结果，不重复解码。
- **确定性**：特征哈希用 MD5 截断取模；解码打分四舍五入到 6 位小数；同分按标签字典序；
  并发结果按句 ID 升序归并；报告 `sort_keys=True` 写盘。同一输入重复运行逐字节一致。
- **快照恢复**：manifest 记录全局版本号、基准/当前模型与词典哈希、已应用编辑数、
  完成句与失败句清单。恢复时先比对基准哈希，再静默重放已应用编辑并校验当前哈希，
  任一不符输出 `E_CONFIG_MISMATCH` 并按策略从一致阶段重启。
- **退出语义**：SIGTERM/SIGINT 置位后在当前批快照原子替换与进度记录完成后以退出码 0 退出。

## 运行方式

```bash
pip install numpy
python3 generate_samples.py work/samples
python3 main.py full --sample-dir work/samples --report work/report_full.json --snapshot-dir work/snap_a
python3 main.py incr --sample-dir work/samples --report work/report_incr.json --snapshot-dir work/snap_b
python3 verify.py
```

## 验证结果（2026-11-17，Python 3.11 + numpy 2.4.6）

`python3 verify.py` 输出 23/23 通过：

- 增量（5 条编辑、批大小 2、3 批，覆盖模型变更/单条增删/批量替换/类型覆盖冲突）
  与空白全量运行的全部句子标签序列、路径总分、实体、被修正项、失败清单逐字段一致；
  共 426 个实体、211 条修正记录（含 14 条"北京"LOC→ORG 类型覆盖）。
- 依赖失效精确：删除"张三"仅重算 9 个命中句（期望集完全一致），39 句复用且逐字段不变。
- 并发 1 与 8 结果完全一致；同一输入重复全量运行报告逐字节一致。
- 异常隔离：重复句 ID、位置不连续、控制字符句、非法词条类型、错误模型形状分别产出
  `E_CORPUS_INVALID` / `E_DICT_INVALID` / `E_MODEL_SHAPE_MISMATCH`，失败句后续批次不再产出实体。
- 快照：篡改 manifest 词典哈希触发 `E_CONFIG_MISMATCH` 并正确重启；无 `.tmp`/`.old` 残片；
  SIGTERM 后退出码 0 且快照与报告完整。
