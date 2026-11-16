package main

import (
	"bufio"
	"context"
	"encoding/binary"
	"errors"
	"flag"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"

	"localdns/internal/biz"
	"localdns/internal/cache"
	"localdns/internal/client"
	"localdns/internal/clock"
	"localdns/internal/engine"
	"localdns/internal/model"
	"localdns/internal/server"
	"localdns/internal/upstream"
	"localdns/internal/zone"
)

type demo struct {
	zone     *zone.Zone
	cache    *cache.Cache
	engine   *engine.Engine
	server   *server.Server
	client   *client.Client
	clock    *clock.Manual
	upstream *upstreamProcess
}

type upstreamProcess struct {
	cmd    *exec.Cmd
	addr   string
	stopMu sync.Mutex
	closed bool
}

func main() {
	zonePath := flag.String("zone", "samples/zone.json", "JSON zone sample")
	wirePath := flag.String("wire", "samples/answer.wire", "wire answer sample")
	wireQueryPath := flag.String("wire-query", "samples/query.wire", "wire query sample")
	upstreamExe := flag.String("upstream-exe", "bin/dnsupstream", "mock upstream executable")
	flag.Parse()
	if err := run(*zonePath, *wirePath, *wireQueryPath, *upstreamExe); err != nil {
		fmt.Fprintln(os.Stderr, "demo failed:", err)
		os.Exit(1)
	}
}

func run(zonePath, wirePath, wireQueryPath, upstreamExe string) error {
	z, err := zone.LoadJSON(zonePath, zone.Options{MaxTTL: 604800, NegativeTTLCap: 60, MaxCNAMEHops: 16})
	if err != nil {
		return err
	}
	_, wireRecords, err := zone.LoadWireMessage(wirePath)
	if err != nil {
		return err
	}
	if err := z.AddWireRecords(wireRecords, zone.OriginWireSample); err != nil {
		return err
	}
	up, err := startUpstream(upstreamExe)
	if err != nil {
		return err
	}
	defer up.stop()
	fwd, err := upstream.NewClient(up.addr, 2*time.Second, 60, 16)
	if err != nil {
		return err
	}
	clk := clock.NewManual(time.Date(2026, 11, 17, 0, 0, 0, 0, time.UTC))
	memCache := cache.New(64, clk)
	eng := engine.New(z, memCache, fwd, clk, engine.Options{MaxCNAMEHops: 16})
	srv := server.New(eng, server.Options{UDPAddr: "127.0.0.1:0", TCPAddr: "127.0.0.1:0"})
	if err := srv.Start(); err != nil {
		return err
	}
	defer srv.Close()
	cl, err := client.New(srv.UDPAddr(), srv.TCPAddr(), 3*time.Second)
	if err != nil {
		return err
	}
	d := &demo{zone: z, cache: memCache, engine: eng, server: srv, client: cl, clock: clk, upstream: up}

	fmt.Println("=== localdns deterministic demo ===")
	fmt.Printf("zone=%s records=%d seq=%d origins=%s\n", z.Apex(), z.RecordCount(), z.Seq(), strings.Join(z.Origins(), "+"))
	fmt.Printf("udp=%s tcp=%s\n", srv.UDPAddr(), srv.TCPAddr())
	fmt.Printf("upstream_child=%s pid=%d\n", up.addr, up.cmd.Process.Pid)

	fmt.Println("\n[1/4] cold-start queries")
	rawQuery, err := os.ReadFile(wireQueryPath)
	if err != nil {
		return err
	}
	wireRes, err := cl.ExchangeUDP(context.Background(), rawQuery)
	if err != nil {
		return err
	}
	if wireRes.Meta.Error != nil || len(wireRes.Message.Answers) != 1 {
		return fmt.Errorf("wire sample replay failed: %+v", wireRes.Meta.Error)
	}
	printResult("wire-replay", "udp", "same.demo.test A", wireRes)
	if !containsOrigin(wireRes.Meta.Origins, "json+wire-sample") {
		return fmt.Errorf("dual-channel normalization did not merge origins")
	}

	wwwRes, err := cl.QueryUDP(context.Background(), "www.demo.test", model.TypeA, model.ClassIN)
	if err != nil {
		return err
	}
	if wwwRes.Meta.CacheHit || len(wwwRes.Message.Answers) != 1 || wwwRes.Message.Answers[0].TTL != 10 {
		return fmt.Errorf("cold local query was not a TTL=10 miss")
	}
	printResult("cold-local", "udp", "www.demo.test A", wwwRes)

	apiRes, err := cl.QueryTCP(context.Background(), "api.external.test", model.TypeA, model.ClassIN)
	if err != nil {
		return err
	}
	if apiRes.Meta.Error != nil || len(apiRes.Message.Answers) != 2 || len(apiRes.Meta.CNAMEChain) != 1 {
		return fmt.Errorf("forwarded CNAME chain failed")
	}
	printResult("cold-forward", "tcp", "api.external.test A", apiRes)

	fmt.Println("\n[2/4] TTL decrement then re-query")
	d.clock.Advance(3 * time.Second)
	wwwHit, err := cl.QueryUDP(context.Background(), "www.demo.test", model.TypeA, model.ClassIN)
	if err != nil {
		return err
	}
	if !wwwHit.Meta.CacheHit || len(wwwHit.Message.Answers) != 1 || wwwHit.Message.Answers[0].TTL != 7 {
		return fmt.Errorf("expected cache hit with TTL=7, got hit=%t", wwwHit.Meta.CacheHit)
	}
	printResult("ttl-requery", "udp", "www.demo.test A", wwwHit)

	fmt.Println("\n[3/4] upstream conflict arbitration")
	conflictRes, err := cl.QueryTCP(context.Background(), "portal.external.test", model.TypeA, model.ClassIN)
	if err != nil {
		return err
	}
	if conflictRes.Meta.Conflict == nil || conflictRes.Meta.Conflict.Winner != "authority" {
		return fmt.Errorf("conflict was not arbitrated")
	}
	if len(conflictRes.Message.Answers) != 2 || conflictRes.Message.Answers[1].Data != "192.0.2.44" {
		return fmt.Errorf("local authority did not win the conflict")
	}
	printResult("conflict", "tcp", "portal.external.test A", conflictRes)
	conflictHit, err := cl.QueryTCP(context.Background(), "portal.external.test", model.TypeA, model.ClassIN)
	if err != nil {
		return err
	}
	if !conflictHit.Meta.CacheHit || conflictHit.Meta.Version != conflictRes.Meta.Version || conflictHit.Meta.Conflict == nil {
		return fmt.Errorf("conflict cache hit lost provenance: hit=%t version=%d/%d conflict=%v source=%s rcode=%s answers=%d error=%+v",
			conflictHit.Meta.CacheHit, conflictHit.Meta.Version, conflictRes.Meta.Version, conflictHit.Meta.Conflict, conflictHit.Meta.Source,
			model.RCodeName(conflictHit.Message.Header.RCode()), len(conflictHit.Message.Answers), conflictHit.Meta.Error,
		)
	}
	printResult("conflict-cache", "tcp", "portal.external.test A", conflictHit)

	fmt.Println("\n[4/4] concurrent UDP/TCP queries")
	fillsBefore := memCache.Stats().Fills
	flightsBefore := eng.Stats().Flights
	type outcome struct {
		transport string
		version   uint64
		ttl       uint32
		source    string
		err       error
	}
	const workers = 24
	outcomes := make([]outcome, workers)
	start := make(chan struct{})
	var wg sync.WaitGroup
	for i := 0; i < workers; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			<-start
			transport := "udp"
			var res *client.Response
			var queryErr error
			ctx := context.Background()
			if i%2 == 0 {
				res, queryErr = d.client.QueryUDP(ctx, "concurrent.external.test", model.TypeA, model.ClassIN)
			} else {
				transport = "tcp"
				res, queryErr = d.client.QueryTCP(ctx, "concurrent.external.test", model.TypeA, model.ClassIN)
			}
			outcomes[i] = outcome{transport: transport, err: queryErr}
			if queryErr == nil {
				outcomes[i].version = res.Meta.Version
				outcomes[i].source = res.Meta.Source
				if len(res.Message.Answers) > 0 {
					outcomes[i].ttl = res.Message.Answers[0].TTL
				}
			}
		}(i)
	}
	close(start)
	wg.Wait()
	versionSet := make(map[uint64]bool)
	transportCount := map[string]int{}
	for _, out := range outcomes {
		if out.err != nil {
			return fmt.Errorf("concurrent query failed: %w", out.err)
		}
		if out.version == 0 || out.ttl != 90 || out.source != "upstream" {
			return fmt.Errorf("concurrent outcome incoherent: %+v", out)
		}
		versionSet[out.version] = true
		transportCount[out.transport]++
	}
	if len(versionSet) != 1 {
		return fmt.Errorf("concurrent queries observed %d versions", len(versionSet))
	}
	fillsDelta := memCache.Stats().Fills - fillsBefore
	flightsDelta := eng.Stats().Flights - flightsBefore
	if fillsDelta != 1 || flightsDelta != 1 {
		return fmt.Errorf("concurrent fills=%d flights=%d", fillsDelta, flightsDelta)
	}
	versions := make([]uint64, 0, len(versionSet))
	for version := range versionSet {
		versions = append(versions, version)
	}
	sort.Slice(versions, func(i, j int) bool { return versions[i] < versions[j] })
	winner, ok := eng.Winner(cache.Key{Name: "concurrent.external.test", Type: model.TypeA, Class: model.ClassIN})
	if !ok || winner.Version != versions[0] || winner.Source != model.SourceUpstream {
		return fmt.Errorf("winning version not recorded")
	}
	fmt.Printf("requests=%d transport_mix=udp:%d tcp:%d\n", workers, transportCount["udp"], transportCount["tcp"])
	fmt.Printf("unique_versions=%d version=%d winner_source=%s winner_seq=%d\n", len(versions), winner.Version, winner.Source, winner.SourceSeq)
	fmt.Printf("cache_fills_delta=%d flights_delta=%d all_ttl=90 coherent=YES\n", fillsDelta, flightsDelta)

	fmt.Println("\n=== business-code guards ===")
	wireCode, wireOffset, err := d.guardWire()
	if err != nil {
		return err
	}
	fmt.Printf("E_WIRE=%s offset=%d\n", wireCode, wireOffset)
	chainCode, err := d.guardChain()
	if err != nil {
		return err
	}
	fmt.Printf("E_CHAIN=%s\n", chainCode)
	cnameCode, err := d.guardCNAME()
	if err != nil {
		return err
	}
	fmt.Printf("E_CNAME=%s\n", cnameCode)
	zoneCode, err := d.guardZone()
	if err != nil {
		return err
	}
	fmt.Printf("E_ZONE=%s\n", zoneCode)
	up.stop()
	upstreamCode, err := d.guardUpstream()
	if err != nil {
		return err
	}
	fmt.Printf("E_UPSTREAM=%s\n", upstreamCode)

	fmt.Println("\n=== stable summary ===")
	fmt.Println("cold_start=PASS local_json=PASS wire_normalization=json+wire-sample forward_chain=PASS")
	fmt.Println("ttl_cache=PASS before=10 after=7 cache_hit=true")
	fmt.Println("conflict=PASS winner=authority local=192.0.2.44 upstream=198.51.100.99")
	fmt.Printf("concurrency=PASS requests=%d unique_versions=1 fills=1 flights=1\n", workers)
	fmt.Println("business_codes=E_WIRE,E_ZONE,E_CNAME,E_UPSTREAM,E_CHAIN")
	fmt.Println("verdict=PASS")
	return nil
}

func printResult(label, transport, query string, res *client.Response) {
	meta := res.Meta
	fmt.Printf("case=%s query=%s transport=%s rcode=%s\n", label, query, transport, model.RCodeName(res.Message.Header.RCode()))
	fmt.Printf("source=%s seq=%d cache_hit=%t version=%d matches=%s origins=%s\n",
		meta.Source, meta.SourceSeq, meta.CacheHit, meta.Version, strings.Join(meta.Matches, ","), strings.Join(meta.Origins, ","))
	if len(meta.CNAMEChain) > 0 {
		fmt.Printf("cname_chain=%s\n", strings.Join(meta.CNAMEChain, " | "))
	}
	if meta.Conflict != nil {
		fmt.Printf("conflict=winner:%s local:%s upstream:%s local_seq:%d upstream_seq:%d\n",
			meta.Conflict.Winner, strings.Join(meta.Conflict.Local.Values, ","),
			strings.Join(meta.Conflict.Upstream.Values, ","), meta.Conflict.Local.SourceSeq, meta.Conflict.Upstream.SourceSeq)
	}
	if meta.Error != nil {
		fmt.Printf("error=%s offset=%d\n", meta.Error.Code, meta.Error.Offset)
	}
	for _, rr := range res.Message.Answers {
		fmt.Printf("answer=%s %s %d %s\n", rr.Name, rr.Type.String(), rr.TTL, rr.Data)
	}
}

func startUpstream(exe string) (*upstreamProcess, error) {
	if exe == "" {
		return nil, fmt.Errorf("empty upstream executable path")
	}
	cmd := exec.Command(exe, "-listen", "127.0.0.1:0", "-mode", "scripted")
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return nil, err
	}
	cmd.Stderr = os.Stderr
	if err := cmd.Start(); err != nil {
		return nil, fmt.Errorf("start upstream child %s: %w", exe, err)
	}
	lineCh := make(chan string, 1)
	errCh := make(chan error, 1)
	go func() {
		scanner := bufio.NewScanner(stdout)
		if scanner.Scan() {
			lineCh <- scanner.Text()
			return
		}
		errCh <- fmt.Errorf("upstream child did not report READY")
	}()
	select {
	case line := <-lineCh:
		fields := strings.Fields(line)
		if len(fields) != 2 || fields[0] != "READY" {
			_ = cmd.Process.Kill()
			_ = cmd.Wait()
			return nil, fmt.Errorf("unexpected upstream readiness line %q", line)
		}
		return &upstreamProcess{cmd: cmd, addr: fields[1]}, nil
	case scanErr := <-errCh:
		_ = cmd.Process.Kill()
		_ = cmd.Wait()
		return nil, scanErr
	case <-time.After(5 * time.Second):
		_ = cmd.Process.Kill()
		_ = cmd.Wait()
		return nil, fmt.Errorf("timeout waiting for upstream child")
	}
}

func (u *upstreamProcess) stop() {
	if u == nil {
		return
	}
	u.stopMu.Lock()
	defer u.stopMu.Unlock()
	if u.closed {
		return
	}
	u.closed = true
	_ = u.cmd.Process.Kill()
	_ = u.cmd.Wait()
}

func (d *demo) guardWire() (string, int, error) {
	payload := make([]byte, 12)
	binary.BigEndian.PutUint16(payload[0:2], 0x9999)
	binary.BigEndian.PutUint16(payload[4:6], 1)
	payload = append(payload, 0xc0, 0x0c, 0, 1, 0, 1)
	res, err := d.client.ExchangeUDP(context.Background(), payload)
	if err != nil {
		return "", 0, err
	}
	if res.Meta.Error == nil || res.Meta.Error.Code != "E_WIRE" || res.Meta.Error.Offset != 12 {
		return "", 0, fmt.Errorf("wire guard returned %+v", res.Meta.Error)
	}
	return res.Meta.Error.Code, res.Meta.Error.Offset, nil
}

func (d *demo) guardChain() (string, error) {
	res, err := d.client.QueryUDP(context.Background(), "bad.external.test", model.TypeA, model.ClassIN)
	if err != nil {
		return "", err
	}
	if res.Meta.Error == nil || res.Meta.Error.Code != "E_CHAIN" {
		return "", fmt.Errorf("chain guard returned %+v", res.Meta.Error)
	}
	return res.Meta.Error.Code, nil
}

func (d *demo) guardCNAME() (string, error) {
	res, err := d.client.QueryUDP(context.Background(), "loop-a.demo.test", model.TypeA, model.ClassIN)
	if err != nil {
		return "", err
	}
	if res.Meta.Error == nil || res.Meta.Error.Code != "E_CNAME" {
		return "", fmt.Errorf("CNAME guard returned %+v", res.Meta.Error)
	}
	return res.Meta.Error.Code, nil
}

func (d *demo) guardZone() (string, error) {
	dir, err := os.MkdirTemp("", "localdns-invalid-zone-")
	if err != nil {
		return "", err
	}
	defer os.RemoveAll(dir)
	path := filepath.Join(dir, "invalid.json")
	body := `{"apex":"demo.test","records":[{"name":"x.demo.test","type":"A","ttl":1.5,"rdata":"192.0.2.1"}]}`
	if err := os.WriteFile(path, []byte(body), 0o600); err != nil {
		return "", err
	}
	_, loadErr := zone.LoadJSON(path, zone.Options{})
	if bizCode := codeOf(loadErr); bizCode != "E_ZONE" {
		return "", fmt.Errorf("zone guard returned %v", loadErr)
	}
	return "E_ZONE", nil
}

func (d *demo) guardUpstream() (string, error) {
	res, err := d.client.QueryUDP(context.Background(), "unreachable.external.test", model.TypeA, model.ClassIN)
	if err != nil {
		return "", err
	}
	if res.Meta.Error == nil || res.Meta.Error.Code != "E_UPSTREAM" {
		return "", fmt.Errorf("upstream guard returned %+v", res.Meta.Error)
	}
	return res.Meta.Error.Code, nil
}

func codeOf(err error) string {
	if err == nil {
		return ""
	}
	var appErr *biz.Error
	if errors.As(err, &appErr) {
		return string(appErr.Code)
	}
	return ""
}

func containsOrigin(origins []string, want string) bool {
	for _, origin := range origins {
		if origin == want {
			return true
		}
	}
	return false
}
