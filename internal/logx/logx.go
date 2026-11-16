package logx

import (
	"encoding/json"
	"io"
	"os"
	"path/filepath"
	"sync"
	"sync/atomic"
	"time"
)

type Logger struct {
	mu     sync.Mutex
	file   *os.File
	writer io.Writer
	seq    atomic.Int64
	start  time.Time
}

func New(path string) (*Logger, error) {
	logger := &Logger{writer: io.Discard, start: time.Now()}
	if path == "" {
		return logger, nil
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil && filepath.Dir(path) != "." {
		return nil, err
	}
	file, err := os.OpenFile(path, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o644)
	if err != nil {
		return nil, err
	}
	logger.file = file
	logger.writer = file
	return logger, nil
}

func (l *Logger) Log(event string, fields map[string]any) {
	if l == nil {
		return
	}
	now := time.Now()
	record := make(map[string]any, len(fields)+5)
	for key, value := range fields {
		record[key] = value
	}
	record["event"] = event
	record["ts"] = now.UTC().Format(time.RFC3339Nano)
	record["mono_ns"] = now.Sub(l.start).Nanoseconds()
	record["event_seq"] = l.seq.Add(1)
	data, err := json.Marshal(record)
	if err != nil {
		return
	}
	data = append(data, '\n')
	l.mu.Lock()
	_, _ = l.writer.Write(data)
	if l.file != nil {
		_ = l.file.Sync()
	}
	l.mu.Unlock()
}

func (l *Logger) Close() error {
	if l == nil || l.file == nil {
		return nil
	}
	return l.file.Close()
}
