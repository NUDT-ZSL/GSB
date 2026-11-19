from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Any

from .compiler import compile_templates
from .equivalence import adjudicate
from .errors import BusinessError
from .expansion import expand_query
from .models import CompilerConfig
from .pipeline import (
    _injections,
    compile_dialect_bundle,
    report_summary,
    run_demo,
)
from .readers import read_queries, read_template_file, read_template_text
from .snapshot import load_current_snapshot


def _node_lines(node: dict[str, Any], depth: int = 0) -> list[str]:
    indent = "  " * depth
    source = node.get("source", {})
    line = (
        f"{indent}- {node['kind']} {node['node_id']} "
        f"@line {source.get('line', '?')} field={source.get('field', '')}"
    )
    if node["kind"] == "text":
        line += f" text={node['text']!r}"
    elif node["kind"] == "slot":
        line += f" slot={node['name']} type={node['slot_type']}"
    elif node["kind"] == "slot_declaration":
        line += f" slot={node['name']} type={node['type']} declared={node['declared']}"
    elif node["kind"] == "reference":
        line += f" target={node['target']}"
    elif node["kind"] == "condition":
        line += f" condition={json.dumps(node['condition'], ensure_ascii=False)}"
    elif node["kind"] == "rule":
        line += f" id={node['rule_id']} weight={node['weight']} depth={node['max_depth']}"
    lines = [line]
    for child in node.get("parts", []):
        lines.extend(_node_lines(child, depth + 1))
    for child in node.get("slots", []):
        lines.extend(_node_lines(child, depth + 1))
    if node["kind"] == "condition":
        lines.append(f"{indent}  then:")
        lines.extend(_node_lines(node["then"], depth + 2))
        lines.append(f"{indent}  else:")
        lines.extend(_node_lines(node["else"], depth + 2))
    for key in ("pattern", "replacement"):
        if isinstance(node.get(key), dict):
            lines.extend(_node_lines(node[key], depth + 1))
    return lines


class InteractiveApp:
    def __init__(self, root: Path, config: CompilerConfig | None = None) -> None:
        self.root = root
        self.samples = root / "samples"
        self.config = config or CompilerConfig()
        self.snapshot, self.version_index = load_current_snapshot(self.samples / "versions.json")
        allowed = {str(item["version"]) for item in self.version_index["snapshots"]}
        self.queries = read_queries(self.samples / "queries.jsonl", allowed)
        self.query_by_id = {query["query_id"]: query for query in self.queries}
        self.compiled = {
            dialect: compile_templates(
                read_template_text(
                    self.snapshot.sources[dialect],
                    f"{self.snapshot.manifest_path.name}#{dialect}",
                    self.snapshot.slot_types,
                    self.config,
                ),
                self.config,
            )
            for dialect in ("v1", "v2", "v3")
        }
        self.dialect = "v1"
        self.last_errors: list[dict[str, Any]] = []

    def _remember(self, payload: dict[str, Any]) -> None:
        error = payload.get("error")
        if isinstance(error, dict) and error.get("code"):
            self.last_errors.append(error)
            self.last_errors = self.last_errors[-20:]

    def status_line(self) -> str:
        last_code = self.last_errors[-1]["code"] if self.last_errors else "none"
        return (
            f"snapshot=v{self.snapshot.version} dialect={self.dialect} "
            f"queries={len(self.queries)} last_error={last_code}"
        )

    def _help(self) -> str:
        return "\n".join(
            [
                "queries                         列出查询",
                "snapshot                        显示当前快照与版本表",
                "view [v1|v2|v3]                 切换/查看方言源码",
                "ir [v1|v2|v3] [rule_id]         查看 IR 节点树或单条规则",
                "candidates <query_id> [v1|v2|v3|all]",
                "equivalence                     运行核心三方言裁决",
                "advanced                        运行高级条件/引用漂移裁决",
                "migrate <v1->v2|v2->v1|v2->v3>  运行迁移并显示补全/丢弃明细",
                "anomalies                       运行全部注入异常",
                "demo                            运行完整报告摘要",
                "errors                          查看最近业务错误",
                "quit                            退出",
            ]
        )

    def execute(self, command_line: str) -> str:
        try:
            args = shlex.split(command_line)
        except ValueError as exc:
            return json.dumps(
                {"error": {"code": "E_FORMAT_CORRUPT", "message": str(exc)}},
                ensure_ascii=False,
                indent=2,
            )
        if not args:
            return ""
        command = args[0].lower()
        try:
            if command in ("quit", "exit", "q"):
                return "__QUIT__"
            if command in ("help", "h", "?"):
                return self._help()
            if command == "queries":
                return json.dumps(self.queries, ensure_ascii=False, indent=2)
            if command == "snapshot":
                return json.dumps(
                    {
                        "snapshot": self.snapshot.summary(),
                        "versions": self.version_index,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            if command in ("view", "switch"):
                if len(args) > 1:
                    dialect = args[1].lower()
                    if dialect not in ("v1", "v2", "v3"):
                        raise BusinessError(
                            "E_VERSION_UNSUPPORTED",
                            f"unknown dialect view {dialect}",
                            field="dialect",
                            details={"supported": ["v1", "v2", "v3"]},
                        )
                    self.dialect = dialect
                return f"[{self.dialect}]\n" + self.snapshot.sources[self.dialect]
            if command == "ir":
                dialect = args[1].lower() if len(args) > 1 else self.dialect
                if dialect not in ("v1", "v2", "v3"):
                    raise BusinessError(
                        "E_VERSION_UNSUPPORTED",
                        f"unknown dialect {dialect}",
                        field="dialect",
                    )
                self.dialect = dialect
                compiled = self.compiled[dialect]
                if len(args) > 2:
                    rule_id = args[2]
                    rule = next(
                        (item for item in compiled["rules"] if item["rule_id"] == rule_id),
                        None,
                    )
                    if rule is None:
                        raise BusinessError(
                            "E_FORMAT_CORRUPT",
                            f"rule {rule_id} does not exist in {dialect}",
                            field="rule_id",
                            details={"rule_id": rule_id, "dialect": dialect},
                        )
                    return json.dumps(rule, ensure_ascii=False, indent=2)
                lines = [f"[{dialect} IR] checksum={compiled['checksum']}"]
                for rule in compiled["rules"]:
                    lines.extend(_node_lines(rule))
                return "\n".join(lines)
            if command == "candidates":
                if len(args) < 2 or args[1] not in self.query_by_id:
                    raise BusinessError(
                        "E_FORMAT_CORRUPT",
                        "candidates requires a known query_id",
                        field="query_id",
                        details={"known_query_ids": sorted(self.query_by_id)},
                    )
                query = self.query_by_id[args[1]]
                target = args[2].lower() if len(args) > 2 else self.dialect
                dialects = ("v1", "v2", "v3") if target == "all" else (target,)
                if any(dialect not in ("v1", "v2", "v3") for dialect in dialects):
                    raise BusinessError(
                        "E_VERSION_UNSUPPORTED",
                        f"unknown dialect {target}",
                        field="dialect",
                    )
                return json.dumps(
                    {
                        dialect: expand_query(self.compiled[dialect], query, self.config)
                        for dialect in dialects
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            if command == "equivalence":
                report = adjudicate(self.compiled, self.queries, self.config)
                if report.get("code"):
                    self._remember(report)
                return json.dumps(
                    {key: value for key, value in report.items() if key != "expansion"},
                    ensure_ascii=False,
                    indent=2,
                )
            if command == "advanced":
                advanced = compile_dialect_bundle(self.samples / "advanced", self.config)
                report = adjudicate(advanced, self.queries, self.config)
                if report.get("code"):
                    self._remember(report)
                return json.dumps(
                    {key: value for key, value in report.items() if key != "expansion"},
                    ensure_ascii=False,
                    indent=2,
                )
            if command == "migrate":
                if len(args) < 2:
                    raise BusinessError(
                        "E_FORMAT_CORRUPT",
                        "migrate requires v1->v2, v2->v1 or v2->v3",
                        field="migration",
                    )
                from .migration import migrate_v1_to_v2, migrate_v2_to_v1, migrate_v2_to_v3

                slot_types = self.snapshot.slot_types
                if args[1] == "v1->v2":
                    parsed = read_template_file(self.samples / "v1.txt", slot_types, self.config)
                    return json.dumps(
                        migrate_v1_to_v2(parsed, self.config), ensure_ascii=False, indent=2
                    )
                if args[1] == "v2->v1":
                    parsed = read_template_file(self.samples / "v2.txt", slot_types, self.config)
                    return json.dumps(migrate_v2_to_v1(parsed), ensure_ascii=False, indent=2)
                if args[1] == "v2->v3":
                    parsed = read_template_file(self.samples / "v2.txt", slot_types, self.config)
                    return json.dumps(migrate_v2_to_v3(parsed), ensure_ascii=False, indent=2)
                raise BusinessError(
                    "E_FORMAT_CORRUPT",
                    f"unsupported migration {args[1]}",
                    field="migration",
                )
            if command == "anomalies":
                injections = _injections(self.samples, self.config)
                for item in injections:
                    if item.get("error"):
                        self.last_errors.append(item["error"])
                self.last_errors = self.last_errors[-20:]
                return json.dumps(injections, ensure_ascii=False, indent=2)
            if command == "demo":
                report = run_demo(self.root, self.config)
                for item in report.get("recent_business_errors", []):
                    self.last_errors.append(item)
                self.last_errors = self.last_errors[-20:]
                return json.dumps(report_summary(report), ensure_ascii=False, indent=2)
            if command == "errors":
                return json.dumps(self.last_errors, ensure_ascii=False, indent=2)
            raise BusinessError(
                "E_FORMAT_CORRUPT",
                f"unknown command {command}",
                field="command",
                details={"command": command},
            )
        except BusinessError as exc:
            payload = exc.to_dict()
            self.last_errors.append(payload)
            self.last_errors = self.last_errors[-20:]
            return json.dumps({"error": payload}, ensure_ascii=False, indent=2)

    def run(self) -> int:
        print("Query Rewrite Template Lab - interactive")
        print(self.status_line())
        print("Type 'help' for commands.")
        while True:
            last_code = self.last_errors[-1]["code"] if self.last_errors else "none"
            try:
                command = input(f"qrw[{self.dialect}|{last_code}]> ")
            except (EOFError, KeyboardInterrupt):
                print()
                return 0
            output = self.execute(command)
            if output == "__QUIT__":
                return 0
            if output:
                print(output)

