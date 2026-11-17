package spec

import (
	"reflect"
	"testing"

	"configledger/internal/domain"
)

func TestLoadDeterministicGraph(t *testing.T) {
	data := []byte(`{
		"interface_version":"config-ledger/v1",
		"manifest_version":"v1",
		"nodes":[{"id":"node-b","depends_on":["node-a"]},{"id":"node-a"}],
		"entries":[
			{"key":"app.b","value":{"z":1,"a":2},"targets":["node-b"],"depends_on":["app.a"]},
			{"key":"app.a","value":1,"targets":["node-a"]}
		]
	}`)
	first, firstGraph, berr := Parse(data, "inline", LoadOptions{})
	if berr != nil {
		t.Fatalf("parse failed: %v", berr)
	}
	second, secondGraph, berr := Parse(data, "inline", LoadOptions{})
	if berr != nil {
		t.Fatalf("second parse failed: %v", berr)
	}
	if !reflect.DeepEqual(first, second) {
		t.Fatalf("normalized manifests are not deterministic")
	}
	if firstGraph.Version != secondGraph.Version {
		t.Fatalf("graph versions differ: %s %s", firstGraph.Version, secondGraph.Version)
	}
	if got, want := EntryIDs(first), []string{"app.a", "app.b"}; !reflect.DeepEqual(got, want) {
		t.Fatalf("entries not sorted: got %v want %v", got, want)
	}
	if len(firstGraph.Edges) != 2 {
		t.Fatalf("expected explicit plus node-derived edges, got %+v", firstGraph.Edges)
	}
}

func TestSpecValidationErrors(t *testing.T) {
	tests := []struct {
		name string
		data string
		code string
		item string
	}{
		{
			name: "duplicate key",
			data: `{"interface_version":"config-ledger/v1","manifest_version":"v1","nodes":[{"id":"n"}],"entries":[
				{"key":"a","value":1,"targets":["n"]},{"key":"a","value":2,"targets":["n"]}]}`,
			code: domain.CodeSpec,
			item: "entry:a",
		},
		{
			name: "missing target node",
			data: `{"interface_version":"config-ledger/v1","manifest_version":"v1","nodes":[{"id":"n"}],"entries":[
				{"key":"a","value":1,"targets":["missing"]}]}`,
			code: domain.CodeSpec,
			item: "entry:a",
		},
		{
			name: "missing dependency",
			data: `{"interface_version":"config-ledger/v1","manifest_version":"v1","nodes":[{"id":"n"}],"entries":[
				{"key":"a","value":1,"targets":["n"],"depends_on":["missing"]}]}`,
			code: domain.CodeSpec,
			item: "entry:a",
		},
		{
			name: "entry cycle",
			data: `{"interface_version":"config-ledger/v1","manifest_version":"v1","nodes":[{"id":"n"}],"entries":[
				{"key":"a","value":1,"targets":["n"],"depends_on":["b"]},
				{"key":"b","value":2,"targets":["n"],"depends_on":["a"]}]}`,
			code: domain.CodeDepend,
			item: "entry:a",
		},
		{
			name: "node cycle",
			data: `{"interface_version":"config-ledger/v1","manifest_version":"v1","nodes":[
				{"id":"a","depends_on":["b"]},{"id":"b","depends_on":["a"]}],"entries":[
				{"key":"x","value":1,"targets":["a"]}]}`,
			code: domain.CodeDepend,
			item: "node:a",
		},
		{
			name: "broken json",
			data: `{"interface_version":`,
			code: domain.CodeSpec,
			item: "inline",
		},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			_, _, berr := Parse([]byte(test.data), "inline", LoadOptions{})
			if berr == nil {
				t.Fatal("expected business error")
			}
			if berr.Code != test.code || berr.Item != test.item {
				t.Fatalf("got %s %s, want %s %s (%s)", berr.Code, berr.Item, test.code, test.item, berr.Reason)
			}
		})
	}
}

func TestAllowDanglingReferences(t *testing.T) {
	data := []byte(`{
		"interface_version":"config-ledger/v1",
		"manifest_version":"v2",
		"nodes":[{"id":"n"}],
		"entries":[{"key":"a","value":1,"targets":["n"],"depends_on":["deleted"]}]
	}`)
	if _, _, berr := Parse(data, "inline", LoadOptions{}); berr == nil || berr.Code != domain.CodeSpec {
		t.Fatalf("expected strict E_SPEC, got %+v", berr)
	}
	manifest, _, berr := Parse(data, "inline", LoadOptions{AllowDanglingRefs: true})
	if berr != nil {
		t.Fatalf("dangling reference should be deferred: %v", berr)
	}
	if len(manifest.Entries) != 1 {
		t.Fatalf("unexpected manifest: %+v", manifest)
	}
}

func TestInjectedDependencyCycle(t *testing.T) {
	data := []byte(`{
		"interface_version":"config-ledger/v1",
		"manifest_version":"v1",
		"nodes":[{"id":"n"}],
		"entries":[{"key":"a","value":1,"targets":["n"]}]
	}`)
	_, _, berr := Parse(data, "inline", LoadOptions{InjectDependencyCycle: true})
	if berr == nil || berr.Code != domain.CodeDepend {
		t.Fatalf("expected E_DEPEND, got %+v", berr)
	}
}

func TestDefinitionHashChangesWithTargetsAndReferences(t *testing.T) {
	base := domain.Entry{Key: "a", Value: 1, Targets: []string{"n1"}, DependsOn: []string{"b"}, ValueSet: true}
	changedTarget := base
	changedTarget.Targets = []string{"n2"}
	changedReference := base
	changedReference.DependsOn = []string{"c"}
	if DefinitionHash(base) == DefinitionHash(changedTarget) {
		t.Fatal("target changes must affect definition hash")
	}
	if DefinitionHash(base) == DefinitionHash(changedReference) {
		t.Fatal("reference changes must affect definition hash")
	}
	if DefinitionHash(base) != DefinitionHash(domain.Entry{Key: "a", Value: 1, Targets: []string{"n1"}, DependsOn: []string{"b"}, ValueSet: true}) {
		t.Fatal("identical definitions must hash identically")
	}
}
