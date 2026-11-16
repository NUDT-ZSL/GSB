package engine

import (
	"sync"
	"time"

	"github.com/NUDT-ZSL/GSB/orderqueue/internal/protocol"
	"github.com/NUDT-ZSL/GSB/orderqueue/internal/store"
)

const (
	defaultMaxBodyBytes     = 64 * 1024
	defaultMaxBatch         = 1000
	defaultWindowTimeout    = 5 * time.Second
	defaultMaxWindowTimeout = 5 * time.Minute
	defaultMinWindowTimeout = 10 * time.Millisecond
	defaultSweepInterval    = 25 * time.Millisecond
)

type Options struct {
	DBPath               string
	Logger               Logger
	Clock                Clock
	MaxRetries           int
	DefaultWindowTimeout time.Duration
	MaxWindowTimeout     time.Duration
	MinWindowTimeout     time.Duration
	MaxBatch             int
	MaxBodyBytes         int
	SweepInterval        time.Duration
	DisableSweeper       bool
	RecoveryReportPath   string
}

type Logger interface {
	Log(event string, fields map[string]any)
}

type Engine struct {
	store                *store.Store
	logger               Logger
	clock                Clock
	startedAt            time.Time
	maxRetries           int
	defaultWindowTimeout time.Duration
	maxWindowTimeout     time.Duration
	minWindowTimeout     time.Duration
	maxBatch             int
	maxBodyBytes         int
	sweepInterval        time.Duration
	recoveryReportPath   string
	recoveryReport       RecoveryReport

	partitionsMu sync.RWMutex
	partitions   map[string]*Partition

	producersMu sync.Mutex
	producers   map[string]*producerState

	stop chan struct{}
	wg   sync.WaitGroup

	closeOnce sync.Once
	closeErr  error
}

type producerState struct {
	mu      sync.Mutex
	lastSeq int64
}

type Partition struct {
	mu    sync.Mutex
	state partitionState
}

type partitionState struct {
	Key               string
	NextOffset        int64
	CommittedOffset   int64
	ConfirmedOffset   int64
	Window            *deliveryWindow
	RetryCounts       map[int64]int
	PendingRedelivery bool
	RedeliveryFrom    int64
	Blocked           bool
	BlockedOffset     int64
	BlockedRetryCount int
	LedgerVersion     int64
	StateRevision     int64
}

type deliveryWindow struct {
	Holder      string
	StartOffset int64
	EndOffset   int64
	Deadline    time.Time
	IssuedAt    time.Time
	TTL         time.Duration
	Recovered   bool
}

func (s partitionState) clone() partitionState {
	next := s
	next.RetryCounts = make(map[int64]int, len(s.RetryCounts))
	for offset, attempts := range s.RetryCounts {
		next.RetryCounts[offset] = attempts
	}
	if s.Window != nil {
		window := *s.Window
		next.Window = &window
	}
	return next
}

func stateFromRow(row store.PartitionRow, retries map[int64]int) partitionState {
	state := partitionState{
		Key:               row.PartitionKey,
		NextOffset:        row.NextOffset,
		CommittedOffset:   row.CommittedOffset,
		ConfirmedOffset:   row.ConfirmedOffset,
		RetryCounts:       retries,
		PendingRedelivery: row.PendingRedelivery,
		RedeliveryFrom:    row.RedeliveryFrom,
		Blocked:           row.Blocked,
		BlockedOffset:     row.BlockedOffset,
		BlockedRetryCount: row.BlockedRetryCount,
		LedgerVersion:     row.LedgerVersion,
		StateRevision:     row.StateRevision,
	}
	if state.NextOffset == 0 {
		state.NextOffset = 1
	}
	if state.LedgerVersion == 0 {
		state.LedgerVersion = store.LedgerFormatVersion
	}
	if row.HasWindow() {
		state.Window = &deliveryWindow{
			Holder:      row.WindowHolder,
			StartOffset: row.WindowStart,
			EndOffset:   row.WindowEnd,
			Recovered:   row.WindowRecovered,
			TTL:         time.Duration(row.WindowTTLMS) * time.Millisecond,
		}
		if row.WindowDeadlineUnixMS > 0 && !row.WindowRecovered {
			state.Window.Deadline = time.UnixMilli(row.WindowDeadlineUnixMS)
			state.Window.IssuedAt = state.Window.Deadline.Add(-state.Window.TTL)
		}
	}
	return state
}

func (s partitionState) row() store.PartitionRow {
	row := store.PartitionRow{
		PartitionKey:      s.Key,
		NextOffset:        s.NextOffset,
		CommittedOffset:   s.CommittedOffset,
		ConfirmedOffset:   s.ConfirmedOffset,
		PendingRedelivery: s.PendingRedelivery,
		RedeliveryFrom:    s.RedeliveryFrom,
		Blocked:           s.Blocked,
		BlockedOffset:     s.BlockedOffset,
		BlockedRetryCount: s.BlockedRetryCount,
		LedgerVersion:     s.LedgerVersion,
		StateRevision:     s.StateRevision,
	}
	if s.Window != nil {
		row.WindowHolder = s.Window.Holder
		row.WindowStart = s.Window.StartOffset
		row.WindowEnd = s.Window.EndOffset
		row.WindowTTLMS = s.Window.TTL.Milliseconds()
		row.WindowRecovered = s.Window.Recovered
		if !s.Window.Deadline.IsZero() {
			row.WindowDeadlineUnixMS = s.Window.Deadline.UnixMilli()
		}
	}
	return row
}

func (s partitionState) view(now time.Time) protocol.PartitionView {
	retries := make(map[string]int, len(s.RetryCounts))
	for offset, attempts := range s.RetryCounts {
		retries[formatOffset(offset)] = attempts
	}
	view := protocol.PartitionView{
		PartitionKey:      s.Key,
		NextOffset:        s.NextOffset,
		CommittedOffset:   s.CommittedOffset,
		ConfirmedOffset:   s.ConfirmedOffset,
		RetryCounts:       retries,
		PendingRedelivery: s.PendingRedelivery,
		RedeliveryFrom:    s.RedeliveryFrom,
		Blocked:           s.Blocked,
		BlockedOffset:     s.BlockedOffset,
		BlockedRetryCount: s.BlockedRetryCount,
		LedgerVersion:     s.LedgerVersion,
		StateRevision:     s.StateRevision,
	}
	if s.Window != nil {
		window := &protocol.WindowInfo{
			Holder:      s.Window.Holder,
			StartOffset: s.Window.StartOffset,
			EndOffset:   s.Window.EndOffset,
			TTLMS:       s.Window.TTL.Milliseconds(),
			Recovered:   s.Window.Recovered,
		}
		if !s.Window.Deadline.IsZero() {
			window.DeadlineUnixMS = s.Window.Deadline.UnixMilli()
		}
		view.Window = window
	}
	return view
}

type AppendResult struct {
	PartitionKey string
	Offset       int64
	NextOffset   int64
}

type PullResult struct {
	PartitionKey string
	Session      string
	Window       *protocol.WindowInfo
	Messages     []protocol.MessageView
	RetryCounts  map[string]int
}

type CommitResult struct {
	PartitionKey    string
	CommittedOffset int64
	ConfirmedOffset int64
	Window          *protocol.WindowInfo
}

type FailResult struct {
	PartitionKey      string
	RedeliveryFrom    int64
	Blocked           bool
	BlockedOffset     int64
	BlockedRetryCount int
}

type UnblockResult struct {
	PartitionKey string
}

type StatusResult struct {
	LogFormatVersion    int64
	LedgerFormatVersion int64
	MaxRetries          int
	Partitions          []protocol.PartitionView
}

type RecoveryReport struct {
	FormatVersion         int64               `json:"format_version"`
	LedgerFormatVersion   int64               `json:"ledger_format_version"`
	GeneratedAt           string              `json:"generated_at"`
	RecoveredPartitions   int                 `json:"recovered_partitions"`
	Partitions            []RecoveryPartition `json:"partitions"`
	RecoveredWindows      []RecoveredWindow   `json:"recovered_windows"`
	VersionRebuild        store.VersionReport `json:"version_rebuild"`
	TailIncompleteRecords int64               `json:"tail_incomplete_records"`
	TailPolicy            string              `json:"tail_policy"`
}

type RecoveryPartition struct {
	PartitionKey        string         `json:"partition_key"`
	NextOffset          int64          `json:"next_offset"`
	CommittedOffset     int64          `json:"committed_offset"`
	ConfirmedOffset     int64          `json:"confirmed_offset"`
	RetryPendingOffsets []int64        `json:"retry_pending_offsets"`
	RetryCounts         map[string]int `json:"retry_counts"`
	Blocked             bool           `json:"blocked"`
	BlockedOffset       int64          `json:"blocked_offset,omitempty"`
	BlockedRetryCount   int            `json:"blocked_retry_count,omitempty"`
}

type RecoveredWindow struct {
	PartitionKey string `json:"partition_key"`
	Holder       string `json:"holder"`
	StartOffset  int64  `json:"start_offset"`
	EndOffset    int64  `json:"end_offset"`
}
