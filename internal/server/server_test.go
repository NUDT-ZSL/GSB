package server

import (
	"bufio"
	"encoding/json"
	"net"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"

	"locklease/internal/model"
	"locklease/internal/proto"
)

func startServer(t *testing.T) (*Server, string) {
	t.Helper()
	dir := t.TempDir()
	gf := model.GraphFile{Version: 1, Nodes: []model.Node{
		{Key: "A"}, {Key: "B", DependsOn: []string{"A"}},
	}}
	gPath := filepath.Join(dir, "graph.json")
	data, _ := json.Marshal(gf)
	_ = os.WriteFile(gPath, data, 0o644)
	srv, err := New("127.0.0.1:0", filepath.Join(dir, "data"), gPath, filepath.Join(dir, "logs"))
	if err != nil {
		t.Fatal(err)
	}
	if err := srv.ListenAndServe(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(srv.Stop)
	return srv, dir
}

// helper because t.TempDir is directly available; thin wrapper kept for naming.
func call(t *testing.T, addr string, req proto.Request) proto.Response {
	t.Helper()
	conn, err := net.Dial("tcp", addr)
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	enc := json.NewEncoder(conn)
	if err := enc.Encode(req); err != nil {
		t.Fatal(err)
	}
	line, err := bufio.NewReader(conn).ReadBytes('\n')
	if err != nil {
		t.Fatal(err)
	}
	var resp proto.Response
	if err := json.Unmarshal(line, &resp); err != nil {
		t.Fatal(err)
	}
	return resp
}

func TestProtocolAcquireDuplicateAndPropagation(t *testing.T) {
	srv, _ := startServer(t)
	addr := srv.Addr()

	r1 := call(t, addr, proto.Request{Op: "acquire", Key: "A", Holder: "h1", TTLMs: 10000, NowMs: 1000})
	if !r1.OK || r1.LeaseVersion != 1 {
		t.Fatalf("acquire failed: %+v", r1)
	}
	r2 := call(t, addr, proto.Request{Op: "acquire", Key: "A", Holder: "h2", TTLMs: 10000, NowMs: 1000})
	if r2.OK || r2.Code != "E_LOCKED" || r2.Detail["holder"] != "h1" {
		t.Fatalf("duplicate acquire wrong: %+v", r2)
	}
	r3 := call(t, addr, proto.Request{Op: "acquire", Key: "B", Holder: "hB", TTLMs: 10000, NowMs: 1001})
	if !r3.OK {
		t.Fatalf("acquire B: %+v", r3)
	}
	r4 := call(t, addr, proto.Request{Op: "release", Key: "A", Holder: "h1", Version: 1, NowMs: 2000})
	if !r4.OK || len(r4.Traces.([]any)) != 1 {
		t.Fatalf("release propagation wrong: %+v", r4)
	}
	r5 := call(t, addr, proto.Request{Op: "query", Key: "B", NowMs: 2000})
	if !r5.OK || r5.Lock != nil {
		t.Fatalf("B should be invalidated: %+v", r5)
	}
}

func TestConcurrentAcquiresSingleHolder(t *testing.T) {
	srv, _ := startServer(t)
	addr := srv.Addr()
	var wg sync.WaitGroup
	results := make([]bool, 20)
	for i := 0; i < 20; i++ {
		wg.Add(1)
		go func(idx int) {
			defer wg.Done()
			holder := "c" + string(rune('a'+idx%26)) + string(rune('0'+idx/26))
			r := call(t, addr, proto.Request{
				Op: "acquire", Key: "A", Holder: holder,
				TTLMs: 10000, NowMs: 5000,
			})
			results[idx] = r.OK
		}(i)
	}
	wg.Wait()
	wins := 0
	for _, ok := range results {
		if ok {
			wins++
		}
	}
	if wins != 1 {
		t.Fatalf("expected exactly 1 winner, got %d", wins)
	}
}

func TestPortConflictReported(t *testing.T) {
	srv, dir := startServer(t)
	_, err := New(srv.Addr(), filepath.Join(dir, "data2"),
		filepath.Join(dir, "graph.json"), filepath.Join(dir, "logs2"))
	if err != nil {
		// construction alone does not bind; bind happens in ListenAndServe
	}
	srv2, err := New(srv.Addr(), filepath.Join(dir, "data2"),
		filepath.Join(dir, "graph.json"), filepath.Join(dir, "logs2"))
	if err != nil {
		t.Fatal(err)
	}
	err = srv2.ListenAndServe()
	if err == nil || !strings.Contains(err.Error(), "E_PORT") {
		t.Fatalf("want E_PORT, got %v", err)
	}
}

func TestPassiveTableDoesNotReap(t *testing.T) {
	srv, _ := startServer(t)
	addr := srv.Addr()
	// Acquire with an already-expired lease; passive_table must still show it.
	r := call(t, addr, proto.Request{Op: "acquire", Key: "A", Holder: "h1", TTLMs: 100, NowMs: 1000})
	if !r.OK {
		t.Fatalf("acquire: %+v", r)
	}
	passive := call(t, addr, proto.Request{Op: "passive_table", NowMs: 5000})
	if !passive.OK || len(passive.Locks.([]any)) != 1 {
		t.Fatalf("passive_table must retain expired lock: %+v", passive)
	}
	active := call(t, addr, proto.Request{Op: "table", NowMs: 5000})
	if !active.OK || len(active.Locks.([]any)) != 0 {
		t.Fatalf("table should lazily reap expired lock: %+v", active)
	}
}
