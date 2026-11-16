package main

import (
	"bufio"
	"context"
	"flag"
	"fmt"
	"log"
	"os"
	"strings"
	"time"

	"localdns/internal/cache"
	"localdns/internal/client"
	"localdns/internal/clock"
	"localdns/internal/engine"
	"localdns/internal/model"
	"localdns/internal/server"
	"localdns/internal/upstream"
	"localdns/internal/zone"
)

func main() {
	zonePath := flag.String("zone", "samples/zone.json", "JSON authoritative zone")
	wirePath := flag.String("wire", "samples/answer.wire", "RFC 1035 answer sample to normalize into the zone")
	wireQueryPath := flag.String("wire-query", "samples/query.wire", "RFC 1035 query sample for replay")
	udpAddr := flag.String("udp", "127.0.0.1:15353", "loopback UDP listen address")
	tcpAddr := flag.String("tcp", "127.0.0.1:15354", "loopback TCP listen address")
	upstreamAddr := flag.String("upstream", "127.0.0.1:15355", "loopback TCP upstream resolver")
	cacheSize := flag.Int("cache-size", 256, "maximum cache entries")
	maxTTL := flag.Uint("max-ttl", uint(zone.DefaultMaxTTL), "maximum accepted record TTL")
	negativeCap := flag.Uint("negative-ttl-cap", uint(zone.DefaultNegativeTTLCap), "negative-cache TTL cap")
	cnameHops := flag.Int("cname-hops", zone.DefaultMaxCNAMEHops, "maximum CNAME chain length")
	flag.Parse()

	z, err := zone.LoadJSON(*zonePath, zone.Options{
		MaxTTL: uint32(*maxTTL), NegativeTTLCap: uint32(*negativeCap), MaxCNAMEHops: *cnameHops,
	})
	if err != nil {
		fatal(err)
	}
	if *wirePath != "" {
		_, records, wireErr := zone.LoadWireMessage(*wirePath)
		if wireErr != nil {
			fatal(wireErr)
		}
		if addErr := z.AddWireRecords(records, zone.OriginWireSample); addErr != nil {
			fatal(addErr)
		}
	}
	fwd, err := upstream.NewClient(*upstreamAddr, 2*time.Second, uint32(*negativeCap), *cnameHops)
	if err != nil {
		fatal(err)
	}
	clk := clock.Real{}
	memCache := cache.New(*cacheSize, clk)
	eng := engine.New(z, memCache, fwd, clk, engine.Options{MaxCNAMEHops: *cnameHops})
	srv := server.New(eng, server.Options{
		UDPAddr: *udpAddr, TCPAddr: *tcpAddr, MaxUDP: 512,
		Logger: log.New(os.Stderr, "localdns: ", 0),
	})
	if err := srv.Start(); err != nil {
		fatal(err)
	}
	defer srv.Close()
	cl, err := client.New(srv.UDPAddr(), srv.TCPAddr(), 3*time.Second)
	if err != nil {
		fatal(err)
	}

	fmt.Println("localdns ready")
	fmt.Printf("udp=%s tcp=%s\n", srv.UDPAddr(), srv.TCPAddr())
	fmt.Printf("zone=%s records=%d seq=%d origins=%s\n", z.Apex(), z.RecordCount(), z.Seq(), strings.Join(z.Origins(), "+"))
	fmt.Printf("upstream=%s (loopback TCP only)\n", *upstreamAddr)
	fmt.Printf("samples zone=%s wire=%s query=%s\n", *zonePath, *wirePath, *wireQueryPath)
	fmt.Println("commands: udp <name> [type] | tcp <name> [type] | wire | stats | zone | help | quit")

	scanner := bufio.NewScanner(os.Stdin)
	for {
		fmt.Print("dns> ")
		if !scanner.Scan() {
			break
		}
		line := strings.TrimSpace(scanner.Text())
		if line == "" {
			continue
		}
		fields := strings.Fields(line)
		command := strings.ToLower(fields[0])
		switch command {
		case "quit", "exit":
			return
		case "help", "?":
			printHelp()
		case "stats":
			printStats(eng, memCache.Stats(), srv.Stats())
		case "zone":
			fmt.Printf("apex=%s records=%d seq=%d origins=%s\n", z.Apex(), z.RecordCount(), z.Seq(), strings.Join(z.Origins(), "+"))
		case "wire", "wiretcp":
			raw, readErr := os.ReadFile(*wireQueryPath)
			if readErr != nil {
				fmt.Println("read error:", readErr)
				continue
			}
			ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
			var res *client.Response
			if command == "wiretcp" {
				res, err = cl.ExchangeTCP(ctx, raw)
			} else {
				res, err = cl.ExchangeUDP(ctx, raw)
			}
			cancel()
			if err != nil {
				fmt.Println("query error:", err)
				continue
			}
			printResponse(command, *wireQueryPath, res)
		case "udp", "tcp":
			name, typ, parseErr := parseQuery(fields)
			if parseErr != nil {
				fmt.Println("usage:", parseErr)
				continue
			}
			ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
			var res *client.Response
			if command == "tcp" {
				res, err = cl.QueryTCP(ctx, name, typ, model.ClassIN)
			} else {
				res, err = cl.QueryUDP(ctx, name, typ, model.ClassIN)
			}
			cancel()
			if err != nil {
				fmt.Println("query error:", err)
				continue
			}
			printResponse(command, name+" "+typ.String(), res)
		default:
			name, typ, parseErr := parseQuery(fields)
			if parseErr != nil {
				fmt.Println("unknown command; try help")
				continue
			}
			ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
			res, queryErr := cl.QueryUDP(ctx, name, typ, model.ClassIN)
			cancel()
			if queryErr != nil {
				fmt.Println("query error:", queryErr)
				continue
			}
			printResponse("udp", name+" "+typ.String(), res)
		}
	}
	if err := scanner.Err(); err != nil {
		fmt.Fprintln(os.Stderr, "input error:", err)
	}
}

func parseQuery(fields []string) (string, model.Type, error) {
	if len(fields) < 2 || len(fields) > 3 {
		return "", 0, fmt.Errorf("query requires <name> [A|AAAA|CNAME|NS|SOA|TYPE<n>]")
	}
	name := fields[1]
	typ := model.TypeA
	if len(fields) == 3 {
		parsed, err := model.ParseType(fields[2])
		if err != nil {
			return "", 0, err
		}
		typ = parsed
	}
	return name, typ, nil
}

func printResponse(transport, query string, res *client.Response) {
	msg := res.Message
	fmt.Printf("query=%s transport=%s rcode=%s flags=0x%04x\n", query, transport, model.RCodeName(msg.Header.RCode()), msg.Header.Flags)
	if res.Meta.Error != nil {
		if res.Meta.Error.HasOffset {
			fmt.Printf("error=%s offset=%d detail=%s\n", res.Meta.Error.Code, res.Meta.Error.Offset, res.Meta.Error.Detail)
		} else {
			fmt.Printf("error=%s detail=%s\n", res.Meta.Error.Code, res.Meta.Error.Detail)
		}
	}
	if res.Meta.Source != "" {
		fmt.Printf("source=%s seq=%d cache_hit=%t coalesced=%t version=%d\n", res.Meta.Source, res.Meta.SourceSeq, res.Meta.CacheHit, res.Meta.Coalesced, res.Meta.Version)
		fmt.Printf("matches=%s origins=%s\n", joinOrNone(res.Meta.Matches), joinOrNone(res.Meta.Origins))
	}
	if len(res.Meta.CNAMEChain) > 0 {
		fmt.Printf("cname_chain=%s\n", strings.Join(res.Meta.CNAMEChain, " | "))
	} else {
		fmt.Println("cname_chain=(none)")
	}
	if res.Meta.Conflict != nil {
		c := res.Meta.Conflict
		fmt.Printf("conflict name=%s type=%s winner=%s local_seq=%d upstream_seq=%d local=%s upstream=%s\n",
			c.Name, c.Type, c.Winner, c.Local.SourceSeq, c.Upstream.SourceSeq,
			strings.Join(c.Local.Values, ","), strings.Join(c.Upstream.Values, ","))
	}
	if len(msg.Answers) == 0 {
		fmt.Println("answers=(none)")
	} else {
		for i, rr := range msg.Answers {
			fmt.Printf("answer[%d]=%s %s %d %s\n", i+1, rr.Name, rr.Type.String(), rr.TTL, rr.Data)
		}
	}
	for i, rr := range msg.Authorities {
		fmt.Printf("authority[%d]=%s %s %d %s\n", i+1, rr.Name, rr.Type.String(), rr.TTL, rr.Data)
	}
	if msg.Header.Flags&0x0200 != 0 {
		fmt.Println("truncated=true udp_degraded=true")
	}
}

func joinOrNone(values []string) string {
	if len(values) == 0 {
		return "(none)"
	}
	return strings.Join(values, ",")
}

func printStats(eng *engine.Engine, cacheStats cache.Stats, serverStats server.Stats) {
	engineStats := eng.Stats()
	fmt.Printf("server requests=%d errors=%d truncated_udp=%d\n", serverStats.Requests, serverStats.Errors, serverStats.Truncated)
	fmt.Printf("engine resolutions=%d errors=%d flights=%d coalesced=%d\n", engineStats.Resolutions, engineStats.Errors, engineStats.Flights, engineStats.Coalesced)
	fmt.Printf("cache entries=%d hits=%d misses=%d expired=%d evictions=%d fills=%d replaced=%d\n",
		cacheStats.Entries, cacheStats.Hits, cacheStats.Misses, cacheStats.Expired, cacheStats.Evictions, cacheStats.Fills, cacheStats.Replaced)
	versions := eng.Winners()
	for _, winner := range versions {
		fmt.Printf("winner key=%s version=%d source=%s seq=%d\n", winner.Key.String(), winner.Version, winner.Source, winner.SourceSeq)
	}
}

func printHelp() {
	fmt.Println("udp <name> [type]   send a DNS query over loopback UDP")
	fmt.Println("tcp <name> [type]   send a DNS query over length-prefixed loopback TCP")
	fmt.Println("wire                replay samples/query.wire over UDP")
	fmt.Println("wiretcp             replay samples/query.wire over TCP")
	fmt.Println("stats               show cache, flight, server and winning-version statistics")
	fmt.Println("zone                show loaded zone metadata and origins")
	fmt.Println("quit                stop the interactive service")
}

func fatal(err error) {
	fmt.Fprintln(os.Stderr, "localdns:", err)
	os.Exit(1)
}
