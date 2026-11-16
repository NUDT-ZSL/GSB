package store

import (
	"context"
	"database/sql"
	"fmt"
	"sort"
)

const partitionColumns = `partition_key,next_offset,committed_offset,confirmed_offset,window_holder,window_start,window_end,window_deadline_unix_ms,window_ttl_ms,window_recovered,pending_redelivery,redelivery_from,blocked,blocked_offset,blocked_retry_count,ledger_version,state_revision`

func scanPartition(scanner interface{ Scan(...any) error }) (PartitionRow, error) {
	var p PartitionRow
	var recovered, pending, blocked int
	err := scanner.Scan(
		&p.PartitionKey, &p.NextOffset, &p.CommittedOffset, &p.ConfirmedOffset,
		&p.WindowHolder, &p.WindowStart, &p.WindowEnd, &p.WindowDeadlineUnixMS, &p.WindowTTLMS, &recovered,
		&pending, &p.RedeliveryFrom, &blocked, &p.BlockedOffset, &p.BlockedRetryCount, &p.LedgerVersion, &p.StateRevision,
	)
	p.WindowRecovered = recovered != 0
	p.PendingRedelivery = pending != 0
	p.Blocked = blocked != 0
	return p, err
}

func (s *Store) ListPartitions(ctx context.Context) ([]PartitionRow, error) {
	rows, err := s.db.QueryContext(ctx, `SELECT `+partitionColumns+` FROM partitions ORDER BY partition_key`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var result []PartitionRow
	for rows.Next() {
		p, err := scanPartition(rows)
		if err != nil {
			return nil, err
		}
		result = append(result, p)
	}
	return result, rows.Err()
}

func (s *Store) GetPartition(ctx context.Context, key string) (PartitionRow, bool, error) {
	row := s.db.QueryRowContext(ctx, `SELECT `+partitionColumns+` FROM partitions WHERE partition_key=?`, key)
	p, err := scanPartition(row)
	if err == sql.ErrNoRows {
		return PartitionRow{}, false, nil
	}
	if err != nil {
		return PartitionRow{}, false, err
	}
	return p, true, nil
}

func (s *Store) LoadRetryCounts(ctx context.Context, key string) (map[int64]int, error) {
	rows, err := s.db.QueryContext(ctx, `SELECT message_offset,attempts FROM retry_counts WHERE partition_key=? ORDER BY message_offset`, key)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	result := map[int64]int{}
	for rows.Next() {
		var offset int64
		var attempts int
		if err := rows.Scan(&offset, &attempts); err != nil {
			return nil, err
		}
		result[offset] = attempts
	}
	return result, rows.Err()
}

func (s *Store) LoadProducerSessions(ctx context.Context) (map[string]int64, error) {
	rows, err := s.db.QueryContext(ctx, `SELECT producer_session,last_seq FROM producer_sessions ORDER BY producer_session`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	result := map[string]int64{}
	for rows.Next() {
		var session string
		var seq int64
		if err := rows.Scan(&session, &seq); err != nil {
			return nil, err
		}
		result[session] = seq
	}
	return result, rows.Err()
}

func (s *Store) LoadMessagesRange(ctx context.Context, key string, start, end int64) ([]MessageRow, error) {
	rows, err := s.db.QueryContext(ctx, `
SELECT partition_key,message_offset,producer_session,producer_seq,body,appended_unix_ms
FROM messages WHERE partition_key=? AND message_offset>=? AND message_offset<=? ORDER BY message_offset`, key, start, end)
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

func (s *Store) LoadMessages(ctx context.Context, key string) ([]MessageRow, error) {
	return s.LoadMessagesRange(ctx, key, 1, int64(^uint64(0)>>1))
}

func (s *Store) LoadEvents(ctx context.Context, key string) ([]EventRow, error) {
	rows, err := s.db.QueryContext(ctx, `
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

func (s *Store) ListPartitionKeys(ctx context.Context) ([]string, error) {
	rows, err := s.db.QueryContext(ctx, `
SELECT partition_key FROM partitions
UNION
SELECT partition_key FROM messages
UNION
SELECT DISTINCT partition_key FROM events
ORDER BY partition_key`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var keys []string
	for rows.Next() {
		var key string
		if err := rows.Scan(&key); err != nil {
			return nil, err
		}
		keys = append(keys, key)
	}
	return keys, rows.Err()
}

func (s *Store) LoadSnapshot(ctx context.Context, key string) (PartitionSnapshot, error) {
	snapshot := PartitionSnapshot{Partition: PartitionRow{PartitionKey: key}}
	p, ok, err := s.GetPartition(ctx, key)
	if err != nil {
		return snapshot, err
	}
	if ok {
		snapshot.Partition = p
	}
	snapshot.RetryCounts, err = s.LoadRetryCounts(ctx, key)
	if err != nil {
		return snapshot, err
	}
	snapshot.Messages, err = s.LoadMessages(ctx, key)
	if err != nil {
		return snapshot, err
	}
	snapshot.Events, err = s.LoadEvents(ctx, key)
	return snapshot, err
}

func (s *Store) ListAllMessages(ctx context.Context) (map[string][]MessageRow, error) {
	rows, err := s.db.QueryContext(ctx, `
SELECT partition_key,message_offset,producer_session,producer_seq,body,appended_unix_ms
FROM messages ORDER BY partition_key,message_offset`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	result := map[string][]MessageRow{}
	for rows.Next() {
		var m MessageRow
		if err := rows.Scan(&m.PartitionKey, &m.Offset, &m.ProducerSession, &m.ProducerSeq, &m.Body, &m.AppendedUnixMS); err != nil {
			return nil, err
		}
		result[m.PartitionKey] = append(result[m.PartitionKey], m)
	}
	return result, rows.Err()
}

func (s *Store) SortedPartitionKeys(ctx context.Context) ([]string, error) {
	keys, err := s.ListPartitionKeys(ctx)
	if err != nil {
		return nil, err
	}
	sort.Strings(keys)
	return keys, nil
}

func (s *Store) CountMessagesWithoutAppendEvent(ctx context.Context) (int64, error) {
	var count int64
	err := s.db.QueryRowContext(ctx, `
SELECT COUNT(*) FROM messages m
WHERE NOT EXISTS (
  SELECT 1 FROM events e
  WHERE e.partition_key=m.partition_key AND e.kind='append' AND e.message_offset=m.message_offset
)`).Scan(&count)
	return count, err
}

func (s *Store) CountEvents(ctx context.Context) (int64, error) {
	var count int64
	err := s.db.QueryRowContext(ctx, `SELECT COUNT(*) FROM events`).Scan(&count)
	return count, err
}

func (s *Store) MustHaveSchema(ctx context.Context) error {
	var name string
	err := s.db.QueryRowContext(ctx, `SELECT name FROM sqlite_master WHERE type='table' AND name='meta'`).Scan(&name)
	if err == sql.ErrNoRows {
		return fmt.Errorf("SQLite database has no orderqueue schema")
	}
	return err
}
