"""Optional Tkinter visualisation; degrades gracefully on headless machines."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import argparse
import json

from gateway.models import LIMIT_PATHS


def load_trace(path: str | Path) -> list[dict]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def text_view(trace: list[dict]) -> str:
    lines = ["seq\tts\troute\tclient\tdecision\t" + "\t".join(LIMIT_PATHS)]
    for row in trace:
        cells = [
            str(row["seq"]),
            str(row["ts_ms"]),
            row["route"],
            row["client_id"],
            row["decision"],
        ]
        cells.extend(
            "A" if row["paths"][path]["allow"] else
            ("X" if row["paths"][path]["remaining"] is None else "R")
            for path in LIMIT_PATHS
        )
        lines.append("\t".join(cells))
    return "\n".join(lines)


def open_window(trace: list[dict], title: str) -> None:
    import tkinter as tk  # local import: Tkinter is optional

    root = tk.Tk()
    root.title(title)
    frame = tk.Frame(root)
    frame.pack(fill=tk.BOTH, expand=True)
    headers = ["seq", "ts_ms", "route", "client", "decision", *LIMIT_PATHS]
    for column, header in enumerate(headers):
        tk.Label(frame, text=header, font=("Consolas", 10, "bold")).grid(
            row=0, column=column, sticky="w"
        )
    for row_index, row in enumerate(trace, start=1):
        values = [row["seq"], row["ts_ms"], row["route"], row["client_id"], row["decision"]]
        values.extend(
            "ALLOW" if row["paths"][path]["allow"] else
            ("SKIP" if row["paths"][path]["remaining"] is None else "REJECT")
            for path in LIMIT_PATHS
        )
        for column, value in enumerate(values):
            color = "black"
            if value in ("REJECT", "E_LIMIT", "E_AUTH"):
                color = "red"
            elif value in ("ALLOW", "E_OK"):
                color = "green"
            tk.Label(frame, text=str(value), fg=color, font=("Consolas", 10)).grid(
                row=row_index, column=column, sticky="w"
            )
    root.mainloop()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Visualise a gateway trace snapshot")
    parser.add_argument("--trace", required=True)
    parser.add_argument("--headless", action="store_true",
                        help="print a text table instead of opening Tk")
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)
    trace = load_trace(args.trace)
    text = text_view(trace)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    if args.headless:
        print(text)
        return 0
    try:
        open_window(trace, f"gateway trace: {args.trace}")
    except Exception as exc:  # headless CI / missing Tk
        print(f"Tk unavailable ({exc}); headless view:\n{text}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
