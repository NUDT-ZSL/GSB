"""工作台命令行入口。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .cli import main


if __name__ == "__main__":
    sys.exit(main(argv=sys.argv[1:]))
