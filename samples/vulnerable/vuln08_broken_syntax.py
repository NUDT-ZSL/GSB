# 样例8：故意制造语法错误，扫描器应跳过并记入 errors，不中断其他文件检测
import os


def save_upload(upload_dir, filename, content)
    target = os.path.join(upload_dir, filename)
    with open(target, "wb") as fh
        fh.write(content)
