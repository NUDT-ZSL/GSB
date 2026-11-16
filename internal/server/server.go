package server

import (
	"bufio"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"os"
	"path/filepath"
	"sync"
	"time"

	"locklease/internal/proto"
	"locklease/internal/store"
)

// Server is the loopback TCP JSON-line lock service.
type Server struct {
	addr   string
	store  *store.Store
	logDir string

	listener net.Listener
	wg       sync.WaitGroup
	stopCh   chan struct{}
	stopOnce sync.Once

	connMu sync.Mutex
	conns  map[net.Conn]struct{}

	reapInterval time.Duration
}

func New(addr, dataDir, graphPath, logDir string) (*Server, error) {
	if err := os.MkdirAll(logDir, 0o755); err != nil {
		return nil, err
	}
	st, err := store.New(dataDir, graphPath)
	if err != nil {
		return nil, err
	}
	return &Server{
		addr:         addr,
		store:        st,
		logDir:       logDir,
		stopCh:       make(chan struct{}),
		conns:        map[net.Conn]struct{}{},
		reapInterval: 100 * time.Millisecond,
	}, nil
}

// ListenAndServe binds the loopback address. A bind failure is reported as
// E_PORT so callers can distinguish "port already in use" deterministically.
func (s *Server) ListenAndServe() error {
	ln, err := net.Listen("tcp", s.addr)
	if err != nil {
		return &store.BizError{
			Code:    "E_PORT",
			Message: "cannot bind loopback address " + s.addr + ": " + err.Error(),
			Detail:  map[string]any{"addr": s.addr},
		}
	}
	s.listener = ln
	s.wg.Add(2)
	go s.reaperLoop()
	go s.acceptLoop()
	return nil
}

func (s *Server) Addr() string {
	if s.listener == nil {
		return s.addr
	}
	return s.listener.Addr().String()
}

func (s *Server) acceptLoop() {
	defer s.wg.Done()
	for {
		conn, err := s.listener.Accept()
		if err != nil {
			select {
			case <-s.stopCh:
				return
			default:
			}
			if errors.Is(err, net.ErrClosed) {
				return
			}
			continue
		}
		s.wg.Add(1)
		s.connMu.Lock()
		s.conns[conn] = struct{}{}
		s.connMu.Unlock()
		go s.handleConn(conn)
	}
}

func (s *Server) reaperLoop() {
	defer s.wg.Done()
	ticker := time.NewTicker(s.reapInterval)
	defer ticker.Stop()
	for {
		select {
		case <-s.stopCh:
			return
		case <-ticker.C:
			reclaims, _ := s.store.ReapExpired(0)
			for _, r := range reclaims {
				s.logf("reclaim key=%s holder=%s reason=%s version=%d", r.Key, r.Holder, r.Reason, r.Version)
			}
		}
	}
}

func (s *Server) logf(format string, args ...any) {
	line := time.Now().UTC().Format(time.RFC3339Nano) + " " + fmt.Sprintf(format, args...) + "\n"
	f, err := os.OpenFile(filepath.Join(s.logDir, "lockd.log"), os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o644)
	if err == nil {
		_, _ = f.WriteString(line)
		_ = f.Close()
	}
}

func (s *Server) handleConn(conn net.Conn) {
	defer s.wg.Done()
	defer func() {
		s.connMu.Lock()
		delete(s.conns, conn)
		s.connMu.Unlock()
		_ = conn.Close()
	}()
	reader := bufio.NewReaderSize(conn, 1<<20)
	writer := bufio.NewWriterSize(conn, 1<<20)
	defer writer.Flush()
	for {
		line, err := reader.ReadBytes('\n')
		if len(line) > 0 {
			resp := s.dispatch(line)
			data, _ := json.Marshal(resp)
			data = append(data, '\n')
			if _, werr := writer.Write(data); werr != nil {
				return
			}
			_ = writer.Flush()
		}
		if err != nil {
			if err != io.EOF {
				s.logf("connection read error: %v", err)
			}
			return
		}
	}
}

func (s *Server) dispatch(raw []byte) proto.Response {
	var req proto.Request
	if err := json.Unmarshal(raw, &req); err != nil {
		return errResp("E_BAD_REQUEST", "invalid JSON line: "+err.Error(), nil)
	}
	switch req.Op {
	case "acquire":
		lk, traces, err := s.store.Acquire(req.Key, req.Holder, req.TTLMs, req.NowMs, req.Force)
		if err != nil {
			return bizToResp(err)
		}
		return proto.Response{OK: true, Lock: lk, Traces: traces,
			Holder: lk.Holder, ExpiresAtMs: lk.ExpiresAt, LeaseVersion: lk.Version}
	case "renew":
		lk, err := s.store.Renew(req.Key, req.Holder, req.Version, req.TTLMs, req.NowMs)
		if err != nil {
			return bizToResp(err)
		}
		return proto.Response{OK: true, Lock: lk,
			Holder: lk.Holder, ExpiresAtMs: lk.ExpiresAt, LeaseVersion: lk.Version}
	case "release":
		traces, err := s.store.Release(req.Key, req.Holder, req.Version, req.NowMs)
		if err != nil {
			return bizToResp(err)
		}
		return proto.Response{OK: true, Traces: traces}
	case "query":
		lk, err := s.store.Query(req.Key, req.NowMs)
		if err != nil {
			return bizToResp(err)
		}
		return proto.Response{OK: true, Lock: lk}
	case "table":
		return proto.Response{OK: true, Locks: s.store.Table(req.NowMs),
			TableVersion: s.store.TableVersion(), GraphVersion: s.store.GraphVersion()}
	case "passive_table":
		return proto.Response{OK: true, Locks: s.store.PassiveTable(),
			TableVersion: s.store.TableVersion(), GraphVersion: s.store.GraphVersion()}
	case "snapshot":
		path, snap, err := s.store.TakeSnapshot(req.ID)
		if err != nil {
			return bizToResp(err)
		}
		return proto.Response{OK: true, Snapshot: map[string]any{
			"path": path, "snapshot_id": snap.SnapshotID,
			"table_version": snap.TableVersion, "graph_version": snap.GraphVersion,
			"trace_count": len(snap.Traces), "reclaim_count": len(snap.Reclaims),
		}}
	case "restore":
		res, err := s.store.RestoreSnapshot(req.Path)
		if err != nil {
			if be, ok := err.(*store.BizError); ok {
				base := errResp(be.Code, be.Message, be.Detail)
				if res != nil {
					base.Restore = res
				}
				return base
			}
			return errResp("E_SNAPSHOT", err.Error(), nil)
		}
		return proto.Response{OK: true, Restore: res}
	case "shutdown":
		s.Stop()
		return proto.Response{OK: true, Message: "shutting down"}
	default:
		return errResp("E_BAD_REQUEST", "unknown op: "+req.Op, map[string]any{"op": req.Op})
	}
}

func errResp(code, msg string, detail map[string]any) proto.Response {
	return proto.Response{OK: false, Code: code, Message: msg, Detail: detail}
}

func bizToResp(err error) proto.Response {
	if be, ok := err.(*store.BizError); ok {
		return errResp(be.Code, be.Message, be.Detail)
	}
	return errResp("E_INTERNAL", err.Error(), nil)
}

// Stop gracefully stops accepting new work and waits for handlers to drain.
func (s *Server) Stop() {
	s.stopOnce.Do(func() {
		close(s.stopCh)
		if s.listener != nil {
			_ = s.listener.Close()
		}
		s.connMu.Lock()
		for conn := range s.conns {
			_ = conn.Close()
		}
		s.connMu.Unlock()
	})
	s.wg.Wait()
}
