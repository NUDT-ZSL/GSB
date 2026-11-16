"""Declarative workspace: group registry, views, drift and convergence."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .compiler import build_graph, compile_view
from .errors import DeclarationError, ReferenceError
from .graph import DependencyGraph
from .loader import load_actual_snapshot, load_declaration_directory
from .models import Artifact, ConvergenceAction, DeclarationVersion, Drift


@dataclass
class View:
    """One immutable workspace view for a declaration version combination."""

    view_id: int
    group_versions: dict[str, int]
    declarations: tuple[DeclarationVersion, ...]
    artifacts: dict[str, Artifact]
    graph: DependencyGraph
    graph_version: int
    changed_keys: tuple[str, ...]
    invalidated_keys: tuple[str, ...]


@dataclass
class Workspace:
    """In-memory workbench state with versioned declaration history."""

    declaration_history: dict[str, list[DeclarationVersion]] = field(default_factory=dict)
    actual_state: dict[str, dict[str, Any]] = field(default_factory=dict)
    current_view: View | None = None
    views: list[View] = field(default_factory=list)
    last_drifts: list[Drift] = field(default_factory=list)
    last_plan: list[ConvergenceAction] = field(default_factory=list)
    actual_source: str = ""

    # ------------------------------------------------------------------ load
    @classmethod
    def from_directories(
        cls,
        declaration_dir: str | Path,
        actual_snapshot: str | Path,
    ) -> "Workspace":
        workspace = cls()
        workspace.load_declarations(declaration_dir)
        workspace.load_actual(actual_snapshot)
        return workspace

    def load_actual(self, path: str | Path) -> None:
        self.actual_state = load_actual_snapshot(path)
        self.actual_source = str(Path(path))

    def load_declarations(self, directory: str | Path) -> None:
        """Load a directory of declaration files and build the initial view."""
        groups = load_declaration_directory(directory)
        candidate: dict[str, list[DeclarationVersion]] = {}
        for group_name, documents in groups.items():
            self._validate_group_chain(group_name, documents)
            candidate[group_name] = documents
        latest_by_group = {name: docs[-1] for name, docs in candidate.items()}

        # depends_on must resolve across the full set of declaration groups.
        for name, documents in sorted(candidate.items()):
            latest = documents[-1]
            for dep_group, dep_version in latest.depends_on.items():
                if dep_group not in latest_by_group:
                    raise DeclarationError(
                        f"depends_on group '{dep_group}' is not loaded",
                        file=latest.source_file,
                        field_path=f"depends_on.{dep_group}",
                    )
                available = {doc.version for doc in candidate[dep_group]}
                if dep_version not in available:
                    raise DeclarationError(
                        f"depends_on requires {dep_group} version {dep_version}, "
                        f"available versions: {sorted(available)}",
                        file=latest.source_file,
                        field_path=f"depends_on.{dep_group}",
                    )

        # Globally unique resource keys.
        seen: dict[str, str] = {}
        for name, documents in sorted(latest_by_group.items()):
            for resource in documents.resources:
                if resource.key in seen:
                    raise DeclarationError(
                        f"resource key '{resource.key}' duplicated across groups "
                        f"'{seen[resource.key]}' and '{name}'",
                        file=documents.source_file,
                    )
                seen[resource.key] = name

        # Reference existence and global cycle check.
        for name, documents in sorted(latest_by_group.items()):
            for resource in documents.resources:
                for reference in resource.references:
                    if reference not in seen:
                        raise DeclarationError(
                            f"reference target '{reference}' does not exist",
                            file=documents.source_file,
                            field_path=f"resource[{resource.key}].references",
                        )
        build_graph(list(latest_by_group.values()))
        self.declaration_history = dict(sorted(candidate.items()))
        self._rebuild_view()

    # -------------------------------------------------------------- registry
    def register_group(
        self,
        group_name: str,
        documents: list[DeclarationVersion],
    ) -> None:
        """Validate and store one full declaration group history.

        Global checks (duplicate resource keys across groups, reference
        existence, dependency version availability) run here and reject the
        whole registration on failure with E_DECL.
        """
        ordered = sorted(documents, key=lambda item: item.version)
        self._validate_group_chain(group_name, ordered)

        latest = ordered[-1]
        candidate_groups = dict(self.declaration_history)
        candidate_groups[group_name] = ordered
        latest_by_group = {
            name: docs[-1] for name, docs in candidate_groups.items()
        }

        # depends_on must resolve to a currently known group/version.
        for dep_group, dep_version in latest.depends_on.items():
            if dep_group not in latest_by_group:
                raise DeclarationError(
                    f"depends_on group '{dep_group}' is not loaded",
                    file=latest.source_file,
                    field_path=f"depends_on.{dep_group}",
                )
            available = {doc.version for doc in candidate_groups[dep_group]}
            if dep_version not in available:
                raise DeclarationError(
                    f"depends_on requires {dep_group} version {dep_version}, "
                    f"available versions: {sorted(available)}",
                    file=latest.source_file,
                    field_path=f"depends_on.{dep_group}",
                )

        # Globally unique resource keys.
        seen: dict[str, str] = {}
        for name, docs in latest_by_group.items():
            for resource in docs.resources:
                if resource.key in seen and seen[resource.key] != name:
                    raise DeclarationError(
                        f"resource key '{resource.key}' duplicated across "
                        f"groups '{seen[resource.key]}' and '{name}'",
                        file=docs.source_file,
                    )
                seen[resource.key] = name

        # Reference targets must exist in the combined latest view.
        for name, docs in latest_by_group.items():
            known = set(seen)
            for resource in docs.resources:
                for reference in resource.references:
                    if reference not in known:
                        raise DeclarationError(
                            f"reference target '{reference}' does not exist",
                            file=docs.source_file,
                            field_path=f"resource[{resource.key}].references",
                        )

        # Global cycle check on the combined latest view.
        combined = list(latest_by_group.values())
        build_graph(combined)

        self.declaration_history = dict(sorted(candidate_groups.items()))

    def _validate_group_chain(
        self,
        group_name: str,
        ordered: list[DeclarationVersion],
    ) -> None:
        if not ordered:
            raise DeclarationError(
                f"group '{group_name}' has no declaration documents"
            )
        names = {doc.group for doc in ordered}
        if names != {group_name}:
            bad = next(doc for doc in ordered if doc.group != group_name)
            raise DeclarationError(
                f"document group '{bad.group}' cannot be registered as '{group_name}'",
                file=bad.source_file,
                field_path="group",
            )
        versions = [doc.version for doc in ordered]
        if len(set(versions)) != len(versions):
            raise DeclarationError(
                f"group '{group_name}' versions must be unique: {versions}",
                field_path="version",
            )
        if versions != sorted(versions):
            raise DeclarationError(
                f"group '{group_name}' versions must be increasing: {versions}",
                field_path="version",
            )
        previous = self.declaration_history.get(group_name, [])
        if previous:
            if ordered[0].version != previous[-1].version + 1:
                raise DeclarationError(
                    f"new version {ordered[0].version} must exceed current "
                    f"version {previous[-1].version} for group '{group_name}'",
                    file=ordered[0].source_file,
                    field_path="version",
                )
            prior_versions = [doc.version for doc in previous]
            if prior_versions + versions != sorted(prior_versions + versions) or len(
                set(prior_versions) & set(versions)
            ):
                raise DeclarationError(
                    f"group '{group_name}' appended versions conflict with history: "
                    f"{prior_versions} + {versions}",
                    file=ordered[0].source_file,
                    field_path="version",
                )

    def submit_version(self, document: DeclarationVersion) -> None:
        """Append one new declaration version and wholesale-replace the view."""
        existing = self.declaration_history.get(document.group, [])
        if existing:
            self._validate_group_chain(document.group, [document])
            self.declaration_history[document.group] = existing + [document]
            # Re-run global reference/cycle validation on the combined view.
            combined = self._current_documents()
            build_graph(combined)
            self._validate_global_references(combined)
        else:
            self.register_group(document.group, [document])
        self._rebuild_view()

    def _validate_global_references(
        self, documents: list[DeclarationVersion]
    ) -> None:
        known = {
            resource.key
            for document in documents
            for resource in document.resources
        }
        for document in documents:
            for resource in document.resources:
                for reference in resource.references:
                    if reference not in known:
                        raise DeclarationError(
                            f"reference target '{reference}' does not exist",
                            file=document.source_file,
                            field_path=f"resource[{resource.key}].references",
                        )

    def delete_resource(self, key: str) -> None:
        """Delete a resource that no other resource still references."""
        if self.current_view is None:
            raise ReferenceError(
                "E_REF: no current view available", {"resource_key": key}
            )
        referrers = list(self.current_view.graph.referrers(key))
        if referrers:
            raise ReferenceError(
                f"E_REF: resource '{key}' is still referenced and cannot be deleted",
                {"resource_key": key, "referrers": referrers},
            )
        target_group = next(
            (
                doc.group
                for doc in self.current_view.declarations
                for resource in doc.resources
                if resource.key == key
            ),
            None,
        )
        if target_group is None:
            raise ReferenceError(
                f"E_REF: resource '{key}' does not exist",
                {"resource_key": key, "referrers": []},
            )
        history = self.declaration_history[target_group]
        latest = history[-1]
        kept = tuple(
            resource for resource in latest.resources if resource.key != key
        )
        from dataclasses import replace as _replace

        next_document = _replace(
            latest,
            version=latest.version + 1,
            resources=kept,
            source_file=latest.source_file + "#delete",
        )
        self.submit_version(next_document)

    # ------------------------------------------------------------------ view
    def _current_documents(self) -> list[DeclarationVersion]:
        return [docs[-1] for _, docs in sorted(self.declaration_history.items())]

    def _rebuild_view(self) -> View:
        documents = self._current_documents()
        previous = (
            dict(self.current_view.artifacts)
            if self.current_view is not None
            else {}
        )
        artifacts, graph, changed, invalidated, graph_delta = compile_view(
            documents, previous
        )
        graph_version = (
            graph_delta
            if self.current_view is None
            else self.current_view.graph_version + graph_delta
        )
        view = View(
            view_id=(self.current_view.view_id + 1) if self.current_view else 1,
            group_versions={
                doc.group: doc.version for doc in documents
            },
            declarations=tuple(documents),
            artifacts=artifacts,
            graph=graph,
            graph_version=graph_version,
            changed_keys=tuple(sorted(changed)),
            invalidated_keys=tuple(sorted(invalidated)),
        )
        # Wholesale replacement: the current view object is replaced as a
        # whole while older views remain available for comparison.
        self.current_view = view
        self.views.append(view)
        self.last_drifts = []
        self.last_plan = []
        return view

    def restore_state(
        self,
        history: dict[str, list[DeclarationVersion]],
        artifacts: dict[str, Artifact],
        graph: DependencyGraph,
        graph_version: int,
        actual: dict[str, dict[str, Any]],
        actual_source: str = "",
    ) -> View:
        """Wholesale replacement used by snapshot restore (no merging)."""
        self.declaration_history = dict(sorted(history.items()))
        self.actual_state = {key: dict(value) for key, value in actual.items()}
        self.actual_source = actual_source
        documents = self._current_documents()
        restored_artifacts = {key: value for key, value in artifacts.items()}
        view = View(
            view_id=1,
            group_versions={doc.group: doc.version for doc in documents},
            declarations=tuple(documents),
            artifacts=restored_artifacts,
            graph=graph,
            graph_version=graph_version,
            changed_keys=(),
            invalidated_keys=tuple(
                sorted(
                    key
                    for key, artifact in restored_artifacts.items()
                    if artifact.invalid
                )
            ),
        )
        self.current_view = view
        self.views = [view]
        self.last_drifts = []
        self.last_plan = []
        return view

    def resource_declaration(self, key: str) -> Any:
        for doc in self.current_view.declarations:  # type: ignore[union-attr]
            for resource in doc.resources:
                if resource.key == key:
                    return resource
        return None
