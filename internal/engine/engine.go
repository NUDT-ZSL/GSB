package engine

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/NUDT-ZSL/GSB/orderqueue/internal/protocol"
	"github.com/NUDT-ZSL/GSB/orderqueue/internal/store"
)

type discardLogger struct{}

func (discardLogger) Log(string, map[string]any) {}

func Open(ctx context.Context, opts Options) (*Engine, error) {
	if opts.DBPath == "" {
		return nil, protocol.NewError(protocol.CodeLedger, "database path is required")
	}
	if opts.MaxRetries < 0 {
		return nil, protocol.NewError(protocol.CodeLedger, "max retries must be non-negative")
	}
	if opts.Logger == nil {
		opts.Logger = discardLogger{}
	}
	if opts.Clock == nil {
		opts.Clock = RealClock{}
	}
	if opts.DefaultWindowTimeout <= 0 {
		opts.DefaultWindowTimeout = defaultWindowTimeout
	}
	if opts.MaxWindowTimeout <= 0 {
		opts.MaxWindowTimeout = defaultMaxWindowTimeout
	}
	if opts.MinWindowTimeout <= 0 {
		opts.MinWindowTimeout = defaultMinWindowTimeout
	}
	if opts.MaxBatch <= 0 {
		opts.MaxBatch = defaultMaxBatch
	}
	if opts.MaxBodyBytes <= 0 {
		opts.MaxBodyBytes = defaultMaxBodyBytes
	}
	if opts.SweepInterval <= 0 {
		opts.SweepInterval = defaultSweepInterval
	}

	db, err := store.Open(opts.DBPath)
	if err != nil {
		return nil, protocol.NewError(protocol.CodeLedger, "open SQLite: %v", err)
	}
	versionReport, err := db.NormalizeVersions(ctx, opts.MaxRetries)
	if err != nil {
		_ = db.Close()
		return nil, asLedgerError(err)
	}
	e := &Engine{
		store:                db,
		logger:               opts.Logger,
		clock:                opts.Clock,
		startedAt:            opts.Clock.Now(),
		maxRetries:           opts.MaxRetries,
		defaultWindowTimeout: opts.DefaultWindowTimeout,
		maxWindowTimeout:     opts.MaxWindowTimeout,
		minWindowTimeout:     opts.MinWindowTimeout,
		maxBatch:             opts.MaxBatch,
		maxBodyBytes:         opts.MaxBodyBytes,
		sweepInterval:        opts.SweepInterval,
		recoveryReportPath:   opts.RecoveryReportPath,
		partitions:           map[string]*Partition{},
		producers:            map[string]*producerState{},
		stop:                 make(chan struct{}),
	}
	if versionReport.Rebuilt {
		e.emit("version_rebuild", map[string]any{
			"reason":                versionReport.Reason,
			"rebuilt_partitions":    versionReport.RebuiltPartitions,
			"log_format_version":    versionReport.LogVersion,
			"ledger_format_version": versionReport.LedgerVersion,
		})
	}
	if err := e.loadState(ctx, versionReport); err != nil {
		_ = db.Close()
		return nil, err
	}
	if !opts.DisableSweeper {
		e.startSweeper()
	}
	return e, nil
}

func (e *Engine) loadState(ctx context.Context, versionReport store.VersionReport) error {
	rows, err := e.store.ListPartitions(ctx)
	if err != nil {
		return protocol.NewError(protocol.CodeLedger, "load partitions: %v", err)
	}
	producerSessions, err := e.store.LoadProducerSessions(ctx)
	if err != nil {
		return protocol.NewError(protocol.CodeLedger, "load producer sessions: %v", err)
	}
	for session, seq := range producerSessions {
		e.producers[session] = &producerState{lastSeq: seq}
	}

	producerMax := map[string]int64{}
	knownPartitions := map[string]store.PartitionRow{}
	for _, row := range rows {
		knownPartitions[row.PartitionKey] = row
		state, err := e.loadPartitionState(ctx, row)
		if err != nil {
			return err
		}
		e.partitions[row.PartitionKey] = &Partition{state: state}
	}

	keys, err := e.store.ListPartitionKeys(ctx)
	if err != nil {
		return protocol.NewError(protocol.CodeLedger, "list partition keys: %v", err)
	}
	for _, key := range keys {
		if _, ok := knownPartitions[key]; !ok {
			return protocol.NewError(protocol.CodeLedger, "message or event exists without a partition ledger row").
				With("partition_key", key).
				With("field", "partition_row")
		}
		messages, err := e.store.LoadMessages(ctx, key)
		if err != nil {
			return protocol.NewError(protocol.CodeLedger, "load message log: %v", err)
		}
		expected := int64(1)
		for _, message := range messages {
			if message.Offset != expected {
				return protocol.NewError(protocol.CodeLedger, "message log has a non-contiguous offset").
					With("partition_key", key).
					With("field", "message_offset").
					With("actual", message.Offset).
					With("expected", expected)
			}
			expected++
			if message.ProducerSeq > producerMax[message.ProducerSession] {
				producerMax[message.ProducerSession] = message.ProducerSeq
			}
		}
		row := knownPartitions[key]
		if row.NextOffset != expected {
			return protocol.NewError(protocol.CodeLedger, "ledger next offset does not match the durable message tail").
				With("partition_key", key).
				With("field", "next_offset").
				With("ledger_value", row.NextOffset).
				With("message_tail", expected-1).
				With("expected_next_offset", expected)
		}
	}
	for session, maxSeq := range producerMax {
		stored, ok := producerSessions[session]
		if !ok || stored != maxSeq {
			return protocol.NewError(protocol.CodeLedger, "producer session high-water mark does not match the message log").
				With("producer_session", session).
				With("field", "producer_sessions.last_seq").
				With("stored", stored).
				With("message_max", maxSeq)
		}
	}
	for session, stored := range producerSessions {
		if _, ok := producerMax[session]; !ok && stored != 0 {
			return protocol.NewError(protocol.CodeLedger, "producer session exists without durable messages").
				With("producer_session", session).
				With("field", "producer_sessions.last_seq").
				With("stored", stored)
		}
	}

	tailCount, err := e.store.CountMessagesWithoutAppendEvent(ctx)
	if err != nil {
		return protocol.NewError(protocol.CodeLedger, "audit append events: %v", err)
	}
	if tailCount != 0 {
		return protocol.NewError(protocol.CodeLedger, "message records are missing durable append events").
			With("field", "append_event").
			With("tail_incomplete_records", tailCount)
	}

	if err := e.markRecoveredWindows(ctx); err != nil {
		return err
	}
	e.recoveryReport = e.buildRecoveryReport(versionReport, tailCount)
	if e.recoveryReportPath != "" {
		if err := writeJSONAtomic(e.recoveryReportPath, e.recoveryReport); err != nil {
			return protocol.NewError(protocol.CodeLedger, "write recovery report: %v", err)
		}
	}
	e.emit("recovery_complete", map[string]any{
		"recovered_partitions":    e.recoveryReport.RecoveredPartitions,
		"recovered_windows":       len(e.recoveryReport.RecoveredWindows),
		"tail_incomplete_records": tailCount,
		"tail_policy":             e.recoveryReport.TailPolicy,
	})
	return nil
}

func (e *Engine) loadPartitionState(ctx context.Context, row store.PartitionRow) (partitionState, error) {
	retries, err := e.store.LoadRetryCounts(ctx, row.PartitionKey)
	if err != nil {
		return partitionState{}, protocol.NewError(protocol.CodeLedger, "load retry counts: %v", err)
	}
	state := stateFromRow(row, retries)
	if err := e.validatePartitionState(ctx, state); err != nil {
		return partitionState{}, err
	}
	return state, nil
}

func (e *Engine) validatePartitionState(ctx context.Context, state partitionState) error {
	fail := func(field string, message string, values map[string]any) *protocol.BizError {
		biz := protocol.NewError(protocol.CodeLedger, message).
			With("partition_key", state.Key).
			With("field", field)
		for key, value := range values {
			biz.With(key, value)
		}
		return biz
	}
	if state.LedgerVersion != store.LedgerFormatVersion {
		return fail("ledger_version", "partition ledger version was not replaced", map[string]any{"actual": state.LedgerVersion, "expected": store.LedgerFormatVersion})
	}
	if state.NextOffset < 1 {
		return fail("next_offset", "next offset must be at least one", map[string]any{"actual": state.NextOffset})
	}
	if state.CommittedOffset < 0 || state.ConfirmedOffset < 0 {
		return fail("committed_offset", "ledger offsets must be non-negative", map[string]any{"committed_offset": state.CommittedOffset, "confirmed_offset": state.ConfirmedOffset})
	}
	if state.CommittedOffset > state.ConfirmedOffset {
		return fail("confirmed_offset", "committed offset exceeds confirmed offset", map[string]any{"committed_offset": state.CommittedOffset, "confirmed_offset": state.ConfirmedOffset})
	}
	if state.CommittedOffset > state.NextOffset-1 {
		return fail("committed_offset", "committed offset exceeds the append tail", map[string]any{"committed_offset": state.CommittedOffset, "next_offset": state.NextOffset})
	}
	if state.ConfirmedOffset > state.NextOffset-1 {
		return fail("confirmed_offset", "confirmed offset exceeds the append tail", map[string]any{"confirmed_offset": state.ConfirmedOffset, "next_offset": state.NextOffset})
	}
	if state.Window != nil {
		window := state.Window
		if window.Holder == "" {
			return fail("window_holder", "window holder is empty", nil)
		}
		if window.StartOffset != state.CommittedOffset+1 {
			return fail("window_start", "window start is not the next uncommitted offset", map[string]any{"window_start": window.StartOffset, "committed_offset": state.CommittedOffset, "expected": state.CommittedOffset + 1})
		}
		if window.EndOffset < window.StartOffset || window.EndOffset >= state.NextOffset {
			return fail("window_end", "window end is outside the durable append tail", map[string]any{"window_start": window.StartOffset, "window_end": window.EndOffset, "next_offset": state.NextOffset})
		}
		if !window.Recovered && window.Deadline.IsZero() {
			return fail("window_deadline", "live window has no deadline", nil)
		}
	}
	if state.PendingRedelivery && state.RedeliveryFrom != state.CommittedOffset+1 {
		return fail("redelivery_from", "pending redelivery does not start at the next uncommitted offset", map[string]any{"redelivery_from": state.RedeliveryFrom, "committed_offset": state.CommittedOffset})
	}
	if state.Blocked {
		if state.BlockedOffset <= state.CommittedOffset || state.BlockedOffset >= state.NextOffset {
			return fail("blocked_offset", "blocked offset is outside the uncommitted range", map[string]any{"blocked_offset": state.BlockedOffset, "committed_offset": state.CommittedOffset, "next_offset": state.NextOffset})
		}
		if state.BlockedRetryCount <= e.maxRetries {
			return fail("blocked_retry_count", "blocked retry count does not exceed max retries", map[string]any{"blocked_retry_count": state.BlockedRetryCount, "max_retries": e.maxRetries})
		}
	}
	for offset, attempts := range state.RetryCounts {
		if offset < 1 || offset >= state.NextOffset {
			return fail("retry_counts", "retry counter points outside the durable append log", map[string]any{"message_offset": offset, "next_offset": state.NextOffset})
		}
		if attempts < 0 {
			return fail("retry_counts", "retry counter is negative", map[string]any{"message_offset": offset, "attempts": attempts})
		}
	}
	messages, err := e.store.LoadMessages(ctx, state.Key)
	if err != nil {
		return protocol.NewError(protocol.CodeLedger, "load messages for validation: %v", err)
	}
	expectedNext := int64(1)
	for _, message := range messages {
		if message.Offset != expectedNext {
			return fail("message_offset", "message log has a non-contiguous offset", map[string]any{"actual": message.Offset, "expected": expectedNext})
		}
		expectedNext++
	}
	if expectedNext != state.NextOffset {
		return fail("next_offset", "next offset does not match the message tail", map[string]any{"next_offset": state.NextOffset, "expected": expectedNext})
	}
	return nil
}

func (e *Engine) markRecoveredWindows(ctx context.Context) error {
	for _, partition := range e.partitions {
		partition.mu.Lock()
		state := partition.state
		if state.Window == nil || state.Window.Recovered {
			partition.mu.Unlock()
			continue
		}
		next := state.clone()
		next.Window.Recovered = true
		next.Window.Deadline = time.Time{}
		next.PendingRedelivery = true
		next.RedeliveryFrom = next.CommittedOffset + 1
		next.StateRevision++
		now := e.clock.Now()
		tx, err := e.store.DB().BeginTx(ctx, nil)
		if err != nil {
			partition.mu.Unlock()
			return protocol.NewError(protocol.CodeLedger, "begin recovery transaction: %v", err)
		}
		event := store.EventRow{
			PartitionKey:  next.Key,
			Kind:          "recovery_detected",
			Session:       next.Window.Holder,
			Offset:        next.CommittedOffset,
			StartOffset:   next.Window.StartOffset,
			EndOffset:     next.Window.EndOffset,
			Code:          protocol.CodeOK,
			Detail:        "unconfirmed window recovered; next pull must redeliver from the earliest unconfirmed offset",
			CreatedUnixMS: now.UnixMilli(),
		}
		if err := e.store.UpsertPartitionTx(ctx, tx, next.row()); err != nil {
			_ = tx.Rollback()
			partition.mu.Unlock()
			return protocol.NewError(protocol.CodeLedger, "persist recovered window: %v", err)
		}
		eventID, err := e.store.InsertEventTx(ctx, tx, event)
		if err != nil {
			_ = tx.Rollback()
			partition.mu.Unlock()
			return protocol.NewError(protocol.CodeLedger, "persist recovery event: %v", err)
		}
		if err := tx.Commit(); err != nil {
			partition.mu.Unlock()
			return protocol.NewError(protocol.CodeLedger, "commit recovery transaction: %v", err)
		}
		partition.state = next
		partition.mu.Unlock()
		e.emit("window_recovered", map[string]any{
			"partition_key":   next.Key,
			"session":         next.Window.Holder,
			"start_offset":    next.Window.StartOffset,
			"end_offset":      next.Window.EndOffset,
			"event_id":        eventID,
			"redelivery_from": next.RedeliveryFrom,
		})
	}
	return nil
}

func (e *Engine) buildRecoveryReport(versionReport store.VersionReport, tailCount int64) RecoveryReport {
	report := RecoveryReport{
		FormatVersion:         store.LogFormatVersion,
		LedgerFormatVersion:   store.LedgerFormatVersion,
		GeneratedAt:           e.clock.Now().UTC().Format(time.RFC3339Nano),
		RecoveredPartitions:   len(e.partitions),
		Partitions:            make([]RecoveryPartition, 0, len(e.partitions)),
		RecoveredWindows:      []RecoveredWindow{},
		VersionRebuild:        versionReport,
		TailIncompleteRecords: tailCount,
		TailPolicy:            "SQLite transactions provide all-or-nothing append durability; uncommitted tail transactions are rolled back, while committed rows and ledger events are recovered by whole-partition replacement.",
	}
	keys := make([]string, 0, len(e.partitions))
	for key := range e.partitions {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	for _, key := range keys {
		partition := e.partitions[key]
		partition.mu.Lock()
		state := partition.state.clone()
		partition.mu.Unlock()
		retryMap := make(map[string]int, len(state.RetryCounts))
		pending := make([]int64, 0)
		for offset, attempts := range state.RetryCounts {
			retryMap[formatOffset(offset)] = attempts
		}
		if state.PendingRedelivery {
			start := state.CommittedOffset + 1
			end := state.NextOffset - 1
			if state.Window != nil && state.Window.Recovered {
				start = state.Window.StartOffset
				end = state.Window.EndOffset
			}
			for offset := start; offset <= end; offset++ {
				pending = append(pending, offset)
			}
		}
		sort.Slice(pending, func(i, j int) bool { return pending[i] < pending[j] })
		report.Partitions = append(report.Partitions, RecoveryPartition{
			PartitionKey:        state.Key,
			NextOffset:          state.NextOffset,
			CommittedOffset:     state.CommittedOffset,
			ConfirmedOffset:     state.ConfirmedOffset,
			RetryPendingOffsets: pending,
			RetryCounts:         retryMap,
			Blocked:             state.Blocked,
			BlockedOffset:       state.BlockedOffset,
			BlockedRetryCount:   state.BlockedRetryCount,
		})
		if state.Window != nil && state.Window.Recovered {
			report.RecoveredWindows = append(report.RecoveredWindows, RecoveredWindow{
				PartitionKey: state.Key,
				Holder:       state.Window.Holder,
				StartOffset:  state.Window.StartOffset,
				EndOffset:    state.Window.EndOffset,
			})
		}
	}
	return report
}

func (e *Engine) Close() error {
	if e == nil {
		return nil
	}
	e.closeOnce.Do(func() {
		close(e.stop)
		e.wg.Wait()
		e.closeErr = e.store.Close()
	})
	return e.closeErr
}

func (e *Engine) startSweeper() {
	e.wg.Add(1)
	go func() {
		defer e.wg.Done()
		ticker := time.NewTicker(e.sweepInterval)
		defer ticker.Stop()
		for {
			select {
			case <-e.stop:
				return
			case <-ticker.C:
				e.sweepExpired(context.Background())
			}
		}
	}()
}

func (e *Engine) sweepExpired(ctx context.Context) {
	for _, partition := range e.partitionList() {
		partition.mu.Lock()
		select {
		case <-e.stop:
			partition.mu.Unlock()
			return
		default:
		}
		_, _ = e.expireIfNeededLocked(ctx, partition, "sweeper", e.clock.Now())
		partition.mu.Unlock()
	}
}

func (e *Engine) partitionList() []*Partition {
	e.partitionsMu.RLock()
	defer e.partitionsMu.RUnlock()
	result := make([]*Partition, 0, len(e.partitions))
	for _, partition := range e.partitions {
		result = append(result, partition)
	}
	return result
}

func (e *Engine) getOrCreatePartition(key string) *Partition {
	e.partitionsMu.RLock()
	partition := e.partitions[key]
	e.partitionsMu.RUnlock()
	if partition != nil {
		return partition
	}
	e.partitionsMu.Lock()
	defer e.partitionsMu.Unlock()
	if partition = e.partitions[key]; partition != nil {
		return partition
	}
	partition = &Partition{state: partitionState{
		Key:           key,
		NextOffset:    1,
		RetryCounts:   map[int64]int{},
		LedgerVersion: store.LedgerFormatVersion,
	}}
	e.partitions[key] = partition
	return partition
}

func (e *Engine) getPartition(key string) *Partition {
	e.partitionsMu.RLock()
	defer e.partitionsMu.RUnlock()
	return e.partitions[key]
}

func (e *Engine) producerState(session string) *producerState {
	e.producersMu.Lock()
	defer e.producersMu.Unlock()
	state := e.producers[session]
	if state == nil {
		state = &producerState{}
		e.producers[session] = state
	}
	return state
}

func (e *Engine) emit(event string, fields map[string]any) {
	if e.logger == nil {
		return
	}
	if fields == nil {
		fields = map[string]any{}
	}
	fields["pid"] = os.Getpid()
	fields["engine_db"] = e.store.Path()
	e.logger.Log(event, fields)
}

func (e *Engine) nowMono(now time.Time) int64 {
	return now.Sub(e.startedAt).Nanoseconds()
}

func (e *Engine) normalizeTimeout(requestMS int64) (time.Duration, *protocol.BizError) {
	timeout := e.defaultWindowTimeout
	if requestMS > 0 {
		timeout = time.Duration(requestMS) * time.Millisecond
	}
	if timeout < e.minWindowTimeout {
		return 0, protocol.NewError(protocol.CodeProtocol, "window timeout is below the minimum").
			With("minimum_ms", e.minWindowTimeout.Milliseconds())
	}
	if timeout > e.maxWindowTimeout {
		return 0, protocol.NewError(protocol.CodeProtocol, "window timeout exceeds the maximum").
			With("maximum_ms", e.maxWindowTimeout.Milliseconds())
	}
	return timeout, nil
}

func asLedgerError(err error) error {
	if biz, ok := err.(*protocol.BizError); ok {
		return biz
	}
	return protocol.NewError(protocol.CodeLedger, "%v", err)
}

func formatOffset(offset int64) string {
	return strconv.FormatInt(offset, 10)
}

func writeJSONAtomic(path string, value any) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil && filepath.Dir(path) != "." {
		return err
	}
	data, err := json.MarshalIndent(value, "", "  ")
	if err != nil {
		return err
	}
	data = append(data, '\n')
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, data, 0o644); err != nil {
		return err
	}
	_ = os.Remove(path)
	return os.Rename(tmp, path)
}

func partitionKeyError(field, message string) *protocol.BizError {
	return protocol.NewError(protocol.CodeAppend, message).With("field", field)
}

func validatePartitionKey(key string) *protocol.BizError {
	if len(key) == 0 {
		return partitionKeyError("partition_key", "partition key is required")
	}
	if len(key) > 128 {
		return partitionKeyError("partition_key", "partition key exceeds 128 bytes")
	}
	for index, r := range key {
		valid := (r >= 'a' && r <= 'z') || (r >= 'A' && r <= 'Z') || (r >= '0' && r <= '9') || r == '.' || r == '_' || r == '-' || r == ':'
		if !valid || (index == 0 && (r == '.' || r == '_' || r == '-' || r == ':')) {
			return partitionKeyError("partition_key", "partition key must match [A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
		}
	}
	return nil
}

func validateSession(field, session string) *protocol.BizError {
	if len(session) == 0 {
		return partitionKeyError(field, field+" is required")
	}
	if len(session) > 128 {
		return partitionKeyError(field, field+" exceeds 128 bytes")
	}
	for _, r := range session {
		if r < 0x20 || r == 0x7f {
			return partitionKeyError(field, field+" contains control characters")
		}
	}
	return nil
}

func jsonDetail(values map[string]any) string {
	data, _ := json.Marshal(values)
	return string(data)
}

func validReason(reason string) string {
	reason = strings.TrimSpace(reason)
	if reason == "" {
		return "unspecified"
	}
	if len(reason) > 512 {
		return reason[:512]
	}
	return reason
}

func (e *Engine) assertContext(ctx context.Context) error {
	if err := ctx.Err(); err != nil {
		if errors.Is(err, context.Canceled) {
			return protocol.NewError(protocol.CodeInternal, "request canceled")
		}
		return protocol.NewError(protocol.CodeInternal, "request failed: %v", err)
	}
	return nil
}

func (e *Engine) revision(state *partitionState) {
	state.StateRevision++
	if state.StateRevision < 1 {
		state.StateRevision = 1
	}
}

func minInt64(a, b int64) int64 {
	if a < b {
		return a
	}
	return b
}

func retryCountsView(retries map[int64]int) map[string]int {
	result := make(map[string]int, len(retries))
	for offset, attempts := range retries {
		result[formatOffset(offset)] = attempts
	}
	return result
}
