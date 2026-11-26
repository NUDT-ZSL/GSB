"""Small interactive editor over pipeline status, runs and budget overrides."""

from __future__ import annotations

import cmd
import json
import shlex
from typing import Any

from .errors import BusinessError
from .utils import json_pretty


class ConstructionEditor(cmd.Cmd):
    intro = (
        "LLM 指令数据构造编辑器。输入 help 查看命令；status、run、resume、"
        "stage <name>、set <key> <value>、budget。"
    )
    prompt = "ifdata> "

    def __init__(self, engine: Any) -> None:
        super().__init__()
        self.engine = engine
        self.overrides: dict[str, int] = {}

    def _show(self, value: Any) -> None:
        self.stdout.write(json_pretty(value))

    def do_status(self, line: str) -> None:
        """Show the current strategy and committed snapshot chain."""
        self._show(self.engine.status())

    def do_strategy(self, line: str) -> None:
        """Show the active strategy metadata."""
        self._show(self.engine.strategy)

    def do_stage(self, line: str) -> None:
        """stage <ingest|template|quality|dedup|sampling|export>"""
        tokens = shlex.split(line)
        if len(tokens) != 1:
            self.stdout.write("usage: stage <name>\n")
            return
        try:
            self._show(self.engine.show_stage(tokens[0]))
        except BusinessError as exc:
            self._show({"status": "error", **exc.to_dict()})

    def do_set(self, line: str) -> None:
        """set workers|compare|memory <integer>"""
        tokens = shlex.split(line)
        mapping = {
            "workers": "workers",
            "compare": "compare_budget",
            "memory": "memory_limit_bytes",
        }
        if len(tokens) != 2 or tokens[0] not in mapping:
            self.stdout.write("usage: set workers|compare|memory <integer>\n")
            return
        try:
            self.overrides[mapping[tokens[0]]] = int(tokens[1])
        except ValueError:
            self.stdout.write("value must be an integer\n")
            return
        self.stdout.write(json.dumps(self.overrides, sort_keys=True) + "\n")

    def do_unset(self, line: str) -> None:
        """unset all temporary budget overrides."""
        self.overrides.clear()
        self.stdout.write("overrides cleared\n")

    def do_budget(self, line: str) -> None:
        """Show active temporary budget overrides."""
        self._show(self.overrides)

    def _run(self, *, resume: bool) -> None:
        try:
            report = self.engine.run(
                resume=resume,
                requested_workers=self.overrides.get("workers"),
                compare_budget=self.overrides.get("compare_budget"),
                memory_limit_bytes=self.overrides.get("memory_limit_bytes"),
            )
            self._show(report)
        except BusinessError as exc:
            self._show({"status": "error", **exc.to_dict()})

    def do_run(self, line: str) -> None:
        """Run remaining stages, resuming any committed chain."""
        self._run(resume=True)

    def do_resume(self, line: str) -> None:
        """Resume from the last committed stage."""
        self._run(resume=True)

    def do_quit(self, line: str) -> bool:
        """Exit the editor."""
        return True

    def do_EOF(self, line: str) -> bool:
        self.stdout.write("\n")
        return True


def run_repl(engine: Any) -> int:
    try:
        ConstructionEditor(engine).cmdloop()
    except KeyboardInterrupt:
        print("\ninterrupted")
    return 0

