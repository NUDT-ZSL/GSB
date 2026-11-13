# 样例2：os.path.join 直接拼接用户文件名（高危；join 不会阻止绝对路径/../逃逸）
import os


def save_upload(upload_dir, filename, content):
    target = os.path.join(upload_dir, filename)
    with open(target, "wb") as fh:
        fh.write(content)
    return target
