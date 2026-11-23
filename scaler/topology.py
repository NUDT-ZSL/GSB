"""Dependency topology ordering and incremental invalidation closures."""

from .errors import bad_sample


def topological_order(services):
    """Kahn's algorithm with deterministic (priority, name) tie breaking.

    Upstream services are emitted before their dependents so scaling decisions
    follow the dependency direction.
    """
    by_name = {service.name: service for service in services}
    indegree = {name: len(by_name[name].upstream) for name in by_name}
    dependents = {name: [] for name in by_name}
    for service in services:
        for up in service.upstream:
            dependents[up].append(service.name)

    def key(name):
        return (by_name[name].priority, name)

    ready = sorted([name for name, deg in indegree.items() if deg == 0], key=key)
    ordered = []
    while ready:
        name = ready.pop(0)
        ordered.append(name)
        for dependent in dependents[name]:
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                ready.append(dependent)
                ready.sort(key=key)
    if len(ordered) != len(by_name):
        raise bad_sample("dependency cycle prevents topological ordering")
    return ordered


def downstream_closure(roots, services):
    """All services transitively depending on any service in ``roots``."""
    dependents = {service.name: set() for service in services}
    for service in services:
        for up in service.upstream:
            dependents[up].add(service.name)
    affected = set()
    stack = list(roots)
    while stack:
        name = stack.pop()
        for dependent in dependents.get(name, ()):
            if dependent not in affected:
                affected.add(dependent)
                stack.append(dependent)
    return affected


def upstream_closure(roots, services):
    """All transitive upstream dependencies of any service in ``roots``."""
    by_name = {service.name: service for service in services}
    affected = set()
    stack = list(roots)
    while stack:
        name = stack.pop()
        for up in by_name[name].upstream:
            if up not in affected:
                affected.add(up)
                stack.append(up)
    return affected
