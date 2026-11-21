"""一键演示脚本：先跑含注入演示，再跑含降级演示，均可重复且结果确定。"""

from __future__ import annotations

import main


if __name__ == "__main__":
    print(">>> demo 1/2: injection")
    main.main(["demo-injection"])
    print("\n>>> demo 2/2: degradation")
    main.main(["demo-degradation"])
