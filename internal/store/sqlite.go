package store

import (
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"time"

	"configledger/internal/domain"

	_ "modernc.org/sqlite"
)

type SnapshotMeta struct {
	ID              int64  `json:"id"`
	SessionID       string `json:"session_id"`
	ManifestVersion string `json:"manifest_version"`
	Sequence        int64  `json:"sequence"`
	ParentID        int64  `json:"parent_id"`
	PayloadHash     string `json:"payload_hash"`
	CreatedAt       string `json:"created_at"`
}

type Store struct {
	db *sql.DB
}

func Open(path string) (*Store, error) {
	if path == "" {
		return nil, errors.New("database path is required")
	}
	if path != ":memory:" {
		dir := filepath.Dir(path)
		if dir != "." && dir != "" {
			if err := os.MkdirAll(dir, 0o755); err != nil {
				return nil, fmt.Errorf("create database directory: %w", err)
			}
		}
	}
	db, err := sql.Open("sqlite", path)
	if err != nil {
		return nil, fmt.Errorf("open sqlite: %w", err)
	}
	db.SetMaxOpenConns(1)
	if _, err := db.Exec(`PRAGMA busy_timeout = 5000; PRAGMA foreign_keys = ON;`); err != nil {
		_ = db.Close()
		return nil, fmt.Errorf("configure sqlite: %w", err)
	}
	if path != ":memory:" {
		if _, err := db.Exec(`PRAGMA journal_mode = WAL;`); err != nil {
			_ = db.Close()
			return nil, fmt.Errorf("enable sqlite WAL: %w", err)
		}
	}
	store := &Store{db: db}
	if err := store.ensureSchema(); err != nil {
		_ = db.Close()
		return nil, err
	}
	return store, nil
}

func (s *Store) Close() error {
	return s.db.Close()
}

func (s *Store) ensureSchema() error {
	statements := []string{
		`CREATE TABLE IF NOT EXISTS schema_meta (
			key TEXT PRIMARY KEY,
			value TEXT NOT NULL
		);`,
		`INSERT INTO schema_meta(key, value) VALUES ('schema_version', '1')
		 ON CONFLICT(key) DO NOTHING;`,
		`CREATE TABLE IF NOT EXISTS snapshots (
			id INTEGER PRIMARY KEY AUTOINCREMENT,
			session_id TEXT NOT NULL,
			manifest_version TEXT NOT NULL,
			sequence INTEGER NOT NULL,
			parent_id INTEGER,
			payload TEXT NOT NULL,
			payload_hash TEXT NOT NULL,
			created_at TEXT NOT NULL,
			UNIQUE(session_id, sequence)
		);`,
		`CREATE INDEX IF NOT EXISTS idx_snapshots_session_version
		 ON snapshots(session_id, manifest_version, sequence DESC);`,
		`CREATE TABLE IF NOT EXISTS active_sessions (
			session_id TEXT PRIMARY KEY,
			snapshot_id INTEGER NOT NULL,
			updated_at TEXT NOT NULL,
			FOREIGN KEY(snapshot_id) REFERENCES snapshots(id)
		);`,
	}
	for _, statement := range statements {
		if _, err := s.db.Exec(statement); err != nil {
			return fmt.Errorf("initialize sqlite schema: %w", err)
		}
	}
	return s.ensureParentColumn()
}

func (s *Store) ensureParentColumn() error {
	rows, err := s.db.Query(`PRAGMA table_info(snapshots)`)
	if err != nil {
		return fmt.Errorf("inspect snapshots schema: %w", err)
	}
	defer rows.Close()
	hasParent := false
	for rows.Next() {
		var cid int
		var name string
		var columnType string
		var notNull int
		var defaultValue any
		var primaryKey int
		if err := rows.Scan(&cid, &name, &columnType, &notNull, &defaultValue, &primaryKey); err != nil {
			return fmt.Errorf("read snapshots schema: %w", err)
		}
		if name == "parent_id" {
			hasParent = true
		}
	}
	if err := rows.Err(); err != nil {
		return fmt.Errorf("read snapshots schema: %w", err)
	}
	if hasParent {
		return nil
	}
	if _, err := s.db.Exec(`ALTER TABLE snapshots ADD COLUMN parent_id INTEGER`); err != nil {
		return fmt.Errorf("migrate snapshots parent_id: %w", err)
	}
	return nil
}

func (s *Store) Save(state *domain.State) (SnapshotMeta, error) {
	if state == nil {
		return SnapshotMeta{}, errors.New("state is required")
	}
	payload, err := json.Marshal(state)
	if err != nil {
		return SnapshotMeta{}, fmt.Errorf("encode snapshot: %w", err)
	}
	sum := sha256.Sum256(payload)
	payloadHash := hex.EncodeToString(sum[:])
	createdAt := time.Now().UTC().Format(time.RFC3339Nano)

	tx, err := s.db.Begin()
	if err != nil {
		return SnapshotMeta{}, fmt.Errorf("begin snapshot transaction: %w", err)
	}
	defer func() { _ = tx.Rollback() }()

	var currentSequence int64
	err = tx.QueryRow(`SELECT COALESCE(MAX(sequence), 0) FROM snapshots WHERE session_id = ?`, state.SessionID).Scan(&currentSequence)
	if err != nil {
		return SnapshotMeta{}, fmt.Errorf("read snapshot sequence: %w", err)
	}
	var parentID int64
	err = tx.QueryRow(`SELECT snapshot_id FROM active_sessions WHERE session_id = ?`, state.SessionID).Scan(&parentID)
	if err != nil && !errors.Is(err, sql.ErrNoRows) {
		return SnapshotMeta{}, fmt.Errorf("read active parent snapshot: %w", err)
	}
	sequence := currentSequence + 1
	result, err := tx.Exec(`INSERT INTO snapshots(session_id, manifest_version, sequence, parent_id, payload, payload_hash, created_at)
		VALUES (?, ?, ?, ?, ?, ?, ?)`, state.SessionID, state.ManifestVersion, sequence, parentID, string(payload), payloadHash, createdAt)
	if err != nil {
		return SnapshotMeta{}, fmt.Errorf("insert snapshot: %w", err)
	}
	id, err := result.LastInsertId()
	if err != nil {
		return SnapshotMeta{}, fmt.Errorf("read snapshot id: %w", err)
	}
	_, err = tx.Exec(`INSERT INTO active_sessions(session_id, snapshot_id, updated_at) VALUES (?, ?, ?)
		ON CONFLICT(session_id) DO UPDATE SET snapshot_id = excluded.snapshot_id, updated_at = excluded.updated_at`,
		state.SessionID, id, createdAt)
	if err != nil {
		return SnapshotMeta{}, fmt.Errorf("update active snapshot: %w", err)
	}
	if err := tx.Commit(); err != nil {
		return SnapshotMeta{}, fmt.Errorf("commit snapshot transaction: %w", err)
	}
	return SnapshotMeta{ID: id, SessionID: state.SessionID, ManifestVersion: state.ManifestVersion, Sequence: sequence, ParentID: parentID, PayloadHash: payloadHash, CreatedAt: createdAt}, nil
}

func (s *Store) LoadActive(sessionID string) (*domain.State, SnapshotMeta, error) {
	var meta SnapshotMeta
	err := s.db.QueryRow(`SELECT s.id, s.session_id, s.manifest_version, s.sequence, COALESCE(s.parent_id, 0), s.payload_hash, s.created_at
		FROM active_sessions a JOIN snapshots s ON s.id = a.snapshot_id
		WHERE a.session_id = ?`, sessionID).Scan(&meta.ID, &meta.SessionID, &meta.ManifestVersion, &meta.Sequence, &meta.ParentID, &meta.PayloadHash, &meta.CreatedAt)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, SnapshotMeta{}, sql.ErrNoRows
	}
	if err != nil {
		return nil, SnapshotMeta{}, fmt.Errorf("load active snapshot metadata: %w", err)
	}
	state, err := s.loadPayload(meta)
	return state, meta, err
}

func (s *Store) LoadVersion(sessionID, manifestVersion string) (*domain.State, SnapshotMeta, error) {
	var meta SnapshotMeta
	err := s.db.QueryRow(`SELECT id, session_id, manifest_version, sequence, COALESCE(parent_id, 0), payload_hash, created_at
		FROM snapshots WHERE session_id = ? AND manifest_version = ?
		ORDER BY sequence DESC LIMIT 1`, sessionID, manifestVersion).
		Scan(&meta.ID, &meta.SessionID, &meta.ManifestVersion, &meta.Sequence, &meta.ParentID, &meta.PayloadHash, &meta.CreatedAt)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, SnapshotMeta{}, sql.ErrNoRows
	}
	if err != nil {
		return nil, SnapshotMeta{}, fmt.Errorf("load snapshot metadata: %w", err)
	}
	state, err := s.loadPayload(meta)
	return state, meta, err
}

func (s *Store) LoadByID(id int64) (*domain.State, SnapshotMeta, error) {
	var meta SnapshotMeta
	err := s.db.QueryRow(`SELECT id, session_id, manifest_version, sequence, COALESCE(parent_id, 0), payload_hash, created_at
		FROM snapshots WHERE id = ?`, id).
		Scan(&meta.ID, &meta.SessionID, &meta.ManifestVersion, &meta.Sequence, &meta.ParentID, &meta.PayloadHash, &meta.CreatedAt)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, SnapshotMeta{}, sql.ErrNoRows
	}
	if err != nil {
		return nil, SnapshotMeta{}, fmt.Errorf("load snapshot metadata: %w", err)
	}
	state, err := s.loadPayload(meta)
	return state, meta, err
}

func (s *Store) Previous(meta SnapshotMeta) (*domain.State, SnapshotMeta, error) {
	if meta.ParentID > 0 {
		return s.LoadByID(meta.ParentID)
	}
	var previousMeta SnapshotMeta
	err := s.db.QueryRow(`SELECT id, session_id, manifest_version, sequence, COALESCE(parent_id, 0), payload_hash, created_at
		FROM snapshots WHERE session_id = ? AND sequence < ?
		ORDER BY sequence DESC LIMIT 1`, meta.SessionID, meta.Sequence).
		Scan(&previousMeta.ID, &previousMeta.SessionID, &previousMeta.ManifestVersion, &previousMeta.Sequence, &previousMeta.ParentID, &previousMeta.PayloadHash, &previousMeta.CreatedAt)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, SnapshotMeta{}, sql.ErrNoRows
	}
	if err != nil {
		return nil, SnapshotMeta{}, fmt.Errorf("load previous snapshot metadata: %w", err)
	}
	state, err := s.loadPayload(previousMeta)
	return state, previousMeta, err
}

func (s *Store) SetActive(meta SnapshotMeta) error {
	updatedAt := time.Now().UTC().Format(time.RFC3339Nano)
	tx, err := s.db.Begin()
	if err != nil {
		return fmt.Errorf("begin active snapshot transaction: %w", err)
	}
	defer func() { _ = tx.Rollback() }()
	var exists int
	if err := tx.QueryRow(`SELECT COUNT(1) FROM snapshots WHERE id = ?`, meta.ID).Scan(&exists); err != nil {
		return fmt.Errorf("check target snapshot: %w", err)
	}
	if exists != 1 {
		return fmt.Errorf("target snapshot %d does not exist", meta.ID)
	}
	if _, err := tx.Exec(`INSERT INTO active_sessions(session_id, snapshot_id, updated_at) VALUES (?, ?, ?)
		ON CONFLICT(session_id) DO UPDATE SET snapshot_id = excluded.snapshot_id, updated_at = excluded.updated_at`,
		meta.SessionID, meta.ID, updatedAt); err != nil {
		return fmt.Errorf("replace active snapshot: %w", err)
	}
	if err := tx.Commit(); err != nil {
		return fmt.Errorf("commit active snapshot replacement: %w", err)
	}
	return nil
}

func (s *Store) ListVersions(sessionID string) ([]SnapshotMeta, error) {
	rows, err := s.db.Query(`SELECT id, session_id, manifest_version, sequence, COALESCE(parent_id, 0), payload_hash, created_at
		FROM snapshots WHERE session_id = ? ORDER BY sequence ASC`, sessionID)
	if err != nil {
		return nil, fmt.Errorf("list snapshots: %w", err)
	}
	defer rows.Close()
	result := make([]SnapshotMeta, 0)
	for rows.Next() {
		var meta SnapshotMeta
		if err := rows.Scan(&meta.ID, &meta.SessionID, &meta.ManifestVersion, &meta.Sequence, &meta.ParentID, &meta.PayloadHash, &meta.CreatedAt); err != nil {
			return nil, fmt.Errorf("scan snapshot metadata: %w", err)
		}
		result = append(result, meta)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("read snapshot list: %w", err)
	}
	return result, nil
}

func (s *Store) loadPayload(meta SnapshotMeta) (*domain.State, error) {
	var payload string
	if err := s.db.QueryRow(`SELECT payload FROM snapshots WHERE id = ?`, meta.ID).Scan(&payload); err != nil {
		return nil, fmt.Errorf("load snapshot payload: %w", err)
	}
	sum := sha256.Sum256([]byte(payload))
	if hex.EncodeToString(sum[:]) != meta.PayloadHash {
		return nil, fmt.Errorf("snapshot %d payload hash mismatch", meta.ID)
	}
	var state domain.State
	if err := json.Unmarshal([]byte(payload), &state); err != nil {
		return nil, fmt.Errorf("decode snapshot payload: %w", err)
	}
	return &state, nil
}
