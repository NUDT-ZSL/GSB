package main

import (
	"flag"
	"fmt"
	"log"
	"os"
	"os/signal"
	"syscall"

	"localdns/internal/mockupstream"
)

func main() {
	listen := flag.String("listen", "127.0.0.1:15355", "TCP loopback listen address")
	mode := flag.String("mode", mockupstream.ModeScripted, "scripted, valid, conflict, bad-chain, or silent")
	readyFile := flag.String("ready-file", "", "optional file populated with the bound address")
	flag.Parse()

	srv, err := mockupstream.Listen(*listen, *mode, log.New(os.Stderr, "upstream: ", 0))
	if err != nil {
		fmt.Fprintln(os.Stderr, "dnsupstream:", err)
		os.Exit(1)
	}
	defer srv.Close()
	if *readyFile != "" {
		if err := os.WriteFile(*readyFile, []byte(srv.Addr()), 0o600); err != nil {
			fmt.Fprintln(os.Stderr, "dnsupstream: cannot write ready file:", err)
			os.Exit(1)
		}
	}
	fmt.Println("READY", srv.Addr())

	stop := make(chan os.Signal, 1)
	signal.Notify(stop, os.Interrupt, syscall.SIGTERM)
	<-stop
}
