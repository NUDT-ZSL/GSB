# 穹幕影院多机投影几何校正与边缘融合内核

本地可复现的半球穹顶投影校正/融合系统。固定种子生成多机样本工程，内核完成
投影机像素到穹顶经纬的反向映射、薄板样条（TPS）残差修正、重叠区检测、羽化
遮罩与亮度权重归一化，并同时提供命令行与 Tkinter 图形界面，二者共享同一内核。

## 环境准备

- Python 3（开发环境为 3.14）、NumPy。
- Tkinter 与系统 Tcl/Tk 运行库。本机已将 `python3.14-tk` 及其 Tcl/Tk、X11
  依赖以普通用户身份解包到 `~/.local/tklocal`，无需 root。

```bash
python3 -c "import numpy; print(numpy.__version__)"
source setenv.sh                 # 仅图形界面需要，设置 Tcl/Tk 环境变量
python3 -c "import tkinter; print(tkinter.TkVersion)"
```

为避免 NumPy 多线程在本机的过度调度，建议设置：

```bash
export OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1
```

## 目录结构

```
domecalib/
  core/
    errors.py        业务错误码与 CalibrationError
    geometry.py      坐标基、径向畸变正反解、射线-半球求交、经纬转换
    models.py        可序列化领域模型与版本哈希
    samplegen.py     固定种子样本工程生成器
    mapping.py       像素->穹顶反向映射、TPS 拟合与交叉验证、真值比对
    blending.py      覆盖光栅化、距离变换、羽化曲线、权重归一化
    render.py        穹顶网格融合预览、亮度一致性、对齐误差报告
    engine.py        编排引擎：缓存、版本号、全量/增量重算、纹理合成
    persistence.py   JSON + NPZ 快照持久化、清单、回滚、版本校验
    visualization.py 无依赖 PPM/PGM 编码
  gui/app.py         Tkinter 操作控制台
  cli.py             命令行入口
tests/               unittest 验收测试（26 项）
run_gui.py           图形界面启动脚本
setenv.sh            用户态 Tcl/Tk 环境变量
```

## 命令行用法

```bash
# 1. 生成固定种子样本工程
python3 -m domecalib.cli generate --out samples

# 2. 全量校正 + 融合 + 报告 + 基线快照
python3 -m domecalib.cli calibrate --out samples/out

# 3. 导出 LUT/遮罩/融合预览图像（PPM/PGM）
python3 -m domecalib.cli export-views --out samples/out/views

# 4. 调整羽化参数并生成新快照（仅重算融合，LUT 不变）
python3 -m domecalib.cli adjust-blend --label baseline --new-label feather4 \
    --feather-width 4 --gamma 1.2 --curve smoothstep

# 5. 增量重算与全量重算逐像素一致性核对
python3 -m domecalib.cli incremental-check

# 6. 回滚到历史快照
python3 -m domecalib.cli rollback --label baseline --out samples/rollback

# 7. 业务错误码演示（不破坏既有校正数据）
python3 -m domecalib.cli error-demo
```

## 图形界面

```bash
source setenv.sh
python3 run_gui.py
```

界面支持：载入样本工程或快照、选择投影机查看 LUT 与融合遮罩、调整羽化宽度/
gamma/曲线并触发受影响投影机重算、查看反向映射真值误差、TPS 交叉验证质量与
亮度一致性报告。所有按钮调用与命令行相同的 `CalibrationEngine` 方法，错误以
业务错误码形式显示在状态栏与报告区，且不覆盖已有有效结果。

## 关键算法与口径

- **反向映射**：像素归一化坐标 → 径向畸变二分反解（保证单调镜头必然收敛、
  不发散）→ 相机射线 → 与球最近正根求交 → 上半球有效性掩码 → 经纬度。
  无效像素写 NaN 并置 `valid=False`。高迭代参考 LUT 作为真值，生产 LUT 的
  有效像素最大角误差约 2e-7 rad。
- **TPS 修正**：在（经度,纬度）空间拟合薄板样条，SVD 伪逆求解避免奇异系统
  发散，修正量有界裁剪；输出拟合残差与留一交叉验证（LOOCV）RMS，对控制点
  不足、近共线、残差分布不均给出可识别降级或错误码。
- **重叠与融合**：穹顶等距经纬网格上做带环绕的覆盖光栅化与 3-4 倒角距离
  变换，按距离生成 linear/smoothstep 羽化，再对所有投影机归一化，使重叠区
  权重和恒为 1；附加每机增益最小二乘优化。三机交叠处同样保证和为 1、无
  接缝跳变。
- **增量与版本**：每个投影机依据参数/控制点/融合参数的稳定哈希缓存 LUT，
  仅变更项重建并对关联融合遮罩重算；结果与全量重建逐像素一致，版本号随
  变更递增，快照加载/回滚时校验版本以防使用过期数据。

## 业务错误码

| 代码 | 含义 |
| --- | --- |
| E1001 | 标定控制点数量不足（< 8） |
| E1002 | 控制点近共线，无法定义二维 TPS |
| E1003 | 投影机视锥与上半球穹顶无交叠 |
| E1004 | 畸变系数越界或径向非单调 |
| E1005 | 分辨率/控制点数量与样本不一致 |
| E1006 | 其他参数非法 |
| E1007 | 快照版本过期冲突 |
| E1008 | 快照读写失败 |
| E1009 | 投影机布局为空 |
| E1010 | 几何求解降级 |

## 验收测试

```bash
OMP_NUM_THREADS=1 python3 -m unittest discover -s tests -v
```

覆盖：固定种子确定性、索引相关扰动、右手坐标系、畸变正反解与不发散、射线
近根、反向映射真值误差与掩码一致、TPS 降级/交叉验证/有界修正、距离变换、
权重区间与和为 1、亮度均匀性与零接缝、六机三机交叠、增量==全量逐像素一致、
版本递增与过期守卫、快照往返、分辨率不一致诊断。
