"""公共模块：路径常量与确定性样本生成。

所有样本内容由代码确定性生成（固定字符串、固定字节序列），
不依赖网络、时间戳或随机数，多次运行结果完全一致。
"""
import os

BASE = os.path.dirname(os.path.abspath(__file__))
SAMPLES_DIR = os.path.join(BASE, "samples")
UPLOADS = os.path.join(SAMPLES_DIR, "uploads")          # 服务允许访问的目录
OUTSIDE = os.path.join(SAMPLES_DIR, "outside")          # uploads 之外的目录
OUTSIDE_FILE = os.path.join(OUTSIDE, "secret.txt")      # 目录外机密样本
REPORTS_DIR = os.path.join(BASE, "reports")

# 目录外样本中的确定性标记，检测器用它判断是否越权读取成功
SECRET_MARKER = "OUTSIDE-SECRET-7f3d9a2c"

# uploads 内的文本样本（文件名 -> 内容，均为合法 UTF-8）
TEXT_SAMPLES = {
    "hello.txt": "你好，文档预览服务！\n这是 uploads 内的公开样本 hello.txt。\n",
    "report.txt": "季度报告（样本）\n行1：收入\n行2：支出\n",
    "notes.md": "# 笔记\n- 条目甲\n- 条目乙\n",
}

# uploads 内的非 UTF-8 二进制样本（固定字节序列）
BINARY_SAMPLE = "binary.bin"


def generate_samples():
    """确定性生成 uploads 样本与目录外样本，返回样本清单。"""
    os.makedirs(UPLOADS, exist_ok=True)
    os.makedirs(OUTSIDE, exist_ok=True)
    os.makedirs(REPORTS_DIR, exist_ok=True)

    for name, content in TEXT_SAMPLES.items():
        with open(os.path.join(UPLOADS, name), "w", encoding="utf-8", newline="\n") as f:
            f.write(content)

    with open(os.path.join(UPLOADS, BINARY_SAMPLE), "wb") as f:
        f.write(bytes(range(256)))  # 含大量非法 UTF-8 字节

    with open(OUTSIDE_FILE, "w", encoding="utf-8", newline="\n") as f:
        f.write(SECRET_MARKER + "\n")
        f.write("这是 uploads 目录之外的机密样本，预览服务不应读到它。\n")

    return {
        "uploads": sorted(TEXT_SAMPLES) + [BINARY_SAMPLE],
        "outside_file": OUTSIDE_FILE,
    }


def outside_relative_name():
    """从 uploads 指向目录外样本的相对路径（统一用正斜杠，便于放进 URL）。"""
    rel = os.path.relpath(OUTSIDE_FILE, UPLOADS)
    return rel.replace(os.sep, "/")


if __name__ == "__main__":
    info = generate_samples()
    print("样本已生成：")
    print("  uploads:", ", ".join(info["uploads"]))
    print("  目录外样本:", info["outside_file"])
