from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from audit_forge import store  # noqa: E402

original_replace = store.replace_file


def crash_before_checkpoint_replace(source: str | Path, destination: str | Path) -> None:
    if Path(destination).name == "checkpoint.json":
        os._exit(137)
    original_replace(source, destination)


store.replace_file = crash_before_checkpoint_replace

from audit_forge.cli import main  # noqa: E402

raise SystemExit(main(sys.argv[1:]))

