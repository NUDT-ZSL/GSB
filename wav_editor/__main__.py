"""支持 ``python -m wav_editor`` 方式调用。"""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
