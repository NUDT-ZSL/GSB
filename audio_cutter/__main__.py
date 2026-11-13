"""支持 python -m audio_cutter 直接进入 CLI。"""

from .cutter import main

if __name__ == "__main__":
    raise SystemExit(main())
