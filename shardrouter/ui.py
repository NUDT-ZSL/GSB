"""Headless-testable view model plus an optional Tkinter renderer."""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from .session import RouterSession


def build_ui_model(
    session: RouterSession,
    *,
    business_codes: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build every UI row in a stable, testable order.

    Routing happens in a detached session so opening the UI never changes the
    caller's cache, history, or invalidation state.
    """

    view_session = RouterSession(
        session.config,
        cache_capacity=max(8, len(session.config.route_units) * 3),
        history_limit=session.history_limit,
    )
    shard_rules = []
    for table in sorted(view_session.config.tables, key=lambda item: item.name):
        algorithm = table.algorithm
        params: dict[str, Any] = {}
        if algorithm.type == "mod":
            params["modulus"] = algorithm.modulus  # type: ignore[union-attr]
        elif algorithm.type == "range":
            params["boundaries"] = list(algorithm.boundaries)  # type: ignore[union-attr]
            params["boundary_policy"] = algorithm.boundary_policy  # type: ignore[union-attr]
        else:
            params["nodes"] = len(algorithm.nodes)  # type: ignore[union-attr]
            params["weight_sum"] = sum(node.weight for node in algorithm.nodes)  # type: ignore[union-attr]
        shard_rules.append(
            {
                "algorithm": algorithm.type,
                "database_count": table.database_count,
                "name": table.name,
                "parameters": params,
                "ring_version": table.ring_version,
                "rule_version": table.version,
                "shard_keys": list(table.shard_keys),
                "tables_per_database": table.tables_per_database,
            }
        )

    target_mapping = []
    reasons = []
    for unit_name in sorted(view_session.config.unit_map()):
        definition = view_session.route_unit_definition(unit_name)
        outcome = view_session.route_unit(unit_name)
        result = outcome["result"]
        targets = [
            f"{target['database']}.{target['table']}" for target in result["targets"]
        ]
        base_plan = result.get("base_plan")
        if base_plan is not None:
            reason_list = list(base_plan["routing"]["reasons"])
            reason_code = base_plan["routing"]["reason_code"]
            coverage = base_plan["routing"]["coverage"]
            broadcast = base_plan["routing"]["broadcast"]
            algorithm = base_plan["routing"]["algorithm"]
            routing_id = base_plan["predicate"]["routing_id"]
        else:
            reason_list = [f"derived by union of dependencies {result['dependencies']}"]
            reason_code = "DERIVED_UNION"
            coverage = "derived"
            broadcast = False
            algorithm = "derived"
            routing_id = result["result_id"]
        target_mapping.append(
            {
                "algorithm": algorithm,
                "broadcast": broadcast,
                "coverage": coverage,
                "reason_code": reason_code,
                "route_unit": unit_name,
                "routing_id": routing_id,
                "table": definition.table or "",
                "target_count": result["target_count"],
                "targets": targets,
            }
        )
        for index, reason in enumerate(reason_list):
            reasons.append(
                {
                    "route_unit": unit_name,
                    "reason": reason,
                    "sequence": index,
                }
            )

    dependency_edges = [
        {
            "dependency": edge["dependency"],
            "dependent": edge["dependent"],
            "display": f"{edge['dependent']} <- {edge['dependency']}",
        }
        for edge in session.dependency_edges()
    ]
    invalidation_rows = []
    for index, unit_name in enumerate(session.last_invalidation.get("order", [])):
        invalidation_rows.append(
            {
                "order": index + 1,
                "reason": session.last_invalidation.get("reason", ""),
                "unit": unit_name,
            }
        )

    code_counts: dict[str, int] = {}
    if business_codes is None:
        for event in session.history:
            code = event.get("cache_code")
            if isinstance(code, str):
                code_counts[code] = code_counts.get(code, 0) + 1
        if not code_counts:
            code_counts["OK"] = 1
    else:
        for item in business_codes:
            code = str(item.get("code", "OK"))
            code_counts[code] = code_counts.get(code, 0) + 1
    business_code_rows = [
        {"code": code, "occurrences": code_counts[code]}
        for code in sorted(code_counts)
    ]

    return {
        "business_codes": business_code_rows,
        "dependency_edges": dependency_edges,
        "invalidation_rows": invalidation_rows,
        "reasons": reasons,
        "shard_rules": shard_rules,
        "target_mapping": target_mapping,
    }


def launch_ui(
    session: RouterSession,
    *,
    model: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Launch Tkinter, or return a deterministic skip result when unavailable."""

    view_model = dict(model) if model is not None else build_ui_model(session)
    try:
        import tkinter as tk
        from tkinter import ttk
    except Exception as error:  # ImportError and platform-specific load errors
        return {
            "code": "UI_SKIPPED",
            "reason": f"tkinter unavailable: {type(error).__name__}",
            "status": "skipped",
        }
    try:
        root = tk.Tk()
    except Exception as error:
        return {
            "code": "UI_SKIPPED",
            "reason": f"display unavailable: {type(error).__name__}",
            "status": "skipped",
        }

    root.title("Shard Router Teaching Console")
    root.geometry("1180x760")
    notebook = ttk.Notebook(root)
    notebook.pack(fill="both", expand=True, padx=8, pady=8)

    def add_table(tab_title: str, rows: list[dict[str, Any]], columns: list[str]) -> None:
        frame = ttk.Frame(notebook)
        notebook.add(frame, text=tab_title)
        tree = ttk.Treeview(frame, columns=columns, show="headings")
        for column in columns:
            tree.heading(column, text=column)
            tree.column(column, width=150, stretch=True)
        for row in rows:
            values = [row.get(column, "") for column in columns]
            rendered = [", ".join(value) if isinstance(value, list) else value for value in values]
            tree.insert("", "end", values=rendered)
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)
        tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

    add_table(
        "Shard Rules",
        list(view_model["shard_rules"]),
        ["name", "algorithm", "shard_keys", "database_count", "tables_per_database", "rule_version", "ring_version", "parameters"],
    )
    add_table(
        "Targets",
        list(view_model["target_mapping"]),
        ["route_unit", "table", "algorithm", "broadcast", "coverage", "reason_code", "target_count", "targets", "routing_id"],
    )
    add_table(
        "Reasons",
        list(view_model["reasons"]),
        ["route_unit", "sequence", "reason"],
    )
    add_table(
        "Dependencies",
        list(view_model["dependency_edges"]),
        ["dependent", "dependency", "display"],
    )
    add_table(
        "Invalidation",
        list(view_model["invalidation_rows"]),
        ["order", "unit", "reason"],
    )
    add_table(
        "Business Codes",
        list(view_model["business_codes"]),
        ["code", "occurrences"],
    )
    root.mainloop()
    return {"code": "OK", "status": "closed"}
