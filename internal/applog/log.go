package applog

import (
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sync"
	"time"
)

type Logger struct {
	mu     sync.Mutex
	writer io.Writer
	file   *os.File
}

func New(path string) (*Logger, error) {
	if path == "" {
		return &Logger{writer: io.Discard}, nil
	}
	dir := filepath.Dir(path)
	if dir != "." && dir != "" {
		if err := os.MkdirAll(dir, 0o755); err != nil {
			return nil, fmt.Errorf("create log directory: %w", err)
		}
	}
	file, err := os.OpenFile(path, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o644)
	if err != nil {
		return nil, fmt.Errorf("open structured log: %w", err)
	}
	return &Logger{writer: file, file: file}, nil
}

func (l *Logger) Event(level, event string, fields map[string]any) {
	if l == nil {
		return
	}
	record := make(map[string]any, len(fields)+3)
	record["time"] = time.Now().UTC().Format(time.RFC3339Nano)
	record["level"] = level
	record["event"] = event
	for key, value := range fields {
		record[key] = value
	}
	data, err := json.Marshal(record)
	if err != nil {
		return
	}
	l.mu.Lock()
	defer l.mu.Unlock()
	_, _ = l.writer.Write(append(data, '\n'))
}

func (l *Logger) Close() error {
	if l == nil || l.file == nil {
		return nil
	}
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.file.Close()
}
