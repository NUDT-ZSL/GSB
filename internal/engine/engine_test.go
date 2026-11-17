package engine

import (
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"testing"

	"configledger/internal/domain"
	"configledger/internal/spec"
)

func loadSample(t *testing.T, name string) (*domain.Manifest, *domain.Graph) {
	t.Helper()
	data, err := os.ReadFile(filepath.Join("..", "..", "samples", name))
	if err != nil {
		t.Fatalf("read sample: %v", err)
	}
	manifest, graph, berr := spec.Parse(data, name, spec.LoadOptions{AllowDanglingRefs: true})
	if berr != nil {
		t.Fatalf("parse sample %s: %v", name, berr)
	}
	return manifest, graph
}

func applySample(t *testing.T, previous *domain.State, name string, capacity int64) *domain.State {
	t.Helper()
	manifest, graph := loadSample(t, name)
	state, berr := Apply(previous, manifest, graph, Options{
		SessionID:        "test",
		Capacity:         capacity,
		StorageUnitPrice: 2,
		CompileUnitPrice: 5,
	})
	if berr != nil {
		t.Fatalf("apply %s: %v", name, berr)
	}
	if berr := Verify(state, previous); berr != nil {
		t.Fatalf("verify %s: %v", name, berr)
	}
	if berr := Replay(state, previous); berr != nil {
		t.Fatalf("replay %s: %v", name, berr)
	}
	return state
}

func product(t *testing.T, state *domain.State, key string) domain.Product {
	t.Helper()
	for _, item := range state.Products {
		if item.Key == key {
			return item
		}
	}
	t.Fatalf("product %s not found", key)
	return domain.Product{}
}

func TestIncrementalVersionsInvalidationAndReuse(t *testing.T) {
	v1 := applySample(t, nil, "manifest-v1.json", 1_000_000)
	v2 := applySample(t, v1, "manifest-v2.json", 1_000_000)

	if got := product(t, v2, "base.port").ProductVersion; got != 2 {
		t.Fatalf("base.port version = %d, want 2", got)
	}
	for _, key := range []string{"api.timeout", "api.rate", "gateway.route", "gateway.retry", "log.level"} {
		if got := product(t, v2, key).ProductVersion; got != 1 {
			t.Fatalf("%s version = %d, want unchanged 1", key, got)
		}
	}
	if got := product(t, v2, "metrics.enabled").ProductVersion; got != 1 {
		t.Fatalf("new metrics.enabled version = %d, want 1", got)
	}
	if product(t, v1, "log.level").ArtifactHash != product(t, v2, "log.level").ArtifactHash {
		t.Fatal("unaffected artifact hash changed")
	}
	wantInvalidated := []string{"api.rate", "api.timeout", "gateway.retry", "gateway.route"}
	if !reflect.DeepEqual(v2.Ledger.Invalidated, wantInvalidated) {
		t.Fatalf("invalidated = %v, want %v", v2.Ledger.Invalidated, wantInvalidated)
	}
	if !contains(v2.Ledger.Reused, "log.level") {
		t.Fatalf("log.level should be reused with ample cache: %v", v2.Ledger.Reused)
	}
	if product(t, v2, "gateway.route").ArtifactHash == product(t, v1, "gateway.route").ArtifactHash {
		t.Fatal("downstream artifact should change even when product version stays stable")
	}

	v3 := applySample(t, v2, "manifest-v3.json", 1_000_000)
	if got := product(t, v3, "api.timeout").ProductVersion; got != 2 {
		t.Fatalf("api.timeout version = %d, want 2", got)
	}
	if got := product(t, v3, "base.port").ProductVersion; got != 2 {
		t.Fatalf("base.port version = %d, want 2", got)
	}
	if !reflect.DeepEqual(product(t, v3, "gateway.route").ProductVersion, int64(1)) {
		t.Fatal("invalidated gateway.route must keep its unchanged product version")
	}
	for _, item := range v3.Products {
		if item.Key == "api.rate" {
			t.Fatal("removed api.rate remained in the replaced view")
		}
	}
	if !reflect.DeepEqual(v3.Ledger.Diff.Removed, []string{"api.rate"}) {
		t.Fatalf("removed diff = %v", v3.Ledger.Diff.Removed)
	}
}

func TestDeterministicPlanning(t *testing.T) {
	v1 := applySample(t, nil, "manifest-v1.json", 1200)
	v2a := applySample(t, v1, "manifest-v2.json", 1200)
	v2b := applySample(t, v1, "manifest-v2.json", 1200)
	if !reflect.DeepEqual(v2a, v2b) {
		t.Fatal("same input produced different state or ledger")
	}
	if v2a.Ledger.CacheBytes > 1200 {
		t.Fatalf("cache bytes %d exceed capacity", v2a.Ledger.CacheBytes)
	}
	if len(v2a.Ledger.Decisions) == 0 {
		t.Fatal("decisions are required")
	}
	for index := 1; index < len(v2a.Ledger.Decisions); index++ {
		if v2a.Ledger.Decisions[index-1].Key >= v2a.Ledger.Decisions[index].Key {
			t.Fatal("decisions are not sorted deterministically")
		}
	}
	foundDropped := false
	for _, decision := range v2a.Ledger.Decisions {
		if decision.CacheAction == "dropped" {
			foundDropped = true
			if decision.CacheReason == "" {
				t.Fatal("dropped cache must explain the tradeoff")
			}
		}
	}
	if !foundDropped {
		t.Fatal("tight capacity sample should demonstrate at least one dropped cache artifact")
	}
}

func TestCapacityZero(t *testing.T) {
	manifest, graph := loadSample(t, "manifest-v1.json")
	_, berr := Apply(nil, manifest, graph, Options{SessionID: "test", Capacity: 0, StorageUnitPrice: 2, CompileUnitPrice: 5})
	if berr == nil || berr.Code != domain.CodeCapacity {
		t.Fatalf("expected E_CAPACITY, got %+v", berr)
	}
}

func TestDeleteReferencedAndDanglingReference(t *testing.T) {
	v1 := applySample(t, nil, "manifest-v1.json", 1_000_000)
	deleteManifest, deleteGraph := loadSample(t, "manifest-delete-referenced.json")
	_, berr := Apply(v1, deleteManifest, deleteGraph, Options{SessionID: "test", Capacity: 1000, StorageUnitPrice: 2, CompileUnitPrice: 5})
	if berr == nil || berr.Code != domain.CodeRef {
		t.Fatalf("expected E_REF, got %+v", berr)
	}
	if !reflect.DeepEqual(berr.Refs, []string{"gateway.route"}) {
		t.Fatalf("E_REF references = %v", berr.Refs)
	}
	_, berr = Apply(nil, deleteManifest, deleteGraph, Options{SessionID: "test", Capacity: 1000, StorageUnitPrice: 2, CompileUnitPrice: 5})
	if berr == nil || berr.Code != domain.CodeSpec {
		t.Fatalf("fresh dangling reference must be E_SPEC, got %+v", berr)
	}
}

func TestNodeTopologyChangeInvalidatesDerivedFragmentsWithoutVersionBump(t *testing.T) {
	v1 := applySample(t, nil, "manifest-v1.json", 1_000_000)
	data, err := os.ReadFile(filepath.Join("..", "..", "samples", "manifest-v1.json"))
	if err != nil {
		t.Fatal(err)
	}
	var changed domain.Manifest
	if err := json.Unmarshal(data, &changed); err != nil {
		t.Fatal(err)
	}
	changed.ManifestVersion = "v1-topology"
	for index := range changed.Nodes {
		if changed.Nodes[index].ID == "edge-gateway" {
			changed.Nodes[index].DependsOn = []string{}
		}
	}
	graph, berr := spec.BuildGraph(&changed)
	if berr != nil {
		t.Fatalf("build graph: %v", berr)
	}
	state, berr := Apply(v1, &changed, graph, Options{SessionID: "test", Capacity: 1_000_000, StorageUnitPrice: 2, CompileUnitPrice: 5})
	if berr != nil {
		t.Fatalf("apply topology change: %v", berr)
	}
	if berr := Verify(state, v1); berr != nil {
		t.Fatalf("verify topology change: %v", berr)
	}
	if !reflect.DeepEqual(state.Ledger.Invalidated, []string{"gateway.retry", "gateway.route"}) {
		t.Fatalf("topology invalidated = %v", state.Ledger.Invalidated)
	}
	if product(t, state, "gateway.route").ProductVersion != 1 {
		t.Fatal("node topology change must not increment product version")
	}
}

func TestLedgerAndReplayErrors(t *testing.T) {
	v1 := applySample(t, nil, "manifest-v1.json", 1_000_000)
	v2 := applySample(t, v1, "manifest-v2.json", 1_000_000)

	ledgerCopy := cloneState(t, v2)
	ledgerCopy.Ledger.Delivery[0].Items[0].ProductVersion++
	berr := Verify(ledgerCopy, v1)
	if berr == nil || berr.Code != domain.CodeLedger {
		t.Fatalf("expected E_LEDGER, got %+v", berr)
	}
	if berr.Item == "" || berr.Node == "" {
		t.Fatalf("E_LEDGER must identify first item and node: %+v", berr)
	}

	replayCopy := cloneState(t, v2)
	replayCopy.Ledger.TotalCost++
	berr = Replay(replayCopy, v1)
	if berr == nil || berr.Code != domain.CodeReplay {
		t.Fatalf("expected E_REPLAY, got %+v", berr)
	}
	if berr.Item == "" || berr.Reason == "" {
		t.Fatalf("E_REPLAY must state the failing basis: %+v", berr)
	}
}

func TestRestoreReportReplacesWholeVersion(t *testing.T) {
	v1 := applySample(t, nil, "manifest-v1.json", 1200)
	v2 := applySample(t, v1, "manifest-v2.json", 1200)
	v3 := applySample(t, v2, "manifest-v3.json", 1200)
	report := BuildRestoreReport(v3, v1)
	if !report.WholeReplaced || report.FromVersion != "v3" || report.ToVersion != "v1" || !report.GraphChanged {
		t.Fatalf("unexpected report: %+v", report)
	}
	if !contains(report.Removed, "metrics.enabled") || !contains(report.Added, "api.rate") {
		t.Fatalf("restore report omitted version diff: %+v", report)
	}
	if len(report.AffectedEntries) == 0 {
		t.Fatal("restore report must list affected entries")
	}
}

func cloneState(t *testing.T, state *domain.State) *domain.State {
	t.Helper()
	data, err := json.Marshal(state)
	if err != nil {
		t.Fatal(err)
	}
	var clone domain.State
	if err := json.Unmarshal(data, &clone); err != nil {
		t.Fatal(err)
	}
	return &clone
}
