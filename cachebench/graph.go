package main

import "sort"

// Graph 为制品引用依赖图（下游 -> 上游引用，反向索引用于失效传播）。
type Graph struct {
	Version   int
	refs      map[string][]Ref    // key -> 其引用的上游
	referrers map[string][]string // key -> 直接引用它的下游
}

// NewGraph 依据清单构建引用图。
func NewGraph(m *Manifest) *Graph {
	g := &Graph{
		Version:   1,
		refs:      map[string][]Ref{},
		referrers: map[string][]string{},
	}
	for _, key := range m.OrderedKeys() {
		a := m.Artifact(key)
		refs := append([]Ref{}, a.References...)
		sort.Slice(refs, func(i, j int) bool { return refs[i].Key < refs[j].Key })
		g.refs[key] = refs
		for _, r := range refs {
			g.referrers[r.Key] = append(g.referrers[r.Key], key)
		}
	}
	for k := range g.referrers {
		sort.Strings(g.referrers[k])
	}
	return g
}

// Refs 返回制品的引用列表（稳定序）。
func (g *Graph) Refs(key string) []Ref {
	return append([]Ref{}, g.refs[key]...)
}

// Referrers 返回直接引用者（稳定序）。
func (g *Graph) Referrers(key string) []string {
	return append([]string{}, g.referrers[key]...)
}

// Downstream 推导全部直接与间接下游，返回稳定序（字典序）。
func (g *Graph) Downstream(root string) []string {
	seen := map[string]bool{}
	queue := append([]string{}, g.referrers[root]...)
	for len(queue) > 0 {
		k := queue[0]
		queue = queue[1:]
		if seen[k] {
			continue
		}
		seen[k] = true
		queue = append(queue, g.referrers[k]...)
	}
	out := make([]string, 0, len(seen))
	for k := range seen {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

// Remove 从图中摘除制品；仍被引用时返回 E_REF 并列出引用者。
func (g *Graph) Remove(key string) error {
	if refs := g.referrers[key]; len(refs) > 0 {
		return &BizError{Code: CodeRef, Field: key, Msg: "制品仍被引用，拒绝删除",
			Detail: map[string]string{"referrers": joinKeys(refs)}}
	}
	for _, r := range g.refs[key] {
		list := g.referrers[r.Key]
		next := list[:0]
		for _, k := range list {
			if k != key {
				next = append(next, k)
			}
		}
		g.referrers[r.Key] = next
	}
	delete(g.refs, key)
	delete(g.referrers, key)
	g.Version++
	return nil
}

func joinKeys(keys []string) string {
	out := ""
	for i, k := range keys {
		if i > 0 {
			out += ","
		}
		out += k
	}
	return out
}
