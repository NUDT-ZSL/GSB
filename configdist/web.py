"""Minimal Flask status API for the platform."""

from flask import Flask, jsonify

from .store import Store
from .verify import verify_db


def create_app(db_path):
    app = Flask(__name__)

    @app.get("/status")
    def status():
        store = Store(db_path)
        try:
            transitions = store.transitions()
            return jsonify({
                "committed_version": store.committed_version(),
                "snapshot_version": store.snapshot_version(),
                "state": transitions[-1]["to"] if transitions else "init",
                "node_status": store.node_statuses(),
                "receipts": len(store.receipts()),
            })
        finally:
            store.close()

    @app.get("/verify")
    def verify():
        return jsonify(verify_db(db_path))

    return app


if __name__ == "__main__":
    import sys

    create_app(sys.argv[1] if len(sys.argv) > 1 else "out/session.db").run(
        host="127.0.0.1", port=8765)
