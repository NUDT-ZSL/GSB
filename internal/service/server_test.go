package service

import (
	"context"
	"net"
	"path/filepath"
	"testing"
	"time"

	"github.com/NUDT-ZSL/GSB/orderqueue/internal/client"
	"github.com/NUDT-ZSL/GSB/orderqueue/internal/engine"
	"github.com/NUDT-ZSL/GSB/orderqueue/internal/protocol"
)

func TestJSONLAppendPullFenceCommit(t *testing.T) {
	eng, err := engine.Open(context.Background(), engine.Options{
		DBPath:         filepath.Join(t.TempDir(), "queue.db"),
		MaxRetries:     3,
		DisableSweeper: true,
	})
	if err != nil {
		t.Fatalf("open engine: %v", err)
	}
	defer eng.Close()
	server := New(eng, nil)
	address, err := server.Listen(context.Background(), "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	defer server.Close()
	if _, _, err := net.SplitHostPort(address); err != nil {
		t.Fatalf("unexpected listener address %q: %v", address, err)
	}
	first, err := client.Dial(address, 2*time.Second)
	if err != nil {
		t.Fatalf("dial first client: %v", err)
	}
	defer first.Close()
	second, err := client.Dial(address, 2*time.Second)
	if err != nil {
		t.Fatalf("dial second client: %v", err)
	}
	defer second.Close()

	appended, err := first.Append("tcp-partition", "producer", 1, "one")
	if err != nil {
		t.Fatalf("append: %v", err)
	}
	if !appended.OK || appended.Offset != 1 || appended.NextOffset != 2 {
		t.Fatalf("unexpected append response: %#v", appended)
	}
	pulled, err := first.Pull("tcp-partition", "consumer-a", 2, 5000)
	if err != nil {
		t.Fatalf("pull: %v", err)
	}
	if !pulled.OK || len(pulled.Messages) != 1 || pulled.Window.Holder != "consumer-a" {
		t.Fatalf("unexpected pull response: %#v", pulled)
	}
	fenced, err := second.Pull("tcp-partition", "consumer-b", 1, 5000)
	if err != nil {
		t.Fatalf("second pull: %v", err)
	}
	if fenced.OK || fenced.Code != protocol.CodeFence || fenced.Holder != "consumer-a" {
		t.Fatalf("expected E_FENCE, got %#v", fenced)
	}
	committed, err := first.Commit("tcp-partition", "consumer-a", 1)
	if err != nil {
		t.Fatalf("commit: %v", err)
	}
	if !committed.OK || committed.CommittedOffset != 1 || committed.Window != nil {
		t.Fatalf("unexpected commit response: %#v", committed)
	}
	status, err := first.Status("tcp-partition")
	if err != nil {
		t.Fatalf("status: %v", err)
	}
	if !status.OK || status.Status == nil || len(status.Status.Partitions) != 1 || status.Status.Partitions[0].CommittedOffset != 1 {
		t.Fatalf("unexpected status response: %#v", status)
	}
}
