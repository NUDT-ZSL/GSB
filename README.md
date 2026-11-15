# 音画时序对照审计工具（project109）

分镜预览片合成前的离线审计工具：读取同目录清单 JSON（帧率、总时长、毫秒节拍点、
帧号镜头区间与转场区间），统一换算到帧口径后产出：

- `report.json`：音画时序对照报告（被吞节拍、镜头首尾缺帧、时序异常、片尾音乐余量）；
- `marker_<镜头id>.png`：逐镜头转场露出标记图，按镜头时长均匀取样 8 帧横向拼接，
  绿色=可见画面（O），红色=被转场遮住（X），每格标注实际帧号。

仅使用 Python 标准库 + Pillow，本机运行，不访问网络、不使用容器。

## 环境要求

- Python 3.10+
- Pillow 10.x（已在 Python 3.10.20 + Pillow 10.4.0 验证）

## 本地启动方式（PowerShell）

```powershell
# 1. 建立虚拟环境并安装依赖（系统已有 Python 时）
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# 2. 确定性生成样本清单与毫秒节拍样本（6 个清单，含 3 个非法清单）
.\.venv\Scripts\python.exe -I src\generate_samples.py workspace\samples

# 3. 对单个清单执行审计（输出目录无需预建，非法清单不会创建/写入任何文件）
.\.venv\Scripts\python.exe -I src\av_sync_audit.py workspace\samples\normal_25fps.json workspace\out_normal

# 4. 一键运行 8 项验收（自动生成样本、跑全部清单并断言）
.\.venv\Scripts\python.exe -I src\verify_all.py workspace
```

`-I` 为隔离模式：忽略环境变量与当前目录，防止被投放的同名模块（如 `json.py`）加载。

## 口径约定（确定性，可手算复核）

- 帧区间一律为半开 `[start, end)`；
- 毫秒→帧：`frame = floor(time_ms * fps / 1000)`，负数也向负无穷取整
  （例：25fps 下 1234ms→30 帧；30fps 下 -100ms→-3 帧）；
- 总帧数：`total_frames = floor(duration_ms * fps / 1000)`；
- 节拍越界：`frame < 0` 或 `frame >= total_frames`；
- 被吞节拍：节拍帧落在任一转场半开区间内，报告记录节拍序号、帧号、所属镜头、转场 id；
- 镜头首/尾缺帧：转场压住镜头首帧（含）→头部缺帧，压住镜头末帧（不含，即 end-1）→尾部缺帧；
- 标记图取样位：`pos = start + floor(i * length / 8)`，镜头短于 8 帧则逐帧全取；
- 全部输出无随机数、无时钟依赖，`json.dump(..., sort_keys=True)`、PNG 确定性保存，
  重复运行逐字节一致（SHA-256 相同）。

## 清单 JSON 结构

```json
{
  "fps": 25,
  "duration_ms": 10000,
  "shots":      [{"id": "S1", "start_frame": 0, "end_frame": 60}],
  "transitions":[{"id": "T1", "start_frame": 55, "end_frame": 70}],
  "beats_ms":   [0, 40, 1234]
}
```

## 非法清单处理（退出码 1，零产出）

帧率 ≤ 0、总时长为负、JSON 无法解析、帧号非整数/为负、区间 end ≤ start 等，
均在创建输出目录之前报错退出，stderr 给出中文原因（如“帧率非法：fps 必须大于 0”）。

## 目录结构

```
src/av_sync_audit.py    审计主程序（换算/分析/标记图/报告）
src/generate_samples.py 确定性样本生成器
src/verify_all.py       8 项验收自动化（子进程 -I 驱动，含哈希比对）
requirements.txt        Pillow 10.x
workspace/              运行产物：samples/ 与 out_*/（运行验证后生成）
```
