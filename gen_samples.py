# -*- coding: utf-8 -*-
"""确定性生成网格 JSON 业务样例（无随机数，纯公式，可复现）。

生成到 samples/ 目录：
  sample_normal.json               正常样例（61x81，10 级）
  sample_nan.json                  含 NaN(null) 无数据单元格
  sample_levels_out_of_range.json  含超出矩阵值域的级别
  sample_mixed.json                NaN + 超范围级别混合异常
  bad_missing_field.json           缺少必需字段 levels（非法样例）
  bad_dim_mismatch.json            rows 与矩阵实际行数不符（非法样例）
"""
import json
import math
import os

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "samples")

ROWS, COLS = 61, 81
X_RANGE = [0.0, 8.0]
Y_RANGE = [-3.0, 5.0]


def height(x, y):
    """确定性的地形高度函数：两座高斯峰/谷 + 缓坡 + 波纹。"""
    return (3.0 * math.exp(-(((x - 2.0) ** 2) / 2.0 + ((y - 1.0) ** 2) / 1.5))
            - 2.0 * math.exp(-(((x - 6.0) ** 2) / 1.5 + ((y + 1.0) ** 2) / 2.0))
            + 0.4 * math.sin(1.3 * x) * math.cos(0.9 * y)
            + 0.15 * x - 0.1 * y)


def build_matrix(nan_cells=()):
    nan_cells = set(nan_cells)
    matrix = []
    for j in range(ROWS):
        y = Y_RANGE[0] + j * (Y_RANGE[1] - Y_RANGE[0]) / (ROWS - 1)
        row = []
        for i in range(COLS):
            x = X_RANGE[0] + i * (X_RANGE[1] - X_RANGE[0]) / (COLS - 1)
            if (i, j) in nan_cells:
                row.append(None)  # JSON null = 无数据单元格
            else:
                row.append(round(height(x, y), 6))
        matrix.append(row)
    return matrix


def linspace(a, b, n):
    return [a + (b - a) * k / (n - 1) for k in range(n)]


def nan_cell_set():
    cells = set()
    # 中央矩形空洞：x∈[3.0, 3.7], y∈[-0.333, 0.6]
    for i in range(30, 38):
        for j in range(20, 28):
            cells.add((i, j))
    # 散布点
    for j in range(ROWS):
        for i in range(COLS):
            if (i * 7 + j * 13) % 131 == 0:
                cells.add((i, j))
    return cells


def write(name, obj):
    path = os.path.join(OUT_DIR, name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    print(f"已生成 {name}")


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    matrix = build_matrix()
    finite = [v for row in matrix for v in row if v is not None]
    zmin, zmax = min(finite), max(finite)
    span = zmax - zmin
    # 10 个级别，全部落在数据值域内
    levels = [round(v, 2) for v in
              linspace(zmin + 0.08 * span, zmax - 0.05 * span, 10)]

    base = {
        "rows": ROWS,
        "cols": COLS,
        "x_range": X_RANGE,
        "y_range": Y_RANGE,
        "matrix": matrix,
        "levels": levels,
    }
    write("sample_normal.json", base)

    nan_cells = nan_cell_set()
    matrix_nan = build_matrix(nan_cells)
    write("sample_nan.json", dict(base, matrix=matrix_nan))

    levels_oor = levels + [round(zmin - 2.0, 2), round(zmax + 2.0, 2)]
    write("sample_levels_out_of_range.json", dict(base, levels=levels_oor))

    write("sample_mixed.json", dict(base, matrix=matrix_nan, levels=levels_oor))

    bad_missing = dict(base)
    del bad_missing["levels"]
    write("bad_missing_field.json", bad_missing)

    write("bad_dim_mismatch.json", dict(base, rows=ROWS + 1))

    print(f"数据值域: [{zmin:.4f}, {zmax:.4f}]")
    print(f"正常级别: {levels}")
    print(f"超范围级别: {levels_oor[-2:]}")


if __name__ == "__main__":
    main()
