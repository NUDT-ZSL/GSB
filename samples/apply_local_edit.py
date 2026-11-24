from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from terrain_baker.sample_generator import apply_edit_to_directory, edit_dict


if __name__ == "__main__":
    sample_dir = Path(__file__).resolve().parent
    edit_path = sample_dir / "edits" / "local_ridge_valley.json"
    if edit_path.exists():
        edit = json.loads(edit_path.read_text(encoding="utf-8"))
    else:
        edit = edit_dict()
    result = apply_edit_to_directory(sample_dir, edit)
    print(json.dumps({"ok": True, **result}, ensure_ascii=False, indent=2))
