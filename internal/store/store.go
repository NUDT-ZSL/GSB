package store

import (
	"context"
	"database/sql"
	"errors"
	"os"
	"path/filepath"
	"strconv"

	_ "github.com/ncruces/go-sqlite3/driver"
	_ "github.com/ncruces/go-sqlite3/embed"
)

type Store struct {
	db   *sql.DB
	path string
}

func Open(path string) (*Store, error) {
	if path == "" {
		return nil, errors.New("database path is required")
	}
	absolute, err := filepath.Abs(path)
	if err != nil {
		return nil, err
	}
	if err := os.MkdirAll(filepath.Dir(absolute), 0o755); err != nil {
		return nil, err
	}
	dsn := "file:" + filepath.ToSlash(absolute) + "?_pragma=busy_timeout(5000)&_pragma=journal_mode(WAL)&_pragma=synchronous(FULL)&_pragma=foreign_keys(1)"
	db, err := sql.Open("sqlite3", dsn)
	if err != nil {
		return nil, err
	}
	db.SetMaxOpenConns(1)
	db.SetMaxIdleConns(1)
	if err := db.Ping(); err != nil {
		_ = db.Close()
		return nil, err
	}
	if _, err := db.Exec(schemaSQL); err != nil {
		_ = db.Close()
		return nil, err
	}
	s := &Store{db: db, path: absolute}
	if err := s.ensureMeta(context.Background()); err != nil {
		_ = db.Close()
		return nil, err
	}
	return s, nil
}

func OpenReadOnly(path string) (*Store, error) {
	if path == "" {
		return nil, errors.New("database path is required")
	}
	absolute, err := filepath.Abs(path)
	if err != nil {
		return nil, err
	}
	dsn := "file:" + filepath.ToSlash(absolute) + "?mode=ro&_pragma=busy_timeout(5000)"
	db, err := sql.Open("sqlite3", dsn)
	if err != nil {
		return nil, err
	}
	db.SetMaxOpenConns(1)
	db.SetMaxIdleConns(1)
	if err := db.Ping(); err != nil {
		_ = db.Close()
		return nil, err
	}
	return &Store{db: db, path: absolute}, nil
}

func (s *Store) Close() error { return s.db.Close() }
func (s *Store) DB() *sql.DB  { return s.db }
func (s *Store) Path() string { return s.path }

func (s *Store) ensureMeta(ctx context.Context) error {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	if err := putMetaIfAbsentTx(ctx, tx, "log_format_version", strconv.FormatInt(LogFormatVersion, 10)); err != nil {
		return err
	}
	if err := putMetaIfAbsentTx(ctx, tx, "ledger_format_version", strconv.FormatInt(LedgerFormatVersion, 10)); err != nil {
		return err
	}
	if err := putMetaIfAbsentTx(ctx, tx, "max_retries", "3"); err != nil {
		return err
	}
	return tx.Commit()
}

func putMetaIfAbsentTx(ctx context.Context, tx *sql.Tx, key, value string) error {
	_, err := tx.ExecContext(ctx, `INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO NOTHING`, key, value)
	return err
}

func (s *Store) LoadMeta(ctx context.Context) (Meta, error) {
	meta := Meta{Raw: map[string]string{}}
	rows, err := s.db.QueryContext(ctx, `SELECT key,value FROM meta ORDER BY key`)
	if err != nil {
		return meta, err
	}
	defer rows.Close()
	for rows.Next() {
		var key, value string
		if err := rows.Scan(&key, &value); err != nil {
			return meta, err
		}
		meta.Raw[key] = value
	}
	if err := rows.Err(); err != nil {
		return meta, err
	}
	meta.LogFormatVersion, _ = strconv.ParseInt(meta.Raw["log_format_version"], 10, 64)
	meta.LedgerFormatVersion, _ = strconv.ParseInt(meta.Raw["ledger_format_version"], 10, 64)
	meta.MaxRetries, _ = strconv.Atoi(meta.Raw["max_retries"])
	return meta, nil
}

func (s *Store) SetMaxRetries(ctx context.Context, maxRetries int) error {
	_, err := s.db.ExecContext(ctx, `INSERT INTO meta(key,value) VALUES('max_retries',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value`, strconv.Itoa(maxRetries))
	return err
}

func (s *Store) UpsertPartitionTx(ctx context.Context, tx *sql.Tx, p PartitionRow) error {
	_, err := tx.ExecContext(ctx, `
INSERT INTO partitions(
  partition_key,next_offset,committed_offset,confirmed_offset,
  window_holder,window_start,window_end,window_deadline_unix_ms,window_ttl_ms,window_recovered,
  pending_redelivery,redelivery_from,blocked,blocked_offset,blocked_retry_count,ledger_version,state_revision
) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
ON CONFLICT(partition_key) DO UPDATE SET
  next_offset=excluded.next_offset,
  committed_offset=excluded.committed_offset,
  confirmed_offset=excluded.confirmed_offset,
  window_holder=excluded.window_holder,
  window_start=excluded.window_start,
  window_end=excluded.window_end,
  window_deadline_unix_ms=excluded.window_deadline_unix_ms,
  window_ttl_ms=excluded.window_ttl_ms,
  window_recovered=excluded.window_recovered,
  pending_redelivery=excluded.pending_redelivery,
  redelivery_from=excluded.redelivery_from,
  blocked=excluded.blocked,
  blocked_offset=excluded.blocked_offset,
  blocked_retry_count=excluded.blocked_retry_count,
  ledger_version=excluded.ledger_version,
  state_revision=excluded.state_revision`,
		p.PartitionKey, p.NextOffset, p.CommittedOffset, p.ConfirmedOffset,
		p.WindowHolder, p.WindowStart, p.WindowEnd, p.WindowDeadlineUnixMS, p.WindowTTLMS, boolInt(p.WindowRecovered),
		boolInt(p.PendingRedelivery), p.RedeliveryFrom, boolInt(p.Blocked), p.BlockedOffset, p.BlockedRetryCount, p.LedgerVersion, p.StateRevision)
	return err
}

func (s *Store) InsertMessageTx(ctx context.Context, tx *sql.Tx, m MessageRow) error {
	_, err := tx.ExecContext(ctx, `
INSERT INTO messages(partition_key,message_offset,producer_session,producer_seq,body,appended_unix_ms)
VALUES(?,?,?,?,?,?)`, m.PartitionKey, m.Offset, m.ProducerSession, m.ProducerSeq, m.Body, m.AppendedUnixMS)
	return err
}

func (s *Store) InsertEventTx(ctx context.Context, tx *sql.Tx, e EventRow) (int64, error) {
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

func (s *Store) UpsertRetryTx(ctx context.Context, tx *sql.Tx, partitionKey string, offset int64, attempts int) error {
	_, err := tx.ExecContext(ctx, `
INSERT INTO retry_counts(partition_key,message_offset,attempts) VALUES(?,?,?)
ON CONFLICT(partition_key,message_offset) DO UPDATE SET attempts=excluded.attempts`, partitionKey, offset, attempts)
	return err
}

func (s *Store) DeleteRetryAfterTx(ctx context.Context, tx *sql.Tx, partitionKey string, offset int64) error {
	_, err := tx.ExecContext(ctx, `DELETE FROM retry_counts WHERE partition_key=? AND message_offset>?`, partitionKey, offset)
	return err
}

func (s *Store) UpsertProducerSessionTx(ctx context.Context, tx *sql.Tx, session string, seq int64) error {
	_, err := tx.ExecContext(ctx, `
INSERT INTO producer_sessions(producer_session,last_seq) VALUES(?,?)
ON CONFLICT(producer_session) DO UPDATE SET last_seq=excluded.last_seq`, session, seq)
	return err
}

func boolInt(value bool) int {
	if value {
		return 1
	}
	return 0
}
