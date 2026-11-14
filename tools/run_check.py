"""Run the checker over a local directory.

    python tools/run_check.py --settings samples/bake_settings.json \
        --output out/sample

This wrapper only exists so the tool can be started from a checkout without
installing the package.  ``python -m lightmap_seams`` does the same thing.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lightmap_seams.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
