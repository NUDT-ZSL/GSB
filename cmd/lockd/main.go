package main

import (
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"os"
	"os/signal"
	"syscall"

	"locklease/internal/graph"
	"locklease/internal/server"
	"locklease/internal/store"
)

func main() {
	addr := flag.String("addr", "127.0.0.1:7801", "loopback TCP listen address")
	dataDir := flag.String("data", "data", "directory for lock state and snapshots")
	graph := flag.String("graph", "samples/locks.graph.json", "declarative lock/dependency definition file")
	logDir := flag.String("logs", "logs", "server log directory")
	flag.Parse()

	srv, err := server.New(*addr, *dataDir, *graph, *logDir)
	if err != nil {
		fatal(err)
	}
	if err := srv.ListenAndServe(); err != nil {
		fatal(err)
	}
	fmt.Fprintf(os.Stdout, "lockd listening on %s data=%s graph=%s\n", srv.Addr(), *dataDir, *graph)

	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, os.Interrupt, syscall.SIGTERM)
	<-sigCh
	srv.Stop()
	fmt.Fprintln(os.Stdout, "lockd stopped")
}

func fatal(err error) {
	code := "E_INTERNAL"
	msg := err.Error()
	var be *store.BizError
	if errors.As(err, &be) {
		code = be.Code
		msg = be.Message
	}
	var de *graph.DependError
	if errors.As(err, &de) {
		code = store.EDepend
		msg = de.Error()
	}
	out, _ := json.Marshal(map[string]any{"ok": false, "code": code, "message": msg})
	fmt.Fprintln(os.Stderr, string(out))
	os.Exit(2)
}
