package service

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"os"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/NUDT-ZSL/GSB/orderqueue/internal/engine"
	"github.com/NUDT-ZSL/GSB/orderqueue/internal/protocol"
)

type Logger interface {
	Log(event string, fields map[string]any)
}

type Config struct {
	Listen       string
	MaxLineBytes int
}

type Server struct {
	engine       *engine.Engine
	logger       Logger
	maxLineBytes int
	requestSeq   atomic.Int64

	mu        sync.Mutex
	listener  net.Listener
	conns     map[net.Conn]struct{}
	closed    bool
	wg        sync.WaitGroup
	readyAddr string
}

func New(engine *engine.Engine, logger Logger) *Server {
	return &Server{engine: engine, logger: logger, maxLineBytes: 2 * 1024 * 1024, conns: map[net.Conn]struct{}{}}
}

func (s *Server) Listen(ctx context.Context, address string) (string, error) {
	listener, err := net.Listen("tcp", address)
	if err != nil {
		return "", err
	}
	s.mu.Lock()
	s.listener = listener
	s.readyAddr = listener.Addr().String()
	addr := s.readyAddr
	s.mu.Unlock()
	s.log("server_ready", map[string]any{"listen": addr, "pid": os.Getpid()})
	s.wg.Add(1)
	go s.acceptLoop(ctx, listener)
	return addr, nil
}

func (s *Server) Addr() string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.readyAddr
}

func (s *Server) acceptLoop(ctx context.Context, listener net.Listener) {
	defer s.wg.Done()
	for {
		conn, err := listener.Accept()
		if err != nil {
			select {
			case <-ctx.Done():
				return
			default:
			}
			if errors.Is(err, net.ErrClosed) {
				return
			}
			s.log("accept_error", map[string]any{"error": err.Error()})
			continue
		}
		s.mu.Lock()
		if s.closed {
			s.mu.Unlock()
			_ = conn.Close()
			return
		}
		s.conns[conn] = struct{}{}
		s.mu.Unlock()
		s.wg.Add(1)
		go s.handleConn(ctx, conn)
	}
}

func (s *Server) handleConn(ctx context.Context, conn net.Conn) {
	defer s.wg.Done()
	defer func() {
		s.mu.Lock()
		delete(s.conns, conn)
		s.mu.Unlock()
		_ = conn.Close()
	}()
	peer := conn.RemoteAddr().String()
	s.log("connection_open", map[string]any{"peer": peer})
	defer s.log("connection_close", map[string]any{"peer": peer})
	reader := bufio.NewReaderSize(conn, 64*1024)
	writer := bufio.NewWriter(conn)
	for {
		select {
		case <-ctx.Done():
			return
		default:
		}
		line, tooLong, err := readLine(reader, s.maxLineBytes)
		if err != nil {
			if !errors.Is(err, io.EOF) && !errors.Is(err, net.ErrClosed) {
				s.log("connection_read_error", map[string]any{"peer": peer, "error": err.Error()})
			}
			return
		}
		if tooLong {
			resp := protocol.Failure("", "", protocol.NewError(protocol.CodeProtocol, "JSON line exceeds the maximum length").With("max_line_bytes", s.maxLineBytes))
			_ = writeResponse(writer, resp)
			return
		}
		line = trimLine(line)
		if len(line) == 0 {
			continue
		}
		start := time.Now()
		resp := s.handleRequest(ctx, line)
		resp.RequestID = ensureRequestID(resp.RequestID, &s.requestSeq)
		if err := writeResponse(writer, resp); err != nil {
			s.log("connection_write_error", map[string]any{"peer": peer, "error": err.Error()})
			return
		}
		s.log("request_completed", map[string]any{
			"request_id":    resp.RequestID,
			"request_type":  resp.Type,
			"partition_key": resp.PartitionKey,
			"code":          resp.Code,
			"ok":            resp.OK,
			"duration_us":   time.Since(start).Microseconds(),
		})
	}
}

func (s *Server) handleRequest(ctx context.Context, line []byte) *protocol.Response {
	var request protocol.Request
	if err := json.Unmarshal(line, &request); err != nil {
		return protocol.Failure("", "", protocol.NewError(protocol.CodeProtocol, "invalid JSON request: %v", err))
	}
	request.Type = strings.ToLower(strings.TrimSpace(request.Type))
	if request.RequestID == "" {
		request.RequestID = fmt.Sprintf("srv-%d", s.requestSeq.Add(1))
	}
	s.log("request_received", map[string]any{
		"request_id":    request.RequestID,
		"request_type":  request.Type,
		"partition_key": request.PartitionKey,
	})
	switch request.Type {
	case "append":
		result, err := s.engine.Append(ctx, request.RequestID, request.PartitionKey, request.ProducerSession, request.ProducerSeq, request.Body)
		if err != nil {
			return protocol.Failure(request.RequestID, request.Type, err)
		}
		resp := protocol.Success(request.RequestID, request.Type)
		resp.PartitionKey = result.PartitionKey
		resp.Offset = result.Offset
		resp.NextOffset = result.NextOffset
		return resp
	case "pull":
		result, err := s.engine.Pull(ctx, request.RequestID, request.PartitionKey, request.ConsumerSession, request.Limit, request.TimeoutMS)
		if err != nil {
			return protocol.Failure(request.RequestID, request.Type, err)
		}
		resp := protocol.Success(request.RequestID, request.Type)
		resp.PartitionKey = result.PartitionKey
		resp.Session = result.Session
		resp.Window = result.Window
		resp.Messages = result.Messages
		resp.RetryCounts = result.RetryCounts
		return resp
	case "commit":
		result, err := s.engine.Commit(ctx, request.RequestID, request.PartitionKey, request.ConsumerSession, request.Offset)
		if err != nil {
			return protocol.Failure(request.RequestID, request.Type, err)
		}
		resp := protocol.Success(request.RequestID, request.Type)
		resp.PartitionKey = result.PartitionKey
		resp.CommittedOffset = result.CommittedOffset
		resp.ConfirmedOffset = result.ConfirmedOffset
		resp.Window = result.Window
		return resp
	case "fail":
		result, err := s.engine.Fail(ctx, request.RequestID, request.PartitionKey, request.ConsumerSession, request.Offset, request.Reason)
		if err != nil {
			if result.PartitionKey != "" {
				resp := protocol.Failure(request.RequestID, request.Type, err)
				resp.PartitionKey = result.PartitionKey
				resp.RedeliveryFrom = result.RedeliveryFrom
				resp.BlockedOffset = result.BlockedOffset
				resp.BlockedRetryCount = result.BlockedRetryCount
				resp.Fields["redelivery_from"] = result.RedeliveryFrom
				return resp
			}
			return protocol.Failure(request.RequestID, request.Type, err)
		}
		resp := protocol.Success(request.RequestID, request.Type)
		resp.PartitionKey = result.PartitionKey
		resp.RedeliveryFrom = result.RedeliveryFrom
		return resp
	case "unblock":
		result, err := s.engine.Unblock(ctx, request.RequestID, request.PartitionKey, request.Operator, request.ExpectedBlockedOffset)
		if err != nil {
			return protocol.Failure(request.RequestID, request.Type, err)
		}
		resp := protocol.Success(request.RequestID, request.Type)
		resp.PartitionKey = result.PartitionKey
		return resp
	case "status", "inspect":
		result, err := s.engine.Status(ctx)
		if err != nil {
			return protocol.Failure(request.RequestID, request.Type, err)
		}
		status := &protocol.StatusView{
			LogFormatVersion:    result.LogFormatVersion,
			LedgerFormatVersion: result.LedgerFormatVersion,
			MaxRetries:          result.MaxRetries,
			Partitions:          result.Partitions,
		}
		if request.PartitionKey != "" {
			filtered := make([]protocol.PartitionView, 0, 1)
			for _, partition := range status.Partitions {
				if partition.PartitionKey == request.PartitionKey {
					filtered = append(filtered, partition)
				}
			}
			status.Partitions = filtered
		}
		resp := protocol.Success(request.RequestID, request.Type)
		resp.Status = status
		return resp
	case "ping":
		return protocol.Success(request.RequestID, request.Type)
	default:
		return protocol.Failure(request.RequestID, request.Type, protocol.NewError(protocol.CodeProtocol, "unknown request type %q", request.Type))
	}
}

func (s *Server) Close() error {
	s.mu.Lock()
	if s.closed {
		s.mu.Unlock()
		return nil
	}
	s.closed = true
	if s.listener != nil {
		_ = s.listener.Close()
	}
	for conn := range s.conns {
		_ = conn.Close()
	}
	s.mu.Unlock()
	s.wg.Wait()
	return nil
}

func (s *Server) log(event string, fields map[string]any) {
	if s.logger != nil {
		s.logger.Log(event, fields)
	}
}

func readLine(reader *bufio.Reader, limit int) ([]byte, bool, error) {
	var out []byte
	for {
		fragment, err := reader.ReadSlice('\n')
		if len(out)+len(fragment) > limit {
			return nil, true, nil
		}
		out = append(out, fragment...)
		if err == nil {
			return out, false, nil
		}
		if errors.Is(err, bufio.ErrBufferFull) {
			continue
		}
		if errors.Is(err, io.EOF) && len(out) > 0 {
			return out, false, nil
		}
		return nil, false, err
	}
}

func trimLine(line []byte) []byte {
	for len(line) > 0 && (line[len(line)-1] == '\n' || line[len(line)-1] == '\r') {
		line = line[:len(line)-1]
	}
	return line
}

func writeResponse(writer *bufio.Writer, resp *protocol.Response) error {
	data, err := json.Marshal(resp)
	if err != nil {
		return err
	}
	if _, err := writer.Write(data); err != nil {
		return err
	}
	if err := writer.WriteByte('\n'); err != nil {
		return err
	}
	return writer.Flush()
}

func ensureRequestID(requestID string, seq *atomic.Int64) string {
	if requestID != "" {
		return requestID
	}
	return fmt.Sprintf("srv-%d", seq.Add(1))
}
