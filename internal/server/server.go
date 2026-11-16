package server

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net"
	"sync"
	"sync/atomic"
	"time"

	"localdns/internal/biz"
	"localdns/internal/dnswire"
	"localdns/internal/engine"
	"localdns/internal/model"
	"localdns/internal/protocolmeta"
)

type Options struct {
	UDPAddr    string
	TCPAddr    string
	MaxUDP     int
	Logger     *log.Logger
	TCPTimeout time.Duration
}

type Server struct {
	engine    *engine.Engine
	opts      Options
	udp       *net.UDPConn
	tcp       *net.TCPListener
	ctx       context.Context
	cancel    context.CancelFunc
	wg        sync.WaitGroup
	closeOnce sync.Once
	connMu    sync.Mutex
	conns     map[*net.TCPConn]struct{}
	requests  atomic.Uint64
	errors    atomic.Uint64
	truncated atomic.Uint64
}

func New(eng *engine.Engine, opts Options) *Server {
	if opts.MaxUDP <= 0 {
		opts.MaxUDP = 512
	}
	if opts.TCPTimeout <= 0 {
		opts.TCPTimeout = 30 * time.Second
	}
	if opts.Logger == nil {
		opts.Logger = log.New(io.Discard, "", 0)
	}
	ctx, cancel := context.WithCancel(context.Background())
	return &Server{engine: eng, opts: opts, ctx: ctx, cancel: cancel, conns: make(map[*net.TCPConn]struct{})}
}

func (s *Server) Start() error {
	udpAddr, err := net.ResolveUDPAddr("udp", s.opts.UDPAddr)
	if err != nil {
		return fmt.Errorf("resolve UDP address: %w", err)
	}
	if udpAddr.IP == nil || !udpAddr.IP.IsLoopback() {
		return fmt.Errorf("UDP address %s is not loopback", udpAddr)
	}
	udp, err := net.ListenUDP("udp", udpAddr)
	if err != nil {
		return fmt.Errorf("bind UDP %s: %w", udpAddr, err)
	}
	tcpAddr, err := net.ResolveTCPAddr("tcp", s.opts.TCPAddr)
	if err != nil {
		udp.Close()
		return fmt.Errorf("resolve TCP address: %w", err)
	}
	if tcpAddr.IP == nil || !tcpAddr.IP.IsLoopback() {
		udp.Close()
		return fmt.Errorf("TCP address %s is not loopback", tcpAddr)
	}
	tcp, err := net.ListenTCP("tcp", tcpAddr)
	if err != nil {
		udp.Close()
		return fmt.Errorf("bind TCP %s: %w", tcpAddr, err)
	}
	s.udp = udp
	s.tcp = tcp
	s.wg.Add(2)
	go s.serveUDP()
	go s.serveTCP()
	return nil
}

func (s *Server) UDPAddr() string {
	if s.udp == nil {
		return ""
	}
	return s.udp.LocalAddr().String()
}

func (s *Server) TCPAddr() string {
	if s.tcp == nil {
		return ""
	}
	return s.tcp.Addr().String()
}

func (s *Server) Close() {
	s.closeOnce.Do(func() {
		s.cancel()
		if s.udp != nil {
			_ = s.udp.Close()
		}
		if s.tcp != nil {
			_ = s.tcp.Close()
		}
		s.connMu.Lock()
		for conn := range s.conns {
			_ = conn.Close()
		}
		s.connMu.Unlock()
		s.wg.Wait()
	})
}

func (s *Server) serveUDP() {
	defer s.wg.Done()
	buf := make([]byte, 65535)
	for {
		n, remote, err := s.udp.ReadFromUDP(buf)
		if err != nil {
			if s.ctx.Err() != nil {
				return
			}
			continue
		}
		payload := append([]byte(nil), buf[:n]...)
		s.wg.Add(1)
		go func(remote *net.UDPAddr) {
			defer s.wg.Done()
			response := s.handlePacket(payload, "udp", remote.String())
			if len(response) == 0 {
				return
			}
			_, _ = s.udp.WriteToUDP(response, remote)
		}(remote)
	}
}

func (s *Server) serveTCP() {
	defer s.wg.Done()
	for {
		conn, err := s.tcp.AcceptTCP()
		if err != nil {
			if s.ctx.Err() != nil {
				return
			}
			continue
		}
		s.connMu.Lock()
		s.conns[conn] = struct{}{}
		s.connMu.Unlock()
		s.wg.Add(1)
		go func() {
			defer s.wg.Done()
			s.serveTCPConn(conn)
		}()
	}
}

func (s *Server) serveTCPConn(conn *net.TCPConn) {
	defer func() {
		conn.Close()
		s.connMu.Lock()
		delete(s.conns, conn)
		s.connMu.Unlock()
	}()
	remote := conn.RemoteAddr().String()
	for {
		_ = conn.SetReadDeadline(time.Now().Add(s.opts.TCPTimeout))
		payload, err := dnswire.ReadTCPFrame(conn, 65535)
		if err != nil {
			if errors.Is(err, dnswire.ErrEmptyFrame) {
				appErr := biz.At(biz.CodeWire, 0, "zero-length TCP DNS frame")
				s.logError(appErr, "tcp", remote, 0)
				response := s.buildErrorResponse(nil, nil, appErr)
				_ = s.writeTCPResponse(conn, response)
			}
			return
		}
		response := s.handlePacket(payload, "tcp", remote)
		if len(response) == 0 {
			continue
		}
		if err := s.writeTCPResponse(conn, response); err != nil {
			return
		}
	}
}

func (s *Server) writeTCPResponse(conn *net.TCPConn, response []byte) error {
	_ = conn.SetWriteDeadline(time.Now().Add(s.opts.TCPTimeout))
	return dnswire.WriteTCPFrame(conn, response)
}

func (s *Server) handlePacket(payload []byte, transport, remote string) []byte {
	s.requests.Add(1)
	msg, parseErr := dnswire.ParseMessage(payload)
	if parseErr != nil {
		s.errors.Add(1)
		s.logError(parseErr, transport, remote, headerID(payload))
		response := s.buildErrorResponse(payload, nil, parseErr)
		return s.fitForTransport(response, nil, transport)
	}
	if msg.Header.IsResponse() {
		appErr := biz.At(biz.CodeWire, 2, "received a DNS response on the query endpoint")
		s.errors.Add(1)
		s.logError(appErr, transport, remote, msg.Header.ID)
		return s.fitForTransport(s.buildErrorResponse(payload, &msg, appErr), &msg, transport)
	}
	if msg.Header.Opcode() != 0 {
		appErr := biz.New(biz.CodeQuery, "only standard DNS queries are supported")
		s.errors.Add(1)
		s.logError(appErr, transport, remote, msg.Header.ID)
		return s.fitForTransport(s.buildErrorResponse(payload, &msg, appErr), &msg, transport)
	}
	if len(msg.Questions) != 1 {
		appErr := biz.At(biz.CodeWire, 4, fmt.Sprintf("question count is %d, want 1", len(msg.Questions)))
		s.errors.Add(1)
		s.logError(appErr, transport, remote, msg.Header.ID)
		return s.fitForTransport(s.buildErrorResponse(payload, &msg, appErr), &msg, transport)
	}
	q := msg.Questions[0]
	result, err := s.engine.Resolve(s.ctx, model.Question{Name: q.Name, Type: q.Type, Class: q.Class})
	if err != nil {
		s.errors.Add(1)
		s.logError(err, transport, remote, msg.Header.ID)
		return s.fitForTransport(s.buildErrorResponse(payload, &msg, err), &msg, transport)
	}
	response := s.buildResponse(&msg, result)
	return s.fitForTransport(response, &msg, transport)
}

func (s *Server) buildResponse(query *dnswire.Message, result *engine.Result) []byte {
	flags := uint16(dnswire.FlagQR | dnswire.FlagRA)
	flags |= query.Header.Flags & (dnswire.FlagRD | 0x7800)
	if result.Resolution.PrimarySource != model.SourceUpstream {
		flags |= dnswire.FlagAA
	}
	flags = (flags &^ 0x000f) | uint16(result.Resolution.RCode&0x0f)
	response := dnswire.Message{
		Header:    dnswire.Header{ID: query.Header.ID, Flags: flags},
		Questions: append([]dnswire.Question(nil), query.Questions...),
	}
	for _, ar := range result.Resolution.Answers {
		response.Answers = append(response.Answers, toRR(ar.Record))
	}
	for _, ar := range result.Resolution.Authorities {
		response.Authorities = append(response.Authorities, toRR(ar.Record))
	}
	meta := protocolmeta.FromResolution(result.Resolution, result.CacheHit, result.Coalesced, result.CacheVersion)
	if payload, err := protocolmeta.Encode(meta); err == nil {
		response.Additionals = append(response.Additionals, dnswire.MetadataRR(payload))
	}
	wire, err := dnswire.EncodeMessage(response)
	if err != nil {
		appErr := biz.New(biz.CodeWire, "cannot encode local response: "+err.Error())
		return s.buildErrorResponse(nil, query, appErr)
	}
	return wire
}

func (s *Server) fitForTransport(response []byte, query *dnswire.Message, transport string) []byte {
	if transport != "udp" {
		return response
	}
	msg, err := dnswire.ParseMessage(response)
	if err != nil {
		return response
	}
	fitted, truncated, fitErr := dnswire.FitUDP(msg, s.opts.MaxUDP)
	if fitErr != nil {
		return response
	}
	if truncated {
		s.truncated.Add(1)
	}
	return fitted
}

func toRR(rec model.Record) dnswire.RR {
	return dnswire.RR{Name: rec.Name, Type: rec.Type, Class: uint16(rec.Class), TTL: rec.TTL, Data: rec.Data}
}

func headerID(payload []byte) uint16 {
	if len(payload) >= 2 {
		return uint16(payload[0])<<8 | uint16(payload[1])
	}
	return 0
}

func (s *Server) buildErrorResponse(payload []byte, msg *dnswire.Message, appErr error) []byte {
	flags := uint16(dnswire.FlagQR | dnswire.FlagRA)
	id := headerID(payload)
	questions := []dnswire.Question(nil)
	if msg != nil {
		id = msg.Header.ID
		flags |= msg.Header.Flags & (dnswire.FlagRD | 0x7800)
		questions = append(questions, msg.Questions...)
	}
	rcode := model.RCodeServFail
	if biz.CodeOf(appErr) == biz.CodeWire {
		rcode = model.RCodeFormErr
	} else if biz.CodeOf(appErr) == biz.CodeQuery {
		rcode = model.RCodeNotImp
	}
	flags = (flags &^ 0x000f) | uint16(rcode)
	response := dnswire.Message{
		Header:    dnswire.Header{ID: id, Flags: flags},
		Questions: questions,
	}
	if metaPayload, err := protocolmeta.Encode(protocolmeta.FromError(appErr)); err == nil {
		response.Additionals = append(response.Additionals, dnswire.MetadataRR(metaPayload))
	}
	wire, err := dnswire.EncodeMessage(response)
	if err != nil {
		return wire
	}
	return wire
}

func (s *Server) logError(err error, transport, remote string, txid uint16) {
	event := map[string]any{
		"event": "query_error", "transport": transport, "remote": remote,
		"txid": txid, "code": string(biz.CodeOf(err)), "detail": err.Error(),
	}
	var appErr *biz.Error
	if errors.As(err, &appErr) && appErr.HasOffset {
		event["offset"] = appErr.Offset
	}
	data, _ := json.Marshal(event)
	s.opts.Logger.Print(string(data))
}

type Stats struct {
	Requests  uint64 `json:"requests"`
	Errors    uint64 `json:"errors"`
	Truncated uint64 `json:"truncated_udp"`
}

func (s *Server) Stats() Stats {
	return Stats{Requests: s.requests.Load(), Errors: s.errors.Load(), Truncated: s.truncated.Load()}
}

// LoopbackAddresses returns the concrete bound addresses for diagnostics.
func (s *Server) LoopbackAddresses() (string, string) {
	return s.UDPAddr(), s.TCPAddr()
}
