在本地构建一个面向中文文本的轻量级分词与词性标注流水线，用于领域组件集成与误差分析。输入为 UTF-8 编码的文本行，每行包含句子 ID 和原始文本。流水线采用基于词典的最大匹配分词，结合 HMM 处理未登录词，再使用预置的概率表进行词性标注。核心挑战在于数值精度与误差传播的可观测性：分词边界错误会逐级放大影响词性标注的发射概率与转移概率累乘，导致路径得分下溢或精度损失。需要实现稳定的对数域计算、分数归一化与误差追踪，输出可复现的中间概率与最终标注。组件需提供确定性的本地样本与测试服务，不依赖外部模型或在线资源。

任务环境与准备：
Windows 10 或以上，安装 Python 3.10 及以上，确保 python 与 pip 可用。
安装 NumPy（pip install numpy），版本 1.24 及以上，用于矩阵运算与对数域计算。
创建工作区目录，准备本地词典文件（UTF-8 编码，每行“词 词频”）与概率表文件（转移概率、发射概率、初始概率，JSON 格式，概率值均为 0 到 1 之间），以及若干包含句子 ID 与文本的输入样本文件。
无需外部模型、在线服务或 GPU；所有样本与概率表由任务提供或按格式生成，确保可离线运行。

业务要求：
1. 实现基于词典的最大匹配分词器：读取本地词典文件（每行“词 词频”），对输入句子执行正向最大匹配，并输出每个词的起始位置、结束位置与词形；当词典中存在多个等长匹配时，按词频降序选择，词频相同按词形字典序升序选择，确保边界决策确定且可复现。
2. 实现 HMM 未登录词识别模块：对最大匹配结果中连续单字片段，使用预置的转移概率表、发射概率表与初始概率表进行维特比解码，所有概率以自然对数域存储与计算，避免浮点下溢；发射概率表缺失某字时使用平滑值，平滑参数可配置且必须记录到输出元数据中。
3. 实现词性标注器：基于分词结果与预置的词性发射概率表、词性转移概率表，使用对数域维特比算法为每个词分配词性；输出每个词的词性、局部路径得分以及整句归一化概率，归一化概率通过 log-sum-exp 在全部候选路径上计算，不得使用近似截断。
4. 实现误差传播追踪：对每个词记录分词边界置信度（基于最大匹配的候选词频比）与词性标注路径得分，并计算整句的误差累积指标，包括分词阶段对数似然损失、词性阶段对数似然损失以及两者之和；当某阶段得分低于可配置阈值时，在输出中标记该词为低置信度并记录触发阈值。
5. 实现流水线阶段化执行与中间结果持久化：将分词、未登录词识别、词性标注划分为独立阶段，每个阶段输出 JSON 格式的中间结果文件，包含阶段名、版本号、配置哈希与逐句结果；恢复执行时读取最新阶段文件，若版本号或配置哈希与当前不一致，输出业务码 E_VERSION_MISMATCH 并拒绝继续，一致时从下一阶段继续。
6. 实现数值稳定性保障：所有概率表在加载时转换为自然对数域，维特比解码过程中使用逐元素加法并与当前最优前驱比较，避免概率累乘；对 log-sum-exp 计算采用减去最大值的稳定形式；输出中保留至少 6 位小数的对数得分，并保证相同输入在不同运行顺序下输出逐字节一致。
7. 实现并发处理与异常隔离：使用线程池并行处理句子，最大并发数由配置指定；单个句子处理异常时记录句子 ID、阶段名与异常类型到失败清单，继续处理其余句子；全部处理完成后输出失败清单，若失败清单非空则退出码为 1，否则为 0；强制终止后仅能依赖已持久化的阶段文件恢复。
8. 实现统计报告输出：每次完整运行结束生成 JSON 报告，包含总句子数、成功句子数、失败句子数、各阶段平均对数得分、低置信度词数量、配置哈希、阶段文件版本号与失败清单；报告以临时文件加原子替换方式写盘，字段名与错误码必须与要求中一致。

执行约束：不使用 Docker 或其他容器环境；使用本地确定性样本验证，不访问未授权目标。按上述任务意图交付相应代码、测试或技术说明，记录实际验证结果。

验收：
使用本地样本验证分词与词性标注输出、对数域数值稳定性及中间文件恢复一致性，并检查失败清单与报告字段。

---

# 交付实现说明

## 1. 环境与安装

- 要求 Python 3.10+、NumPy 1.24+，全程离线，无外部模型/网络/GPU 依赖。
- 安装依赖：`python3 -m pip install -r requirements.txt`
- 本验证环境为 Python 3.11 + NumPy 2.4.6（离线 wheel 已装入用户 site-packages）。

## 2. 目录结构

- `zh_nlp/config.py`：配置 dataclass 与确定性配置哈希（数据文件内容参与哈希，`max_workers` 不参与）。
- `zh_nlp/data.py`：词典与概率表加载；原始概率在加载时即转为自然对数域。
- `zh_nlp/numeric.py`：稳定 `log-sum-exp`（减最大值）、6 位小数统一取整、临时文件 + `os.replace` 原子写盘。
- `zh_nlp/segmenter.py`：正向最大匹配；等长匹配按词频降序、词形字典序升序裁决。
- `zh_nlp/hmm.py`：BMES 对数域维特比，缺字使用可配置平滑并记录；前向 log-sum-exp 求归一化概率。
- `zh_nlp/pos_tagger.py`：词性对数域维特比、全路径归一化、逐词低置信度标记与阈值记录。
- `zh_nlp/pipeline.py`：三阶段编排、线程池并发与异常隔离、阶段持久化/恢复、版本校验与报告。
- `zh_nlp/cli.py` / `zh_nlp/__main__.py`：命令行入口。
- `samples/generate_samples.py`：确定性生成词典、HMM/词性概率表、输入样本与配置。
- `tests/test_pipeline.py`：19 个 `unittest` 用例。
- `run_demo.sh`：一键生成样本、运行流水线与测试。

## 3. 运行方式

```bash
python3 samples/generate_samples.py
python3 -m zh_nlp.cli --config samples/config.json \
  --input samples/input.txt --output out/demo
python3 -m unittest tests.test_pipeline
```

输入每行格式：`句子ID<TAB>原始文本`（无 Tab 时按首个空白切分）。

## 4. 关键设计与需求对应

- 分词输出每词 `start`/`end`（半开区间，字符偏移）、`word`、`in_lexicon`、`boundary_confidence`（候选词频比）与 `segmentation_loss=-log(置信度)`。
- HMM 仅对最大匹配产生的连续单字（未登录）片段解码，按 B/E/M/S 重组词；发射表缺字用 `hmm_emission_smoothing`，平滑值写入阶段文件 `metadata`，被平滑的字记录到 `smoothed_chars`。
- 词性标注输出 `pos`、`local_path_score`、整句 `pos_log_normalized_probability`（全候选路径前向 log-sum-exp，无截断）与 `pos_log_loss`。
- 误差累积：`segmentation_log_loss`、`pos_log_loss` 及二者之和 `total_log_loss`；逐词 `low_confidence`、`low_confidence_stage`、`triggered_threshold`。
- 阶段文件统一含 `stage`、`stage_version`、`config_hash`、`metadata`、`sentences`、`failures`。恢复时校验版本与哈希，不一致输出业务码 `E_VERSION_MISMATCH` 并以退出码 2 拒绝继续；一致则从下一阶段续跑。
- 数值稳定：全程对数域加法、维特比逐元素比较；`log-sum-exp` 减最大值；得分保留 6 位小数；JSON 固定 `sort_keys`、`ensure_ascii=False`、统一缩进与 `\n`，结果按句子 ID 排序，跨运行/跨并发数逐字节一致。
- 并发与隔离：`ThreadPoolExecutor`，并发数 `max_workers`；单句异常记录 `sentence_id`/`stage`/`exception_type`/`message` 后继续；失败清单非空退出码 1，否则 0。
- 报告 `report.json` 字段：`total_sentences`、`succeeded_sentences`、`failed_sentences`、`stage_average_log_scores`、`low_confidence_word_count`、`config_hash`、`stage_versions`、`failures`，原子替换写盘。

## 5. 实际验证结果（本环境）

- 完整运行 7 句样本退出码 0，生成 3 个阶段文件与报告；HMM 正确将“猫咪”识别为 B-E 单词，未登录词“跑/跳/星云/高”走平滑。
- 数值稳定性：长度 50 的未登录序列维特比/前向得分仍为有限值；`log-sum-exp(-1000,-1001,-1002)` 有限且与理论公式一致，无下溢。
- 确定性：`max_workers=4` 与 `max_workers=1` 两次运行的全部阶段文件与报告 SHA256 完全一致（逐字节相同）。
- 恢复一致性：仅保留 `stage_segmentation.json` 后续跑，HMM/POS/报告与完整运行逐字节一致；篡改版本号后返回 `E_VERSION_MISMATCH`、退出码 2。
- 异常隔离：注入单句故障后退出码 1，报告 `failed_sentences=1`、`succeeded_sentences=6`，失败清单含句子 ID、阶段名与异常类型。
- 测试：`python3 -m unittest tests.test_pipeline` 共 19 个用例全部通过。
