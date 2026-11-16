package logx

import (
	"encoding/json"
	"io"
	"os"
	"path/filepath"
	"sync"
	"time"
)

type Logger struct {
	mu     sync.Mutex
	w      io.Writer
	closer io.Closer
	base   map[string]any
}

func NewWriter(w io.Writer) *Logger {
	return &Logger{w: w}
}

func Open(path string) (*Logger, error) {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return nil, err
	}
	f, err := os.OpenFile(path, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o644)
	if err != nil {
		return nil, err
	}
	return &Logger{w: f, closer: f}, nil
}

func (l *Logger) Close() error {
	if l == nil || l.closer == nil {
		return nil
	}
	return l.closer.Close()
}

func (l *Logger) With(fields map[string]any) *Logger {
	base := make(map[string]any, len(l.base)+len(fields))
	for k, v := range l.base {
		base[k] = v
	}
	for k, v := range fields {
		base[k] = v
	}
	return &Logger{w: l.w, base: base}
}

func (l *Logger) Log(level, roundID, event string, fields map[string]any) {
	if l == nil {
		return
	}
	rec := make(map[string]any, len(l.base)+len(fields)+5)
	for k, v := range l.base {
		rec[k] = v
	}
	rec["ts"] = time.Now().UTC().Format(time.RFC3339Nano)
	rec["level"] = level
	if roundID != "" {
		rec["round"] = roundID
	}
	if event != "" {
		rec["event"] = event
	}
	for k, v := range fields {
		rec[k] = v
	}
	b, err := json.Marshal(rec)
	if err != nil {
		return
	}
	l.mu.Lock()
	_, _ = l.w.Write(append(b, '\n'))
	l.mu.Unlock()
}

func (l *Logger) Info(roundID, event string, fields map[string]any) {
	l.Log("info", roundID, event, fields)
}

func (l *Logger) Warn(roundID, event string, fields map[string]any) {
	l.Log("warn", roundID, event, fields)
}

func (l *Logger) Error(roundID, event string, fields map[string]any) {
	l.Log("error", roundID, event, fields)
}
