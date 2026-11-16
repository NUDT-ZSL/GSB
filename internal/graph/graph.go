package graph

import (
	"fmt"
	"sort"
	"strings"

	"locklease/internal/model"
)

// DependError carries the business code E_DEPEND and a concrete cycle path.
type DependError struct {
	Message string
	Cycle   []string
	Missing []string
}

func (e *DependError) Error() string {
	if len(e.Missing) > 0 {
		return fmt.Sprintf("E_DEPEND: %s (missing keys: %s)", e.Message, strings.Join(e.Missing, ","))
	}
	return fmt.Sprintf("E_DEPEND: %s (cycle: %s)", e.Message, strings.Join(e.Cycle, " -> "))
}

// Graph is an adjacency-list directed dependency graph. Edge a -> b means
// lock b depends on lock a, so invalidating a must invalidate b downstream.
type Graph struct {
	version int64
	// downstream[x] = keys that directly depend on x.
	downstream map[string]map[string]struct{}
	// deps[x] = keys that x directly depends on.
	deps map[string]map[string]struct{}
	keys map[string]struct{}
}

func New() *Graph {
	return &Graph{
		downstream: map[string]map[string]struct{}{},
		deps:       map[string]map[string]struct{}{},
		keys:       map[string]struct{}{},
	}
}

func (g *Graph) Version() int64 { return g.version }

func (g *Graph) Has(key string) bool {
	_, ok := g.keys[key]
	return ok
}

// SortedKeys returns all graph keys in stable lexicographic order.
func (g *Graph) SortedKeys() []string {
	out := make([]string, 0, len(g.keys))
	for k := range g.keys {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

// DependsOn returns the sorted dependency set of key.
func (g *Graph) DependsOn(key string) []string {
	set := g.deps[key]
	out := make([]string, 0, len(set))
	for k := range set {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

// Build validates the declarative graph file: every dependency reference must
// exist and the graph must be acyclic. Any violation is reported as E_DEPEND.
func Build(file model.GraphFile) (*Graph, error) {
	g := New()
	g.version = file.Version
	for _, n := range file.Nodes {
		if n.Key == "" {
			return nil, &DependError{Message: "empty lock key"}
		}
		if _, dup := g.keys[n.Key]; dup {
			return nil, &DependError{Message: "duplicate key " + n.Key}
		}
		g.keys[n.Key] = struct{}{}
		g.deps[n.Key] = map[string]struct{}{}
		g.downstream[n.Key] = map[string]struct{}{}
	}
	// Edges, sorted for determinism.
	nodes := append([]model.Node(nil), file.Nodes...)
	sort.Slice(nodes, func(i, j int) bool { return nodes[i].Key < nodes[j].Key })
	var missing []string
	for _, n := range nodes {
		deps := append([]string(nil), n.DependsOn...)
		sort.Strings(deps)
		for _, d := range deps {
			if !g.Has(d) {
				missing = append(missing, fmt.Sprintf("%s->%s", n.Key, d))
				continue
			}
			g.deps[n.Key][d] = struct{}{}
			g.downstream[d][n.Key] = struct{}{}
		}
	}
	if len(missing) > 0 {
		sort.Strings(missing)
		return nil, &DependError{Message: "dependency references unknown lock", Missing: missing}
	}
	if cycle := findCycle(g); len(cycle) > 0 {
		return nil, &DependError{Message: "dependency graph contains a cycle", Cycle: cycle}
	}
	return g, nil
}

// findCycle performs an iterative-safe DFS with white/gray/black coloring and
// returns a concrete cycle path (with the start node repeated at the end).
func findCycle(g *Graph) []string {
	const white, gray, black = 0, 1, 2
	color := map[string]int{}
	parent := map[string]string{}
	keys := g.SortedKeys()
	var dfs func(string) []string
	dfs = func(start string) []string {
		type frame struct {
			key string
			idx int
		}
		stack := []frame{{start, 0}}
		color[start] = gray
		for len(stack) > 0 {
			top := &stack[len(stack)-1]
			children := g.DependsOn(top.key)
			if top.idx >= len(children) {
				color[top.key] = black
				stack = stack[:len(stack)-1]
				continue
			}
			child := children[top.idx]
			top.idx++
			switch color[child] {
			case white:
				color[child] = gray
				parent[child] = top.key
				stack = append(stack, frame{child, 0})
			case gray:
				// Reconstruct along dependency edges: child depends on
				// top.key, so the directed cycle reads child -> ... -> child.
				path := []string{child}
				cur := top.key
				for cur != child {
					path = append(path, cur)
					cur = parent[cur]
				}
				path = append(path, child)
				return path
			}
		}
		return nil
	}
	for _, k := range keys {
		if color[k] == white {
			if cyc := dfs(k); len(cyc) > 0 {
				return cyc
			}
		}
	}
	return nil
}

// DownstreamClosure returns every lock that directly or transitively depends
// on source, in a deterministic BFS order with ties broken lexicographically.
// source itself is excluded.
func (g *Graph) DownstreamClosure(source string) []string {
	seen := map[string]struct{}{source: {}}
	var order []string
	frontier := []string{source}
	for len(frontier) > 0 {
		var next []string
		for _, node := range frontier {
			children := make([]string, 0, len(g.downstream[node]))
			for c := range g.downstream[node] {
				if _, ok := seen[c]; ok {
					continue
				}
				children = append(children, c)
			}
			sort.Strings(children)
			for _, c := range children {
				seen[c] = struct{}{}
				order = append(order, c)
				next = append(next, c)
			}
		}
		frontier = next
	}
	return order
}
