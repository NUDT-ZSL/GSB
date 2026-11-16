package upstream

import (
	"context"
	"encoding/binary"
	"errors"
	"net"
	"testing"
	"time"

	"localdns/internal/biz"
	"localdns/internal/dnswire"
	"localdns/internal/mockupstream"
	"localdns/internal/model"
)

func startMock(t *testing.T, mode string) *mockupstream.Server {
	t.Helper()
	srv, err := mockupstream.Listen("127.0.0.1:0", mode, nil)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(srv.Close)
	return srv
}

func newTestClient(t *testing.T, addr string) *Client {
	t.Helper()
	client, err := NewClient(addr, time.Second, 60, 16)
	if err != nil {
		t.Fatal(err)
	}
	return client
}

func TestForwardingValidChainAndConflictingData(t *testing.T) {
	srv := startMock(t, mockupstream.ModeScripted)
	client := newTestClient(t, srv.Addr())
	ctx := context.Background()

	res, err := client.Query(ctx, model.Question{Name: "API.EXTERNAL.TEST", Type: model.TypeA, Class: model.ClassIN})
	if err != nil {
		t.Fatal(err)
	}
	if len(res.Answers) != 2 || len(res.CNAMEChain) != 1 {
		t.Fatalf("valid chain mismatch: %+v", res)
	}
	if res.Answers[0].Record.Type != model.TypeCNAME || res.Answers[1].Record.Type != model.TypeA {
		t.Fatalf("unexpected answer ordering: %+v", res.Answers)
	}
	if res.PrimarySource != model.SourceUpstream {
		t.Fatalf("source=%s", res.PrimarySource)
	}

	res, err = client.Query(ctx, model.Question{Name: "portal.external.test", Type: model.TypeA, Class: model.ClassIN})
	if err != nil {
		t.Fatal(err)
	}
	if res.Answers[1].Record.Data != "198.51.100.99" {
		t.Fatalf("conflict sample data mismatch: %+v", res.Answers)
	}

	_, err = client.Query(ctx, model.Question{Name: "bad.external.test", Type: model.TypeA, Class: model.ClassIN})
	if biz.CodeOf(err) != biz.CodeChain {
		t.Fatalf("want E_CHAIN, got %v", err)
	}

	res, err = client.Query(ctx, model.Question{Name: "nx.external.test", Type: model.TypeA, Class: model.ClassIN})
	if err != nil {
		t.Fatal(err)
	}
	if !res.Negative || res.NegativeTTL != 45 || len(res.Authorities) != 1 || res.Authorities[0].Record.TTL != 45 {
		t.Fatalf("negative forwarding mismatch: %+v", res)
	}
}

func TestUpstreamUnreachableIsEUpstream(t *testing.T) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	addr := ln.Addr().String()
	_ = ln.Close()
	client := newTestClient(t, addr)
	_, err = client.Query(context.Background(), model.Question{Name: "down.external.test", Type: model.TypeA, Class: model.ClassIN})
	if biz.CodeOf(err) != biz.CodeUpstream {
		t.Fatalf("want E_UPSTREAM, got %v", err)
	}
}

type fakeHandler func(request []byte) []byte

func startFakeUpstream(t *testing.T, handler fakeHandler) string {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = ln.Close() })
	go func() {
		conn, acceptErr := ln.Accept()
		if acceptErr != nil {
			return
		}
		defer conn.Close()
		request, frameErr := dnswire.ReadTCPFrame(conn, 65535)
		if frameErr != nil {
			return
		}
		response := handler(request)
		if response != nil {
			_ = dnswire.WriteTCPFrame(conn, response)
		}
	}()
	return ln.Addr().String()
}

func TestCompressedUpstreamAnswerIsExpanded(t *testing.T) {
	addr := startFakeUpstream(t, func(request []byte) []byte {
		response := append([]byte(nil), request...)
		flags := binary.BigEndian.Uint16(response[2:4]) | dnswire.FlagQR | dnswire.FlagRA
		binary.BigEndian.PutUint16(response[2:4], flags)
		binary.BigEndian.PutUint16(response[6:8], 1)
		response = append(response, 0xc0, 0x0c) // compressed owner: question name
		response = append(response, 0, 1, 0, 1, 0, 0, 0, 60, 0, 4)
		response = append(response, 203, 0, 113, 11)
		return response
	})
	client := newTestClient(t, addr)
	res, err := client.Query(context.Background(), model.Question{Name: "compressed.external.test", Type: model.TypeA, Class: model.ClassIN})
	if err != nil {
		t.Fatal(err)
	}
	if len(res.Answers) != 1 || res.Answers[0].Record.Name != "compressed.external.test" || res.Answers[0].Record.Data != "203.0.113.11" {
		t.Fatalf("compressed answer not expanded: %+v", res.Answers)
	}
}

func TestTransactionAndQuestionEchoMismatch(t *testing.T) {
	addr := startFakeUpstream(t, func(request []byte) []byte {
		response := append([]byte(nil), request...)
		binary.BigEndian.PutUint16(response[0:2], binary.BigEndian.Uint16(response[0:2])+1)
		binary.BigEndian.PutUint16(response[2:4], binary.BigEndian.Uint16(response[2:4])|dnswire.FlagQR)
		return response
	})
	client := newTestClient(t, addr)
	_, err := client.Query(context.Background(), model.Question{Name: "tx.external.test", Type: model.TypeA, Class: model.ClassIN})
	if biz.CodeOf(err) != biz.CodeChain {
		t.Fatalf("want E_CHAIN for txid mismatch, got %v", err)
	}
}

func TestMalformedUpstreamWireIsChainError(t *testing.T) {
	addr := startFakeUpstream(t, func(request []byte) []byte {
		response := append([]byte(nil), request...)
		binary.BigEndian.PutUint16(response[2:4], binary.BigEndian.Uint16(response[2:4])|dnswire.FlagQR)
		binary.BigEndian.PutUint16(response[6:8], 1)
		response = append(response, 0xe8, 0x00) // answer owner pointer to offset 0xe800, out of bounds
		response = append(response, 0, 1, 0, 1, 0, 0, 0, 60, 0, 4, 192, 0, 2, 1)
		return response
	})
	client := newTestClient(t, addr)
	_, err := client.Query(context.Background(), model.Question{Name: "malformed.external.test", Type: model.TypeA, Class: model.ClassIN})
	if biz.CodeOf(err) != biz.CodeChain {
		t.Fatalf("want E_CHAIN, got %v", err)
	}
	var appErr *biz.Error
	if !errors.As(err, &appErr) || !appErr.HasOffset {
		t.Fatalf("E_CHAIN should retain the underlying wire offset: %v", err)
	}
}
