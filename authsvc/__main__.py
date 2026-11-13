"""``python -m authsvc --version {legacy,fixed} [--port N]`` 入口。"""

from __future__ import annotations

import sys

from .server import main

if __name__ == "__main__":
    sys.exit(main())
