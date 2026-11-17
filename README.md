# 本地确定性视频预测与运动时序渲染诊断工作台

纯 CPU、离线、仅依赖 NumPy 与 Pillow 的双路径（文生 / 图生）多分辨率视频渲染
诊断工作台。全部随机量由固定种子经 sha256 + PCG64 派生，重复运行逐元素一致。

## 环境准备

```bash
python3 --version            # 需要 Python 3.11
pip install -r requirements.txt
```

## 快速开始

```bash
# 1. 生成确定性样本（轨迹/标签 JSONL、初始图 PNG、噪声/引导表、配置、9 类异常用例）
python3 run_workbench.py generate --sample-dir samples/valid

# 2. 双路径多档位全量渲染，输出诊断报告
python3 run_workbench.py run \
    --config samples/valid/config.json \
    --sample-dir samples/valid \
    --output-dir artifacts/run

# 3. 首帧预算内的最低档预览
python3 run_workbench.py preview \
    --config samples/valid/config.json \
    --sample-dir samples/valid \
    --output-dir artifacts/preview
```

## 测试与验收

```bash
python3 -m unittest discover -t .   # 49 个单元/集成测试
python3 acceptance_check.py         # 18 项端到端验收，输出 artifacts/acceptance/
```

## 目录结构

- `vbench/`：工作台源码（配置/样本加载、嵌入、去噪、调度、快照、管线、报告）。
- `tests/`：基于 `unittest` 的测试套件与共享夹具。
- `samples/valid/`：合法确定性样本；`samples/faults/`：异常注入样本。
- `artifacts/`：渲染帧 PNG、阶段潜变量快照、清单与诊断报告。
- `DESIGN.md`：架构、算法取舍、错误码与恢复语义的技术说明。
- `acceptance_check.py`：端到端验收脚本。

## 产物布局

```
<output-dir>/
  manifest.json                  # 全局版本号、配置哈希、档位指纹、片段游标
  diagnostic_report.json         # 诊断报告
  stages/init/<clip>/<tier>/     # 初始化潜变量与依赖清单
  stages/denoise/<clip>/<tier>/<path>/step_XXXX.raw
  frames/<clip>/<tier>/{text,image}_XXXX.png
```
