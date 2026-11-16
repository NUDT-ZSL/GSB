"""Entry point: python -m ddp_ckpt_lab [ui|demo|check]."""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments:
        from .ui import main as ui_main

        return ui_main([])
    command = arguments[0]
    if command == "ui":
        from .ui import main as ui_main

        return ui_main(arguments[1:])
    if command == "demo":
        from .demo import main as demo_main

        return demo_main(arguments[1:])
    if command == "check":
        from .cli import main as check_main

        return check_main(arguments[1:])
    if command in ("-h", "--help"):
        print(
            "python -m ddp_ckpt_lab [ui|demo|check] [options]\n"
            "  ui     open the Tkinter interface (default)\n"
            "  demo   run the three-scenario scripted demonstration\n"
            "  check  print environment/storage/initialization preflight JSON\n"
            "\n"
            "python -m ddp_ckpt_lab.demo  is also supported."
        )
        return 0
    from .ui import main as ui_main

    return ui_main(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
