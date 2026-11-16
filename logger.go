package docquery

import (
	"encoding/json"
	"io"
	"sync"
)

// EventLogger receives deterministic, structured engine events. Implementations
// must not add timestamps if their output is used for byte-level comparison.
type EventLogger interface {
	Log(event, code string, fields map[string]any)
}

// JSONLogger writes one compact JSON object per event. No timestamp or random
// identifier is added, so logs are reproducible for the same operation order.
type JSONLogger struct {
	mu      sync.Mutex
	w       io.Writer
	enabled bool
}

func NewJSONLogger(w io.Writer) *JSONLogger {
	if w == nil {
		w = io.Discard
	}
	return &JSONLogger{w: w, enabled: true}
}

func (l *JSONLogger) Log(event, code string, fields map[string]any) {
	if l == nil || !l.enabled {
		return
	}
	record := make(map[string]any, len(fields)+2)
	for k, v := range fields {
		record[k] = v
	}
	record["event"] = event
	record["code"] = code
	data, err := json.Marshal(record)
	if err != nil {
		return
	}
	l.mu.Lock()
	defer l.mu.Unlock()
	_, _ = l.w.Write(append(data, '\n'))
}

type discardLogger struct{}

func (discardLogger) Log(string, string, map[string]any) {}

func loggerOrNil(logger EventLogger) EventLogger {
	if logger == nil {
		return discardLogger{}
	}
	return logger
}
