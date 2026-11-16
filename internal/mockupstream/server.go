package mockupstream

import (
	"context"
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync/atomic"
	"time"
)

type EndpointSpec struct {
	Address   string
	Mode      string
	Delay     time.Duration
	FailCount int
}

// ParseEndpoint accepts address=mode:delay_ms[:fail_count]. Supported modes
// are ok, fail and flaky.
func ParseEndpoint(spec string) (EndpointSpec, error) {
	addr, behavior, ok := strings.Cut(spec, "=")
	if !ok {
		return EndpointSpec{}, fmt.Errorf("endpoint must be address=mode:delay_ms[:fail_count]: %q", spec)
	}
	parts := strings.Split(behavior, ":")
	if len(parts) < 2 || len(parts) > 3 {
		return EndpointSpec{}, fmt.Errorf("endpoint behavior must be mode:delay_ms[:fail_count]: %q", spec)
	}
	out := EndpointSpec{Address: strings.TrimSpace(addr), Mode: strings.ToLower(parts[0])}
	delay, err := strconv.Atoi(parts[1])
	if err != nil || delay < 0 || delay > 60_000 {
		return EndpointSpec{}, fmt.Errorf("invalid delay in %q", spec)
	}
	out.Delay = time.Duration(delay) * time.Millisecond
	if len(parts) == 3 {
		n, err := strconv.Atoi(parts[2])
		if err != nil || n < 0 {
			return EndpointSpec{}, fmt.Errorf("invalid fail count in %q", spec)
		}
		out.FailCount = n
	}
	switch out.Mode {
	case "ok", "fail", "flaky":
	default:
		return EndpointSpec{}, fmt.Errorf("unknown mode %q", out.Mode)
	}
	return out, nil
}

type server struct {
	spec     EndpointSpec
	failures atomic.Int64
}

func Run(ctx context.Context, specs []EndpointSpec, readyFile string) error {
	if len(specs) == 0 {
		return fmt.Errorf("at least one endpoint is required")
	}
	listeners := make([]net.Listener, 0, len(specs))
	for _, spec := range specs {
		ln, err := net.Listen("tcp", spec.Address)
		if err != nil {
			for _, open := range listeners {
				_ = open.Close()
			}
			return fmt.Errorf("listen %s: %w", spec.Address, err)
		}
		listeners = append(listeners, ln)
	}
	servers := make([]*http.Server, 0, len(specs))
	for i, ln := range listeners {
		s := &server{spec: specs[i]}
		httpServer := &http.Server{
			Handler:           s,
			ReadHeaderTimeout: 2 * time.Second,
			IdleTimeout:       10 * time.Second,
		}
		servers = append(servers, httpServer)
		go func(srv *http.Server, listener net.Listener) {
			_ = srv.Serve(listener)
		}(httpServer, ln)
		fmt.Printf("mock listening address=%s mode=%s delay_ms=%d fail_count=%d\n",
			specs[i].Address, specs[i].Mode, specs[i].Delay.Milliseconds(), specs[i].FailCount)
	}
	if readyFile != "" {
		if err := os.MkdirAll(filepath.Dir(readyFile), 0o755); err != nil {
			return err
		}
		if err := os.WriteFile(readyFile, []byte("ready\n"), 0o644); err != nil {
			return err
		}
	}
	<-ctx.Done()
	shutdownCtx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	for _, srv := range servers {
		_ = srv.Shutdown(shutdownCtx)
	}
	return nil
}

func (s *server) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path == "/health" {
		writeJSON(w, http.StatusOK, map[string]any{"status": "ok", "address": s.spec.Address})
		return
	}
	delay := s.spec.Delay
	if q := r.URL.Query().Get("delay_ms"); q != "" {
		if ms, err := strconv.Atoi(q); err == nil && ms >= 0 && ms <= 10_000 {
			delay += time.Duration(ms) * time.Millisecond
		}
	}
	if delay > 0 {
		timer := time.NewTimer(delay)
		select {
		case <-timer.C:
		case <-r.Context().Done():
			timer.Stop()
			return
		}
	}
	status := http.StatusOK
	reason := "ok"
	switch s.spec.Mode {
	case "fail":
		status = http.StatusInternalServerError
		reason = "injected_failure"
	case "flaky":
		n := s.failures.Add(1)
		if n <= int64(s.spec.FailCount) {
			status = http.StatusInternalServerError
			reason = "injected_flaky_failure"
		}
	}
	writeJSON(w, status, map[string]any{
		"address":   s.spec.Address,
		"method":    r.Method,
		"path":      r.URL.Path,
		"query":     r.URL.RawQuery,
		"reason":    reason,
		"timestamp": time.Now().UTC().Format(time.RFC3339Nano),
	})
}

func writeJSON(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}
