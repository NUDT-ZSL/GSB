package server

import (
	"context"
	"encoding/binary"
	"fmt"
	"io"
	"log"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"localdns/internal/cache"
	"localdns/internal/client"
	"localdns/internal/clock"
	"localdns/internal/dnswire"
	"localdns/internal/engine"
	"localdns/internal/mockupstream"
	"localdns/internal/model"
	"localdns/internal/upstream"
	"localdns/internal/zone"
)

type rig struct {
	engine   *engine.Engine
	cache    *cache.Cache
	server   *Server
	client   *client.Client
	clk      *clock.Manual
	upstream *mockupstream.Server
}

func sampleLoadedZone(t *testing.T) *zone.Zone {
	t.Helper()
	z, err := zone.LoadJSON(filepath.Join("../../samples", "zone.json"), zone.Options{})
	if err != nil {
		t.Fatal(err)
	}
	_, records, err := zone.LoadWireMessage(filepath.Join("../../samples", "answer.wire"))
	if err != nil {
		t.Fatal(err)
	}
	if err := z.AddWireRecords(records, zone.OriginWireSample); err != nil {
		t.Fatal(err)
	}
	return z
}

func newRig(t *testing.T, z *zone.Zone) *rig {
	t.Helper()
	up, err := mockupstream.Listen("127.0.0.1:0", mockupstream.ModeScripted, nil)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(up.Close)
	fwd, err := upstream.NewClient(up.Addr(), time.Second, 60, 16)
	if err != nil {
		t.Fatal(err)
	}
	clk := clock.NewManual(time.Date(2026, 11, 17, 0, 0, 0, 0, time.UTC))
	memCache := cache.New(64, clk)
	eng := engine.New(z, memCache, fwd, clk, engine.Options{MaxCNAMEHops: 16})
	srv := New(eng, Options{UDPAddr: "127.0.0.1:0", TCPAddr: "127.0.0.1:0", Logger: silentLogger()})
	if err := srv.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(srv.Close)
	cl, err := client.New(srv.UDPAddr(), srv.TCPAddr(), 2*time.Second)
	if err != nil {
		t.Fatal(err)
	}
	return &rig{engine: eng, cache: memCache, server: srv, client: cl, clk: clk, upstream: up}
}

func silentLogger() *log.Logger {
	return log.New(io.Discard, "", 0)
}

func TestUDPAndTCPLocalAuthorityMetadataAndSampleReplay(t *testing.T) {
	r := newRig(t, sampleLoadedZone(t))
	ctx := context.Background()
	udpRes, err := r.client.QueryUDP(ctx, "SAME.DEMO.TEST.", model.TypeA, model.ClassIN)
	if err != nil {
		t.Fatal(err)
	}
	if udpRes.Message.Header.RCode() != model.RCodeNoError || len(udpRes.Message.Answers) != 1 {
		t.Fatalf("unexpected UDP response: %+v", udpRes.Message)
	}
	if udpRes.Message.Answers[0].Data != "192.0.2.77" || udpRes.Message.Answers[0].TTL != 45 {
		t.Fatalf("UDP answer mismatch: %+v", udpRes.Message.Answers[0])
	}
	if udpRes.Meta.Source != string(model.SourceAuthority) || udpRes.Meta.CacheHit || udpRes.Meta.Version == 0 {
		t.Fatalf("metadata mismatch: %+v", udpRes.Meta)
	}
	if !contains(udpRes.Meta.Origins, zone.OriginJSON+"+"+zone.OriginWireSample) {
		t.Fatalf("dual origins missing: %v", udpRes.Meta.Origins)
	}

	tcpRes, err := r.client.QueryTCP(ctx, "same.demo.test", model.TypeA, model.ClassIN)
	if err != nil {
		t.Fatal(err)
	}
	if !tcpRes.Meta.CacheHit || tcpRes.Meta.Version != udpRes.Meta.Version {
		t.Fatalf("TCP did not observe the complete UDP-filled cache entry: %+v", tcpRes.Meta)
	}
	r.clk.Advance(2 * time.Second)
	tcpRes, err = r.client.QueryTCP(ctx, "same.demo.test", model.TypeA, model.ClassIN)
	if err != nil {
		t.Fatal(err)
	}
	if tcpRes.Message.Answers[0].TTL != 43 || !tcpRes.Meta.CacheHit {
		t.Fatalf("TTL did not decrement over TCP cache hit: %+v", tcpRes.Message.Answers[0])
	}

	aliasRes, err := r.client.QueryTCP(ctx, "alias.demo.test", model.TypeA, model.ClassIN)
	if err != nil {
		t.Fatal(err)
	}
	if len(aliasRes.Message.Answers) != 2 || len(aliasRes.Meta.CNAMEChain) != 1 {
		t.Fatalf("CNAME chain missing: answers=%+v meta=%+v", aliasRes.Message.Answers, aliasRes.Meta)
	}

	rawQuery, err := os.ReadFile(filepath.Join("../../samples", "query.wire"))
	if err != nil {
		t.Fatal(err)
	}
	replay, err := r.client.ExchangeUDP(ctx, rawQuery)
	if err != nil {
		t.Fatal(err)
	}
	if len(replay.Message.Answers) != 1 || replay.Message.Answers[0].Data != "192.0.2.77" {
		t.Fatalf("wire sample replay mismatch: %+v", replay.Message.Answers)
	}
}

func TestWireErrorsCarryCodeAndOffsetWithoutPollutingTCP(t *testing.T) {
	r := newRig(t, sampleLoadedZone(t))
	payload := make([]byte, 12)
	binary.BigEndian.PutUint16(payload[0:2], 0x4242)
	binary.BigEndian.PutUint16(payload[4:6], 1)
	payload = append(payload, 0xc0, 0x0c, 0, 1, 0, 1) // self pointer at offset 12

	res, err := r.client.ExchangeUDP(context.Background(), payload)
	if err != nil {
		t.Fatal(err)
	}
	if res.Meta.Error == nil || res.Meta.Error.Code != "E_WIRE" || !res.Meta.Error.HasOffset || res.Meta.Error.Offset != 12 {
		t.Fatalf("E_WIRE metadata mismatch: %+v", res.Meta.Error)
	}
	if res.Message.Header.RCode() != model.RCodeFormErr {
		t.Fatalf("want FORMERR, got %s", model.RCodeName(res.Message.Header.RCode()))
	}
	tcpRes, err := r.client.QueryTCP(context.Background(), "same.demo.test", model.TypeA, model.ClassIN)
	if err != nil {
		t.Fatal(err)
	}
	if tcpRes.Meta.Error != nil || len(tcpRes.Message.Answers) != 1 {
		t.Fatalf("TCP was polluted by the UDP wire error: %+v", tcpRes)
	}
}

func TestUDPTruncationAndTCPFullResponse(t *testing.T) {
	var body strings.Builder
	body.WriteString(`{"apex":"demo.test","records":[{"name":"demo.test","type":"SOA","ttl":300,"rdata":"ns1.demo.test hostmaster.demo.test 1 3600 600 86400 30"}`)
	for i := 1; i <= 40; i++ {
		fmt.Fprintf(&body, `,{"name":"bulk.demo.test","type":"A","ttl":60,"rdata":"192.0.2.%d"}`, i)
	}
	body.WriteString(`]}`)
	path := filepath.Join(t.TempDir(), "bulk.json")
	if err := os.WriteFile(path, []byte(body.String()), 0o600); err != nil {
		t.Fatal(err)
	}
	z, err := zone.LoadJSON(path, zone.Options{})
	if err != nil {
		t.Fatal(err)
	}
	r := newRig(t, z)
	udpRes, err := r.client.QueryUDP(context.Background(), "bulk.demo.test", model.TypeA, model.ClassIN)
	if err != nil {
		t.Fatal(err)
	}
	if udpRes.Message.Header.Flags&dnswire.FlagTC == 0 {
		t.Fatal("UDP TC bit not set")
	}
	if len(udpRes.Message.Answers) != 0 {
		t.Fatalf("truncated UDP response still contains %d answers", len(udpRes.Message.Answers))
	}
	tcpRes, err := r.client.QueryTCP(context.Background(), "bulk.demo.test", model.TypeA, model.ClassIN)
	if err != nil {
		t.Fatal(err)
	}
	if tcpRes.Message.Header.Flags&dnswire.FlagTC != 0 {
		t.Fatal("TCP response was incorrectly truncated")
	}
	if len(tcpRes.Message.Answers) != 40 {
		t.Fatalf("TCP answer count=%d", len(tcpRes.Message.Answers))
	}
	if tcpRes.Meta.Version == 0 || !tcpRes.Meta.CacheHit {
		t.Fatalf("UDP and TCP should share the complete cached entry: %+v", tcpRes.Meta)
	}
}

func TestForwardChainErrorIsNotCached(t *testing.T) {
	r := newRig(t, sampleLoadedZone(t))
	bad, err := r.client.QueryUDP(context.Background(), "bad.external.test", model.TypeA, model.ClassIN)
	if err != nil {
		t.Fatal(err)
	}
	if bad.Meta.Error == nil || bad.Meta.Error.Code != "E_CHAIN" {
		t.Fatalf("want E_CHAIN metadata, got %+v", bad.Meta)
	}
	if bad.Message.Header.RCode() != model.RCodeServFail {
		t.Fatalf("want SERVFAIL, got %s", model.RCodeName(bad.Message.Header.RCode()))
	}
	if r.cache.Len() != 0 {
		t.Fatalf("suspicious response populated %d cache entries", r.cache.Len())
	}
	good, err := r.client.QueryUDP(context.Background(), "api.external.test", model.TypeA, model.ClassIN)
	if err != nil {
		t.Fatal(err)
	}
	if good.Meta.Error != nil || len(good.Message.Answers) != 2 || good.Meta.Source != string(model.SourceUpstream) {
		t.Fatalf("valid forwarded query failed: %+v", good)
	}
}

func TestMixedConcurrentUDPAndTCPShareOneVersion(t *testing.T) {
	r := newRig(t, sampleLoadedZone(t))
	const workers = 32
	versions := make([]uint64, workers)
	errs := make([]error, workers)
	cacheHits := make([]bool, workers)
	ttls := make([]uint32, workers)
	start := make(chan struct{})
	var wg sync.WaitGroup
	for i := 0; i < workers; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			<-start
			ctx := context.Background()
			var res *client.Response
			var err error
			if i%2 == 0 {
				res, err = r.client.QueryUDP(ctx, "concurrent.external.test", model.TypeA, model.ClassIN)
			} else {
				res, err = r.client.QueryTCP(ctx, "concurrent.external.test", model.TypeA, model.ClassIN)
			}
			errs[i] = err
			if err == nil {
				versions[i] = res.Meta.Version
				cacheHits[i] = res.Meta.CacheHit
				if len(res.Message.Answers) > 0 {
					ttls[i] = res.Message.Answers[0].TTL
				}
			}
		}(i)
	}
	close(start)
	wg.Wait()
	version := uint64(0)
	hitCount := 0
	for i := 0; i < workers; i++ {
		if errs[i] != nil {
			t.Fatalf("worker %d: %v", i, errs[i])
		}
		if versions[i] == 0 {
			t.Fatalf("worker %d has no cache version", i)
		}
		if version == 0 {
			version = versions[i]
		} else if versions[i] != version {
			t.Fatalf("mixed versions: %d vs %d", version, versions[i])
		}
		if ttls[i] != 90 {
			t.Fatalf("worker %d TTL=%d", i, ttls[i])
		}
		if cacheHits[i] {
			hitCount++
		}
	}
	stats := r.cache.Stats()
	if stats.Fills != 1 {
		t.Fatalf("cache fills=%d want 1", stats.Fills)
	}
	if hitCount == workers {
		t.Fatal("all requests were cache hits; concurrency was not exercised")
	}
	if r.engine.Stats().Flights != 1 {
		t.Fatalf("engine flights=%d", r.engine.Stats().Flights)
	}
}

func contains(values []string, want string) bool {
	for _, value := range values {
		if value == want {
			return true
		}
	}
	return false
}
