# 样例7：用户文件名直接拼入读取路径，任意文件读取（低危）
from pathlib import Path


def save_upload(upload_dir, filename, content):
    # 读取侧汇聚点 Path.read_bytes：可读取上传目录之外的文件
    target = Path(upload_dir) / filename
    data = target.read_bytes()
    return data
