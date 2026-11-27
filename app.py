"""Run the local welding listening-training Flask service."""

from __future__ import annotations

import os

from weld_train import create_app


app = create_app()


if __name__ == "__main__":
    app.run(
        host=os.environ.get("WELD_HOST", "127.0.0.1"),
        port=int(os.environ.get("WELD_PORT", "8010")),
        debug=False,
    )
