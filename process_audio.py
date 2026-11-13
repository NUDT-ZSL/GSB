"""项目根便捷入口：python -I process_audio.py -i ... -o ... -c ...

隔离模式（-I）下 sys.path 不含当前目录，这里显式补上项目根，
保证从任意工作目录调用都能找到 audio_pipeline 包。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from audio_pipeline.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
