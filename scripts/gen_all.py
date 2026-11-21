"""本地确定性样本生成脚本：生成清单、词表两版本与注入清单。"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from detwb.workbench import main  # noqa: E402

if __name__ == "__main__":
    data_dir = sys.argv[1] if len(sys.argv) > 1 else "data"
    raise SystemExit(main(["--data", data_dir, "gen"]))
