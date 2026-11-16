package verify

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"time"

	"github.com/NUDT-ZSL/GSB/orderqueue/internal/protocol"
	"github.com/NUDT-ZSL/GSB/orderqueue/internal/store"
)

type Options struct {
	DBPath     string
	ReportPath string
}

type Report struct {
	OK                  bool        `json:"ok"`
	Code                string      `json:"code"`
	GeneratedAt         string      `json:"generated_at"`
	DBPath              string      `json:"db_path"`
	LogFormatVersion    int64       `json:"log_format_version"`
	LedgerFormatVersion int64       `json:"ledger_format_version"`
	PartitionsChecked   int         `json:"partitions_checked"`
	MessagesChecked     int         `json:"messages_checked"`
	EventsChecked       int         `json:"events_checked"`
	FirstViolation      *Violation  `json:"first_violation,omitempty"`
	Violations          []Violation `json:"violations"`
}

type Violation struct {
	PartitionKey string `json:"partition_key"`
	Offset       int64  `json:"offset,omitempty"`
	EventID      int64  `json:"event_id,omitempty"`
	Field        string `json:"field"`
	Expected     any    `json:"expected,omitempty"`
	Actual       any    `json:"actual,omitempty"`
	Detail       string `json:"detail"`
}

type verifier struct {
	snapshot map[string]store.PartitionSnapshot
	messages map[string][]store.MessageRow
	ledger   map[string]store.PartitionRow
	meta     store.Meta
	report   Report
}

type replayWindow struct {
	holder string
	start  int64
	end    int64
}

func Run(ctx context.Context, opts Options) (Report, error) {
	report := Report{
		Code:        protocol.CodeVerify,
		GeneratedAt: time.Now().UTC().Format(time.RFC3339Nano),
		DBPath:      opts.DBPath,
		Violations:  []Violation{},
	}
	db, err := store.OpenReadOnly(opts.DBPath)
	if err != nil {
		return report, err
	}
	defer db.Close()
	if err := db.MustHaveSchema(ctx); err != nil {
		return report, err
	}
	meta, err := db.LoadMeta(ctx)
	if err != nil {
		return report, err
	}
	report.LogFormatVersion = meta.LogFormatVersion
	report.LedgerFormatVersion = meta.LedgerFormatVersion
	partitions, err := db.ListPartitions(ctx)
	if err != nil {
		return report, err
	}
	keys, err := db.SortedPartitionKeys(ctx)
	if err != nil {
		return report, err
	}
	allMessages, err := db.ListAllMessages(ctx)
	if err != nil {
		return report, err
	}
	v := &verifier{
		snapshot: map[string]store.PartitionSnapshot{},
		messages: allMessages,
		ledger:   map[string]store.PartitionRow{},
		meta:     meta,
		report:   report,
	}
	for _, partition := range partitions {
		v.ledger[partition.PartitionKey] = partition
	}
	for _, key := range keys {
		snapshot, err := db.LoadSnapshot(ctx, key)
		if err != nil {
			return report, err
		}
		v.snapshot[key] = snapshot
		v.report.MessagesChecked += len(snapshot.Messages)
		v.report.EventsChecked += len(snapshot.Events)
	}
	v.report.PartitionsChecked = len(keys)
	if meta.LogFormatVersion != store.LogFormatVersion || meta.LedgerFormatVersion != store.LedgerFormatVersion {
		v.add(Violation{Field: "format_version", Expected: map[string]int64{"log": store.LogFormatVersion, "ledger": store.LedgerFormatVersion}, Actual: map[string]int64{"log": meta.LogFormatVersion, "ledger": meta.LedgerFormatVersion}, Detail: "database format versions do not match this verifier"})
	}
	if meta.MaxRetries < 0 {
		v.add(Violation{Field: "max_retries", Actual: meta.MaxRetries, Detail: "max retries is negative"})
	}
	v.verifyAppendOrder(keys)
	for _, key := range keys {
		v.verifyPartition(key)
	}
	if len(v.report.Violations) > 0 {
		first := v.report.Violations[0]
		v.report.FirstViolation = &first
		v.report.OK = false
		v.report.Code = protocol.CodeVerify
	} else {
		v.report.OK = true
		v.report.Code = protocol.CodeOK
	}
	return v.report, nil
}

func (v *verifier) verifyAppendOrder(keys []string) {
	type appendRecord struct {
		event store.EventRow
		seq   int64
	}
	var records []appendRecord
	for _, key := range keys {
		for _, event := range v.snapshot[key].Events {
			if event.Kind != "append" {
				continue
			}
			var detail struct {
				ProducerSeq int64 `json:"producer_seq"`
			}
			_ = json.Unmarshal([]byte(event.Detail), &detail)
			records = append(records, appendRecord{event: event, seq: detail.ProducerSeq})
		}
	}
	sort.Slice(records, func(i, j int) bool { return records[i].event.EventID < records[j].event.EventID })
	last := map[string]int64{}
	for _, record := range records {
		event := record.event
		if record.seq <= last[event.Session] {
			v.add(Violation{
				PartitionKey: event.PartitionKey,
				Offset:       event.Offset,
				EventID:      event.EventID,
				Field:        "producer_seq",
				Expected:     last[event.Session] + 1,
				Actual:       record.seq,
				Detail:       "producer sequence is not strictly increasing in durable append-event order",
			})
		}
		last[event.Session] = record.seq
	}
}

func (v *verifier) verifyPartition(key string) {
	snapshot := v.snapshot[key]
	messages := snapshot.Messages
	ledger, hasLedger := v.ledger[key]
	if !hasLedger {
		v.add(Violation{PartitionKey: key, Field: "partition_row", Detail: "durable partition does not have a ledger row"})
		return
	}
	expectedNext := int64(1)
	appendEvents := map[int64]store.EventRow{}
	for _, event := range snapshot.Events {
		if event.Kind == "append" {
			if _, exists := appendEvents[event.Offset]; exists {
				v.add(Violation{PartitionKey: key, Offset: event.Offset, EventID: event.EventID, Field: "append_event", Detail: "duplicate append event for offset"})
			}
			appendEvents[event.Offset] = event
		}
	}
	for _, message := range messages {
		if message.Offset != expectedNext {
			v.add(Violation{PartitionKey: key, Offset: message.Offset, Field: "message_offset", Expected: expectedNext, Actual: message.Offset, Detail: "message log is not contiguous"})
		}
		expectedNext++
		if message.ProducerSeq < 1 {
			v.add(Violation{PartitionKey: key, Offset: message.Offset, Field: "producer_seq", Actual: message.ProducerSeq, Detail: "producer sequence is not positive"})
		}
		event, ok := appendEvents[message.Offset]
		if !ok {
			v.add(Violation{PartitionKey: key, Offset: message.Offset, Field: "append_event", Detail: "message has no durable append event"})
			continue
		}
		if event.Session != message.ProducerSession {
			v.add(Violation{PartitionKey: key, Offset: message.Offset, EventID: event.EventID, Field: "append_session", Expected: message.ProducerSession, Actual: event.Session, Detail: "append event producer session differs from message log"})
		}
		var detail struct {
			ProducerSeq int64 `json:"producer_seq"`
		}
		if err := json.Unmarshal([]byte(event.Detail), &detail); err != nil || detail.ProducerSeq != message.ProducerSeq {
			v.add(Violation{PartitionKey: key, Offset: message.Offset, EventID: event.EventID, Field: "append_producer_seq", Expected: message.ProducerSeq, Actual: detail.ProducerSeq, Detail: "append event sequence differs from message log"})
		}
	}
	for offset, event := range appendEvents {
		if offset < 1 || offset >= expectedNext {
			v.add(Violation{PartitionKey: key, Offset: offset, EventID: event.EventID, Field: "append_event_offset", Expected: expectedNext - 1, Actual: offset, Detail: "append event points outside the message log"})
		}
	}
	if ledger.NextOffset != expectedNext {
		v.add(Violation{PartitionKey: key, Offset: expectedNext - 1, Field: "next_offset", Expected: expectedNext, Actual: ledger.NextOffset, Detail: "ledger next offset does not match the message tail"})
	}
	if ledger.LedgerVersion != store.LedgerFormatVersion {
		v.add(Violation{PartitionKey: key, Field: "ledger_version", Expected: store.LedgerFormatVersion, Actual: ledger.LedgerVersion, Detail: "partition ledger version differs from the supported version"})
	}

	committed := int64(0)
	confirmed := int64(0)
	retries := map[int64]int{}
	blocked := false
	blockedOffset := int64(0)
	blockedCount := 0
	var active *replayWindow
	for _, event := range snapshot.Events {
		switch event.Kind {
		case "window_issued":
			if blocked {
				v.add(Violation{PartitionKey: key, Offset: event.StartOffset, EventID: event.EventID, Field: "window_issued", Detail: "window issued while partition is blocked"})
			}
			if active != nil {
				v.add(Violation{PartitionKey: key, Offset: event.StartOffset, EventID: event.EventID, Field: "window_issued", Expected: active.end, Actual: event.StartOffset, Detail: "new window issued before the previous window closed"})
			}
			if event.StartOffset != committed+1 {
				v.add(Violation{PartitionKey: key, Offset: event.StartOffset, EventID: event.EventID, Field: "window_start", Expected: committed + 1, Actual: event.StartOffset, Detail: "window does not start at the earliest uncommitted offset"})
			}
			if event.EndOffset < event.StartOffset || event.EndOffset >= expectedNext {
				v.add(Violation{PartitionKey: key, Offset: event.EndOffset, EventID: event.EventID, Field: "window_end", Expected: expectedNext - 1, Actual: event.EndOffset, Detail: "window end exceeds the durable append tail"})
			}
			active = &replayWindow{holder: event.Session, start: event.StartOffset, end: event.EndOffset}
		case "commit":
			if active == nil {
				v.add(Violation{PartitionKey: key, Offset: event.Offset, EventID: event.EventID, Field: "commit", Detail: "commit occurred without an active ordered window"})
				continue
			}
			if event.Session != active.holder {
				v.add(Violation{PartitionKey: key, Offset: event.Offset, EventID: event.EventID, Field: "commit_session", Expected: active.holder, Actual: event.Session, Detail: "commit session does not hold the active window"})
			}
			if event.CommittedBefore != committed {
				v.add(Violation{PartitionKey: key, Offset: event.Offset, EventID: event.EventID, Field: "committed_before", Expected: committed, Actual: event.CommittedBefore, Detail: "commit event before-offset is not the replayed ledger position"})
			}
			if event.CommittedAfter <= committed || event.CommittedAfter > active.end || event.CommittedAfter < active.start {
				v.add(Violation{PartitionKey: key, Offset: event.Offset, EventID: event.EventID, Field: "commit_order", Expected: map[string]int64{"minimum": active.start, "maximum": active.end}, Actual: event.CommittedAfter, Detail: "commit is outside the active window or moves backwards"})
			} else {
				committed = event.CommittedAfter
				confirmed = event.CommittedAfter
				if committed == active.end {
					active = nil
				} else {
					active.start = committed + 1
				}
			}
		case "fail", "timeout", "recovery_redelivery":
			if active == nil {
				v.add(Violation{PartitionKey: key, Offset: event.Offset, EventID: event.EventID, Field: event.Kind, Detail: "redelivery event occurred without an active window"})
				continue
			}
			if event.StartOffset != active.start || event.EndOffset != active.end {
				v.add(Violation{PartitionKey: key, Offset: event.StartOffset, EventID: event.EventID, Field: event.Kind + "_range", Expected: map[string]int64{"start": active.start, "end": active.end}, Actual: map[string]int64{"start": event.StartOffset, "end": event.EndOffset}, Detail: "redelivery did not replay the complete earliest-unconfirmed window"})
			}
			for offset := event.StartOffset; offset <= event.EndOffset; offset++ {
				retries[offset]++
			}
			active = nil
		case "poison":
			blocked = true
			blockedOffset = event.Offset
			blockedCount = event.RetryCount
			if event.RetryCount != retries[event.Offset] {
				v.add(Violation{PartitionKey: key, Offset: event.Offset, EventID: event.EventID, Field: "blocked_retry_count", Expected: retries[event.Offset], Actual: event.RetryCount, Detail: "poison retry count does not match replayed per-offset retries"})
			}
			if event.RetryCount <= v.meta.MaxRetries {
				v.add(Violation{PartitionKey: key, Offset: event.Offset, EventID: event.EventID, Field: "poison_threshold", Expected: v.meta.MaxRetries + 1, Actual: event.RetryCount, Detail: "poison was declared before exceeding max retries"})
			}
		case "unblock":
			if !blocked {
				v.add(Violation{PartitionKey: key, Offset: event.Offset, EventID: event.EventID, Field: "unblock", Detail: "unblock event without a blocked ledger"})
			}
			blocked = false
			blockedOffset = 0
			blockedCount = 0
			for offset := event.StartOffset; offset <= event.EndOffset; offset++ {
				delete(retries, offset)
			}
		case "append", "recovery_detected", "version_rebuild":
		default:
			v.add(Violation{PartitionKey: key, Offset: event.Offset, EventID: event.EventID, Field: "event_kind", Actual: event.Kind, Detail: "unknown durable event kind"})
		}
	}

	if active != nil {
		if ledger.WindowHolder != active.holder || ledger.WindowStart != active.start || ledger.WindowEnd != active.end {
			v.add(Violation{PartitionKey: key, Offset: active.start, Field: "window", Expected: map[string]any{"holder": active.holder, "start": active.start, "end": active.end}, Actual: map[string]any{"holder": ledger.WindowHolder, "start": ledger.WindowStart, "end": ledger.WindowEnd}, Detail: "active replay window differs from the ledger"})
		}
	} else if ledger.WindowHolder != "" {
		v.add(Violation{PartitionKey: key, Offset: ledger.WindowStart, Field: "window", Expected: "", Actual: ledger.WindowHolder, Detail: "ledger retains a window that replay closed"})
	}
	if ledger.CommittedOffset != committed {
		v.add(Violation{PartitionKey: key, Offset: committed, Field: "committed_offset", Expected: committed, Actual: ledger.CommittedOffset, Detail: "ledger committed offset differs from the commit journal replay"})
	}
	if ledger.ConfirmedOffset != confirmed {
		v.add(Violation{PartitionKey: key, Offset: confirmed, Field: "confirmed_offset", Expected: confirmed, Actual: ledger.ConfirmedOffset, Detail: "ledger confirmed offset differs from the commit journal replay"})
	}
	if ledger.CommittedOffset > ledger.ConfirmedOffset {
		v.add(Violation{PartitionKey: key, Offset: ledger.CommittedOffset, Field: "ledger_invariant", Expected: "committed_offset <= confirmed_offset", Actual: fmt.Sprintf("%d > %d", ledger.CommittedOffset, ledger.ConfirmedOffset), Detail: "committed offset exceeds confirmed offset"})
	}
	if ledger.Blocked != blocked || ledger.BlockedOffset != blockedOffset || ledger.BlockedRetryCount != blockedCount {
		v.add(Violation{PartitionKey: key, Offset: blockedOffset, Field: "blocked_state", Expected: map[string]any{"blocked": blocked, "offset": blockedOffset, "retry_count": blockedCount}, Actual: map[string]any{"blocked": ledger.Blocked, "offset": ledger.BlockedOffset, "retry_count": ledger.BlockedRetryCount}, Detail: "blocked ledger state differs from the poison journal"})
	}
	v.compareRetries(key, retries, snapshot.RetryCounts)
}

func (v *verifier) compareRetries(key string, replayed, stored map[int64]int) {
	allOffsets := map[int64]bool{}
	for offset := range replayed {
		allOffsets[offset] = true
	}
	for offset := range stored {
		allOffsets[offset] = true
	}
	offsets := make([]int64, 0, len(allOffsets))
	for offset := range allOffsets {
		offsets = append(offsets, offset)
	}
	sort.Slice(offsets, func(i, j int) bool { return offsets[i] < offsets[j] })
	for _, offset := range offsets {
		if replayed[offset] != stored[offset] {
			v.add(Violation{PartitionKey: key, Offset: offset, Field: "retry_counts", Expected: replayed[offset], Actual: stored[offset], Detail: "durable per-offset retry count differs from the redelivery journal replay"})
		}
	}
}

func (v *verifier) add(violation Violation) {
	if len(v.report.Violations) >= 200 {
		return
	}
	v.report.Violations = append(v.report.Violations, violation)
}

func (r Report) Write(path string) error {
	if path == "" {
		return nil
	}
	data, err := json.MarshalIndent(r, "", "  ")
	if err != nil {
		return err
	}
	data = append(data, '\n')
	return writeAtomic(path, data)
}

func writeAtomic(path string, data []byte) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil && filepath.Dir(path) != "." {
		return err
	}
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, data, 0o644); err != nil {
		return err
	}
	_ = os.Remove(path)
	return os.Rename(tmp, path)
}
