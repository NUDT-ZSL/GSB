"""入口：python -m tts_app [workspace]"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scripts.gen_samples import generate, load_data  # noqa: E402
from tts_app.app import TtsApp  # noqa: E402


def main():
    workspace = sys.argv[1] if len(sys.argv) > 1 else "workspace"
    data_dir = os.path.join(workspace, "data")
    if not os.path.exists(os.path.join(data_dir, "tasks.jsonl")):
        generate(data_dir)
    speakers, tasks = load_data(data_dir)
    config_path = os.path.join("configs", "config_v2.txt")
    app = TtsApp(workspace, config_path, speakers, tasks)
    app.interactive()


if __name__ == "__main__":
    main()
