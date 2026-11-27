# Schema 归一约定

## 版本识别

按顺序识别 `schema_version`、`schema`、`version`。接受 `v1/v2/v3`、`1/2/3`、`schema-1/2/3` 等写法；未知版本直接 `E_SCHEMA`。

## 必填字段

### 工程

- v1：`project_id`、`domain_id`、`connections`、`mapping_rules`、`address_pools`、`timeline`、`horizon`
- v2：`project.id`、`project.domain`、`connections`、`rules`、`pools`、`events`、`horizon`
- v3：`meta.project`、`meta.domain`、`meta.horizon`、`conns`、`map_rules`、`pools`、`log`

### 连接

连接 ID、大棚、方向、跟踪超时、五元组的源/目的 IP、源/目的端口、协议均为必填。

### 规则

规则 ID、大棚、优先级、协议、方向、源 CIDR、目的端口起止、目标地址池均为必填。源端口起止为可选；缺失表示不限制源端口。

### 地址池

池 ID、大棚、外网 IP、端口起止均为必填。

### 事件

事件 ID、逻辑时刻、同时刻序号、事件类型为必填。会话事件还须有连接 ID。策略变更事件还须有合法 `operation` 及该操作所需字段。

## 归一模型

```text
{
  canonical_schema: "natcheck-canonical/1",
  source_schema: "v1|v2|v3",
  project_id, domain_id, horizon,
  connections: [{
    connection_id, greenhouse_id, direction, tracking_timeout,
    five_tuple: {src_ip, src_port, dst_ip, dst_port, protocol}
  }],
  rules: [{
    rule_id, greenhouse_id, priority: [int...], protocol, direction,
    src_cidr, dst_port_start, dst_port_end,
    src_port_start|null, src_port_end|null, pool_id
  }],
  pools: [{pool_id, greenhouse_id, external_ip, port_start, port_end}],
  events: [{
    event_id, time, seq, type,
    connection_id? | operation + operation fields
  }],
  provenance: [{entity_type, entity_id, field, raw_path, raw_value, normalized}]
}
```

`provenance` 保存每个归一字段的原始路径与原值。跨版本验收使用它生成字段级差异清单；即使原始表达不同，只要 `normalized` 相同的 `normalized_equal` 即为 `true`。

## 优先级

`normalized_priority` 提取整数或字符串中的数字段。例如：

```text
10            -> [10]
tier.10.0     -> [10]
zone.edge.10  -> [10]
tier.2.1      -> [2, 1]
```

规则选择键为 `(priority_tuple, rule_id)`，升序优先。比较不依赖规则列表、字典插入或地址池扫描起点以外的输入顺序。

## 端口表达式

十进制形式直接接受整数。基数+偏移接受：

```json
{"base": 50000, "offset": 3}
{"base_port": 50000, "offset_start": 3}
```

`base` 与 `offset` 必须是非负整数，结果必须落在闭区间 `[1, 65535]`。规则和地址池的 v3 范围通过 `dst_port_base + dst_port_offset_*` 或 `port_base + offset_*` 计算。

## 事件归一

类型同义词：

- `new`：`new|connect|open|create|session_start|session_new`
- `established`：`established|establish|accept|active|session_established`
- `teardown`：`teardown|close|disconnect|destroy|remove|session_end`
- `timeout`：`timeout|expire|expired|session_timeout`
- `tick`：`tick|advance|sweep|clock|checkpoint`
- `policy_change`：`policy|policy_change|config|config_change|rule_change|pool_change`

策略操作同义词最终归一为 `set_rule_priority`、`upsert_rule`、`delete_rule`、`upsert_pool`、`delete_pool`。

## 拒绝规则

`E_SCHEMA` 会包含 `schema`、`entity_type`、`entity_id`、`field`、`reason` 与 `source`。下列情况整版拒绝：

- 必填字段缺失或类型不符。
- IP/CIDR 非法，协议/方向无法识别。
- 十进制端口或基数+偏移结果越界。
- 端口/偏移窗口起点大于终点。
- 超时非正整数，时刻/序号为负。
- 连接、规则、地址池 ID 重复。
- 规则引用未知地址池。
- 事件引用未知连接。
- 同一 `(time, seq)` 重复。
- `horizon` 早于事件时刻。

