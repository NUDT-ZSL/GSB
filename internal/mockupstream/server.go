package mockupstream

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log"
	"net"
	"sync"
	"time"

	"localdns/internal/dnswire"
	"localdns/internal/model"
	"localdns/internal/netutil"
)

const (
	ModeScripted = "scripted"
	ModeValid    = "valid"
	ModeConflict = "conflict"
	ModeBadChain = "bad-chain"
	ModeSilent   = "silent"
)

type Server struct {
	listener *net.TCPListener
	mode     string
	logger   *log.Logger
	ctx      context.Context
	cancel   context.CancelFunc
	wg       sync.WaitGroup
	once     sync.Once
}

func Listen(addr, mode string, logger *log.Logger) (*Server, error) {
	if err := netutil.EnsureLoopbackAddress(addr); err != nil {
		return nil, err
	}
	tcpAddr, err := net.ResolveTCPAddr("tcp", addr)
	if err != nil {
		return nil, err
	}
	if tcpAddr.IP == nil || !tcpAddr.IP.IsLoopback() {
		return nil, fmt.Errorf("upstream address %s is not loopback", tcpAddr)
	}
	listener, err := net.ListenTCP("tcp", tcpAddr)
	if err != nil {
		return nil, err
	}
	if logger == nil {
		logger = log.New(io.Discard, "", 0)
	}
	ctx, cancel := context.WithCancel(context.Background())
	s := &Server{listener: listener, mode: mode, logger: logger, ctx: ctx, cancel: cancel}
	if s.mode == "" {
		s.mode = ModeScripted
	}
	s.wg.Add(1)
	go s.serve()
	return s, nil
}

func (s *Server) Addr() string { return s.listener.Addr().String() }

func (s *Server) Close() {
	s.once.Do(func() {
		s.cancel()
		_ = s.listener.Close()
		s.wg.Wait()
	})
}

func (s *Server) serve() {
	defer s.wg.Done()
	for {
		conn, err := s.listener.AcceptTCP()
		if err != nil {
			if s.ctx.Err() != nil {
				return
			}
			continue
		}
		s.wg.Add(1)
		go func() {
			defer s.wg.Done()
			s.serveConn(conn)
		}()
	}
}

func (s *Server) serveConn(conn *net.TCPConn) {
	defer conn.Close()
	for {
		_ = conn.SetReadDeadline(time.Now().Add(10 * time.Second))
		payload, err := dnswire.ReadTCPFrame(conn, 65535)
		if err != nil {
			return
		}
		if s.mode == ModeSilent {
			continue
		}
		response, err := s.Respond(payload)
		if err != nil {
			s.logger.Print(err)
			return
		}
		_ = conn.SetWriteDeadline(time.Now().Add(5 * time.Second))
		if err := dnswire.WriteTCPFrame(conn, response); err != nil {
			return
		}
	}
}

func (s *Server) Respond(payload []byte) ([]byte, error) {
	query, appErr := dnswire.ParseMessage(payload)
	if appErr != nil {
		return nil, appErr
	}
	if len(query.Questions) != 1 {
		return nil, errors.New("mock upstream requires exactly one question")
	}
	q := query.Questions[0]
	response := dnswire.Message{
		Header: dnswire.Header{
			ID:    query.Header.ID,
			Flags: dnswire.FlagQR | dnswire.FlagRA | (query.Header.Flags & dnswire.FlagRD),
		},
		Questions: append([]dnswire.Question(nil), query.Questions...),
	}
	switch s.mode {
	case ModeValid:
		if q.Type == model.TypeA {
			response.Answers = []dnswire.RR{aRR(q.Name, "203.0.113.10", 60)}
		}
	case ModeConflict:
		response.Answers = []dnswire.RR{
			cnameRR(q.Name, "clash.demo.test", 120),
			aRR("clash.demo.test", "198.51.100.99", 120),
		}
	case ModeBadChain:
		response.Answers = []dnswire.RR{
			cnameRR(q.Name, "broken.external.test", 120),
			aRR("wrong.external.test", "203.0.113.66", 120),
		}
	default:
		s.scripted(q, &response)
	}
	wire, err := dnswire.EncodeMessage(response)
	if err != nil {
		return nil, err
	}
	return wire, nil
}

func (s *Server) scripted(q dnswire.Question, response *dnswire.Message) {
	switch q.Name {
	case "api.external.test":
		if q.Type == model.TypeA {
			response.Answers = []dnswire.RR{
				cnameRR(q.Name, "edge.external.test", 120),
				aRR("edge.external.test", "203.0.113.10", 120),
			}
		}
	case "portal.external.test":
		if q.Type == model.TypeA {
			response.Answers = []dnswire.RR{
				cnameRR(q.Name, "clash.demo.test", 120),
				aRR("clash.demo.test", "198.51.100.99", 120),
			}
		}
	case "bad.external.test":
		if q.Type == model.TypeA {
			response.Answers = []dnswire.RR{
				cnameRR(q.Name, "broken.external.test", 120),
				aRR("wrong.external.test", "203.0.113.66", 120),
			}
		}
	case "concurrent.external.test":
		if q.Type == model.TypeA {
			response.Answers = []dnswire.RR{aRR(q.Name, "203.0.113.77", 90)}
		}
	case "nx.external.test":
		if q.Type == model.TypeA {
			response.Header.Flags |= model.RCodeNXDomain
			response.Authorities = []dnswire.RR{{
				Name: "external.test", Type: model.TypeSOA, Class: uint16(model.ClassIN), TTL: 180,
				Data: "ns1.external.test hostmaster.external.test 2026111701 3600 600 86400 45",
			}}
		}
	default:
		if q.Type == model.TypeA {
			response.Answers = []dnswire.RR{aRR(q.Name, "203.0.113.200", 60)}
		}
	}
}

func aRR(name, ip string, ttl uint32) dnswire.RR {
	return dnswire.RR{Name: name, Type: model.TypeA, Class: uint16(model.ClassIN), TTL: ttl, Data: ip}
}

func cnameRR(name, target string, ttl uint32) dnswire.RR {
	return dnswire.RR{Name: name, Type: model.TypeCNAME, Class: uint16(model.ClassIN), TTL: ttl, Data: target}
}
