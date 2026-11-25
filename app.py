"""Local Flask entry point: `python app.py` or `flask --app app run`."""
from __future__ import annotations

import os

from health_machine.web import create_app

app = create_app(os.environ.get("HSM_DB", "state/hsm.sqlite"))


if __name__ == "__main__":
    app.run(
        host=os.environ.get("HSM_HOST", "127.0.0.1"),
        port=int(os.environ.get("HSM_PORT", "5050")),
        debug=False,
    )
