遥感影像解译团队在区域土地利用变化监测中，把卷积分类模型输出的多通道地块特征图接入空间分析链路。每张特征图按影像分块组织，块内既记录块内空间索引，也声明地理原点与像元分辨率；解译员需要为每个空间位置施加旋转位置编码（RoPE），以便后续地块边界判别和变化图斑定位能利用方位与尺度关系。当前同一批特征会以三个 schema 版本进入流程，字段命名、坐标单位（米与像元）、通道分组表达各不相同，且不同分块常由不同班组独立编码后拼接。若各块各自按块内坐标计算旋转角，相邻块边界会出现相位断裂；若单位换算不一致，同一地理位置在不同版本下编码漂移，最终边界判别会在接缝处产生错位图斑。团队需要一个本地可运行的服务，把三版本描述归一为统一地理坐标与通道模型，逐位置逐通道计算旋转角并施加编码，输出旋转角记录、拼接后的连续编码场、分块边界连续性报告与跨版本差异清单。

任务环境与准备：
准备 Linux（WSL Ubuntu）下的 Python 3 运行环境，创建题目工作目录。
安装 NumPy 用于特征张量与旋转角数值计算，安装 Flask 用于本地编码服务；两者均为第三方包，需在运行前准备好。
题目自带确定性生成器，按固定种子在本地生成三 schema 版本的地理影像特征张量、分块配置与频段组合样本，不依赖外部影像数据或网络服务。
在本地启动 Flask 服务后，使用题内提供的请求样本调用编码接口与核对接口，所有计算与校验均在本机完成。

业务要求：
1. 实现三个 schema 版本的特征分块与配置归一器：将字段命名差异（如 origin_x 与 geo_origin[1]）、坐标单位（米与像元，按像元分辨率换算）和通道分组表达（连续区间与显式索引列表）统一为内部模型，使同一地理位置在三个版本下得到相同的地理坐标与通道语义，并提供归一结果的结构化摘要。
2. 按全局地理坐标与每个分块声明的频率参数逐空间位置、逐通道成对计算旋转角：对通道分组内成对通道施加旋转位置编码，输出每个位置每个通道的旋转角（弧度）以及编码后的特征张量，并保证角度基数、频率缩放与分组边界在计算中生效。
3. 对分块特征按全局坐标编码后拼接为连续位置编码场，检测相邻分块边界处同一条地理网格线上相位差是否超过声明阈值，标记因使用块内坐标而会产生相位断裂的位置，并输出边界连续性报告（含接缝编号、相邻块标识与相位差）。
4. 支持按不同频段组合分别计算编码场：对每个频段组合使用对应的频率参数子集与角度基数重新计算旋转角和编码特征，输出各组合下的编码摘要，并核验不同频段组合在重叠通道上的旋转角一致性，记录不一致的通道与位置。
5. 当模型版本或分块参数（原点、分辨率、通道分组、频率参数）变化时，依据旧参数与新参数推断受影响的位置集合，仅重算这些位置的旋转角与编码特征，输出需更新的位置清单以及重算前后的旋转角差值，保证未变化位置的编码不被扰动。
6. 对同一批特征在三个 schema 版本归一后产出相同的位置旋转角与编码特征：逐位置核对旋转角与编码值是否一致，输出跨版本差异清单，并单独标记因坐标单位换算或通道分组表达差异导致的编码漂移位置及其来源版本。
7. 以题内确定性生成器内嵌的独立旋转角参考计算器作为真值，对逐位置逐通道旋转角与编码特征进行逐元素核对，报告偏差超过声明容差的位置、通道与偏差值；对人工构造的跨块边界样本和同地理位置不同分块参数样本，执行相位连续性与编码一致性检查并给出依据。
8. 通过本地 Flask 服务提供编码接口与核对接口：接收三版本特征描述、分块配置与频段组合请求，返回编码结果、旋转角记录、边界连续性报告与跨版本差异清单；当请求存在坐标单位歧义、通道分组重叠或分块地理范围冲突时返回明确的业务错误码与错误位置，不产生部分编码结果。

验收：
三个 schema 版本归一后同一地理位置的旋转角与编码特征逐位置一致；分块边界相位断裂位置与参考计算器核对偏差在容差内。

## 已实现内容

本仓库提供一个可本地运行的 Python/Flask 服务，入口为 `app.py`，核心包为
`georope/`。实现覆盖三 schema 归一、全局地理坐标 RoPE、分块连续场拼接、
接缝相位诊断、频段组合、增量重算、跨版本差异、独立参考计算器和人工验收样本。

主要文件：

- `georope/normalize.py`：三版本字段、单位、通道分组与频率参数归一。
- `georope/rope.py`：逐位置逐通道旋转角计算、成对旋转与连续场拼接。
- `georope/continuity.py`：接缝线全局/块内坐标相位差诊断。
- `georope/compare.py`：频段组合重叠通道与跨 schema 版本一致性核对。
- `georope/incremental.py`：参数变更影响集合、角差和未扰动位置核验。
- `georope/reference.py`：独立实现的旋转角与编码参考计算器。
- `georope/generator.py`：固定种子三版本影像特征与分块生成器。
- `georope/manual.py`：人工跨块边界和同地理位置异构参数样本。
- `georope/server.py`：Flask 编码、核对和增量接口。

## 运行准备

```bash
python3 -m pip install -r requirements.txt
```

当前实现使用 NumPy 进行特征张量和旋转角计算，使用 Flask 提供本地 HTTP 服务。

## 生成确定性样本

```bash
python3 -m georope.cli generate --seed 42 --output samples/request.json
python3 -m georope.cli generate-incremental --seed 42 \
  --output samples/incremental_request.json
```

同一 `--seed` 的生成结果逐字节一致。样本包含：

- v1：`origin_x/origin_y`、米制原点、连续半开区间 `channel_range`。
- v2：`geo_origin` 数组、像元原点、显式 `indices` 通道列表。
- v3：嵌套 `geo_origin`、米制原点、`start/end` 区间对象。
- 每个版本 4 个分块，场景网格为 6 行 8 列，共 12 个特征通道。
- 三个频段组合：`low_only`、`mid_high`、`all_bands`。

## 启动服务

```bash
python3 app.py
```

默认监听 `http://127.0.0.1:5000`。也可指定地址：

```bash
python3 -m georope.cli serve --host 127.0.0.1 --port 5000
```

接口：

- `GET /api/health`：服务状态。
- `GET /api/sample?seed=42`：生成并返回确定性请求样本。
- `POST /api/encode`：编码并返回全部编码报告。
- `POST /api/check`：执行参考计算器、接缝与人工样本核对。
- `POST /api/incremental`：提交 `{"old": ..., "new": ...}` 执行增量重算。
- `/encode`、`/check`、`/incremental`：同上接口的无 `/api` 前缀别名。

调用示例：

```bash
curl -s http://127.0.0.1:5000/api/health
curl -s -X POST http://127.0.0.1:5000/api/encode \
  -H 'Content-Type: application/json' \
  --data-binary @samples/request.json
curl -s -X POST http://127.0.0.1:5000/api/check \
  -H 'Content-Type: application/json' \
  --data-binary @samples/request.json
curl -s -X POST http://127.0.0.1:5000/api/incremental \
  -H 'Content-Type: application/json' \
  --data-binary @samples/incremental_request.json
```

也可在命令行直接生成结果文件：

```bash
python3 -m georope.cli encode --input samples/request.json \
  --output artifacts/encoding_result.json
python3 -m georope.cli check --input samples/request.json \
  --output artifacts/check_result.json
python3 -m georope.cli incremental --input samples/incremental_request.json \
  --output artifacts/incremental_result.json
```

## 统一地理与通道模型

内部坐标统一为米，块内位置使用像素中心：

```text
x_m = origin_x_m + (local_col + 0.5) * resolution_x_m
y_m = origin_y_m + (local_row + 0.5) * resolution_y_m
```

原点声明为像元时按分辨率换算：

```text
origin_m = origin_raw_pixel * resolution_m_per_pixel
```

请求可用 `pixel_anchor: "corner"` 改为原点即首像素角点。原点数组默认顺序为
`[x, y]`，可用 `origin_order: "yx"` 或 `["y", "x"]` 明确声明其他顺序。

通道统一为 0 基规范索引。连续区间、显式索引列表和 `start/end` 区间对象都会
归一为同一组通道集合；组内按规范通道升序两两配对。不同块或不同版本若声明相同
通道语义，则得到相同位置与通道映射。

旋转角公式：

```text
theta(position, channel_pair p) =
    frequency_scale * (f_x * x_m + f_y * y_m)
    * angle_base^(-2*p / channel_group_size)
```

其中 `f_x/f_y` 来自 `frequency_axis` 或显式 `pair_frequencies`。同一通道对的两
个通道共享旋转角，编码为：

```text
left'  = left  * cos(theta) - right * sin(theta)
right' = left  * sin(theta) + right * cos(theta)
```

不同 `band_combinations` 选择对应频段子集和角度基数重新计算。只包含频段子集的
组合不会改写未选中通道。

## 关键响应

`/api/encode` 返回：

- `normalized_summary`：三版本逐块归一摘要、单位换算、分组来源与频率参数。
- `rotation_angle_records`：逐组合、逐版本、逐块的逐位置逐通道旋转角。
- `encoded_fields`：按全局栅格拼接后的连续编码场和旋转角场。
- `boundary_continuity_report`：每个组合的接缝编号、相邻块、全局相位差、
  块内坐标相位差与断裂位置。
- `cross_combination_differences`：重叠通道的角度/编码一致性。
- `cross_version_differences`：三版本逐位置逐通道差异、单位换算漂移和分组
  表达漂移来源。
- `reference_verification`：独立参考计算器逐元素偏差与容差超限位置。
- `verification_summary`：各项检查总览。

接缝报告中的 `local_phase_break_positions` 表示若各块各自使用块内原点坐标会
出现的相位断裂；`global_discontinuity_positions` 表示使用同一全局地理坐标后仍
存在的参数或相位不连续。

## 业务错误

失败响应统一为：

```json
{
  "ok": false,
  "error": {
    "code": "COORD_UNIT_AMBIGUOUS",
    "message": "origin coordinate unit is not declared",
    "locations": [{"block_id": "v1-tile-r0c0"}]
  }
}
```

主要错误码：

- `COORD_UNIT_AMBIGUOUS` / `COORD_UNIT_UNSUPPORTED`
- `CHANNEL_GROUP_OVERLAP` / `CHANNEL_GROUP_DUPLICATE`
- `BLOCK_EXTENT_CONFLICT`
- `CHANNEL_OUT_OF_RANGE` / `UNASSIGNED_CHANNEL` / `UNPAIRED_CHANNEL`
- `BAND_COMBINATION_UNKNOWN` / `BAND_COMBINATION_OVERLAP`
- `FEATURE_SHAPE_MISMATCH` / `MISSING_RESOLUTION`
- `INCREMENTAL_PAYLOAD_INVALID`

所有校验在编码前完成；错误响应不包含部分编码结果。

## 验收

```bash
python3 -m pytest -q
python3 -m georope.cli check --input samples/request.json \
  --output artifacts/check_result.json
```

内置人工样本覆盖：

- 两个相邻块在全局坐标下接缝相位差为 `0`，改用块内坐标后出现 `3.0` 弧度断裂。
- 米制原点、像元原点、连续区间、显式索引列表三种表达在同一地理位置得到
  一致旋转角与编码特征，单位/分组漂移计数均为 `0`。

`/api/check` 的 `passed`、`checks` 与 `manual_cases` 可直接作为验收依据。
