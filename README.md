# 运动动作精排台

面向体育科学研究所动作分析研究员的本地训练片段精排系统。每个“运动员 + 训练周期阶段”构成一次排序请求，系统先对候选片段做传感器统计和档案标签对齐，再计算动作质量、负荷风险、档案匹配三路得分，按请求级权重融合并稳定重排。

## 快速启动

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

python generate.py
python verify.py
python run.py
```

打开 `http://127.0.0.1:8000`。FastAPI 接口文档位于 `http://127.0.0.1:8000/docs`。

所有数据由固定种子 `20261128` 在本地生成，SQLite 使用 Python 标准库，不需要独立数据库服务，也不会访问外部站点或真实训练数据。

## 数据规模

- 8 名运动员，每人包含专项、训练年限、伤病历史标签和档案快照。
- 192 段训练片段，每段包含加速度/角速度三轴序列、动作类别、重复次数、时长和人工质量分。
- 16 个排序请求，每个运动员 2 个不同训练周期阶段，每个请求 12 个候选片段。
- 数据库文件：`data/motion_ranking.db`。
- 生成摘要：`artifacts/generation_summary.json`。
- 独立核对报告：`artifacts/verification.json`。

## 精排契约

### 统一传感器统计

每条通道先按归一化时间轴重采样到 128 点，再计算：

- 均值与总体方差
- 峰值因子（绝对峰值 / RMS）
- 去均值后的过零率
- 重复节奏稳定度（去均值包络的自相关峰）

动作类别、专项与伤病标签映射到固定排序词表；伤病标签先排序、去重并归并为身体部位，因此标签书写顺序不会改变数值表示。

### 三路得分

- 动作质量：人工质量分、节奏稳定度、峰值因子合理度、时长适配度。
- 负荷风险：峰值三维负荷、动作类别机械负荷、重复次数、持续时间暴露。
- 档案匹配：专项—动作亲和度、训练年限匹配、旧伤动作相容度、周期阶段适配度。

伤病风险上调要求旧伤部位、动作应力类别和峰值负荷门槛同时命中。同一身体部位上的多条标签只上调一次。每条上调依据包含命中标签、动作类别、峰值负荷、上调前后风险与上调幅度。

### 融合与稳定落位

请求级权重必须声明为 `quality + risk + profile = 1`：

```text
fused = wq * quality + wr * (100 - load_risk) + wp * profile_match
```

负荷风险越高，风险控制分越低。融合得分保留 6 位小数后降序排列；同分候选按 `clip_id` 升序稳定落位。

重点回看清单至少覆盖：

- 所有因伤病标签触发负荷风险上调的片段。
- 精排名次相对粗排下滑达到 3 位的片段。
- 重复组尾段且人工质量低于本请求中位水平的片段。

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | 服务与数据表状态 |
| GET | `/api/meta` | 周期阶段、默认权重、词表和评分契约 |
| GET | `/api/athletes` | 运动员档案列表 |
| GET | `/api/athletes/{athlete_id}` | 单个运动员档案 |
| GET | `/api/athletes/{athlete_id}/clips` | 运动员全部训练片段摘要 |
| GET | `/api/requests` | 请求列表，支持 `athlete_id`、`phase` 筛选 |
| POST | `/api/requests` | 提交新排序请求 |
| GET | `/api/requests/{request_id}` | 请求详情、候选摘要和最近结果 |
| PATCH | `/api/requests/{request_id}` | 编辑阶段/权重并持久化重排 |
| POST | `/api/requests/{request_id}/rank` | 使用覆盖参数重排 |
| GET | `/api/requests/{request_id}/result` | 本轮完整精排结果 |
| GET | `/api/requests/{request_id}/focus` | 重点回看与风险上调清单 |
| GET | `/api/requests/{request_id}/verification` | 单个请求的独立参考核对 |
| GET | `/api/consistency` | 按运动员/阶段的一致性统计 |
| GET | `/api/verification` | 完整独立核对报告 |
| POST | `/api/verify` | 重跑全量独立核对 |

业务错误统一返回：

```json
{
  "error": {
    "code": "INVALID_WEIGHTS",
    "message": "三项权重之和必须为 1",
    "details": {}
  }
}
```

主要错误码：`ATHLETE_NOT_FOUND`、`REQUEST_NOT_FOUND`、`PROFILE_SNAPSHOT_MISSING`、`CANDIDATE_CLIP_MISSING`、`CANDIDATE_ATHLETE_MISMATCH`、`INVALID_PHASE`、`INVALID_WEIGHTS`、`INVALID_REQUEST`、`REQUEST_EXISTS`、`EMPTY_CANDIDATE_SET`。

## 独立核对

`app/reference.py` 不调用正式特征、打分或排序模块，而是直接从原始通道数组和档案快照重新计算统计量、三路得分、融合分与名次。`python verify.py` 对每个请求输出：

- 正式名次与参考名次
- 三路得分与融合分偏差
- 得分分解求和校验
- 伤病上调标签、部位与幅度对照
- 候选乱序和特征字段顺序不变性
- 64 个不同采样点/标签顺序不变性用例
- 同分 `clip_id` 升序落位检查
- 首个名次偏差请求及双方得分依据（若有）
- 按运动员/阶段的成对比较一致率、关键片段命中率和名次扰动

验收失败时命令返回非零退出码，完整报告仍写入 `artifacts/verification.json`。

## 测试

```bash
pytest -q
```

测试覆盖固定种子可重复生成、独立参考全量核对、采样与标签顺序不变性、同分稳定落位、伤病部位去重和 HTTP 业务错误码。
