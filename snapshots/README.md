# 快照清单

`catalog.json` 记录不同打印线数/墨点尺寸/目标密度下的快照，以及局部重算差异报告入口。

推荐目录：

- `screen_50lpi_default_density`：50 LPI 默认密度全量快照。
- `screen_75lpi_default_density`：75 LPI、部分零件密度覆盖后的全量快照。
- `local_recompute_escape_density_boost`：零件 1 目标密度提升后的局部重算及全量对照。

每个快照的 `snapshot.json` 是权威入口，`reports/` 下的 JSON 可直接用于印前复核。
