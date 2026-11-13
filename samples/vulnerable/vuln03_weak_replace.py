# 样例3：黑名单替换 "../"，可被 ....// 嵌套绕过（中危）
import os


def save_upload(upload_dir, filename, content):
    # 一次替换：....//  -> ../
    safe_name = filename.replace("../", "")
    target = os.path.join(upload_dir, safe_name)
    with open(target, "wb") as fh:
        fh.write(content)
    return target
