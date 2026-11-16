package main

import (
	"bufio"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"net"
	"os"
	"os/signal"
	"strings"
	"sync"
	"syscall"
	"time"

	"local/message-interop/internal/engine"
	"local/message-interop/internal/protocol"
)

const maxLineBytes = 4 << 20

type writer struct {
	mu   sync.Mutex
	conn net.Conn
	buf  *bufio.Writer
}

func newWriter(conn net.Conn) *writer {
	return &writer{conn: conn, buf: bufio.NewWriterSize(conn, 32*1024)}
}

func (w *writer) write(value any) error {
	encoded, err := json.Marshal(value)
	if err != nil {
		return err
	}
	w.mu.Lock()
	defer w.mu.Unlock()
	if _, err := w.buf.Write(encoded); err != nil {
		return err
	}
	if err := w.buf.WriteByte('\n'); err != nil {
		return err
	}
	return w.buf.Flush()
}

type server struct {
	engine *engine.Engine
}

func main() {
	addr := flag.String("addr", "127.0.0.1:19090", "loopback listen address")
	dataDir := flag.String("data", "./data", "persistent data directory")
	retryAfter := flag.Duration("retry-after", time.Second, "unacknowledged at-least-once retry interval")
	readyFile := flag.String("ready-file", "", "optional file receiving the ready JSON line")
	flag.Parse()

	if err := validateLoopback(*addr); err != nil {
		log.Fatalf("refusing non-loopback listener: %v", err)
	}
	core, report, err := engine.Open(*dataDir, *retryAfter)
	if err != nil {
		log.Fatalf("open engine: %v", err)
	}
	defer func() {
		if err := core.Close(); err != nil {
			log.Printf("close engine: %v", err)
		}
	}()

	listener, err := net.Listen("tcp", *addr)
	if err != nil {
		log.Fatalf("listen: %v", err)
	}
	actualAddr := listener.Addr().String()
	ready := map[string]any{
		"event":                 "ready",
		"addr":                  actualAddr,
		"data_dir":              *dataDir,
		"protocol":              protocol.WireProtocol,
		"recovered_messages":    report.RecoveredMessages,
		"recovered_deliveries":  report.RecoveredDeliveries,
		"recovered_acks":        report.RecoveredAcks,
		"rebuilt_dedup_entries": report.RebuiltDedupEntries,
		"last_message_offset":   report.LastMessageOffset,
		"tail_action":           report.TailAction,
		"truncated_bytes":       report.TruncatedBytes,
	}
	readyLine, _ := json.Marshal(ready)
	fmt.Println(string(readyLine))
	if *readyFile != "" {
		if err := os.WriteFile(*readyFile, append(readyLine, '\n'), 0o644); err != nil {
			log.Printf("write ready file: %v", err)
		}
	}

	srv := &server{engine: core}
	stop := make(chan os.Signal, 1)
	signal.Notify(stop, os.Interrupt, syscall.SIGTERM)
	go func() {
		<-stop
		log.Printf("shutdown requested")
		_ = listener.Close()
	}()

	var connections sync.WaitGroup
	for {
		conn, err := listener.Accept()
		if err != nil {
			if errors.Is(err, net.ErrClosed) {
				break
			}
			log.Printf("accept: %v", err)
			continue
		}
		connections.Add(1)
		go func() {
			defer connections.Done()
			defer conn.Close()
			srv.handle(conn)
		}()
	}
	connections.Wait()
}

func validateLoopback(addr string) error {
	host, _, err := net.SplitHostPort(addr)
	if err != nil {
		return err
	}
	if strings.EqualFold(host, "localhost") {
		return nil
	}
	ip := net.ParseIP(host)
	if ip == nil || !ip.IsLoopback() {
		return fmt.Errorf("host %q is not a loopback IP", host)
	}
	return nil
}

func (s *server) handle(conn net.Conn) {
	output := newWriter(conn)
	reader := bufio.NewReaderSize(conn, 64*1024)
	for {
		line, err := readLine(reader)
		if err != nil {
			if !errors.Is(err, io.EOF) && !errors.Is(err, net.ErrClosed) {
				_ = output.write(errorResponse(protocol.NewError(protocol.CodeProtocol, err.Error())))
			}
			return
		}
		if len(strings.TrimSpace(string(line))) == 0 {
			continue
		}
		requestType, apiErr := protocol.DetectType(line)
		if apiErr != nil {
			if output.write(errorResponse(apiErr)) != nil {
				return
			}
			continue
		}
		switch requestType {
		case "publish":
			message, semantics, parseErr := protocol.ParsePublish(line)
			if parseErr != nil {
				if output.write(errorResponse(parseErr)) != nil {
					return
				}
				continue
			}
			result, publishErr := s.engine.Publish(message, semantics)
			if publishErr != nil {
				if output.write(errorResponse(publishErr)) != nil {
					return
				}
				continue
			}
			if output.write(result) != nil {
				return
			}
		case "subscribe":
			request, parseErr := protocol.ParseSubscribe(line)
			if parseErr != nil {
				if output.write(errorResponse(parseErr)) != nil {
					return
				}
				continue
			}
			session, subscribeErr := s.engine.Subscribe(request)
			if subscribeErr != nil {
				if output.write(errorResponse(subscribeErr)) != nil {
					return
				}
				continue
			}
			s.consume(conn, output, reader, session)
			return
		case "ping":
			if output.write(map[string]any{"ok": true, "type": "pong", "protocol": protocol.WireProtocol}) != nil {
				return
			}
		case "ack":
			if output.write(errorResponse(protocol.NewError(protocol.CodeProtocol, "ack requires an active subscribe on this connection"))) != nil {
				return
			}
		default:
			apiErr := protocol.NewError(protocol.CodeProtocol, "unknown request type")
			apiErr.Details = map[string]any{"type": requestType}
			if output.write(errorResponse(apiErr)) != nil {
				return
			}
		}
	}
}

func (s *server) consume(conn net.Conn, output *writer, reader *bufio.Reader, session *engine.Session) {
	ack := map[string]any{
		"ok":               true,
		"type":             "subscribe_ack",
		"consumer_id":      session.ConsumerID,
		"version":          session.Version,
		"partition_key":    session.Partition,
		"session_id":       session.ID,
		"protocol":         protocol.WireProtocol,
		"semantics_source": "message",
	}
	if output.write(ack) != nil {
		s.engine.CloseSession(session.ID)
		return
	}
	done := make(chan struct{})
	go func() {
		defer close(done)
		ticker := time.NewTicker(50 * time.Millisecond)
		defer ticker.Stop()
		for {
			deliveries, err := s.engine.NextDeliveries(session.ID)
			if err != nil {
				_ = conn.Close()
				return
			}
			for _, delivery := range deliveries {
				if err := output.write(delivery); err != nil {
					_ = conn.Close()
					return
				}
			}
			select {
			case <-session.Wake:
			case <-ticker.C:
			}
		}
	}()

	for {
		line, err := readLine(reader)
		if err != nil {
			break
		}
		if len(strings.TrimSpace(string(line))) == 0 {
			continue
		}
		request, parseErr := protocol.ParseAck(line)
		if parseErr != nil {
			if output.write(errorResponse(parseErr)) != nil {
				break
			}
			continue
		}
		result, ackErr := s.engine.Ack(session.ID, request)
		if ackErr != nil {
			if output.write(errorResponse(ackErr)) != nil {
				break
			}
			continue
		}
		if output.write(result) != nil {
			break
		}
	}
	s.engine.CloseSession(session.ID)
	_ = conn.Close()
	select {
	case <-done:
	case <-time.After(time.Second):
	}
}

func readLine(reader *bufio.Reader) ([]byte, error) {
	line, err := reader.ReadBytes('\n')
	if err != nil {
		if errors.Is(err, io.EOF) && len(line) > 0 {
			return nil, fmt.Errorf("unterminated JSON line")
		}
		return nil, err
	}
	if len(line) > maxLineBytes {
		return nil, fmt.Errorf("JSON line exceeds %d bytes", maxLineBytes)
	}
	return line, nil
}

func errorResponse(apiErr *protocol.APIError) protocol.ErrorResponse {
	return protocol.ErrorResponse{OK: false, Type: "error", Error: apiErr}
}
