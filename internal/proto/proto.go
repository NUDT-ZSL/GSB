package proto

// Request is one JSON-line request. NowMs lets clients inject a deterministic
// "current time" (epoch ms); when zero the server clock is authoritative.
type Request struct {
	Op      string `json:"op"`
	Key     string `json:"key,omitempty"`
	Holder  string `json:"holder,omitempty"`
	Version int64  `json:"lease_version,omitempty"`
	TTLMs   int64  `json:"ttl_ms,omitempty"`
	NowMs   int64  `json:"now_ms,omitempty"`
	Force   bool   `json:"force,omitempty"`
	ID      string `json:"snapshot_id,omitempty"`
	Path    string `json:"path,omitempty"`
}

// Response is the single JSON line returned for every request.
type Response struct {
	OK           bool           `json:"ok"`
	Code         string         `json:"code,omitempty"`
	Message      string         `json:"message,omitempty"`
	Lock         any            `json:"lock,omitempty"`
	Locks        any            `json:"locks,omitempty"`
	Holder       string         `json:"holder,omitempty"`
	ExpiresAtMs  int64          `json:"expires_at_ms,omitempty"`
	LeaseVersion int64          `json:"lease_version,omitempty"`
	Traces       any            `json:"traces,omitempty"`
	Reclaims     any            `json:"reclaims,omitempty"`
	TableVersion int64          `json:"table_version,omitempty"`
	GraphVersion int64          `json:"graph_version,omitempty"`
	Restore      any            `json:"restore,omitempty"`
	Snapshot     any            `json:"snapshot,omitempty"`
	Detail       map[string]any `json:"detail,omitempty"`
}
