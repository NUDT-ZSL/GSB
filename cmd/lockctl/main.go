package main

import (
	"bufio"
	"encoding/json"
	"flag"
	"fmt"
	"net"
	"os"
	"strings"
	"time"

	"locklease/internal/proto"
)

func main() {
	addr := flag.String("addr", "127.0.0.1:7801", "lockd loopback address")
	batch := flag.String("batch", "", "read JSON-line requests from a local file instead of flags")
	flag.Parse()

	conn, err := net.DialTimeout("tcp", *addr, 3*time.Second)
	if err != nil {
		fmt.Fprintln(os.Stderr, mustJSON(map[string]any{"ok": false, "code": "E_PORT", "message": err.Error()}))
		os.Exit(2)
	}
	defer conn.Close()
	reader := bufio.NewReader(conn)
	writer := bufio.NewWriter(conn)

	if *batch != "" {
		raw, err := os.ReadFile(*batch)
		if err != nil {
			fmt.Fprintln(os.Stderr, mustJSON(map[string]any{"ok": false, "code": "E_BAD_REQUEST", "message": err.Error()}))
			os.Exit(2)
		}
		exit := 0
		for _, line := range strings.Split(string(raw), "\n") {
			line = strings.TrimSpace(line)
			if line == "" || strings.HasPrefix(line, "//") {
				continue
			}
			if !json.Valid([]byte(line)) {
				fmt.Fprintln(os.Stderr, mustJSON(map[string]any{"ok": false, "code": "E_BAD_REQUEST", "message": "invalid batch line"}))
				exit = 2
				continue
			}
			resp, err := roundTrip(writer, reader, []byte(line))
			if err != nil {
				fmt.Fprintln(os.Stderr, mustJSON(map[string]any{"ok": false, "code": "E_PORT", "message": err.Error()}))
				os.Exit(2)
			}
			fmt.Println(string(resp))
			var r proto.Response
			_ = json.Unmarshal(resp, &r)
			if !r.OK {
				exit = 1
			}
		}
		os.Exit(exit)
	}

	if flag.NArg() == 0 {
		fmt.Fprintln(os.Stderr, `usage: lockctl [flags] <op> [key=value ...]`)
		os.Exit(2)
	}
	req := proto.Request{Op: flag.Arg(0)}
	for _, pair := range flag.Args()[1:] {
		kv := strings.SplitN(pair, "=", 2)
		if len(kv) != 2 {
			continue
		}
		applyKV(&req, kv[0], kv[1])
	}
	resp, err := roundTrip(writer, reader, mustJSON(req))
	if err != nil {
		fmt.Fprintln(os.Stderr, mustJSON(map[string]any{"ok": false, "code": "E_PORT", "message": err.Error()}))
		os.Exit(2)
	}
	fmt.Println(string(resp))
	var r proto.Response
	_ = json.Unmarshal(resp, &r)
	if !r.OK {
		os.Exit(1)
	}
}

func applyKV(req *proto.Request, k, v string) {
	switch k {
	case "key":
		req.Key = v
	case "holder":
		req.Holder = v
	case "ttl_ms":
		req.TTLMs = parseInt(v)
	case "now_ms":
		req.NowMs = parseInt(v)
	case "lease_version":
		req.Version = parseInt(v)
	case "force":
		req.Force = v == "true"
	case "snapshot_id":
		req.ID = v
	case "path":
		req.Path = v
	}
}

func roundTrip(w *bufio.Writer, r *bufio.Reader, payload []byte) ([]byte, error) {
	if _, err := w.Write(append(payload, '\n')); err != nil {
		return nil, err
	}
	if err := w.Flush(); err != nil {
		return nil, err
	}
	line, err := r.ReadBytes('\n')
	if err != nil {
		return nil, err
	}
	return line[:len(line)-1], nil
}

func parseInt(v string) int64 {
	var n int64
	fmt.Sscanf(v, "%d", &n)
	return n
}

func mustJSON(v any) []byte {
	b, _ := json.Marshal(v)
	return b
}
