from __future__ import annotations

import json

from app.generator import generate_dataset


if __name__ == "__main__":
    print(json.dumps(generate_dataset(), ensure_ascii=False, indent=2))
