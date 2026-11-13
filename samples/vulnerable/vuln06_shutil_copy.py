# 样例6：临时文件经 shutil.copyfile 复制到用户指定目标（高危）
import os
import shutil
import tempfile


def save_upload(upload_dir, filename, content):
    tmp = tempfile.NamedTemporaryFile(delete=False)
    try:
        tmp.write(content)
        tmp.close()
        target = os.path.join(upload_dir, filename)
        shutil.copyfile(tmp.name, target)
    finally:
        os.unlink(tmp.name)
    return target
