package domain

import (
	"bytes"
	"encoding/json"
	"fmt"
)

const (
	InterfaceVersion = "config-ledger/v1"
	EstimatorVersion = "integer-cost/v1"
	SnapshotSchema   = "snapshot/v1"
	ArtifactSchema   = "artifact/v1"
)

type Node struct {
	ID        string   `json:"id"`
	DependsOn []string `json:"depends_on,omitempty"`
}

type Entry struct {
	Key       string   `json:"key"`
	Value     any      `json:"value"`
	Targets   []string `json:"targets"`
	DependsOn []string `json:"depends_on,omitempty"`
	ValueSet  bool     `json:"-"`
}

type entryWire struct {
	Key       string          `json:"key"`
	Value     json.RawMessage `json:"value"`
	Targets   []string        `json:"targets"`
	DependsOn []string        `json:"depends_on"`
}

func (e *Entry) UnmarshalJSON(data []byte) error {
	var wire entryWire
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&wire); err != nil {
		return err
	}
	e.Key = wire.Key
	e.Targets = wire.Targets
	e.DependsOn = wire.DependsOn
	if len(wire.Value) == 0 {
		e.Value = nil
		e.ValueSet = false
		return nil
	}
	valueDecoder := json.NewDecoder(bytes.NewReader(wire.Value))
	valueDecoder.UseNumber()
	var value any
	if err := valueDecoder.Decode(&value); err != nil {
		return fmt.Errorf("invalid value: %w", err)
	}
	e.Value = value
	e.ValueSet = true
	return nil
}

type Manifest struct {
	InterfaceVersion string  `json:"interface_version"`
	ManifestVersion  string  `json:"manifest_version"`
	Nodes            []Node  `json:"nodes"`
	Entries          []Entry `json:"entries"`
}

type Edge struct {
	From string `json:"from"`
	To   string `json:"to"`
	Kind string `json:"kind"`
}

type Graph struct {
	Version          string   `json:"version"`
	TopologicalOrder []string `json:"topological_order"`
	Edges            []Edge   `json:"edges"`
}

type Product struct {
	Key             string          `json:"key"`
	ProductVersion  int64           `json:"product_version"`
	DefinitionHash  string          `json:"definition_hash"`
	ArtifactHash    string          `json:"artifact_hash"`
	ArtifactPayload json.RawMessage `json:"artifact_payload"`
	Targets         []string        `json:"targets"`
	DependsOn       []string        `json:"depends_on,omitempty"`
	StorageBytes    int64           `json:"storage_bytes"`
	CompileWork     int64           `json:"compile_work"`
	DeliveryEntries int64           `json:"delivery_entries"`
}

type CacheItem struct {
	Key            string `json:"key"`
	ProductVersion int64  `json:"product_version"`
	ArtifactHash   string `json:"artifact_hash"`
	Bytes          int64  `json:"bytes"`
}

type Decision struct {
	Key                  string `json:"key"`
	Action               string `json:"action"`
	CacheAction          string `json:"cache_action"`
	Reason               string `json:"reason"`
	CacheReason          string `json:"cache_reason"`
	AbandonedReuseReason string `json:"abandoned_reuse_reason,omitempty"`
	Node                 string `json:"node,omitempty"`
}

type PlanItem struct {
	Key             string   `json:"key"`
	ProductVersion  int64    `json:"product_version"`
	Targets         []string `json:"targets"`
	DeliveryEntries int64    `json:"delivery_entries"`
	Compiled        bool     `json:"compiled"`
	CompileWork     int64    `json:"compile_work"`
	StorageBytes    int64    `json:"storage_bytes"`
	ArtifactHash    string   `json:"artifact_hash"`
}

type VersionDiff struct {
	From      string   `json:"from"`
	To        string   `json:"to"`
	Added     []string `json:"added"`
	Modified  []string `json:"modified"`
	Removed   []string `json:"removed"`
	Unchanged []string `json:"unchanged"`
}

type DeliveredItem struct {
	Key            string `json:"key"`
	ProductVersion int64  `json:"product_version"`
	ArtifactHash   string `json:"artifact_hash"`
}

type NodeDelivery struct {
	Node  string          `json:"node"`
	Items []DeliveredItem `json:"items"`
}

type Ledger struct {
	InterfaceVersion        string         `json:"interface_version"`
	EstimatorVersion        string         `json:"estimator_version"`
	CostSource              string         `json:"cost_source"`
	ManifestVersion         string         `json:"manifest_version"`
	PreviousManifestVersion string         `json:"previous_manifest_version"`
	ManifestHash            string         `json:"manifest_hash"`
	GraphVersion            string         `json:"graph_version"`
	PreviousGraphVersion    string         `json:"previous_graph_version"`
	GraphChanged            bool           `json:"graph_changed"`
	Capacity                int64          `json:"capacity"`
	StorageUnitPrice        int64          `json:"storage_unit_price"`
	CompileUnitPrice        int64          `json:"compile_unit_price"`
	Diff                    VersionDiff    `json:"diff"`
	Invalidated             []string       `json:"invalidated"`
	Affected                []string       `json:"affected"`
	Reused                  []string       `json:"reused"`
	Recompiled              []string       `json:"recompiled"`
	Dropped                 []string       `json:"dropped"`
	Decisions               []Decision     `json:"decisions"`
	Plan                    []PlanItem     `json:"plan"`
	Delivery                []NodeDelivery `json:"delivery"`
	TotalDeliveryEntries    int64          `json:"total_delivery_entries"`
	TotalCompileWork        int64          `json:"total_compile_work"`
	PreviousCacheBytes      int64          `json:"previous_cache_bytes"`
	CacheBytes              int64          `json:"cache_bytes"`
	StorageCost             int64          `json:"storage_cost"`
	CompileCost             int64          `json:"compile_cost"`
	TotalCost               int64          `json:"total_cost"`
}

type State struct {
	SchemaVersion   string      `json:"schema_version"`
	SessionID       string      `json:"session_id"`
	ManifestVersion string      `json:"manifest_version"`
	ManifestHash    string      `json:"manifest_hash"`
	GraphVersion    string      `json:"graph_version"`
	Graph           Graph       `json:"graph"`
	Manifest        Manifest    `json:"manifest"`
	Products        []Product   `json:"products"`
	Cache           []CacheItem `json:"cache"`
	Ledger          Ledger      `json:"ledger"`
}
