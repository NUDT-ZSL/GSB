package store

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"hash/crc32"
	"io"
	"os"
	"path/filepath"
	"sync"
	"time"

	"local/message-interop/internal/protocol"
)

type Kind string

const (
	Messages   Kind = "messages"
	Deliveries Kind = "deliveries"
	Acks       Kind = "acks"
	Dedup      Kind = "dedup"
)

var fileName = map[Kind]string{
	Messages:   "messages.log",
	Deliveries: "deliveries.log",
	Acks:       "acks.log",
	Dedup:      "dedup.index",
}

type Envelope struct {
	Seq    int64           `json:"seq"`
	CRC32  string          `json:"crc32"`
	Record json.RawMessage `json:"record"`
}

type TailIssue struct {
	Journal    string `json:"journal"`
	ByteOffset int64  `json:"byte_offset"`
	Bytes      int64  `json:"bytes"`
	Reason     string `json:"reason"`
	Action     string `json:"action"`
}

type ScanResult struct {
	Messages       []json.RawMessage `json:"-"`
	Deliveries     []json.RawMessage `json:"-"`
	Acks           []json.RawMessage `json:"-"`
	Dedup          []json.RawMessage `json:"-"`
	TailIssues     []TailIssue       `json:"tail_issues"`
	TruncatedBytes int64             `json:"truncated_bytes"`
	MessageCount   int               `json:"message_count"`
	DeliveryCount  int               `json:"delivery_count"`
	AckCount       int               `json:"ack_count"`
	DedupCount     int               `json:"dedup_count"`
}

type RecoveryReport struct {
	Version             int              `json:"version"`
	GeneratedAt         string           `json:"generated_at"`
	DataDir             string           `json:"data_dir"`
	RecoveredOffsets    map[string]int64 `json:"recovered_offsets"`
	RebuiltDedupEntries int              `json:"rebuilt_dedup_entries"`
	RecoveredMessages   int              `json:"recovered_messages"`
	RecoveredDeliveries int              `json:"recovered_deliveries"`
	RecoveredAcks       int              `json:"recovered_acks"`
	LastMessageOffset   int64            `json:"last_message_offset"`
	TailAction          string           `json:"tail_action"`
	TailIssues          []TailIssue      `json:"tail_issues"`
	TruncatedBytes      int64            `json:"truncated_bytes"`
	ConsumerStates      json.RawMessage  `json:"consumer_states"`
}

type journal struct {
	path    string
	file    *os.File
	nextSeq int64
	records []json.RawMessage
}

type Store struct {
	dir      string
	mu       sync.Mutex
	journals map[Kind]*journal
	scan     ScanResult
}

func Open(dir string) (*Store, ScanResult, error) {
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return nil, ScanResult{}, fmt.Errorf("create data directory: %w", err)
	}
	store := &Store{dir: dir, journals: make(map[Kind]*journal)}
	result := ScanResult{}
	for _, kind := range []Kind{Messages, Deliveries, Acks, Dedup} {
		path := filepath.Join(dir, fileName[kind])
		scan, err := recoverJournal(path)
		if err != nil {
			store.Close()
			return nil, ScanResult{}, err
		}
		file, err := os.OpenFile(path, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o644)
		if err != nil {
			store.Close()
			return nil, ScanResult{}, fmt.Errorf("open %s: %w", path, err)
		}
		entry := &journal{
			path:    path,
			file:    file,
			nextSeq: scan.nextSeq,
			records: scan.records,
		}
		store.journals[kind] = entry
		result.TailIssues = append(result.TailIssues, scan.tailIssues...)
		result.TruncatedBytes += scan.truncatedBytes
		switch kind {
		case Messages:
			result.Messages = scan.records
			result.MessageCount = len(scan.records)
		case Deliveries:
			result.Deliveries = scan.records
			result.DeliveryCount = len(scan.records)
		case Acks:
			result.Acks = scan.records
			result.AckCount = len(scan.records)
		case Dedup:
			result.Dedup = scan.records
			result.DedupCount = len(scan.records)
		}
	}
	store.scan = result
	return store, result, nil
}

type journalScan struct {
	records        []json.RawMessage
	nextSeq        int64
	tailIssues     []TailIssue
	truncatedBytes int64
}

func recoverJournal(path string) (journalScan, error) {
	out := journalScan{nextSeq: 1}
	data, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return out, nil
	}
	if err != nil {
		return out, fmt.Errorf("read %s: %w", path, err)
	}
	pos := 0
	lineNumber := 0
	for pos < len(data) {
		lineNumber++
		lineStart := pos
		relativeEnd := bytes.IndexByte(data[pos:], '\n')
		hasNewline := relativeEnd >= 0
		lineEnd := len(data)
		nextPos := len(data)
		if hasNewline {
			lineEnd = pos + relativeEnd
			nextPos = lineEnd + 1
		}
		line := data[pos:lineEnd]
		pos = nextPos
		if len(bytes.TrimSpace(line)) == 0 {
			if pos == len(data) {
				break
			}
			continue
		}
		var envelope Envelope
		if err := json.Unmarshal(line, &envelope); err != nil {
			if pos == len(data) {
				out.truncatedBytes += int64(len(data) - lineStart)
				out.tailIssues = append(out.tailIssues, TailIssue{
					Journal: filepath.Base(path), ByteOffset: int64(lineStart),
					Bytes: int64(len(data) - lineStart), Reason: "invalid_json",
					Action: "truncated_incomplete_tail",
				})
				if err := truncateFile(path, int64(lineStart)); err != nil {
					return out, err
				}
				break
			}
			return out, fmt.Errorf("%s line %d: invalid JSON frame", path, lineNumber)
		}
		computed := fmt.Sprintf("%08x", crc32.ChecksumIEEE(envelope.Record))
		reason := ""
		if !hasNewline {
			reason = "missing_newline"
		} else if envelope.Seq != out.nextSeq {
			reason = fmt.Sprintf("sequence_gap_expected_%d", out.nextSeq)
		} else if envelope.CRC32 != computed {
			reason = "crc_mismatch"
		}
		if reason != "" {
			if pos == len(data) {
				out.truncatedBytes += int64(len(data) - lineStart)
				out.tailIssues = append(out.tailIssues, TailIssue{
					Journal: filepath.Base(path), ByteOffset: int64(lineStart),
					Bytes: int64(len(data) - lineStart), Reason: reason,
					Action: "truncated_incomplete_tail",
				})
				if err := truncateFile(path, int64(lineStart)); err != nil {
					return out, err
				}
				break
			}
			return out, fmt.Errorf("%s line %d: %s", path, lineNumber, reason)
		}
		out.records = append(out.records, append(json.RawMessage(nil), envelope.Record...))
		out.nextSeq++
	}
	return out, nil
}

func truncateFile(path string, size int64) error {
	file, err := os.OpenFile(path, os.O_WRONLY, 0o644)
	if err != nil {
		return fmt.Errorf("open for recovery truncation %s: %w", path, err)
	}
	defer file.Close()
	if err := file.Truncate(size); err != nil {
		return fmt.Errorf("truncate %s: %w", path, err)
	}
	return file.Sync()
}

func (s *Store) ScanResult() ScanResult {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.scan
}

func (s *Store) AppendMessage(message *protocol.StoredMessage) (int64, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	entry := s.journals[Messages]
	seq := entry.nextSeq
	message.GlobalOffset = seq
	err := appendRecordAtSeq(entry, seq, message)
	if err != nil {
		return 0, err
	}
	return seq, nil
}

func (s *Store) Append(kind Kind, record any) (int64, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	entry, ok := s.journals[kind]
	if !ok {
		return 0, fmt.Errorf("unknown journal %s", kind)
	}
	return appendRecord(entry, record)
}

func appendRecord(entry *journal, record any) (int64, error) {
	seq := entry.nextSeq
	if err := appendRecordAtSeq(entry, seq, record); err != nil {
		return 0, err
	}
	return seq, nil
}

func appendRecordAtSeq(entry *journal, seq int64, record any) error {
	recordBytes, err := json.Marshal(record)
	if err != nil {
		return fmt.Errorf("marshal %s record: %w", fileNameForPath(entry.path), err)
	}
	envelope := Envelope{
		Seq:    seq,
		CRC32:  fmt.Sprintf("%08x", crc32.ChecksumIEEE(recordBytes)),
		Record: recordBytes,
	}
	line, err := json.Marshal(envelope)
	if err != nil {
		return fmt.Errorf("marshal log frame: %w", err)
	}
	line = append(line, '\n')
	if _, err := entry.file.Write(line); err != nil {
		return fmt.Errorf("append %s: %w", entry.path, err)
	}
	if err := entry.file.Sync(); err != nil {
		return fmt.Errorf("sync %s: %w", entry.path, err)
	}
	entry.nextSeq++
	return nil
}

func fileNameForPath(path string) string {
	return filepath.Base(path)
}

func (s *Store) Close() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	var first error
	for _, entry := range s.journals {
		if entry.file != nil {
			if err := entry.file.Sync(); err != nil && first == nil {
				first = err
			}
			if err := entry.file.Close(); err != nil && first == nil {
				first = err
			}
		}
	}
	return first
}

func (s *Store) WriteJSONAtomic(name string, value any) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	return WriteJSONAtomic(filepath.Join(s.dir, name), value)
}

func (s *Store) WriteRecoveryReport(report RecoveryReport) error {
	return s.WriteJSONAtomic("recovery-report.json", report)
}

func (s *Store) ReadJSON(name string, target any) error {
	file, err := os.Open(filepath.Join(s.dir, name))
	if err != nil {
		return err
	}
	defer file.Close()
	return json.NewDecoder(file).Decode(target)
}

func WriteJSONAtomic(path string, value any) error {
	parent := filepath.Dir(path)
	if err := os.MkdirAll(parent, 0o755); err != nil {
		return err
	}
	temp, err := os.CreateTemp(parent, filepath.Base(path)+".tmp-*")
	if err != nil {
		return err
	}
	tempPath := temp.Name()
	cleanup := func() {
		_ = temp.Close()
		_ = os.Remove(tempPath)
	}
	encoder := json.NewEncoder(temp)
	encoder.SetIndent("", "  ")
	if err := encoder.Encode(value); err != nil {
		cleanup()
		return err
	}
	if err := temp.Sync(); err != nil {
		cleanup()
		return err
	}
	if err := temp.Close(); err != nil {
		_ = os.Remove(tempPath)
		return err
	}
	if err := os.Rename(tempPath, path); err != nil {
		_ = os.Remove(tempPath)
		return err
	}
	return nil
}

func (s *Store) Dir() string { return s.dir }

func Now() string {
	return time.Now().UTC().Format(time.RFC3339Nano)
}

func CopyFile(src, dst string) error {
	in, err := os.Open(src)
	if err != nil {
		return err
	}
	defer in.Close()
	out, err := os.Create(dst)
	if err != nil {
		return err
	}
	defer out.Close()
	if _, err := io.Copy(out, in); err != nil {
		return err
	}
	return out.Sync()
}
