package model

// Lock is the persisted record of one lock key.
// All timestamps are Unix epoch milliseconds stored as int64.
type Lock struct {
	Key        string   `json:"key"`
	Holder     string   `json:"holder"`
	ExpiresAt  int64    `json:"expires_at_ms"`
	Version    int64    `json:"lease_version"`
	AcquiredAt int64    `json:"acquired_at_ms"`
	DependsOn  []string `json:"dependencies"`
}

// TraceEntry is one step of a lease-invalidation propagation event.
type TraceEntry struct {
	Seq       int64  `json:"seq"`
	EventID   int64  `json:"event_id"`
	Cause     string `json:"cause"`
	SourceKey string `json:"source_key"`
	Affected  string `json:"affected_key"`
	Version   int64  `json:"affected_version"`
	AtMs      int64  `json:"at_ms"`
}

// ReclaimRecord documents server-side reclamation of a lock, including the
// lease version the lock carried when it was reaped.
type ReclaimRecord struct {
	Key     string `json:"key"`
	Holder  string `json:"holder"`
	Reason  string `json:"reason"`
	Version int64  `json:"lease_version"`
	AtMs    int64  `json:"at_ms"`
}

// StateFile is the incremental lock-state file persisted after every mutation.
type StateFile struct {
	TableVersion int64            `json:"table_version"`
	GraphVersion int64            `json:"graph_version"`
	Locks        map[string]*Lock `json:"locks"`
	UpdatedAtMs  int64            `json:"updated_at_ms"`
}

// Node is one node in the declarative dependency definition file.
type Node struct {
	Key       string   `json:"key"`
	DependsOn []string `json:"depends_on"`
}

// GraphFile is the declarative lock/dependency definition file.
type GraphFile struct {
	Version int64  `json:"graph_version"`
	Nodes   []Node `json:"nodes"`
}

// Snapshot is a complete, self-contained session snapshot. It replaces the
// live lock table wholesale on restore rather than being merged field by field.
type Snapshot struct {
	Kind         string           `json:"kind"`
	SnapshotID   string           `json:"snapshot_id"`
	TableVersion int64            `json:"table_version"`
	GraphVersion int64            `json:"graph_version"`
	Graph        GraphFile        `json:"graph"`
	Locks        map[string]*Lock `json:"locks"`
	KeyVersions  map[string]int64 `json:"key_versions"`
	Traces       []TraceEntry     `json:"propagation_traces"`
	Reclaims     []ReclaimRecord  `json:"reclaims"`
	TakenAtMs    int64            `json:"taken_at_ms"`
}
