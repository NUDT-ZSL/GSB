# OCR 本地可视化诊断工作台

从零构建的确定性 OCR 计算图、内容寻址缓存、增量失效传播与 Tkinter 诊断
工作台。全部样本由固定种子本地生成，无网络、无外部图像或 OCR 模型依赖。

## 环境准备

需要 Python 3.11+（标准库 `tkinter` 可用）与 `numpy>=1.26`：

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

## 目录

- `ocrbench/`：核心包（配置、模板、样本、计算图、缓存、快照、引擎、增量、
  自检、工作台门面、演示、UI）
- `tests/`：28 个 unittest 测试
- `scripts/run_demo.py`：可重复端到端演示
- `scripts/run_ui.py`：Tkinter 工作台（无显示时自动无头摘要）
- `docs/DESIGN.md`：技术设计与错误码对照
- `docs/VALIDATION.md`：实际验证结果

## 运行

```bash
# 全量测试
.venv/bin/python -m unittest discover -s tests

# 端到端演示（写出 <workspace>/demo_report.json）
.venv/bin/python scripts/run_demo.py --workspace ./workspace_demo

# 交互工作台；无显示服务器时用无头模式
.venv/bin/python scripts/run_ui.py --workspace ./workspace_ui
DISPLAY= .venv/bin/python scripts/run_ui.py --workspace ./workspace_ui
```

## 计算图

`preprocess → detect → features → match → recognize → aggregate`
（`detect → aggregate` 另有一条逻辑依赖边），拓扑由配置驱动并做无环校验。

## 关键能力

- 相同种子与配置下逐字节一致；规范化 JSON + SHA-256 内容哈希 + 定点量化。
- 复合缓存键覆盖像素哈希、行框摘要、模板版本、检测阈值、上游哈希；缺项
  返回 `E_CACHE_KEY_INVALID`。
- 追加页面/真值修正/阈值调整/模板升级的最小后缀失效，输出复用与重算清单。
- 模板快照整体替换、回退拦截（`E_SNAPSHOT_ROLLBACK`）、损坏检测
  （`E_CACHE_CORRUPT`）。
- 分块与整页识别逐字节一致，差异返回 `E_CHUNK_MISMATCH` 并定位行列。
- 资源上限与流式降级、同键并发非阻塞 `E_STAGE_BUSY`、跨模块自检
  `E_INCONSISTENT`。

完整错误码与设计细节见 `docs/DESIGN.md`，实测结果见 `docs/VALIDATION.md`。
