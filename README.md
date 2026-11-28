# 分级故事改写与练习工作台

这是一个只依赖 Python 3 标准库的本地工作台。它从固定种子生成故事、事件标注和三套分级标准，对每句话做掩码去噪评分、约束改写、情节核对、独立可读性核对，并输出与改写句一一对应的 cloze 和错词练习。

## 快速开始

```bash
python3 generate_data.py
python3 run_workbench.py
python3 run_workbench.py verify
python3 -m unittest discover -s tests -v
```

`generate_data.py` 写入 `data/`；`run_workbench.py` 读取数据并写入 `outputs/`；`verify` 会连续运行两次，比较整条流水线的稳定哈希并重新执行验收检查。

也支持模块调用和自定义目录：

```bash
python3 -m storybench --data-dir data --out-dir outputs run
python3 -m storybench --data-dir data --out-dir outputs verify
python3 generate_data.py --data-dir data --seed 20261128
```

## 数据文件

`data/` 全部由固定种子和 SHA-256 派生的子种子生成，不包含时间戳：

- `stories.json`：故事、逐句事件标注、事件序号、关键实体和实体别名。
- `grading_levels.json`：`L1`/`L2`/`L3` 的可用词表、句长上限、同义词条目、修饰语删除规则和拆句规则。
- `lexicon.json`：词性、语义类别、名词数、动词配价框架及上下文兼类信息。
- `model_corpus.json`：掩码去噪评分器唯一的本地训练语料。
- `generation_manifest.json`：故事顺序、练习子种子和生成参数。

默认生成 6 篇故事、每篇 4 个句子事件、3 套分级标准，共 72 个“故事 × 级别 × 句子”处理单元。

## 流水线

1. **逐句单元**：把 `sentence_id`、`event_id`、`event_order` 和关键实体绑定到每个处理单元；实体别名让 `veterinarian → vet`、`Nina → she` 这类表面变化仍可追溯。
2. **掩码去噪评分**：每当评分一个词位时，该词位被替换为 `[MASK]`，模型用左邻词、右邻词、左右词对、句内位置桶和一元词频做插值，输出还原概率、还原词及排名。
3. **约束解码**：确定性搜索同义词替换、合法修饰语删除和带主语检查的拆句组合；只保留同时满足词表、句长和关键实体保护的结果，再按平均掩码对数概率选优。无安全变换时明确记录 `unchanged_original`；无法满足约束时记录 `fallback_original` 及原因。
4. **情节核对**：逐事件检查关键实体是否仍在，并按事件实体在改写文本中的位置检查顺序；丢失实体或错序会输出人工复核标记和丢失项名称。
5. **独立可读性核对**：`readability.py` 不调用改写器的句长/词表判断逻辑，而是用独立分词和分句实现重新验证；报告给出违规句、违规条目和分级规则引用。
6. **练习生成**：在通过核对的改写句中选掩码可预测度最低、且存在同词性合法错词候选的非实体位置。同一位置生成共享编号的 cloze、错词句和扰动位置；错词候选进一步匹配名词数、动词配价框架，并要求上下文对数概率低于原词。

## 输出文件

- `outputs/ledger.jsonl`：逐句改编账目。每行含原句、改写句、删除词、替换词、拆句记录、规则引用、逐位置掩码得分，以及情节/可读性复核结果。
- `outputs/scored_candidates.jsonl`：每个选中候选的完整逐位置评分轨迹和前 8 个有效候选摘要。
- `outputs/cloze.jsonl`：挖空句、答案、位置、避开关键实体标记和可预测度证据。
- `outputs/wrong_word.jsonl`：原句、扰动句、扰动位置、目标词/替换词、词性与配价匹配、上下文不匹配证据和扰动种子。
- `outputs/exercises.jsonl`：cloze 与错词练习的一对一配对总表。
- `outputs/plot_review.json`：全部事件的情节覆盖与顺序核对。
- `outputs/readability_report.json`：独立核对器的违规句清单和违规条目。
- `outputs/stories/<story_id>.json`：单篇故事的全部级别、逐句证据和练习。
- `outputs/summary.json`：单元数、改写状态、情节/可读性/练习统计和总载荷哈希。
- `outputs/manifest.json`：所有产物的 SHA-256 和字节数。
- `outputs/verification.json`：仅在 `verify` 命令后生成，记录两次运行哈希和验收结果。

账目中的 `rubric_refs` 均可在 `grading_levels.json` 找到，例如 `L1-SYN-002`、`L1-VOCAB-BIG`、`L1-MOD-DEL`、`L1-SPLIT-CONJ`、`L1-LEN`。

## 确定性约束

- 所有随机选择都来自固定种子的 `random.Random`，故事顺序和练习扰动子种子由 SHA-256 派生。
- 所有候选、词表、文件和报告均按稳定键排序；输出不含运行时间或绝对路径。
- `verify` 会比较两次运行的 `payload_sha256`，并检查词表/句长、实体覆盖、规则引用、实体避让、同词性与配价匹配、练习配对和逐词单次变更不变量。
- 当前默认种子下，72 个单元全部通过独立可读性和情节核对，练习生成无跳过项。

## 项目结构

```text
generate_data.py          数据生成入口
run_workbench.py          运行/验证入口
storybench/generate.py    故事、事件、分级标准与词库生成
storybench/model.py       掩码去噪评分器
storybench/rewrite.py     约束候选搜索与解码
storybench/plot.py        事件覆盖和顺序核对
storybench/exercises.py   cloze 与错词扰动
storybench/readability.py 独立词表/句长核对器
storybench/pipeline.py    整条流水线、验收与产物写出
tests/                    标准库 unittest 测试
```
