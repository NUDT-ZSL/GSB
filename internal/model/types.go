package model

import "localmesh/internal/rules"

const SchemaVersion = 1

type Request struct {
	ID      string            `json:"id"`
	Method  string            `json:"method"`
	Path    string            `json:"path"`
	Headers map[string]string `json:"headers,omitempty"`
	Body    string            `json:"body,omitempty"`
}

type Attempt struct {
	RequestID     string `json:"request_id"`
	RoundID       string `json:"round_id"`
	AttemptNo     int    `json:"attempt_no"`
	InstanceID    string `json:"instance_id"`
	Service       string `json:"service"`
	Endpoint      string `json:"endpoint"`
	StartedMonoMS int64  `json:"started_mono_ms"`
	StartMonoNS   int64  `json:"start_mono_ns"`
	EndMonoNS     int64  `json:"end_mono_ns"`
	DurationMS    int64  `json:"duration_ms"`
	Outcome       string `json:"outcome"` // success or failure
	ErrorCode     string `json:"error_code,omitempty"`
	Error         string `json:"error,omitempty"`
	HTTPStatus    int    `json:"http_status,omitempty"`
	InjectedFault string `json:"injected_fault,omitempty"`
}

type RequestResult struct {
	Request
	RoutePrefix    string    `json:"route_prefix"`
	Service        string    `json:"service"`
	TimeoutMS      int64     `json:"timeout_ms"`
	Retries        int       `json:"retries"`
	AttemptBudget  int       `json:"attempt_budget"`
	Attempts       []Attempt `json:"attempts"`
	Outcome        string    `json:"outcome"` // pending, success, failure, unknown
	ErrorCode      string    `json:"error_code,omitempty"`
	Error          string    `json:"error,omitempty"`
	NeedsRerun     bool      `json:"needs_rerun"`
	QueuedMonoMS   int64     `json:"queued_mono_ms"`
	StartMonoMS    int64     `json:"start_mono_ms,omitempty"`
	EndMonoMS      int64     `json:"end_mono_ms,omitempty"`
	CurrentAttempt *Attempt  `json:"current_attempt,omitempty"`
}

type CircuitState struct {
	InstanceID          string `json:"instance_id"`
	Service             string `json:"service"`
	State               string `json:"state"` // closed, open, half_open
	ConsecutiveFailures int    `json:"consecutive_failures"`
	FailureThreshold    int    `json:"failure_threshold"`
	CooldownMS          int64  `json:"cooldown_ms"`
	OpenedMonoMS        int64  `json:"opened_mono_ms,omitempty"`
	OpenedAt            string `json:"opened_at,omitempty"`
	CooldownUntilMonoMS int64  `json:"cooldown_until_mono_ms,omitempty"`
	CooldownUntilNS     int64  `json:"cooldown_until_ns,omitempty"`
	LastFailureAt       string `json:"last_failure_at,omitempty"`
}

type CircuitEvent struct {
	Type                string   `json:"type"` // breaker_open, breaker_half_open, breaker_close
	RoundID             string   `json:"round_id"`
	RequestID           string   `json:"request_id,omitempty"`
	Service             string   `json:"service"`
	InstanceID          string   `json:"instance_id"`
	Reason              string   `json:"reason"`
	MonoMS              int64    `json:"mono_ms"`
	MonoNS              int64    `json:"mono_ns"`
	TriggeredAt         string   `json:"triggered_at"`
	CooldownMS          int64    `json:"cooldown_ms"`
	CooldownUntilMonoMS int64    `json:"cooldown_until_mono_ms,omitempty"`
	CooldownUntilNS     int64    `json:"cooldown_until_ns,omitempty"`
	TriggeringInstances []string `json:"triggering_instances,omitempty"`
}

type TraceEvent struct {
	Type                string   `json:"type"`
	RoundID             string   `json:"round_id"`
	RequestID           string   `json:"request_id,omitempty"`
	Service             string   `json:"service,omitempty"`
	InstanceID          string   `json:"instance_id,omitempty"`
	AttemptNo           int      `json:"attempt_no,omitempty"`
	MonoMS              int64    `json:"mono_ms"`
	MonoNS              int64    `json:"mono_ns"`
	StartMonoNS         int64    `json:"start_mono_ns,omitempty"`
	EndMonoNS           int64    `json:"end_mono_ns,omitempty"`
	DurationMS          int64    `json:"duration_ms,omitempty"`
	ActiveGlobal        int      `json:"active_global,omitempty"`
	ActiveService       int      `json:"active_service,omitempty"`
	Outcome             string   `json:"outcome,omitempty"`
	ErrorCode           string   `json:"error_code,omitempty"`
	Error               string   `json:"error,omitempty"`
	HTTPStatus          int      `json:"http_status,omitempty"`
	Reason              string   `json:"reason,omitempty"`
	TriggeredAt         string   `json:"triggered_at,omitempty"`
	CooldownUntilMonoMS int64    `json:"cooldown_until_mono_ms,omitempty"`
	CooldownUntilNS     int64    `json:"cooldown_until_ns,omitempty"`
	TriggeringInstances []string `json:"triggering_instances,omitempty"`
}

type QuotaStats struct {
	GlobalLimit            int            `json:"global_limit"`
	GlobalPeak             int            `json:"global_peak"`
	ServiceLimits          map[string]int `json:"service_limits"`
	ServicePeaks           map[string]int `json:"service_peaks"`
	RecoveredActiveGlobal  int            `json:"recovered_active_global,omitempty"`
	RecoveredActiveService map[string]int `json:"recovered_active_service,omitempty"`
}

type Summary struct {
	RoundID          string         `json:"round_id"`
	RulesVersion     string         `json:"rules_version"`
	Total            int            `json:"total"`
	Success          int            `json:"success"`
	Failure          int            `json:"failure"`
	Unknown          int            `json:"unknown"`
	BreakerTriggers  int            `json:"breaker_triggers"`
	QuotaPeakGlobal  int            `json:"quota_peak_global"`
	QuotaPeakService map[string]int `json:"quota_peak_service"`
}

type RoundMeta struct {
	ID                    string `json:"id"`
	Status                string `json:"status"` // running, completed, recovered, invalidated
	StartedAt             string `json:"started_at"`
	FinishedAt            string `json:"finished_at,omitempty"`
	RulesPath             string `json:"rules_path"`
	RulesVersion          string `json:"rules_version"`
	DeclaredVersion       string `json:"declared_version"`
	RulesHash             string `json:"rules_hash"`
	RulesChangedDuringRun bool   `json:"rules_changed_during_run,omitempty"`
	ExitReportAvailable   bool   `json:"exit_report_available"`
}

type Document struct {
	SchemaVersion        int                     `json:"schema_version"`
	Round                RoundMeta               `json:"round"`
	Rules                rules.Config            `json:"rules"`
	Requests             []RequestResult         `json:"requests"`
	CircuitEvents        []CircuitEvent          `json:"circuit_events"`
	Breakers             map[string]CircuitState `json:"breakers"`
	Quota                QuotaStats              `json:"quota"`
	TracePath            string                  `json:"trace_path"`
	RouteFingerprints    map[string]string       `json:"route_fingerprints"`
	InstanceFingerprints map[string]string       `json:"instance_fingerprints"`
	Summary              Summary                 `json:"summary"`
}

type RequestState struct {
	Request        Request   `json:"request"`
	RoutePrefix    string    `json:"route_prefix"`
	Service        string    `json:"service"`
	TimeoutMS      int64     `json:"timeout_ms"`
	Retries        int       `json:"retries"`
	AttemptBudget  int       `json:"attempt_budget"`
	Status         string    `json:"status"` // pending, running, done, unknown
	Attempts       []Attempt `json:"attempts"`
	CurrentAttempt *Attempt  `json:"current_attempt,omitempty"`
	Outcome        string    `json:"outcome"`
	ErrorCode      string    `json:"error_code,omitempty"`
	Error          string    `json:"error,omitempty"`
	NeedsRerun     bool      `json:"needs_rerun"`
}

type Checkpoint struct {
	SchemaVersion   int                     `json:"schema_version"`
	RoundID         string                  `json:"round_id"`
	Status          string                  `json:"status"` // running, completed
	StartedAt       string                  `json:"started_at"`
	SavedAt         string                  `json:"saved_at"`
	RulesPath       string                  `json:"rules_path"`
	RulesVersion    string                  `json:"rules_version"`
	DeclaredVersion string                  `json:"declared_version"`
	RulesHash       string                  `json:"rules_hash"`
	Rules           rules.Config            `json:"rules"`
	Requests        []RequestState          `json:"requests"`
	ActiveGlobal    int                     `json:"active_global"`
	ActiveService   map[string]int          `json:"active_service"`
	Trace           []TraceEvent            `json:"trace"`
	CircuitEvents   []CircuitEvent          `json:"circuit_events"`
	Breakers        map[string]CircuitState `json:"breakers"`
	Quota           QuotaStats              `json:"quota"`
	Completed       int                     `json:"completed"`
	InFlight        []string                `json:"in_flight,omitempty"`
}

type VerificationReport struct {
	OK               bool           `json:"ok"`
	Code             string         `json:"code"`
	RoundID          string         `json:"round_id"`
	RulesVersion     string         `json:"rules_version"`
	Total            int            `json:"total"`
	Success          int            `json:"success"`
	Failure          int            `json:"failure"`
	Unknown          int            `json:"unknown"`
	BreakerTriggers  int            `json:"breaker_triggers"`
	QuotaPeakGlobal  int            `json:"quota_peak_global"`
	QuotaPeakService map[string]int `json:"quota_peak_service"`
	TracePath        string         `json:"trace_path"`
	SnapshotPath     string         `json:"snapshot_path"`
	FirstDeviation   *Deviation     `json:"first_deviation,omitempty"`
	Deviations       []Deviation    `json:"deviations,omitempty"`
}

type Deviation struct {
	RequestID string `json:"request_id"`
	Field     string `json:"field"`
	Message   string `json:"message"`
}
