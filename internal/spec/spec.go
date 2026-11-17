package spec

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"regexp"
	"sort"
	"strings"

	"configledger/internal/domain"
)

var identifierPattern = regexp.MustCompile(`^[A-Za-z][A-Za-z0-9_.-]{0,127}$`)

type LoadOptions struct {
	InjectDependencyCycle bool
	InjectCorruptSpec     bool
	AllowDanglingRefs     bool
}

func LoadFile(path string, options LoadOptions) (*domain.Manifest, *domain.Graph, *domain.BusinessError) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, nil, domain.BizError(domain.CodeSpec, path, fmt.Sprintf("cannot read manifest: %v", err))
	}
	if options.InjectCorruptSpec {
		data = injectCorruption(data)
	}
	return Parse(data, path, options)
}

func Parse(data []byte, source string, options LoadOptions) (*domain.Manifest, *domain.Graph, *domain.BusinessError) {
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.DisallowUnknownFields()
	var manifest domain.Manifest
	if err := decoder.Decode(&manifest); err != nil {
		return nil, nil, domain.BizError(domain.CodeSpec, source, fmt.Sprintf("invalid JSON manifest: %v", err))
	}
	if err := ensureEOF(decoder); err != nil {
		return nil, nil, domain.BizError(domain.CodeSpec, source, err.Error())
	}
	if berr := validate(&manifest, source, options); berr != nil {
		return nil, nil, berr
	}
	graph, berr := BuildGraph(&manifest)
	if berr != nil {
		return nil, nil, berr
	}
	return &manifest, graph, nil
}

func ensureEOF(decoder *json.Decoder) error {
	var extra any
	err := decoder.Decode(&extra)
	if err == io.EOF {
		return nil
	}
	if err == nil {
		return fmt.Errorf("manifest contains more than one JSON value")
	}
	return fmt.Errorf("invalid trailing JSON: %w", err)
}

func validate(manifest *domain.Manifest, source string, options LoadOptions) *domain.BusinessError {
	if manifest.InterfaceVersion != domain.InterfaceVersion {
		return domain.BizError(domain.CodeSpec, "interface_version", fmt.Sprintf("unsupported interface version %q, want %q", manifest.InterfaceVersion, domain.InterfaceVersion))
	}
	if !identifierPattern.MatchString(manifest.ManifestVersion) {
		return domain.BizError(domain.CodeSpec, "manifest_version", "manifest version must match ^[A-Za-z][A-Za-z0-9_.-]{0,63}$")
	}
	if len(manifest.Nodes) == 0 {
		return domain.BizError(domain.CodeSpec, "nodes", "at least one target node is required")
	}
	nodeSet := make(map[string]struct{}, len(manifest.Nodes))
	for i := range manifest.Nodes {
		node := &manifest.Nodes[i]
		if !identifierPattern.MatchString(node.ID) {
			return domain.BizError(domain.CodeSpec, "node:"+node.ID, "node id is empty or invalid")
		}
		if _, exists := nodeSet[node.ID]; exists {
			return domain.BizError(domain.CodeSpec, "node:"+node.ID, "duplicate node id")
		}
		nodeSet[node.ID] = struct{}{}
		seenDeps := make(map[string]struct{}, len(node.DependsOn))
		for _, dep := range node.DependsOn {
			if dep == node.ID {
				return domain.BizErrorNode(domain.CodeDepend, "node:"+node.ID, node.ID, "node cannot depend on itself")
			}
			if _, exists := seenDeps[dep]; exists {
				return domain.BizError(domain.CodeSpec, "node:"+node.ID, "duplicate node dependency "+dep)
			}
			seenDeps[dep] = struct{}{}
		}
	}
	for _, node := range manifest.Nodes {
		for _, dep := range node.DependsOn {
			if _, exists := nodeSet[dep]; !exists {
				return domain.BizErrorNode(domain.CodeSpec, "node:"+node.ID, node.ID, "node dependency target does not exist: "+dep)
			}
		}
	}
	if berr := detectNodeCycle(manifest); berr != nil {
		return berr
	}

	if len(manifest.Entries) == 0 {
		return domain.BizError(domain.CodeSpec, "entries", "at least one configuration entry is required")
	}
	keySet := make(map[string]struct{}, len(manifest.Entries))
	for i := range manifest.Entries {
		entry := &manifest.Entries[i]
		if !identifierPattern.MatchString(entry.Key) {
			return domain.BizError(domain.CodeSpec, "entry:"+entry.Key, "entry key is empty or invalid")
		}
		if _, exists := keySet[entry.Key]; exists {
			return domain.BizError(domain.CodeSpec, "entry:"+entry.Key, "duplicate entry key")
		}
		keySet[entry.Key] = struct{}{}
		if !entry.ValueSet {
			return domain.BizError(domain.CodeSpec, "entry:"+entry.Key, "value is required")
		}
	}
	for i := range manifest.Entries {
		entry := &manifest.Entries[i]
		if len(entry.Targets) == 0 {
			return domain.BizError(domain.CodeSpec, "entry:"+entry.Key, "at least one target node is required")
		}
		seenTargets := make(map[string]struct{}, len(entry.Targets))
		for _, target := range entry.Targets {
			if _, exists := nodeSet[target]; !exists {
				return domain.BizError(domain.CodeSpec, "entry:"+entry.Key, "target node does not exist: "+target)
			}
			if _, exists := seenTargets[target]; exists {
				return domain.BizError(domain.CodeSpec, "entry:"+entry.Key, "duplicate target node "+target)
			}
			seenTargets[target] = struct{}{}
		}
		seenDeps := make(map[string]struct{}, len(entry.DependsOn))
		for _, dep := range entry.DependsOn {
			if dep == entry.Key {
				return domain.BizError(domain.CodeDepend, "entry:"+entry.Key, "entry cannot depend on itself")
			}
			if _, exists := keySet[dep]; !exists && !options.AllowDanglingRefs {
				return domain.BizError(domain.CodeSpec, "entry:"+entry.Key, "dependency reference does not exist: "+dep)
			}
			if _, exists := seenDeps[dep]; exists {
				return domain.BizError(domain.CodeSpec, "entry:"+entry.Key, "duplicate dependency reference "+dep)
			}
			seenDeps[dep] = struct{}{}
		}
	}

	if options.InjectDependencyCycle {
		entry := manifest.Entries[0]
		return domain.BizError(domain.CodeDepend, "entry:"+entry.Key, "injected dependency cycle: "+entry.Key+" -> "+entry.Key)
	}
	if berr := detectEntryCycle(manifest); berr != nil {
		return berr
	}

	sort.Slice(manifest.Nodes, func(i, j int) bool { return manifest.Nodes[i].ID < manifest.Nodes[j].ID })
	for i := range manifest.Nodes {
		sort.Strings(manifest.Nodes[i].DependsOn)
	}
	sort.Slice(manifest.Entries, func(i, j int) bool { return manifest.Entries[i].Key < manifest.Entries[j].Key })
	for i := range manifest.Entries {
		sort.Strings(manifest.Entries[i].Targets)
		sort.Strings(manifest.Entries[i].DependsOn)
	}
	return nil
}

func detectNodeCycle(manifest *domain.Manifest) *domain.BusinessError {
	deps := make(map[string][]string, len(manifest.Nodes))
	for _, node := range manifest.Nodes {
		deps[node.ID] = append([]string(nil), node.DependsOn...)
		sort.Strings(deps[node.ID])
	}
	order, remaining := stableTopologicalOrder(nodesList(manifest), deps)
	_ = order
	if len(remaining) == 0 {
		return nil
	}
	key := remaining[0]
	return domain.BizErrorNode(domain.CodeDepend, "node:"+key, key, "node dependency graph contains a cycle")
}

func detectEntryCycle(manifest *domain.Manifest) *domain.BusinessError {
	deps := make(map[string][]string, len(manifest.Entries))
	vertices := make([]string, 0, len(manifest.Entries))
	for _, entry := range manifest.Entries {
		vertices = append(vertices, entry.Key)
		deps[entry.Key] = append([]string(nil), entry.DependsOn...)
		sort.Strings(deps[entry.Key])
	}
	sort.Strings(vertices)
	_, remaining := stableTopologicalOrder(vertices, deps)
	if len(remaining) == 0 {
		return nil
	}
	key := remaining[0]
	return domain.BizError(domain.CodeDepend, "entry:"+key, "configuration dependency graph contains a cycle")
}

func nodesList(m *domain.Manifest) []string {
	result := make([]string, 0, len(m.Nodes))
	for _, node := range m.Nodes {
		result = append(result, node.ID)
	}
	sort.Strings(result)
	return result
}

func stableTopologicalOrder(vertices []string, deps map[string][]string) ([]string, []string) {
	sortedVertices := append([]string(nil), vertices...)
	sort.Strings(sortedVertices)
	indegree := make(map[string]int, len(sortedVertices))
	downstream := make(map[string][]string, len(sortedVertices))
	for _, vertex := range sortedVertices {
		indegree[vertex] = 0
	}
	for _, vertex := range sortedVertices {
		for _, dep := range deps[vertex] {
			if _, exists := indegree[dep]; !exists {
				continue
			}
			indegree[vertex]++
			downstream[dep] = append(downstream[dep], vertex)
		}
	}
	for dep := range downstream {
		sort.Strings(downstream[dep])
	}
	ready := make([]string, 0, len(sortedVertices))
	for _, vertex := range sortedVertices {
		if indegree[vertex] == 0 {
			ready = append(ready, vertex)
		}
	}
	order := make([]string, 0, len(sortedVertices))
	for len(ready) > 0 {
		sort.Strings(ready)
		vertex := ready[0]
		ready = ready[1:]
		order = append(order, vertex)
		for _, next := range downstream[vertex] {
			indegree[next]--
			if indegree[next] == 0 {
				ready = append(ready, next)
			}
		}
	}
	remaining := make([]string, 0)
	if len(order) != len(sortedVertices) {
		for _, vertex := range sortedVertices {
			if indegree[vertex] > 0 {
				remaining = append(remaining, vertex)
			}
		}
	}
	return order, remaining
}

func BuildGraph(manifest *domain.Manifest) (*domain.Graph, *domain.BusinessError) {
	entryByKey := make(map[string]domain.Entry, len(manifest.Entries))
	entriesByNode := make(map[string][]string, len(manifest.Nodes))
	for _, entry := range manifest.Entries {
		entryByKey[entry.Key] = entry
		for _, target := range entry.Targets {
			entriesByNode[target] = append(entriesByNode[target], entry.Key)
		}
	}
	edges := make([]domain.Edge, 0)
	seen := make(map[string]struct{})
	addEdge := func(from, to, kind string) {
		if from == "" || to == "" || from == to {
			return
		}
		id := from + "\x00" + to + "\x00" + kind
		if _, exists := seen[id]; exists {
			return
		}
		seen[id] = struct{}{}
		edges = append(edges, domain.Edge{From: from, To: to, Kind: kind})
	}
	for _, entry := range manifest.Entries {
		for _, dep := range entry.DependsOn {
			addEdge(dep, entry.Key, "entry")
		}
	}
	for _, node := range manifest.Nodes {
		for _, upstreamNode := range node.DependsOn {
			for _, upstream := range entriesByNode[upstreamNode] {
				for _, downstream := range entriesByNode[node.ID] {
					addEdge(upstream, downstream, "node")
				}
			}
		}
	}
	sort.Slice(edges, func(i, j int) bool {
		if edges[i].From != edges[j].From {
			return edges[i].From < edges[j].From
		}
		if edges[i].To != edges[j].To {
			return edges[i].To < edges[j].To
		}
		return edges[i].Kind < edges[j].Kind
	})
	deps := make(map[string][]string, len(manifest.Entries))
	vertices := make([]string, 0, len(manifest.Entries))
	for _, entry := range manifest.Entries {
		vertices = append(vertices, entry.Key)
	}
	for _, edge := range edges {
		deps[edge.To] = append(deps[edge.To], edge.From)
	}
	for key := range deps {
		sort.Strings(deps[key])
	}
	order, remaining := stableTopologicalOrder(vertices, deps)
	if len(remaining) > 0 {
		key := remaining[0]
		return nil, domain.BizError(domain.CodeDepend, "entry:"+key, "combined dependency graph contains a cycle")
	}
	graph := &domain.Graph{TopologicalOrder: order, Edges: edges}
	graph.Version = graphHash(manifest, graph)
	return graph, nil
}

func graphHash(manifest *domain.Manifest, graph *domain.Graph) string {
	type entryTarget struct {
		Key     string   `json:"key"`
		Targets []string `json:"targets"`
	}
	targets := make([]entryTarget, 0, len(manifest.Entries))
	for _, entry := range manifest.Entries {
		targets = append(targets, entryTarget{Key: entry.Key, Targets: append([]string(nil), entry.Targets...)})
	}
	doc := struct {
		Nodes        []domain.Node `json:"nodes"`
		EntryTargets []entryTarget `json:"entry_targets"`
		Edges        []domain.Edge `json:"edges"`
	}{
		Nodes:        manifest.Nodes,
		EntryTargets: targets,
		Edges:        graph.Edges,
	}
	data, _ := json.Marshal(doc)
	sum := sha256.Sum256(data)
	return hex.EncodeToString(sum[:8])
}

func ManifestHash(manifest *domain.Manifest) string {
	data, _ := json.Marshal(manifest)
	sum := sha256.Sum256(data)
	return hex.EncodeToString(sum[:])
}

func DefinitionHash(entry domain.Entry) string {
	doc := struct {
		Key       string   `json:"key"`
		Value     any      `json:"value"`
		Targets   []string `json:"targets"`
		DependsOn []string `json:"depends_on"`
	}{
		Key:       entry.Key,
		Value:     entry.Value,
		Targets:   append([]string(nil), entry.Targets...),
		DependsOn: append([]string(nil), entry.DependsOn...),
	}
	data, _ := json.Marshal(doc)
	sum := sha256.Sum256(data)
	return hex.EncodeToString(sum[:8])
}

func CanonicalJSON(value any) ([]byte, error) {
	return json.Marshal(value)
}

func injectCorruption(data []byte) []byte {
	if len(data) <= 2 {
		return []byte("{")
	}
	return append([]byte{'{', '"', 'x'}, data[2:len(data)-1]...)
}

func EntryIDs(manifest *domain.Manifest) []string {
	result := make([]string, 0, len(manifest.Entries))
	for _, entry := range manifest.Entries {
		result = append(result, entry.Key)
	}
	sort.Strings(result)
	return result
}

func FindEntry(manifest *domain.Manifest, key string) (domain.Entry, bool) {
	for _, entry := range manifest.Entries {
		if entry.Key == key {
			return entry, true
		}
	}
	return domain.Entry{}, false
}

func NodeExists(manifest *domain.Manifest, node string) bool {
	for _, candidate := range manifest.Nodes {
		if candidate.ID == node {
			return true
		}
	}
	return false
}

func JoinReasons(reasons []string) string {
	sorted := append([]string(nil), reasons...)
	sort.Strings(sorted)
	return strings.Join(sorted, "; ")
}
