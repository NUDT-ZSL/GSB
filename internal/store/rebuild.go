package store

import (
	"context"
	"database/sql"
	"fmt"
	"strconv"

	"github.com/NUDT-ZSL/GSB/orderqueue/internal/protocol"
)

type activeWindow struct {
	holder string
	start  int64
	end    int64
	ttlMS  int64
}

func (s *Store) NormalizeVersions(ctx context.Context, maxRetries int) (VersionReport, error) {
	meta, err := s.LoadMeta(ctx)
	if err != nil {
		return VersionReport{}, err
	}
	report := VersionReport{
		OriginalLogVersion:    meta.LogFormatVersion,
		OriginalLedgerVersion: meta.LedgerFormatVersion,
		LogVersion:            LogFormatVersion,
		LedgerVersion:         LedgerFormatVersion,
	}
	if meta.LogFormatVersion < 0 || meta.LedgerFormatVersion < 0 {
		return report, protocol.NewError(protocol.CodeLedger, "negative format version").
			With("field", "format_version").
			With("log_format_version", meta.LogFormatVersion).
			With("ledger_format_version", meta.LedgerFormatVersion)
	}
	if meta.LogFormatVersion > LogFormatVersion {
		return report, protocol.NewError(protocol.CodeLedger, "unsupported newer log format version").
			With("field", "log_format_version").
			With("stored", meta.LogFormatVersion).
			With("supported", LogFormatVersion)
	}
	if meta.LedgerFormatVersion > LedgerFormatVersion {
		return report, protocol.NewError(protocol.CodeLedger, "unsupported newer ledger format version").
			With("field", "ledger_format_version").
			With("stored", meta.LedgerFormatVersion).
			With("supported", LedgerFormatVersion)
	}

	globalChanged := meta.LogFormatVersion != LogFormatVersion || meta.LedgerFormatVersion != LedgerFormatVersion
	rows, err := s.ListPartitions(ctx)
	if err != nil {
		return report, err
	}
	var mismatched []string
	for _, row := range rows {
		if row.LedgerVersion != LedgerFormatVersion {
			if row.LedgerVersion > LedgerFormatVersion {
				return report, protocol.NewError(protocol.CodeLedger, "unsupported newer partition ledger version").
					With("partition_key", row.PartitionKey).
					With("field", "ledger_version").
					With("stored", row.LedgerVersion).
					With("supported", LedgerFormatVersion)
			}
			mismatched = append(mismatched, row.PartitionKey)
		}
	}

	if globalChanged || len(mismatched) > 0 {
		report.Rebuilt = true
		if globalChanged {
			report.Reason = fmt.Sprintf("format version replacement: log %d->%d ledger %d->%d", meta.LogFormatVersion, LogFormatVersion, meta.LedgerFormatVersion, LedgerFormatVersion)
			report.RebuiltPartitions, err = s.rebuildAllTx(ctx, maxRetries, report.Reason)
		} else {
			report.Reason = "partition ledger version replacement"
			report.RebuiltPartitions, err = s.rebuildPartitionsTx(ctx, mismatched, maxRetries, report.Reason)
		}
		if err != nil {
			return report, err
		}
	}

	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return report, err
	}
	defer tx.Rollback()
	if err := setMetaTx(ctx, tx, "log_format_version", strconv.FormatInt(LogFormatVersion, 10)); err != nil {
		return report, err
	}
	if err := setMetaTx(ctx, tx, "ledger_format_version", strconv.FormatInt(LedgerFormatVersion, 10)); err != nil {
		return report, err
	}
	if err := setMetaTx(ctx, tx, "max_retries", strconv.Itoa(maxRetries)); err != nil {
		return report, err
	}
	if err := tx.Commit(); err != nil {
		return report, err
	}
	return report, nil
}

func setMetaTx(ctx context.Context, tx *sql.Tx, key, value string) error {
	_, err := tx.ExecContext(ctx, `INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value`, key, value)
	return err
}

func (s *Store) rebuildAllTx(ctx context.Context, maxRetries int, reason string) ([]string, error) {
	keys, err := s.ListPartitionKeys(ctx)
	if err != nil {
		return nil, err
	}
	rebuilt, err := s.rebuildPartitionsTx(ctx, keys, maxRetries, reason)
	if err != nil {
		return nil, err
	}
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback()
	if _, err := tx.ExecContext(ctx, `DELETE FROM producer_sessions`); err != nil {
		return nil, err
	}
	if _, err := tx.ExecContext(ctx, `
INSERT INTO producer_sessions(producer_session,last_seq)
SELECT producer_session,MAX(producer_seq) FROM messages GROUP BY producer_session`); err != nil {
		return nil, err
	}
	if err := tx.Commit(); err != nil {
		return nil, err
	}
	return rebuilt, nil
}

func (s *Store) rebuildPartitionsTx(ctx context.Context, keys []string, maxRetries int, reason string) ([]string, error) {
	if len(keys) == 0 {
		return nil, nil
	}
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback()
	rebuilt := make([]string, 0, len(keys))
	for _, key := range keys {
		messages, err := loadMessagesTx(ctx, tx, key)
		if err != nil {
			return nil, err
		}
		events, err := loadEventsTx(ctx, tx, key)
		if err != nil {
			return nil, err
		}
		row, retries, err := rebuildPartitionState(key, messages, events, maxRetries)
		if err != nil {
			return nil, err
		}
		if err := upsertPartitionTx(ctx, tx, row); err != nil {
			return nil, err
		}
		if _, err := tx.ExecContext(ctx, `DELETE FROM retry_counts WHERE partition_key=?`, key); err != nil {
			return nil, err
		}
		for offset, attempts := range retries {
			if _, err := tx.ExecContext(ctx, `INSERT INTO retry_counts(partition_key,message_offset,attempts) VALUES(?,?,?)`, key, offset, attempts); err != nil {
				return nil, err
			}
		}
		event := EventRow{
			PartitionKey: key,
			Kind:         "version_rebuild",
			Offset:       row.CommittedOffset,
			StartOffset:  row.CommittedOffset + 1,
			EndOffset:    row.NextOffset - 1,
			RetryCount:   row.BlockedRetryCount,
			Code:         "OK",
			Detail:       reason,
		}
		if _, err := insertEventTx(ctx, tx, event); err != nil {
			return nil, err
		}
		rebuilt = append(rebuilt, key)
	}
	if err := tx.Commit(); err != nil {
		return nil, err
	}
	return rebuilt, nil
}

func rebuildPartitionState(key string, messages []MessageRow, events []EventRow, maxRetries int) (PartitionRow, map[int64]int, error) {
	row := PartitionRow{PartitionKey: key, NextOffset: 1, LedgerVersion: LedgerFormatVersion, StateRevision: 1}
	expectedOffset := int64(1)
	for _, message := range messages {
		if message.Offset != expectedOffset {
			return row, nil, protocol.NewError(protocol.CodeLedger, "message log has a non-contiguous offset").
				With("partition_key", key).
				With("field", "message_offset").
				With("actual", message.Offset).
				With("expected", expectedOffset)
		}
		expectedOffset++
	}
	row.NextOffset = expectedOffset

	committed := int64(0)
	retries := map[int64]int{}
	var active *activeWindow
	blocked := false
	blockedOffset := int64(0)
	blockedCount := 0

	for _, event := range events {
		switch event.Kind {
		case "window_issued":
			if active != nil {
				return row, nil, ledgerEventError(key, event, "window overlaps an unclosed window")
			}
			if event.StartOffset < 1 || event.EndOffset < event.StartOffset || event.EndOffset >= row.NextOffset {
				return row, nil, ledgerEventError(key, event, "invalid issued window bounds")
			}
			if event.StartOffset != committed+1 {
				return row, nil, ledgerEventError(key, event, "window start is not the next uncommitted offset")
			}
			active = &activeWindow{holder: event.Session, start: event.StartOffset, end: event.EndOffset}
		case "commit":
			if active == nil {
				return row, nil, ledgerEventError(key, event, "commit without an active window")
			}
			if event.Session != active.holder {
				return row, nil, ledgerEventError(key, event, "commit session does not hold the window")
			}
			if event.CommittedBefore != committed {
				return row, nil, ledgerEventError(key, event, "commit before-offset does not match ledger")
			}
			if event.CommittedAfter <= committed || event.CommittedAfter > active.end || event.CommittedAfter < active.start {
				return row, nil, ledgerEventError(key, event, "commit offset is outside the active window or moves backwards")
			}
			committed = event.CommittedAfter
			if committed == active.end {
				active = nil
			} else {
				active.start = committed + 1
			}
		case "fail", "timeout", "recovery_redelivery":
			if active == nil {
				return row, nil, ledgerEventError(key, event, "redelivery without an active window")
			}
			if event.StartOffset != active.start || event.EndOffset != active.end || event.EndOffset < event.StartOffset {
				return row, nil, ledgerEventError(key, event, "redelivery range does not match the unconfirmed window")
			}
			incrementRetries(retries, event.StartOffset, event.EndOffset)
			active = nil
		case "poison":
			blocked = true
			blockedOffset = event.Offset
			blockedCount = event.RetryCount
		case "unblock":
			blocked = false
			blockedOffset = 0
			blockedCount = 0
			from := event.StartOffset
			to := event.EndOffset
			if from < 1 {
				from = committed + 1
			}
			if to < from {
				to = row.NextOffset - 1
			}
			for offset := from; offset <= to; offset++ {
				delete(retries, offset)
			}
		}
	}

	if committed > row.NextOffset-1 {
		return row, nil, protocol.NewError(protocol.CodeLedger, "committed offset exceeds the durable message tail").
			With("partition_key", key).
			With("field", "committed_offset").
			With("committed_offset", committed).
			With("next_offset", row.NextOffset)
	}
	row.CommittedOffset = committed
	row.ConfirmedOffset = committed
	row.Blocked = blocked
	row.BlockedOffset = blockedOffset
	row.BlockedRetryCount = blockedCount
	if active != nil {
		row.WindowHolder = active.holder
		row.WindowStart = active.start
		row.WindowEnd = active.end
		row.WindowTTLMS = active.ttlMS
		row.WindowDeadlineUnixMS = 0
		row.WindowRecovered = true
		row.PendingRedelivery = true
		row.RedeliveryFrom = committed + 1
	}
	if blocked && blockedOffset <= committed {
		return row, nil, protocol.NewError(protocol.CodeLedger, "blocked offset is already committed").
			With("partition_key", key).
			With("field", "blocked_offset").
			With("blocked_offset", blockedOffset).
			With("committed_offset", committed)
	}
	if blocked && maxRetries >= 0 && blockedCount <= maxRetries {
		return row, nil, protocol.NewError(protocol.CodeLedger, "poison event retry count does not exceed max retries").
			With("partition_key", key).
			With("field", "blocked_retry_count").
			With("blocked_retry_count", blockedCount).
			With("max_retries", maxRetries)
	}
	return row, retries, nil
}

func ledgerEventError(key string, event EventRow, message string) *protocol.BizError {
	return protocol.NewError(protocol.CodeLedger, message).
		With("partition_key", key).
		With("event_id", event.EventID).
		With("event_kind", event.Kind).
		With("offset", event.Offset)
}

func incrementRetries(retries map[int64]int, start, end int64) {
	for offset := start; offset <= end; offset++ {
		retries[offset]++
	}
}

func loadMessagesTx(ctx context.Context, tx *sql.Tx, key string) ([]MessageRow, error) {
	rows, err := tx.QueryContext(ctx, `
SELECT partition_key,message_offset,producer_session,producer_seq,body,appended_unix_ms
FROM messages WHERE partition_key=? ORDER BY message_offset`, key)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var result []MessageRow
	for rows.Next() {
		var m MessageRow
		if err := rows.Scan(&m.PartitionKey, &m.Offset, &m.ProducerSession, &m.ProducerSeq, &m.Body, &m.AppendedUnixMS); err != nil {
			return nil, err
		}
		result = append(result, m)
	}
	return result, rows.Err()
}

func loadEventsTx(ctx context.Context, tx *sql.Tx, key string) ([]EventRow, error) {
	rows, err := tx.QueryContext(ctx, `
SELECT event_id,partition_key,kind,session,message_offset,start_offset,end_offset,committed_before,committed_after,retry_count,code,detail,created_unix_ms
FROM events WHERE partition_key=? ORDER BY event_id`, key)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var result []EventRow
	for rows.Next() {
		var e EventRow
		if err := rows.Scan(&e.EventID, &e.PartitionKey, &e.Kind, &e.Session, &e.Offset, &e.StartOffset, &e.EndOffset, &e.CommittedBefore, &e.CommittedAfter, &e.RetryCount, &e.Code, &e.Detail, &e.CreatedUnixMS); err != nil {
			return nil, err
		}
		result = append(result, e)
	}
	return result, rows.Err()
}

func upsertPartitionTx(ctx context.Context, tx *sql.Tx, p PartitionRow) error {
	_, err := tx.ExecContext(ctx, `
INSERT INTO partitions(
  partition_key,next_offset,committed_offset,confirmed_offset,
  window_holder,window_start,window_end,window_deadline_unix_ms,window_ttl_ms,window_recovered,
  pending_redelivery,redelivery_from,blocked,blocked_offset,blocked_retry_count,ledger_version,state_revision
) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
ON CONFLICT(partition_key) DO UPDATE SET
  next_offset=excluded.next_offset, committed_offset=excluded.committed_offset,
  confirmed_offset=excluded.confirmed_offset, window_holder=excluded.window_holder,
  window_start=excluded.window_start, window_end=excluded.window_end,
  window_deadline_unix_ms=excluded.window_deadline_unix_ms, window_ttl_ms=excluded.window_ttl_ms,
  window_recovered=excluded.window_recovered, pending_redelivery=excluded.pending_redelivery,
  redelivery_from=excluded.redelivery_from, blocked=excluded.blocked,
  blocked_offset=excluded.blocked_offset, blocked_retry_count=excluded.blocked_retry_count,
  ledger_version=excluded.ledger_version, state_revision=excluded.state_revision`,
		p.PartitionKey, p.NextOffset, p.CommittedOffset, p.ConfirmedOffset,
		p.WindowHolder, p.WindowStart, p.WindowEnd, p.WindowDeadlineUnixMS, p.WindowTTLMS, boolInt(p.WindowRecovered),
		boolInt(p.PendingRedelivery), p.RedeliveryFrom, boolInt(p.Blocked), p.BlockedOffset, p.BlockedRetryCount, p.LedgerVersion, p.StateRevision)
	return err
}

func insertEventTx(ctx context.Context, tx *sql.Tx, e EventRow) (int64, error) {
	result, err := tx.ExecContext(ctx, `
INSERT INTO events(partition_key,kind,session,message_offset,start_offset,end_offset,committed_before,committed_after,retry_count,code,detail,created_unix_ms)
VALUES(?,?,?,?,?,?,?,?,?,?,?,?)`,
		e.PartitionKey, e.Kind, e.Session, e.Offset, e.StartOffset, e.EndOffset,
		e.CommittedBefore, e.CommittedAfter, e.RetryCount, e.Code, e.Detail, e.CreatedUnixMS)
	if err != nil {
		return 0, err
	}
	return result.LastInsertId()
}
